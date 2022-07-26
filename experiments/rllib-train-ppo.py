import sys

sys.path.append("/LOCAL2/jjerome/GitHub/DRL4AMM/")  # Location on rahul-n
import os

os.environ["PYTHONPATH"] = "/LOCAL2/jjerome/GitHub/DRL4AMM/"  # Location on rahul-n
# Note that we need to set the PYTHONPATH so that the workers are all aware of DRL4AMM
import ray
from ray import tune
from ray.tune.registry import register_env
from copy import copy
from ray.rllib.agents.ppo import DEFAULT_CONFIG


from DRL4AMM.gym.AvellanedaStoikovEnvironment import AvellanedaStoikovEnvironment
from DRL4AMM.gym.wrappers import ReduceStateSizeWrapper
from DRL4AMM.rewards.RewardFunctions import CJ_criterion, PnL

num_workers = 10
ray.init(ignore_reinit_error=True, num_cpus=num_workers + 1)

terminal_time = 3
arrival_rate = 1.0
env_config = dict(
    terminal_time=terminal_time,
    arrival_rate=arrival_rate,
    n_steps=int(terminal_time * arrival_rate * 10),
    reward_function=PnL(),  # CJ_criterion(phi=2 * 10 ** (-4), alpha=0.0001),
    drift=0.0,
    volatility=0.01,
    fill_exponent=100.0,
    max_inventory=100,
    max_half_spread=10.0,
)

def wrapped_env_creator(env_config:dict):
    return ReduceStateSizeWrapper(AvellanedaStoikovEnvironment(**env_config))

register_env("AvellanedaStoikovEnvironment", wrapped_env_creator)

config = copy(DEFAULT_CONFIG)
config["use_gae"] = True  # Don't use generalised advantage estimation
config["framework"] = "tf2"
config["sample_async"] = False
config["entropy_coeff"] = 0.01
config["lr"] = 0.001,
config["use_critic"] = True # False # For reinforce,
config["optimizer"] = "SGD",
config["model"]["fcnet_hiddens"] = [16,16]
config["eager_tracing"] = True,
config["train_batch_size"] = 100000,
config["env"] = "AvellanedaStoikovEnvironment"
config["env_config"] = env_config
config["num_workers"] = num_workers
config["model"] = {"fcnet_activation": "tanh", "fcnet_hiddens": [16, 16]}
config["sgd_minibatch_size"] = 10000
config["num_sgd_iter"] = 10

tensorboard_logdir = "../data/tensorboard"

analysis = tune.run(
    "PPO",
    config=config,
    checkpoint_at_end=True,
    local_dir=tensorboard_logdir,
    stop={"training_iteration": 3000}
)

best_checkpoint = analysis.get_trial_checkpoints_paths(
        trial=analysis.get_best_trial("episode_reward_mean"),
        metric="episode_reward_mean",
        mode="max"
    )
print(best_checkpoint)
path_to_save_dir = tensorboard_logdir
save_best_checkpoint_path(path_to_save_dir, best_checkpoint[0][0])
