import argparse
import copy
import math
import os
from itertools import chain

import numpy as np
from torch.utils.tensorboard import SummaryWriter
import torch
import torch.nn.functional as F
import tqdm

from . import envs, nets, replay, run, utils

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


class DisCorAgent:
    def __init__(
        self,
        obs_space_size,
        act_space_size,
        log_std_low,
        log_std_high,
        actor_net=nets.StochasticActor,
        critic_net=nets.BigCritic,
        delta_net=nets.BigCritic,
        hidden_size=1024,
    ):
        self.actor = actor_net(
            obs_space_size,
            act_space_size,
            log_std_low,
            log_std_high,
            hidden_size=hidden_size,
            dist_impl="pyd",
        )
        self.critic1 = critic_net(
            obs_space_size, act_space_size, hidden_size=hidden_size
        )
        self.critic2 = critic_net(
            obs_space_size, act_space_size, hidden_size=hidden_size
        )

        # delta networks are similar to critic networks but learn
        # the error between the critic and the target in a given (s, a).
        # the paper suggests using a slightly larger Q network for the deltas.
        self.delta1 = delta_net(obs_space_size, act_space_size, hidden_size=hidden_size)
        self.delta2 = delta_net(obs_space_size, act_space_size, hidden_size=hidden_size)

    def to(self, device):
        self.actor = self.actor.to(device)
        self.critic1 = self.critic1.to(device)
        self.critic2 = self.critic2.to(device)
        self.delta1 = self.delta1.to(device)
        self.delta2 = self.delta2.to(device)

    def eval(self):
        self.actor.eval()
        self.critic1.eval()
        self.critic2.eval()
        self.delta1.eval()
        self.delta2.eval()

    def train(self):
        self.actor.train()
        self.critic1.train()
        self.critic2.train()
        self.delta1.train()
        self.delta2.train()

    def save(self, path):
        actor_path = os.path.join(path, "actor.pt")
        critic1_path = os.path.join(path, "critic1.pt")
        critic2_path = os.path.join(path, "critic2.pt")
        delta1_path = os.path.join(path, "delta1.pt")
        delta2_path = os.path.join(path, "delta2.pt")
        torch.save(self.actor.state_dict(), actor_path)
        torch.save(self.critic1.state_dict(), critic1_path)
        torch.save(self.critic2.state_dict(), critic2_path)
        torch.save(self.delta1.state_dict(), delta1_path)
        torch.save(self.delta2.state_dict(), delta2_path)

    def load(self, path):
        actor_path = os.path.join(path, "actor.pt")
        critic1_path = os.path.join(path, "critic1.pt")
        critic2_path = os.path.join(path, "critic2.pt")
        delta1_path = os.path.join(path, "delta1.pt")
        delta2_path = os.path.join(path, "delta2.pt")
        self.actor.load_state_dict(torch.load(actor_path))
        self.critic1.load_state_dict(torch.load(critic1_path))
        self.critic2.load_state_dict(torch.load(critic2_path))
        self.delta1.load_state_dict(torch.load(delta1_path))
        self.delta2.load_state_dict(torch.load(delta2_path))

    def forward(self, state, from_cpu=True):
        if from_cpu:
            state = self.process_state(state)
        self.actor.eval()
        with torch.no_grad():
            act_dist = self.actor.forward(state)
            act = act_dist.mean
        self.actor.train()
        if from_cpu:
            act = self.process_act(act)
        return act

    def sample_action(self, state, from_cpu=True):
        if from_cpu:
            state = self.process_state(state)
        self.actor.eval()
        with torch.no_grad():
            act_dist = self.actor.forward(state)
            act = act_dist.sample()
        self.actor.train()
        if from_cpu:
            act = self.process_act(act)
        return act

    def process_state(self, state):
        return torch.from_numpy(np.expand_dims(state, 0).astype(np.float32)).to(
            utils.device
        )

    def process_act(self, act):
        return np.squeeze(act.clamp(-1.0, 1.0).cpu().numpy(), 0)


def discor(
    agent,
    buffer,
    train_env,
    test_env,
    num_steps=1_000_000,
    transitions_per_step=1,
    max_episode_steps=100_000,
    batch_size=512,
    tau=0.005,
    actor_lr=1e-4,
    critic_lr=1e-4,
    alpha_lr=1e-4,
    delta_lr=1e-4,
    gamma=0.99,
    eval_interval=5000,
    eval_episodes=10,
    warmup_steps=1000,
    actor_clip=None,
    critic_clip=None,
    delta_clip=None,
    actor_l2=0.0,
    critic_l2=0.0,
    delta_l2=0.0,
    target_delay=2,
    actor_delay=1,
    save_interval=100_000,
    name="discor_run",
    render=False,
    save_to_disk=True,
    log_to_disk=True,
    verbosity=0,
    gradient_updates_per_step=1,
    init_alpha=0.1,
    init_temp=10.0,
    infinite_bootstrap=True,
    **kwargs,
):
    """
    "DisCor: Corrective Feedback in Reinforcement Learning via
    Distribution Correction", Kumar et al., 2020.

    Reference: https://arxiv.org/abs/2003.07305

    Reduce the effect of inaccurate target values propagating
    through the Q-function by learning to estimate the target
    networks' inaccuracies and adjusting the TD error accordingly.

    Compare with: SAC
    """
    if save_to_disk or log_to_disk:
        save_dir = utils.make_process_dirs(name)
    if log_to_disk:
        writer = SummaryWriter(save_dir)
        writer.add_hparams(locals(), {})

    ###########
    ## SETUP ##
    ###########
    agent.to(device)
    agent.train()
    target_agent = copy.deepcopy(agent)
    target_agent.to(device)
    utils.hard_update(target_agent.critic1, agent.critic1)
    utils.hard_update(target_agent.critic2, agent.critic2)
    # update new delta networks
    utils.hard_update(target_agent.delta1, agent.delta1)
    utils.hard_update(target_agent.delta2, agent.delta2)
    target_agent.train()

    critic_optimizer = torch.optim.Adam(
        chain(
            agent.critic1.parameters(),
            agent.critic2.parameters(),
        ),
        lr=critic_lr,
        weight_decay=critic_l2,
        betas=(0.9, 0.999),
    )
    actor_optimizer = torch.optim.Adam(
        agent.actor.parameters(),
        lr=actor_lr,
        weight_decay=actor_l2,
        betas=(0.9, 0.999),
    )
    # pair of delta networks will be optimized similar to critics
    delta_optimizer = torch.optim.Adam(
        chain(
            agent.delta1.parameters(),
            agent.delta2.parameters(),
        ),
        lr=delta_lr,
        weight_decay=delta_l2,
        betas=(0.9, 0.999),
    )
    log_alpha = torch.Tensor([math.log(init_alpha)]).to(device)
    log_alpha.requires_grad = True
    log_alpha_optimizer = torch.optim.Adam([log_alpha], lr=alpha_lr, betas=(0.5, 0.999))
    target_entropy = -train_env.action_space.shape[0]
    # DisCor temperature parameters (DisCor paper Eq 8 tau variable).
    temp1 = torch.Tensor([init_temp]).to(device)
    temp2 = torch.Tensor([init_temp]).to(device)

    ###################
    ## TRAINING LOOP ##
    ###################
    run.warmup_buffer(buffer, train_env, warmup_steps, max_episode_steps)
    done = True
    steps_iter = range(num_steps)
    if verbosity:
        steps_iter = tqdm.tqdm(steps_iter)
    for step in steps_iter:
        for _ in range(transitions_per_step):
            if done:
                state = train_env.reset()
                steps_this_ep = 0
                done = False
            action = agent.sample_action(state)
            next_state, reward, done, info = train_env.step(action)
            if infinite_bootstrap:
                if steps_this_ep + 1 == max_episode_steps:
                    done = False
            buffer.push(state, action, reward, next_state, done)
            state = next_state
            steps_this_ep += 1
            if steps_this_ep >= max_episode_steps:
                done = True

        for _ in range(gradient_updates_per_step):
            learn_discor(
                buffer=buffer,
                target_agent=target_agent,
                agent=agent,
                actor_optimizer=actor_optimizer,
                critic_optimizer=critic_optimizer,
                log_alpha=log_alpha,
                log_alpha_optimizer=log_alpha_optimizer,
                delta_optimizer=delta_optimizer,
                target_entropy=target_entropy,
                batch_size=batch_size,
                gamma=gamma,
                critic_clip=critic_clip,
                actor_clip=actor_clip,
                delta_clip=delta_clip,
                temp1=temp1,
                temp2=temp2,
                tau=tau,
                update_policy=step % actor_delay == 0,
            )

        if step % target_delay == 0:
            utils.soft_update(target_agent.critic1, agent.critic1, tau)
            utils.soft_update(target_agent.critic2, agent.critic2, tau)
            # also update the new error "delta" networks
            utils.soft_update(target_agent.delta1, agent.delta1, tau)
            utils.soft_update(target_agent.delta2, agent.delta2, tau)

        if (step % eval_interval == 0) or (step == num_steps - 1):
            mean_return = run.evaluate_agent(
                agent, test_env, eval_episodes, max_episode_steps, render
            )
            if log_to_disk:
                writer.add_scalar("return", mean_return, step * transitions_per_step)

        if step % save_interval == 0 and save_to_disk:
            agent.save(save_dir)

    if save_to_disk:
        agent.save(save_dir)
    return agent


def learn_discor(
    buffer,
    target_agent,
    agent,
    actor_optimizer,
    critic_optimizer,
    log_alpha_optimizer,
    delta_optimizer,
    target_entropy,
    batch_size,
    log_alpha,
    gamma,
    critic_clip,
    actor_clip,
    delta_clip,
    temp1,
    temp2,
    tau,
    update_policy=True,
):
    assert not isinstance(buffer, replay.PrioritizedReplayBuffer)

    batch = buffer.sample(batch_size)
    state_batch, action_batch, reward_batch, next_state_batch, done_batch = batch
    state_batch = state_batch.to(device)
    next_state_batch = next_state_batch.to(device)
    action_batch = action_batch.to(device)
    reward_batch = reward_batch.to(device)
    done_batch = done_batch.to(device)

    agent.train()
    ###################
    ## CRITIC UPDATE ##
    ###################
    alpha = torch.exp(log_alpha)
    with torch.no_grad():
        action_dist_s1 = agent.actor(next_state_batch)
        action_s1 = action_dist_s1.rsample()
        logp_a1 = action_dist_s1.log_prob(action_s1).sum(-1, keepdim=True)
        # compute TD target as normal
        target_action_value_s1 = torch.min(
            target_agent.critic1(next_state_batch, action_s1),
            target_agent.critic2(next_state_batch, action_s1),
        )
        td_target = reward_batch + gamma * (1.0 - done_batch) * (
            target_action_value_s1 - (alpha * logp_a1)
        )
        target_delta1_s1 = target_agent.delta1(next_state_batch, action_s1)
        target_delta2_s1 = target_agent.delta2(next_state_batch, action_s1)
        # computing new transition weights to downweight states
        # where the bootstrapped target is inaccurate. we estimate
        # this by using the delta networks to predict the error
        # on the next_state_batch. I am not 100% sure whether it is
        # better to use the target_agent delta nets or agent delta nets
        # here. Figure 22 in the DisCor paper suggests the online agent,
        # but using the target would save us 2 forward passes...
        disCor_weights1 = batch_size * torch.softmax(
            -(1.0 - done_batch)
            * gamma
            * agent.delta1(next_state_batch, action_s1)
            / temp1,
            dim=0,
        )
        disCor_weights2 = batch_size * torch.softmax(
            -(1.0 - done_batch)
            * gamma
            * agent.delta2(next_state_batch, action_s1)
            / temp2,
            dim=0,
        )

    agent_critic1_pred = agent.critic1(state_batch, action_batch)
    agent_critic2_pred = agent.critic2(state_batch, action_batch)
    td_error1 = td_target - agent_critic1_pred
    td_error2 = td_target - agent_critic2_pred
    # reweight based on discor weights
    critic1_loss = disCor_weights1 * (td_error1 ** 2)
    critic2_loss = disCor_weights2 * (td_error2 ** 2)
    critic_loss = (critic1_loss + critic2_loss).mean()
    critic_optimizer.zero_grad()
    critic_loss.backward()
    if critic_clip:
        torch.nn.utils.clip_grad_norm_(
            chain(agent.critic1.parameters(), agent.critic2.parameters()), critic_clip
        )
    critic_optimizer.step()

    #########################
    ## DisCor Delta Update ##
    #########################
    with torch.no_grad():
        # compute error targets (DisCor paper Alg 3 line 5)
        target_delta1 = (
            torch.abs(td_error1) + gamma * (1.0 - done_batch) * target_delta1_s1
        )
        target_delta2 = (
            torch.abs(td_error2) + gamma * (1.0 - done_batch) * target_delta2_s1
        )
    # delta network loss (DisCor paper Alg 3 line 8)
    delta1_pred = agent.delta1(state_batch, action_batch)
    delta2_pred = agent.delta2(state_batch, action_batch)
    delta1_loss = (target_delta1 - delta1_pred) ** 2
    delta2_loss = (target_delta2 - delta2_pred) ** 2
    delta_loss = (delta1_loss + delta2_loss).mean()
    delta_optimizer.zero_grad()
    delta_loss.backward()
    if delta_clip:
        torch.nn.utils.clip_grad_norm_(
            chain(agent.delta1.parameters(), agent.delta2.parameters()), delta_clip
        )
    delta_optimizer.step()
    # auto-adjust temperatures (DisCor paper Alg 3 line 11)
    temp1.data = temp1.data * (1.0 - tau) + (tau * torch.mean(delta1_pred))
    temp2.data = temp2.data * (1.0 - tau) + (tau * torch.mean(delta2_pred))

    if update_policy:
        ##################
        ## ACTOR UPDATE ##
        ##################
        dist = agent.actor(state_batch)
        agent_actions = dist.rsample()
        logp_a = dist.log_prob(agent_actions).sum(-1, keepdim=True)
        actor_loss = -(
            torch.min(
                agent.critic1(state_batch, agent_actions),
                agent.critic2(state_batch, agent_actions),
            )
            - (alpha.detach() * logp_a)
        ).mean()
        actor_optimizer.zero_grad()
        actor_loss.backward()
        if actor_clip:
            torch.nn.utils.clip_grad_norm_(agent.actor.parameters(), actor_clip)
        actor_optimizer.step()

        ##################
        ## ALPHA UPDATE ##
        ##################
        alpha_loss = (-alpha * (logp_a + target_entropy).detach()).mean()
        log_alpha_optimizer.zero_grad()
        alpha_loss.backward()
        log_alpha_optimizer.step()


def add_args(parser):
    parser.add_argument(
        "--num_steps", type=int, default=10 ** 6, help="Number of steps in training"
    )
    parser.add_argument(
        "--transitions_per_step",
        type=int,
        default=1,
        help="env transitions per training step. Defaults to 1, but will need to \
        be set higher for repaly ratios < 1",
    )
    parser.add_argument(
        "--max_episode_steps",
        type=int,
        default=100000,
        help="maximum steps per episode",
    )
    parser.add_argument(
        "--batch_size", type=int, default=512, help="training batch size"
    )
    parser.add_argument(
        "--tau", type=float, default=0.005, help="for model parameter % update"
    )
    parser.add_argument(
        "--actor_lr", type=float, default=3e-4, help="actor learning rate"
    )
    parser.add_argument(
        "--critic_lr", type=float, default=3e-4, help="critic learning rate"
    )
    parser.add_argument(
        "--delta_lr", type=float, default=3e-4, help="delta learning rate"
    )
    parser.add_argument(
        "--gamma", type=float, default=0.99, help="gamma, the discount factor"
    )
    parser.add_argument(
        "--init_alpha",
        type=float,
        default=0.1,
        help="initial entropy regularization coefficeint.",
    )
    parser.add_argument(
        "--init_temp",
        type=float,
        default=10.0,
        help="initial temperature of discor reweighting coeff",
    )
    parser.add_argument(
        "--alpha_lr",
        type=float,
        default=1e-4,
        help="alpha (entropy regularization coefficeint) learning rate",
    )
    parser.add_argument(
        "--buffer_size", type=int, default=1_000_000, help="replay buffer size"
    )
    parser.add_argument(
        "--eval_interval",
        type=int,
        default=5000,
        help="how often to test the agent without exploration (in episodes)",
    )
    parser.add_argument(
        "--eval_episodes",
        type=int,
        default=10,
        help="how many episodes to run for when testing",
    )
    parser.add_argument(
        "--warmup_steps", type=int, default=1000, help="warmup length, in steps"
    )
    parser.add_argument(
        "--render",
        action="store_true",
        help="flag to enable env rendering during training",
    )
    parser.add_argument(
        "--actor_clip",
        type=float,
        default=None,
        help="gradient clipping for actor updates",
    )
    parser.add_argument(
        "--critic_clip",
        type=float,
        default=None,
        help="gradient clipping for critic updates",
    )
    parser.add_argument(
        "--delta_cilp",
        type=float,
        default=None,
        help="gradient clipping for delta network updates",
    )
    parser.add_argument(
        "--name", type=str, default="discor_run", help="dir name for saves"
    )
    parser.add_argument(
        "--actor_l2",
        type=float,
        default=0.0,
        help="L2 regularization coeff for actor network",
    )
    parser.add_argument(
        "--critic_l2",
        type=float,
        default=0.0,
        help="L2 regularization coeff for critic networks",
    )
    parser.add_argument(
        "--delta_l2",
        type=float,
        default=0.0,
        help="L2 regularization coeff for delta networks",
    )
    parser.add_argument(
        "--target_delay",
        type=int,
        default=2,
        help="How many steps to go between target network updates",
    )
    parser.add_argument(
        "--actor_delay",
        type=int,
        default=1,
        help="How many steps to go between actor updates",
    )
    parser.add_argument(
        "--save_interval",
        type=int,
        default=100_000,
        help="How many steps to go between saving the agent params to disk",
    )
    parser.add_argument(
        "--verbosity",
        type=int,
        default=1,
        help="verbosity > 0 displays a progress bar during training",
    )
    parser.add_argument(
        "--gradient_updates_per_step",
        type=int,
        default=1,
        help="how many gradient updates to make per env step",
    )
    parser.add_argument(
        "--skip_save_to_disk",
        action="store_true",
        help="flag to skip saving agent params to disk during training",
    )
    parser.add_argument(
        "--skip_log_to_disk",
        action="store_true",
        help="flag to skip saving agent performance logs to disk during training",
    )
    parser.add_argument(
        "--log_std_low",
        type=float,
        default=-10,
        help="Lower bound for log std of action distribution.",
    )
    parser.add_argument(
        "--log_std_high",
        type=float,
        default=2,
        help="Upper bound for log std of action distribution.",
    )
