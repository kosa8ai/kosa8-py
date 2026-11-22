# Copyright (c) 2026 kosa8. Licensed under the MIT License; see LICENSE.
"""A Gymnasium-style environment whose actions are shell commands in a kosa8
sandbox.

    from kosa8.rl.gym import SandboxEnv

    env = SandboxEnv(cloud, base, tasks, grade_cmd=["python", "/grader/test.py"])
    obs, info = env.reset(seed=0)
    obs, reward, terminated, truncated, info = env.step("python -c 'print(1)'")
    obs, reward, terminated, truncated, info = env.step("submit")   # graded here
    env.close()

The action is a command string; the observation is its output (stdout then
stderr, clipped). The episode ends on the ``submit`` action or after
``max_steps``; it is then graded in a sealed fork (``grade_cmd`` with the
task's hidden files) or by ``reward``. ``info`` carries ``failure_class``
and ``masked``: a masked episode (infra failure) has reward 0.0 because
Gymnasium needs a float, so check ``info["masked"]`` before training on it.

It subclasses ``gymnasium.Env`` and sets ``Text`` spaces when Gymnasium is
installed (``pip install gymnasium``); without it, it is a plain class with
the same methods.
"""

from __future__ import annotations

import random
from typing import Any, Callable, Dict, Optional, Sequence, Tuple, Union

from ._episode import OBS_LIMIT, Episode

try:  # optional
    import gymnasium as _gym  # type: ignore
    from gymnasium import spaces as _spaces  # type: ignore

    _Base: Any = _gym.Env
except ImportError:  # pragma: no cover (exercised when gymnasium is absent)
    _gym = None
    _spaces = None
    _Base = object


class SandboxEnv(_Base):  # type: ignore[misc]
    """See the module docstring. ``base`` is a sandbox to fork per episode
    (set it up once), a ``snap_`` id, create options (a dict) or
    ``make(task)``. ``tasks`` is a sequence (``reset`` draws one with its
    seed, or takes ``options={"task": t}``) or a function returning a task."""

    metadata: Dict[str, Any] = {"render_modes": []}

    def __init__(
        self,
        cloud: Any,
        base: Any,
        tasks: Union[Sequence[Any], Callable[[], Any], None] = None,
        *,
        grade_cmd: Optional[Union[str, Sequence[str]]] = None,
        files: Any = None,
        reward: Optional[Callable[..., Any]] = None,
        setup: Optional[Callable[[Any, Any], Optional[str]]] = None,
        max_steps: int = 30,
        submit: str = "submit",
        obs_limit: int = OBS_LIMIT,
        run_id: Optional[str] = None,
        lease_s: Optional[int] = None,
    ) -> None:
        if _gym is not None:
            super().__init__()
            self.observation_space = _spaces.Text(max_length=obs_limit + 200)
            self.action_space = _spaces.Text(max_length=65536)
        self.tasks = tasks
        self.max_steps = max_steps
        self.submit = submit
        self.episode = Episode(cloud, base, grade_cmd=grade_cmd, files=files, reward=reward, setup=setup,
                               run_id=run_id, lease_s=lease_s, obs_limit=obs_limit)
        self._rng = random.Random()
        self._done = True

    @property
    def sandbox(self) -> Any:
        """The current episode's sandbox (``None`` before ``reset``)."""
        return self.episode.box

    def _pick(self, options: Optional[Dict[str, Any]]) -> Any:
        if options and "task" in options:
            return options["task"]
        if callable(self.tasks):
            return self.tasks()
        if self.tasks:
            return self._rng.choice(list(self.tasks))
        return None

    def reset(self, *, seed: Optional[int] = None, options: Optional[Dict[str, Any]] = None) -> Tuple[str, Dict[str, Any]]:
        if seed is not None:
            self._rng.seed(seed)
        task = self._pick(options)
        obs = self.episode.reset(task)
        self._done = False
        return obs, {"task": task, "sandbox_id": self.episode.box.id if self.episode.box else None}

    def step(self, action: str) -> Tuple[str, float, bool, bool, Dict[str, Any]]:
        if self._done:
            raise RuntimeError("the episode is over: call reset()")
        if action.strip() == self.submit:
            return self._end("", terminated=True)
        obs, code = self.episode.run(action)
        if self.episode.masked:
            return self._end(obs, terminated=True)
        if self.episode.steps >= self.max_steps:
            return self._end(obs, terminated=False)
        return obs, 0.0, False, False, {"exit_code": code, "failure_class": "ok", "masked": False}

    def _end(self, obs: str, *, terminated: bool) -> Tuple[str, float, bool, bool, Dict[str, Any]]:
        reward, info = self.episode.grade()
        self._done = True
        self.episode.close()
        return obs, float(reward or 0.0), terminated, not terminated, info

    def close(self) -> None:
        self.episode.close()
        self._done = True
