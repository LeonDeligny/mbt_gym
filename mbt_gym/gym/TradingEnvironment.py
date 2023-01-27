from collections import OrderedDict
from typing import Union, Tuple, Callable

import gym
import numpy as np

from gym.spaces import Box

from mbt_gym.stochastic_processes.StochasticProcessModel import StochasticProcessModel
from mbt_gym.stochastic_processes.arrival_models import ArrivalModel
from mbt_gym.stochastic_processes.fill_probability_models import FillProbabilityModel
from mbt_gym.stochastic_processes.midprice_models import MidpriceModel, BrownianMotionMidpriceModel
from mbt_gym.stochastic_processes.price_impact_models import PriceImpactModel
from mbt_gym.gym.info_calculators import InfoCalculator
from mbt_gym.rewards.RewardFunctions import RewardFunction, PnL

MARKET_MAKING_ACTION_TYPES = ["touch", "limit", "limit_and_market"]
EXECUTION_ACTION_TYPES = ["speed"]
ACTION_TYPES = MARKET_MAKING_ACTION_TYPES + EXECUTION_ACTION_TYPES

CASH_INDEX = 0
INVENTORY_INDEX = 1
TIME_INDEX = 2
ASSET_PRICE_INDEX = 3

BID_INDEX = 0
ASK_INDEX = 1


class TradingEnvironment(gym.Env):
    metadata = {"render.modes": ["human"]}

    def __init__(
        self,
        terminal_time: float = 1.0,
        n_steps: int = 20 * 10,
        reward_function: RewardFunction = None,
        midprice_model: MidpriceModel = None,
        arrival_model: ArrivalModel = None,
        fill_probability_model: FillProbabilityModel = None,
        price_impact_model: PriceImpactModel = None,
        action_type: str = "limit",
        initial_cash: float = 0.0,
        initial_inventory: Union[int, Tuple[float, float]] = 0,  # Either a deterministic initial inventory, or a tuple
        max_inventory: int = 10_000,  # representing the mean and variance of it.
        max_cash: float = None,
        max_stock_price: float = None,
        max_depth: float = None,
        max_speed: float = None,
        half_spread: float = None,
        random_start: Union[float, int, Callable] = 0.0,
        info_calculator: InfoCalculator = None,  # episode given as a proportion.
        seed: int = None,
        num_trajectories: int = 1,
    ):
        super(TradingEnvironment, self).__init__()
        self.terminal_time = terminal_time
        self._num_trajectories = num_trajectories
        self.n_steps = n_steps
        self._step_size = self.terminal_time / self.n_steps
        self.reward_function = reward_function or PnL()
        self.midprice_model = midprice_model or BrownianMotionMidpriceModel(
            step_size=self._step_size, num_trajectories=num_trajectories
        )
        self.arrival_model = arrival_model
        self.fill_probability_model = fill_probability_model
        self.price_impact_model = price_impact_model
        self.action_type = action_type
        self._check_required_stochastic_processes()
        self.stochastic_processes = self._get_stochastic_processes()
        self.stochastic_process_indices = self._get_stochastic_process_indices()
        self.initial_cash = initial_cash
        self.initial_inventory = initial_inventory
        self.max_inventory = max_inventory
        self.rng = np.random.default_rng(seed)
        if seed:
            self.seed(seed)
        self.start_time = random_start
        self.state = self.initial_state
        self.max_stock_price = max_stock_price or self.midprice_model.max_value[0, 0]
        self.max_cash = max_cash or self._get_max_cash()
        self.max_depth = max_depth or self._get_max_depth()
        self.max_speed = max_speed or self._get_max_speed()
        self.observation_space = self._get_observation_space()
        self.action_space = self._get_action_space()
        self.half_spread = half_spread
        self.info_calculator = info_calculator
        self.empty_infos = [{} for _ in range(self.num_trajectories)] if self.num_trajectories > 1 else {}
        ones = np.ones((self.num_trajectories, 1))
        self.multiplier = np.append(-ones, ones, axis=1)

    def reset(self):
        for process in self.stochastic_processes.values():
            process.reset()
        self.state = self.initial_state
        self.reward_function.reset(self.state.copy())
        return self.state.copy()

    def step(self, action: np.ndarray):
        if action.shape != (self.num_trajectories, self.action_space.shape[0]):
            action = action.reshape(self.num_trajectories, self.action_space.shape[0])
        current_state = self.state.copy()
        next_state = self._update_state(action)
        done = self.state[0, TIME_INDEX] >= self.terminal_time - self.step_size / 2
        dones = np.full((self.num_trajectories,), done, dtype=bool)
        rewards = self.reward_function.calculate(current_state, action, next_state, done)
        infos = (
            self.info_calculator.calculate(current_state, action, rewards)
            if self.info_calculator is not None
            else self.empty_infos
        )
        return next_state.copy(), rewards, dones, infos

    @property
    def initial_state(self) -> np.ndarray:
        scalar_initial_state = np.array([[self.initial_cash, 0, 0.0]])
        initial_state = np.repeat(scalar_initial_state, self.num_trajectories, axis=0)
        start_time = self._get_start_time()
        initial_state[:, TIME_INDEX] = start_time * np.ones((self.num_trajectories,))
        initial_state[:, INVENTORY_INDEX] = self._get_initial_inventories()
        for process in self.stochastic_processes.values():
            initial_state = np.append(initial_state, process.initial_vector_state, axis=1)
        return initial_state

    @property
    def is_at_max_inventory(self):
        return self.state[:, INVENTORY_INDEX] >= self.max_inventory

    @property
    def is_at_min_inventory(self):
        return self.state[:, INVENTORY_INDEX] <= -self.max_inventory

    @property
    def midprice(self):
        return self.midprice_model.current_state[:, 0].reshape(-1, 1)

    @property
    def step_size(self):
        return self._step_size

    @step_size.setter
    def step_size(self, step_size: float, verbose:bool = True):
        self._step_size = step_size
        for process_name, process in self.stochastic_processes.items():
            if process.step_size != step_size:
                if verbose:
                    print(f"Setting value of {process_name}.step_size to {step_size}.")
                process.step_size = step_size
        if hasattr(self.reward_function, "step_size"):
            if verbose:
                print(f"Setting value of reward_function.step_size to {step_size}.")
            self.reward_function.step_size = step_size

    @property
    def num_trajectories(self):
        return self._num_trajectories

    @num_trajectories.setter
    def num_trajectories(self, num_trajectories: float, verbose: bool = True):
        self._num_trajectories = num_trajectories
        for process_name, process in self.stochastic_processes.items():
            if process.num_trajectories != num_trajectories:
                if verbose:
                    print(f"Setting value of {process_name}.num_trajectories to {num_trajectories}.")
                process.num_trajectories = num_trajectories

    # The action space depends on the action_type but bids always precede asks for limit and market order actions.
    # state[0]=cash, state[1]=inventory, state[2]=time, state[3] = asset_price, and then remaining states depend on
    # the dimensionality of the arrival process, the midprice process and the fill probability process.
    def _update_state(self, action: np.ndarray) -> np.ndarray:
        if self.action_type in MARKET_MAKING_ACTION_TYPES:
            arrivals, fills = self._get_arrivals_and_fills(action)
        else:
            arrivals, fills = None, None
        self._update_agent_state(arrivals, fills, action)
        self._update_market_state(arrivals, fills, action)
        return self.state

    def _update_market_state(self, arrivals, fills, action):
        for process_name, process in self.stochastic_processes.items():
            process.update(arrivals, fills, action)
            lower_index = self.stochastic_process_indices[process_name][0]
            upper_index = self.stochastic_process_indices[process_name][1]
            self.state[:, lower_index:upper_index] = process.current_state

    def _update_agent_state(self, arrivals: np.ndarray, fills: np.ndarray, action: np.ndarray):
        if self.action_type == "limit_and_market":
            mo_buy = np.single(self._market_order_buy(action) > 0.5)
            mo_sell = np.single(self._market_order_sell(action) > 0.5)
            best_bid = self.midprice - self.half_spread
            best_ask = self.midprice + self.half_spread
            self.state[:, CASH_INDEX] += mo_sell * best_bid - mo_buy * best_ask
            self.state[:, INVENTORY_INDEX] += mo_buy - mo_sell
        elif self.action_type == "touch":
            self.state[:, CASH_INDEX] += np.sum(
                self.multiplier * arrivals * fills * (self.midprice + self.half_spread * self.multiplier), axis=1
            )
            self.state[:, INVENTORY_INDEX] += np.sum(arrivals * fills * -self.multiplier, axis=1)
        elif self.action_type in ["limit", "limit_and_market"]:
            self.state[:, INVENTORY_INDEX] += np.sum(arrivals * fills * -self.multiplier, axis=1)
            self.state[:, CASH_INDEX] += np.sum(
                self.multiplier * arrivals * fills * (self.midprice + self._limit_depths(action) * self.multiplier),
                axis=1,
            )
        if self.action_type in EXECUTION_ACTION_TYPES:
            price_impact = self.price_impact_model.get_impact(action)
            execution_price = self.midprice + price_impact
            volume = action * self.step_size
            self.state[:, CASH_INDEX] -= np.squeeze(volume * execution_price)
            self.state[:, INVENTORY_INDEX] += np.squeeze(volume)
        self._clip_inventory_and_cash()
        self.state[:, TIME_INDEX] += self.step_size

    def _get_arrivals_and_fills(self, action: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        arrivals = self.arrival_model.get_arrivals()
        if self.action_type in ["limit", "limit_and_market"]:
            depths = self._limit_depths(action)
            fills = self.fill_probability_model.get_fills(depths)
        elif self.action_type == "touch":
            fills = self._post_at_touch(action)
        else:
            raise NotImplementedError
        fills = self._remove_max_inventory_fills(fills)
        return arrivals, fills

    def _remove_max_inventory_fills(self, fills: np.ndarray) -> np.ndarray:
        fill_multiplier = np.concatenate(
            ((1 - self.is_at_max_inventory).reshape(-1, 1), (1 - self.is_at_min_inventory).reshape(-1, 1)), axis=1
        )
        return fill_multiplier * fills

    def _limit_depths(self, action: np.ndarray):
        if self.action_type in ["limit", "limit_and_market"]:
            return action[:, 0:2]
        else:
            raise Exception('Bid depth only exists for action_type in ["limit", "limit_and_market"].')

    def _market_order_buy(self, action: np.ndarray):
        if self.action_type == "limit_and_market":
            return action[:, 2]
        else:
            raise Exception('Market order buy action only exists for action_type == "limit_and_market".')

    def _market_order_sell(self, action: np.ndarray):
        if self.action_type == "limit_and_market":
            return action[:, 3]
        else:
            raise Exception('Market order sell action only exists for action_type == "limit_and_market".')

    def _post_at_touch(self, action: np.ndarray):
        if self.action_type == "touch":
            return action[:, 0:2]
        else:
            raise Exception('Post buy at touch action only exists for action_type == "touch".')

    def _get_max_cash(self) -> float:
        return self.n_steps * self.max_stock_price  # TODO: make this a tighter bound

    def _get_max_depth(self) -> float:
        if self.fill_probability_model is not None:
            return self.fill_probability_model.max_depth
        else:
            return None

    def _get_max_speed(self) -> float:
        if self.price_impact_model is not None:
            return self.price_impact_model.max_speed
        else:
            return None

    def _get_observation_space(self) -> gym.spaces.Space:
        """The observation space consists of a numpy array containg the agent's cash, the agent's inventory and the
        current time. It also contains the states of the arrival model, the midprice model and the fill probability
        model in that order."""
        low = np.array([-self.max_cash, -self.max_inventory, 0])
        high = np.array([self.max_cash, self.max_inventory, self.terminal_time])
        for process in self.stochastic_processes.values():
            low = np.append(low, process.min_value)
            high = np.append(high, process.max_value)
        return Box(low=np.float32(low), high=np.float32(high))

    def _get_start_time(self):
        if isinstance(self.start_time, (float, int)):
            random_start = self.start_time
        elif isinstance(self.start_time, Callable):
            random_start = self.start_time()
        else:
            raise NotImplementedError
        return self._quantise_time_to_step(random_start)

    def _quantise_time_to_step(self, time: float):
        assert (time >= 0.0) and (time < self.terminal_time), "Start time is not within (0, env.terminal_time)."
        return np.round(time / self.step_size) * self.step_size

    def _get_initial_inventories(self) -> np.ndarray:
        if isinstance(self.initial_inventory, tuple) and len(self.initial_inventory) == 2:
            return self.rng.integers(*self.initial_inventory, size=self.num_trajectories)
        elif isinstance(self.initial_inventory, int):
            return self.initial_inventory * np.ones((self.num_trajectories,))
        else:
            raise Exception("Initial inventory must be a tuple of length 2 or an int.")

    def _clip_inventory_and_cash(self):
        self.state[:, INVENTORY_INDEX] = self._clip(
            self.state[:, INVENTORY_INDEX], -self.max_inventory, self.max_inventory, cash_flag=False
        )
        self.state[:, CASH_INDEX] = self._clip(self.state[:, CASH_INDEX], -self.max_cash, self.max_cash, cash_flag=True)

    def _clip(self, not_clipped: float, min: float, max: float, cash_flag: bool) -> float:
        clipped = np.clip(not_clipped, min, max)
        if (not_clipped != clipped).any() and cash_flag:
            print(f"Clipping agent's cash from {not_clipped} to {clipped}.")
        if (not_clipped != clipped).any() and not cash_flag:
            print(f"Clipping agent's inventory from {not_clipped} to {clipped}.")
        return clipped

    def _get_action_space(self) -> gym.spaces.Space:
        if self.action_type == "touch":
            return gym.spaces.MultiBinary(2)  # agent chooses spread on bid and ask
        if self.action_type == "limit":
            assert self.max_depth is not None, "For limit orders max_depth cannot be None."
            # agent chooses spread on bid and ask
            return gym.spaces.Box(low=np.float32(0.0), high=np.float32(self.max_depth), shape=(2,))
        if self.action_type == "limit_and_market":
            return gym.spaces.Box(
                low=np.zeros(4),
                high=np.array([self.max_depth, self.max_depth, 1, 1], dtype=np.float32),
            )
        if self.action_type == "speed":
            # agent chooses speed of trading: positive buys, negative sells
            return gym.spaces.Box(low=np.float32([-self.max_speed]), high=np.float32([self.max_speed]))

    @staticmethod
    def _clamp(probability):
        return max(min(probability, 1), 0)

    def _check_required_stochastic_processes(self) -> None:
        assert self.action_type in ACTION_TYPES, f"Action type '{self.action_type}' is not in {ACTION_TYPES}."
        if self.action_type == "touch":
            processes = ["arrival_model"]
        elif self.action_type in ["limit", "limit_and_market"]:
            processes = ["arrival_model", "fill_probability_model"]
        elif self.action_type == "speed":
            processes = ["price_impact_model"]
        else:
            raise NotImplementedError
        for process in processes:
            self._check_process_is_not_none(process)

    def _check_process_is_not_none(self, process: str):
        assert getattr(self, process) is not None, f"Action type is '{self.action_type}' but env.{process} is None."

    def _get_stochastic_processes(self):
        stochastic_processes = dict()
        for process_name in ["midprice_model", "arrival_model", "fill_probability_model", "price_impact_model"]:
            process: StochasticProcessModel = getattr(self, process_name)
            if process is not None:
                stochastic_processes[process_name] = process
        return OrderedDict(stochastic_processes)

    def _get_stochastic_process_indices(self):
        process_indices = dict()
        count = 3
        for process_name, process in self.stochastic_processes.items():
            dimension = int(process.initial_vector_state.shape[1])
            process_indices[process_name] = (count, count + dimension)
            count += dimension
        return OrderedDict(process_indices)

    def seed(self, seed: int = None):
        self.rng = np.random.default_rng(seed)
        for i, process in enumerate(self.stochastic_processes.values()):
            process.seed(seed + i + 1)
