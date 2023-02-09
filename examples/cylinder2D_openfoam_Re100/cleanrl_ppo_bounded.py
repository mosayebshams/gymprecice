import argparse
from collections import deque
from typing import Optional
import gym
from OpenFoamRLEnv_4_multiEnv import OpenFoamRLEnv
from utils import fix_randseeds
import numpy as np
import time
import math
import torch
import torch.nn as nn
from torch.optim import Adam
from gym.spaces import Box
from distutils.util import strtobool
from torch.distributions import Normal
from torch.utils.tensorboard import SummaryWriter

from gym import spaces
from datetime import datetime

try:
    from collections.abc import Iterable
except ImportError:
    Iterable = (tuple, list)

EPSILON = 1e-6
LOG_EPSILON = math.log(EPSILON)


def layer_init(layer, std=np.sqrt(2), bias_const=0.0):
    torch.nn.init.orthogonal_(layer.weight, std)
    torch.nn.init.constant_(layer.bias, bias_const)
    return layer


class Agent(nn.Module):
    def __init__(self, env, relative_action, use_sde=False):
        super().__init__()
        self.n_actions = np.prod(env.single_action_space.shape)
        self.n_obs = np.prod(env.single_observation_space.shape)

        self.action_min = torch.from_numpy(np.copy(envs.single_action_space.low))
        self.action_max = torch.from_numpy(np.copy(envs.single_action_space.high))

        self.action_scale = (self.action_max - self.action_min) / 2.0
        self.action_bias = (self.action_max + self.action_min) / 2.0

        self.latent_dim = 64
        self.use_sde = use_sde

        if relative_action:
            self.action_min = self.action_min / 3
            self.action_max = self.action_max / 3

        self.critic = nn.Sequential(
            layer_init(nn.Linear(self.n_obs, 64)),
            nn.Tanh(),
            layer_init(nn.Linear(64, self.latent_dim)),
            nn.Tanh(),
            layer_init(nn.Linear(self.latent_dim, 1), std=1.0)
        )

        self.actor = nn.Sequential(
            layer_init(nn.Linear(self.n_obs, 64)),
            nn.Tanh(),
            layer_init(nn.Linear(64, self.latent_dim)),
            nn.Tanh()
        )
        self.actor_mean = layer_init(nn.Linear(self.latent_dim, self.n_actions), std=0.1)

        if self.use_sde:
            # std_init = 0.0
            # std = torch.ones(self.latent_dim, self.n_actions)
            # # Transform it to a parameter so it can be optimized
            # self.std = nn.Parameter(std * std_init, requires_grad=True)

            log_std_init = 0.0  # TODO: set this as a hyper-parameter
            log_std = torch.ones(self.latent_dim, self.n_actions)
            # Transform it to a parameter so it can be optimized
            self.log_std = nn.Parameter(log_std * log_std_init, requires_grad=True)

            self.sample_weights(env.num_envs)
        else:
            self.std = nn.Parameter(torch.zeros(self.n_actions), requires_grad=True)

    def sample_weights(self, num_envs: int = 1) -> None:
        """
        Sample weights for the noise exploration matrix,
        using a centered Gaussian distribution.

        :param num_envs:
        """
        std = torch.exp(self.log_std)
        # Clip stddev for numerical stability (epsilon < 1.0, hence negative)
        std = torch.clip(std, LOG_EPSILON, -LOG_EPSILON)
        # Softplus transformation (based on https://arxiv.org/abs/2007.06059)
        std = 0.25 * (torch.log(1.0 + torch.exp(std)) + 0.2) / (math.log(2.0) + 0.2)

        # log_std = self.log_std
        # # From gSDE paper, it allows to keep variance
        # # above zero and prevent it from growing too fast
        # below_threshold = torch.exp(log_std) * (log_std <= 0)
        # # Avoid NaN: zeros values that are below zero
        # safe_log_std = log_std * (log_std > 0) + EPSILON
        # above_threshold = (torch.log1p(safe_log_std) + 1.0) * (log_std > 0)
        # std = 0.25 * (below_threshold + above_threshold)

        self.weights_dist = Normal(torch.zeros_like(std), std)
        # Reparametrization trick to pass gradients
        self.exploration_mat = self.weights_dist.rsample()
        # Pre-compute matrices in case of parallel exploration
        self.exploration_matrices = self.weights_dist.rsample((num_envs,))

    def get_value(self, x):
        x = x.reshape(-1, self.n_obs)
        return self.critic(x)

    def get_action_and_value(self, x, action=None):
        x = x.reshape(-1, self.n_obs)
        latent_pi = self.actor(x)
        mean = self.actor_mean(latent_pi)

        if self.use_sde:
            # distribution = self._get_action_dist_from_latent(latent_pi)
            # Stop gradient if we don't want to influence the features
            latent_sde = latent_pi.detach()

            std = torch.exp(self.log_std)
            # Clip stddev for numerical stability (epsilon < 1.0, hence negative)
            std = torch.clip(std, LOG_EPSILON, -LOG_EPSILON)
            # Softplus transformation (based on https://arxiv.org/abs/2007.06059)
            std = 0.25 * (torch.log(1.0 + torch.exp(std)) + 0.2) / (math.log(2.0) + 0.2)

            # log_std = self.log_std
            # # From gSDE paper, it allows to keep variance
            # # above zero and prevent it from growing too fast
            # below_threshold = torch.exp(log_std) * (log_std <= 0)
            # # Avoid NaN: zeros values that are below zero
            # safe_log_std = log_std * (log_std > 0) + EPSILON
            # above_threshold = (torch.log1p(safe_log_std) + 1.0) * (log_std > 0)
            # std = 0.25 * (below_threshold + above_threshold)

            variance = torch.mm(latent_sde**2, std**2)
            probs = Normal(mean, torch.sqrt(variance + EPSILON))

            # Default case: only one exploration matrix
            if len(latent_sde) == 1 or len(latent_sde) != len(self.exploration_matrices):
                noise = torch.mm(latent_sde, self.exploration_mat)
            else:
                # Use batch matrix multiplication for efficient computation
                # (batch_size, n_features) -> (batch_size, 1, n_features)
                latent_sde = latent_sde.unsqueeze(1)
                # (batch_size, 1, n_actions)
                noise = torch.bmm(latent_sde, self.exploration_matrices)
            noise = noise.squeeze(1)
            sample = mean + noise
        else:
            std = self.std.expand_as(mean)
            # Clip stddev for numerical stability (epsilon < 1.0, hence negative)
            std = torch.clip(std, LOG_EPSILON, -LOG_EPSILON)
            # Softplus transformation (based on https://arxiv.org/abs/2007.06059)
            std = 0.25 * (torch.log(1.0 + torch.exp(std)) + 0.2) / (math.log(2.0) + 0.2)
            probs = Normal(mean, std)
            sample = probs.rsample()

        if action is None:
            squashed_sample = torch.tanh(sample)
            action = squashed_sample * self.action_scale + self.action_bias  # we scale the sampled action

        # TODO: this should be done only when action is not None
        squashed_action = 2.0 * (action - self.action_min) / (self.action_max - self.action_min) - 1.0
        clip = 1.0 - EPSILON
        squashed_action = torch.clip(squashed_action, -clip, clip)

        gaussian_action = torch.atanh(squashed_action)

        log_prob = probs.log_prob(gaussian_action)
        # log_prob -= torch.log(self.action_scale * (1 - squashed_action.pow(2)) + EPSILON)
        log_prob -= 2.0 * (math.log(2.0) - gaussian_action - torch.log(1.0 + torch.exp(-2.0 * gaussian_action)))

        if self.use_sde:
            entropy = None
        else:
            entropy = probs.entropy().sum(1)

        # agent returns the mean action for CAP method
        return action, mean, log_prob.sum(1), entropy, self.critic(x)


class ClipFlowAction(gym.ActionWrapper):
    """Clip the continuous action within the valid bounds.  """

    def __init__(self, env: gym.Env):
        """A wrapper for clipping continuous actions within the valid bound.
        Args:
            env: The environment to apply the wrapper
        """
        super().__init__(env)

    def action(self, action):
        """Clips the action within the valid bounds.
        Args:
            action: The action to clip
        Returns:
            The clipped action
        """
        return np.clip(action, self.action_min, self.action_max)


class ObservationRewardWrapper(gym.Wrapper):
    """This wrapper will augment the observation (aka env.state) with the current action"""

    def __init__(self, env, deque_size: int = 50):
        super().__init__(env)
        self.env = env
        self.num_envs = getattr(env, "num_envs", 1)
        self.obs_queue = deque(maxlen=deque_size)

    def step(self, action):
        observations, rewards, dones, infos = self.env.step(action)

        # print(observations.shape, action_np.shape)
        if self.num_envs > 1:
            action_ = np.array(action).reshape(self.num_envs, -1)
            wrapped_observations = np.concatenate((observations, action_), axis=1)
        else:
            action_ = np.array(action).flatten()
            wrapped_observations = np.concatenate((observations, action_), axis=0)
        return wrapped_observations, rewards, dones, infos

    def reset(self, **kwargs):
        """Resets the environment and add fake action."""
        observations = super().reset(**kwargs)
        fake_action = self.num_envs * [0.0 * self.env.single_action_space.sample()]
        if self.num_envs > 1:
            action_ = np.array(fake_action).reshape(self.num_envs, -1)
            wrapped_observations = np.concatenate((observations, action_), axis=1)
        else:
            action_ = np.array(fake_action).flatten()
            # print(observations.shape, action_.shape)
            wrapped_observations = np.concatenate((observations, action_), axis=0)
        return wrapped_observations


class ObservationRewardWrapper2(gym.Wrapper):
    """This wrapper will augment the observation (aka env.state) with the current action"""

    def __init__(self, env, use_relative_action, deque_size: int = 50):
        super().__init__(env)
        self.env = env
        self.use_relative_action = use_relative_action
        self.num_envs = getattr(env, "num_envs", 1)
        self.obs_queue = deque(maxlen=deque_size)

        # for relative action, observations are augmented with the current action
        if self.use_relative_action:
            low = np.append(env.single_observation_space.low, env.single_observation_space.low[0])
            high = np.append(env.single_observation_space.high, env.single_observation_space.high[0])
        else:
            low = env.single_observation_space.low
            high = env.single_observation_space.high

        # stacking is inspired by what is done
        # https://github.com/openai/gym/blob/master/gym/wrappers/frame_stack.py
        low = np.repeat(low, 2, axis=0)
        high = np.repeat(high, 2, axis=0)

        self.observation_space = Box(
            low=low, high=high, dtype=self.observation_space.dtype
        )

    def step(self, action):
        observations, rewards, dones, infos = self.env.step(action)

        # for relative action, observations are augmented with the current action
        if self.use_relative_action:
            if self.num_envs > 1:
                action_ = np.array(action).reshape(self.num_envs, -1)
                wrapped_observations = np.concatenate((observations, action_), axis=1)
            else:
                action_ = np.array(action).flatten()
                wrapped_observations = np.concatenate((observations, action_), axis=0)
        else:
            wrapped_observations = observations

        # return wrapped_observations, rewards, dones, infos

        self.obs_queue.append(wrapped_observations)
        if self.num_envs > 1:
            return np.concatenate((self.obs_queue[0], self.obs_queue[-1]), axis=1), rewards, dones, infos
        else:
            return np.concatenate((self.obs_queue[0], self.obs_queue[-1]), axis=0), rewards, dones, infos

    def reset(self, **kwargs):
        """Resets the environment and add fake action."""
        observations = super().reset(**kwargs)
        fake_action = self.num_envs * [0.0 * self.env.single_action_space.sample()]

        # for relative action, observations are augmented with the current action
        if self.use_relative_action:
            if self.num_envs > 1:
                action_ = np.array(fake_action).reshape(self.num_envs, -1)
                wrapped_observations = np.concatenate((observations, action_), axis=1)
            else:
                action_ = np.array(fake_action).flatten()
                # print(observations.shape, action_.shape)
                wrapped_observations = np.concatenate((observations, action_), axis=0)
        else:
            wrapped_observations = observations

        # return wrapped_observations
        self.obs_queue.clear()
        self.obs_queue.append(wrapped_observations)
        if self.num_envs > 1:
            return np.concatenate((self.obs_queue[0], self.obs_queue[-1]), axis=1)
        else:
            return np.concatenate((self.obs_queue[0], self.obs_queue[-1]), axis=0)


class WandBRewardRecoder(gym.Wrapper):
    """This wrapper will keep track of cumulative rewards and episode lengths """

    def __init__(self, env: gym.Env, wandb_context=None):
        """This wrapper will keep track of cumulative rewards and episode lengths.
        Args:
            env (Env): The environment to apply the wrapper
            deque_size: The size of the buffers :attr:`return_queue` and :attr:`length_queue`
        """
        super().__init__(env)
        # self.num_envs = num_envs
        self.t0 = time.perf_counter()
        self.episode_count = 0
        self.episode_returns: Optional[np.ndarray] = None
        self.episode_lengths: Optional[np.ndarray] = None
        self.wandb_context = wandb_context

    def reset(self, **kwargs):
        """Resets the environment using kwargs and resets the episode returns and lengths."""
        observations = super().reset(**kwargs)
        self.episode_returns_saved = np.zeros(self.num_envs, dtype=np.float32)
        self.episode_lengths_saved = np.zeros(self.num_envs, dtype=np.int32)
        self.episode_returns = np.zeros(self.num_envs, dtype=np.float32)
        self.episode_lengths = np.zeros(self.num_envs, dtype=np.int32)
        
        return observations

    def step(self, action):
        """Steps through the environment, recording the episode statistics."""
        (
            observations,
            rewards,
            dones,
            infos,
        ) = self.env.step(action)

        self.episode_returns += rewards.flatten()
        self.episode_lengths += 1

        if self.num_envs == 1:
            dones = [dones]
        dones = list(dones)

        for i in range(len(dones)):
            if dones[i]:
                episode_return = self.episode_returns[i]
                episode_length = self.episode_lengths[i]
                print(episode_length)
                if self.wandb_context:
                    metrics_dict = {
                        "rewards": episode_return / episode_length,
                        "episode": self.episode_count,
                    }
                    self.wandb_context.log(metrics_dict, commit=True)
                print(f"DEBUG print, episode: {self.episode_count}, rewards : {episode_return / episode_length}")

                self.episode_count += 1
                self.episode_returns_saved[i] = self.episode_returns[i]
                self.episode_lengths_saved[i] = self.episode_lengths[i]
                self.episode_returns[i] = 0
                self.episode_lengths[i] = 0

        return (
            observations,
            rewards,
            dones if self.num_envs > 1 else dones[0],
            infos,
        )
    
    def close(self):
        self.env.close()



def parse_args():
    # fmt: off
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=1,
        help="seed of the experiment")
    parser.add_argument("--torch-deterministic", type=lambda x: bool(strtobool(x)), default=True, nargs="?", const=True,
        help="if toggled, `torch.backends.cudnn.deterministic=False`")
    parser.add_argument("--cuda", type=lambda x: bool(strtobool(x)), default=True, nargs="?", const=True,
        help="if toggled, cuda will be enabled by default")
    parser.add_argument("--track", type=lambda x: bool(strtobool(x)), default=True, nargs="?", const=True,
        help="if toggled, this experiment will be tracked with Weights and Biases")
    parser.add_argument("--wandb-project-name", type=str, default="cleanRL",
        help="the wandb's project name")
    parser.add_argument("--wandb-entity", type=str, default=None,
        help="the entity (team) of wandb's project")
    parser.add_argument("--capture-video", type=lambda x: bool(strtobool(x)), default=False, nargs="?", const=True,
        help="weather to capture videos of the agent performances (check out `videos` folder)")

    # Algorithm specific arguments
    parser.add_argument("--env-id", type=str, default="HalfCheetahBulletEnv-v0",
        help="the id of the environment")
    parser.add_argument("--total-timesteps", type=int, default=1000000,
        help="total timesteps of the experiments")
    parser.add_argument("--learning-rate", type=float, default=5e-4,
        help="the learning rate of the optimizer")
    parser.add_argument("--num-envs", type=int, default=32,
        help="the number of parallel game environments")
    parser.add_argument("--num-steps", type=int, default=80,
        help="the number of steps to run in each environment per policy rollout")
    parser.add_argument("--anneal-lr", type=lambda x: bool(strtobool(x)), default=True, nargs="?", const=True,
        help="Toggle learning rate annealing for policy and value networks")
    parser.add_argument("--gae", type=lambda x: bool(strtobool(x)), default=True, nargs="?", const=True,
        help="Use GAE for advantage computation")
    parser.add_argument("--gamma", type=float, default=0.99,
        help="the discount factor gamma")
    parser.add_argument("--gae-lambda", type=float, default=0.95,
        help="the lambda for the general advantage estimation")
    parser.add_argument("--num-minibatches", type=int, default=32,
        help="the number of mini-batches")
    parser.add_argument("--update-epochs", type=int, default=10,
        help="the K epochs to update the policy")
    parser.add_argument("--norm-adv", type=lambda x: bool(strtobool(x)), default=True, nargs="?", const=True,
        help="Toggles advantages normalization")
    parser.add_argument("--clip-coef", type=float, default=0.2,
        help="the surrogate clipping coefficient")
    parser.add_argument("--clip-vloss", type=lambda x: bool(strtobool(x)), default=True, nargs="?", const=True,
        help="Toggles whether or not to use a clipped loss for the value function, as per the paper.")
    parser.add_argument("--ent-coef", type=float, default=1e-2,
        help="coefficient of the entropy")
    parser.add_argument("--vf-coef", type=float, default=0.5,
        help="coefficient of the value function")
    parser.add_argument("--max-grad-norm", type=float, default=0.5,
        help="the maximum norm for the gradient clipping")
    parser.add_argument("--target-kl", type=float, default=None,
        help="the target KL divergence threshold")
    parser.add_argument("--use-sde", type=bool, default=False,
        help="Whether to use generalized State Dependent Exploration (gSDE) instead of action noise exploration")
    parser.add_argument("--sde-sample-freq", type=int, default=-1,
        help="Sample a new noise matrix every n steps when using gSDE. Default: -1 (only sample at the beginning of the rollout)")

    args = parser.parse_args()
    args.batch_size = int(args.num_envs * args.num_steps)
    args.minibatch_size = int(args.batch_size // args.num_minibatches)
    # fmt: on
    return args


if __name__ == '__main__':
    run_method = 'standard'  # 'caps_loss'  # 'relative_update' # 'standard'
    if run_method == 'standard':
        use_caps_loss = False
        use_relative_action = False
    elif run_method == 'caps_loss':
        use_caps_loss = True
        use_relative_action = False
        caps_lambda = 10.0
    elif run_method == 'relative_update':
        use_caps_loss = False
        use_relative_action = True
    else:
        assert 0, 'not implemented'

    rand_seed = 12345
    fix_randseeds(rand_seed)
    args = parse_args()

    # shell options to run the solver (this can/should be placed in a
    # separate python script)
    foam_case_path = "cylinder2D-unstructured-mesh"
    foam_shell_cmd = "foam-functions-cylinder2D.sh"
    foam_clean_cmd = "cleanfoam"
    foam_softclean_cmd = "softcleanfoam"

    foam_preprocess_cmd = "preprocessfoam"
    foam_run_cmd = "runfoam"
    foam_preprocess_log = "foam_preprocess.log"
    foam_clean_log = "foam_clean.log"
    foam_softclean_log = "foam_softclean.log"
    foam_run_log = "foam_run.log"

    parallel_run = False
    if parallel_run:
        foam_preprocess_cmd += " -parallel"
        foam_run_cmd += " -parallel"

    # if True, then the preprocessing (here: blockMesh) happens per each epoch:
    foam_full_reset = False

    foam_clean_cmd = f" && {foam_clean_cmd}"  # > {foam_clean_log} 2>&1"
    foam_softclean_cmd = f" && {foam_softclean_cmd}"  # > {foam_softclean_log} 2>&1"
    foam_preprocess_cmd = f" && {foam_preprocess_cmd}"  # > {foam_preprocess_log} 2>&1"
    foam_run_cmd = f" && {foam_run_cmd} > {foam_run_log}"  # 2>&1"

    # Size and type is redundant data (included controlDict or called from a file)
    # Multiple way to do this in OpenFoam so we delegate it to user
    postprocessing_data = {
        'forces': {
            'use': 'reward',  # goes into observation or rewards
            'type': 'forces',  # forces|probe|?
            'datatype': 'scaler',  # scaler vs field
            'size': 12,  # number of forces
            'output_file': '/postProcessing/forces/0/coefficient.dat',  # depends on the type of the probe/patchProbe/etc
        },
        'p': {
            'use': 'observation',  # goes into observation or rewards
            'type': 'probe',  # forces|probe|?
            'datatype': 'scaler',  # scaler vs field
            'size': 11,  # number of probes
            'output_file': '/postProcessing/probes/0/p',  # depends on the type of the probe/patchProbe/etc
        }
    }

    options = {
        "precice_cfg": "precice-config.xml",
        "case_path": foam_case_path,
        "foam_shell_cmd": foam_shell_cmd,
        "clean_cmd": foam_clean_cmd,
        "softclean_cmd": foam_softclean_cmd,
        "preprocess_cmd": foam_preprocess_cmd,
        "run_cmd": foam_run_cmd,
        "solver_full_reset": foam_full_reset,
        "rand_seed": rand_seed,
        "postprocessing_data": postprocessing_data,
        "n_parallel_env": args.num_envs,
        "is_dummy_run": False,
        "prerun": True,
        "prerun_available": True,
        "prerun_time": 0.335,
    }

    args.track = True

    if args.track:
        import wandb
        run_name = f'test_RL_{run_method}_{int(time.time())}'

        wandb_recorder = wandb.init(
            project='RL_CFD',
            entity='cfddrl',
            sync_tensorboard=False,
            config=vars(args),
            name=run_name,
            monitor_gym=False,
            save_code=True,
        )
    else:
        wandb_recorder = None

    t0 = time.time()

    def make_env(options=options, idx=None, wrappers=None):
        def _make_env():
            env = OpenFoamRLEnv(options, idx)
            if wrappers is not None:
                if callable(wrappers):
                    env = wrappers(env)
                elif isinstance(wrappers, Iterable) and all(
                    [callable(w) for w in wrappers]
                ):
                    for wrapper in wrappers:
                        env = wrapper(env)
                else:
                    raise NotImplementedError
            return env
        return _make_env

    env_fns = []
    for idx in range(args.num_envs):
        env_fns.append(make_env(options=options, idx=idx, wrappers=[gym.wrappers.ClipAction]))

    action_space = spaces.Box(
            low=-2.5e-4, high=2.5e-4, shape=(1, ), dtype=np.float32)
    observation_space = spaces.Box(
            low=-np.inf, high=np.inf, shape=(11,), dtype=np.float32)
    
    from pathlib import Path
    from os.path import join
    import os
    
    cwd = Path.cwd()
    time_str = datetime.now().strftime('%d%m%Y_%H%M%S')
    run_folder_name = f'rl_gym_run_{time_str}'
    run_folder = cwd.joinpath(run_folder_name)

    case_path = options['case_path']
    source_folder_str = join(str(cwd), case_path)
    run_folder_str = join(str(cwd), run_folder_name)

    try:
        run_folder.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        raise Exception(f'failed to create run folder: {e}')
        
    # copy base case to RL_gym folder
    try:
        os.system(f'cp -r {source_folder_str} {run_folder_str}')
    except Exception as e:
        raise Exception(f'Failed to copy base case to run folder: {e}')
    
    source_folder_str = join(run_folder_str, case_path)
    case_path = source_folder_str
    shell_cmd = options['foam_shell_cmd']
    precice_cfg = options['precice_cfg']
    os.system(f'cp ./{shell_cmd} {run_folder_str}')
    os.system(f'cp ./{precice_cfg} {run_folder_str}')    

    os.chdir(str(run_folder))

    
    envs = gym.vector.AsyncVectorEnv(
        env_fns=env_fns,
        context='fork',
        observation_space=observation_space,
        action_space=action_space,
        shared_memory=False
    )
    
    # envs = gym.vector.SyncVectorEnv(
    #     env_fns=env_fns
    # )

    envs = WandBRewardRecoder(envs, wandb_recorder)

    print(f"Run time of defining OpenFoamRLEnv is {time.time()-t0} seconds")

    obs_dim = np.prod(envs.single_observation_space.shape)
    n_acts = np.prod(envs.single_action_space.shape)
    device = "cpu"  # torch.device("cuda" if torch.cuda.is_available() and args.cuda else "cpu")

    agent = Agent(envs, use_relative_action, args.use_sde)
    optimizer = Adam(agent.parameters(), lr=args.learning_rate, eps=1e-5)

    # TRY NOT TO MODIFY: start the game
    global_step = 0
    start_time = time.time()

    # ALGO Logic: Storage setup --> (timesteps, num_env, n_obs) if obs is 2d then the following will be 4 dimesional
    obs = torch.zeros((args.num_steps, args.num_envs) + envs.single_observation_space.shape).to(device)
    nxtobs = torch.zeros((args.num_steps, args.num_envs) + envs.single_observation_space.shape).to(device)
    actions = torch.zeros((args.num_steps, args.num_envs) + envs.single_action_space.shape).to(device)

    logprobs = torch.zeros((args.num_steps, args.num_envs)).to(device)
    rewards = torch.zeros((args.num_steps, args.num_envs)).to(device)
    dones = torch.zeros((args.num_steps, args.num_envs)).to(device)
    values = torch.zeros((args.num_steps, args.num_envs)).to(device)
    
    num_updates = 1000  #args.total_timesteps // args.batch_size

    next_obs = torch.Tensor(envs.reset()).to(device)
    next_done = torch.zeros(args.num_envs).to(device)

    prev_action = None
    for update in range(1, num_updates + 1):
        # Annealing the rate if instructed to do so.
        if args.anneal_lr:
            frac = 1.0 - (update - 1.0) / num_updates
            lrnow = frac * args.learning_rate
            optimizer.param_groups[0]["lr"] = lrnow
        t0 = time.time()

        if args.use_sde:
            # Sample new weights for gSDE
            agent.sample_weights(envs.num_envs)

        for step in range(0, args.num_steps):
            # Sample new weights for gSDE
            if args.use_sde and args.sde_sample_freq > 0 and step % args.sde_sample_freq == 0:
                agent.sample_weights(envs.num_envs)

            global_step += 1 * args.num_envs
            obs[step] = next_obs
            dones[step] = next_done

            # ALGO LOGIC: action logic
            with torch.no_grad():
                action, _, logprob, _, value = agent.get_action_and_value(next_obs)
                values[step] = value.flatten()
            actions[step] = action
            logprobs[step] = logprob

            if prev_action is None:
                prev_action = 0 * action

            subcycle_counter = 0
            subcycle_max = 50  # in precice config set the time step to 0.025 / 50 = 5e-4
            # subcycle_max = 25  # in precice config set the time step to 1e-3

            # little bit inefficient communication modes but lets try
            while subcycle_counter < subcycle_max:
                if use_relative_action:
                    # smoothing_fraction = (subcycle_counter / subcycle_max)
                    # smoothed_action = (1 - smoothing_fraction) * prev_action + smoothing_fraction * action
                    action_fraction = (1 / subcycle_max)
                    smoothed_action = prev_action + action_fraction * action
                else:  # this is valid for both standard and caps method
                    action_fraction = 1 / (subcycle_max - subcycle_counter)
                    smoothed_action = prev_action + action_fraction * (action - prev_action)

                # TRY NOT TO MODIFY: execute the game and log data.
                next_obs, reward, done, info = envs.step(smoothed_action.cpu().numpy())
                # print(f'PPO will took the following action:\n{smoothed_action}\n vs previous action:\n{prev_action}\n at subcycle {subcycle_counter} out of {subcycle_max}, reward {reward}')
                subcycle_counter += 1

                if envs.num_envs == 1:
                    done = [done]
                
                # TODO: Async setup 
                if done[0]:
                    prev_action = None
                    break
                else:
                    prev_action = smoothed_action

            rewards[step] = torch.tensor(reward).to(device).view(-1)
            next_obs = torch.Tensor(next_obs).to(device)
            next_done = torch.Tensor(done).to(device=device)


        # bootstrap value if not done
        with torch.no_grad():
            next_value = agent.get_value(next_obs).reshape(1, -1)
            if args.gae:
                advantages = torch.zeros_like(rewards).to(device)
                lastgaelam = 0
                for t in reversed(range(args.num_steps)):
                    if t == args.num_steps - 1:
                        nextnonterminal = 1.0 - next_done
                        nextvalues = next_value
                    else:
                        nextnonterminal = 1.0 - dones[t + 1]
                        nextvalues = values[t + 1]
                    delta = rewards[t] + args.gamma * nextvalues * nextnonterminal - values[t]
                    advantages[t] = lastgaelam = delta + args.gamma * args.gae_lambda * nextnonterminal * lastgaelam
                returns = advantages + values
            else:
                returns = torch.zeros_like(rewards).to(device)
                for t in reversed(range(args.num_steps)):
                    if t == args.num_steps - 1:
                        nextnonterminal = 1.0 - next_done
                        next_return = next_value
                    else:
                        nextnonterminal = 1.0 - dones[t + 1]
                        next_return = returns[t + 1]
                    returns[t] = rewards[t] + args.gamma * nextnonterminal * next_return
                advantages = returns - values

        # flatten the batch
        b_obs = obs.reshape((-1,) + envs.single_observation_space.shape)
        b_logprobs = logprobs.reshape(-1)
        b_actions = actions.reshape((-1,) + envs.single_action_space.shape)
        b_advantages = advantages.reshape(-1)
        b_returns = returns.reshape(-1)
        b_values = values.reshape(-1)

        # for CAPS
        nxtobs[0:-1, :] = obs[1:, :].detach().clone()
        nxtobs[-1, :] = obs[-1, :].detach().clone()

        b_nxtobs = nxtobs.reshape((-1,) + envs.single_observation_space.shape)

        # Optimizing the policy and value network
        b_inds = np.arange(args.batch_size)
        clipfracs = []
        for epoch in range(args.update_epochs):
            np.random.shuffle(b_inds)
            for start in range(0, args.batch_size, args.minibatch_size):
                end = start + args.minibatch_size
                mb_inds = b_inds[start:end]

                # Re-sample the noise matrix because the log_std has changed
                if args.use_sde:
                    agent.sample_weights(args.minibatch_size)

                _, newmu, newlogprob, entropy, newvalue = agent.get_action_and_value(b_obs[mb_inds], b_actions[mb_inds])

                _, newnextmu, _, _, _ = agent.get_action_and_value(b_nxtobs[mb_inds])

                logratio = newlogprob - b_logprobs[mb_inds]
                ratio = logratio.exp()

                with torch.no_grad():
                    # calculate approx_kl http://joschu.net/blog/kl-approx.html
                    old_approx_kl = (-logratio).mean()
                    approx_kl = ((ratio - 1) - logratio).mean()
                    clipfracs += [((ratio - 1.0).abs() > args.clip_coef).float().mean().item()]

                mb_advantages = b_advantages[mb_inds]
                if args.norm_adv:
                    mb_advantages = (mb_advantages - mb_advantages.mean()) / (mb_advantages.std() + 1e-8)

                # Policy loss
                pg_loss1 = -mb_advantages * ratio
                pg_loss2 = -mb_advantages * torch.clamp(ratio, 1 - args.clip_coef, 1 + args.clip_coef)
                pg_loss = torch.max(pg_loss1, pg_loss2).mean()

                # Value loss
                newvalue = newvalue.view(-1)
                if args.clip_vloss:
                    v_loss_unclipped = (newvalue - b_returns[mb_inds]) ** 2
                    v_clipped = b_values[mb_inds] + torch.clamp(
                        newvalue - b_values[mb_inds],
                        -args.clip_coef,
                        args.clip_coef,
                    )
                    v_loss_clipped = (v_clipped - b_returns[mb_inds]) ** 2
                    v_loss_max = torch.max(v_loss_unclipped, v_loss_clipped)
                    v_loss = 0.5 * v_loss_max.mean()
                else:
                    v_loss = 0.5 * ((newvalue - b_returns[mb_inds]) ** 2).mean()

                # Entropy loss favor exploration
                if entropy is None:
                    # Approximate entropy when no analytical form
                    entropy_loss = -torch.mean(-newlogprob)
                else:
                    entropy_loss = -entropy.mean()

                loss = pg_loss + args.ent_coef * entropy_loss + args.vf_coef * v_loss

                if use_caps_loss:
                    # CAPS loss
                    caps_loss = 0.5 * ((newmu - newnextmu) ** 2).mean()  # nn.functional.mse_loss(newmu, newnextmu, reduction='mean')
                    loss = loss + caps_lambda * caps_loss
                    print(pg_loss, entropy_loss, v_loss, caps_loss)

                optimizer.zero_grad()
                loss.backward()
                nn.utils.clip_grad_norm_(agent.parameters(), args.max_grad_norm)
                optimizer.step()

            if args.target_kl is not None:
                if approx_kl > args.target_kl:
                    print(f"approx_kl is violated break at update_epochs {epoch}")
                    break

        y_pred, y_true = b_values.cpu().numpy(), b_returns.cpu().numpy()
        var_y = np.var(y_true)
        explained_var = np.nan if var_y == 0 else 1 - np.var(y_true - y_pred) / var_y

        metrics_dict = {
            "update": update,
            "charts/learning_rate": optimizer.param_groups[0]["lr"],
            "losses/value_loss": v_loss.item(),
            "losses/policy_loss": pg_loss.item(),
            "losses/entropy": entropy_loss.item(),
            "losses/old_approx_kl": old_approx_kl.item(),
            "losses/approx_kl": approx_kl.item(),
            "losses/clipfrac": np.mean(clipfracs),
            "losses/explained_variance": explained_var,
            "global_step": global_step,
            "charts/SPS": int(global_step / (time.time() - start_time))
        }

        if wandb_recorder:
            wandb_recorder.log(metrics_dict, commit=True)
        
        if update % 5 == 0:
            torch.save(agent.state_dict(), f'agent_{update}.pt')

    envs.close()
