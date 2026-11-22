# Copyright (c) 2026 kosa8. Licensed under the MIT License; see LICENSE.
"""An OpenEnv environment backed by kosa8 sandboxes.

Checked against OpenEnv's ``openenv.core.env_server`` source (2026-11):
``Environment`` with ``reset(seed, episode_id, **kw)``, ``step(action,
timeout_s, **kw)`` and a ``state`` property, pydantic ``Action`` /
``Observation`` (``done``, ``reward``, ``metadata``) / ``State``
(``episode_id``, ``step_count``), and ``create_app(env_factory, ActionCls,
ObservationCls, max_concurrent_envs=...)``. Each environment instance owns
its own sandbox, so ``SUPPORTS_CONCURRENT_SESSIONS`` is true and one server
can serve many TRL generations at once::

    from kosa8.rl.openenv import create_app

    app = create_app(lambda: kosa8.Cloud(), base_snapshot, tasks,
                     grade_cmd=["python", "/grader/test.py"], max_concurrent_envs=64)
    # uvicorn module:app --port 8001

``pip install "openenv>=0.3"``; the classes are built on first use, so
importing this module does not need OpenEnv.
"""

from __future__ import annotations

import random
import uuid
from typing import Any, Callable, Dict, Optional, Sequence, Union

from ._episode import Episode

_CLASSES: Dict[str, Any] = {}


def _require() -> Any:
    try:
        from openenv.core import env_server  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise ImportError("kosa8.rl.openenv needs OpenEnv: pip install 'openenv>=0.3'") from e
    return env_server


def _classes() -> Dict[str, Any]:
    if _CLASSES:
        return _CLASSES
    es = _require()

    class ShellAction(es.Action):  # type: ignore[misc, name-defined]
        """A shell command to run in the sandbox (``sh -c``); ``submit=True``
        ends the episode and grades it."""

        command: str = ""
        submit: bool = False

    class ShellObservation(es.Observation):  # type: ignore[misc, name-defined]
        """Command output (stdout, then stderr, clipped); ``reward`` is set when
        the episode ends, ``masked`` when it failed for infra reasons."""

        output: str = ""
        exit_code: Optional[int] = None
        failure_class: str = "ok"
        masked: bool = False

    class Kosa8ShellEnvironment(es.Environment):  # type: ignore[misc, name-defined]
        SUPPORTS_CONCURRENT_SESSIONS = True

        def __init__(self, cloud: Any, base: Any, tasks: Any = None, *, max_steps: int = 30, **episode_kw: Any):
            super().__init__()
            self._episode = Episode(cloud, base, **episode_kw)
            self._tasks = tasks
            self._max_steps = max_steps
            self._rng = random.Random()
            self._state = es.State(episode_id=None, step_count=0)

        def _pick(self, kw: Dict[str, Any]) -> Any:
            if "task" in kw:
                return kw["task"]
            if callable(self._tasks):
                return self._tasks()
            return self._rng.choice(list(self._tasks)) if self._tasks else None

        def reset(self, seed: Optional[int] = None, episode_id: Optional[str] = None, **kw: Any) -> Any:
            if seed is not None:
                self._rng.seed(seed)
            obs = self._episode.reset(self._pick(kw))
            self._state = es.State(episode_id=episode_id or uuid.uuid4().hex, step_count=0)
            return ShellObservation(output=obs, done=False, reward=None)

        def step(self, action: Any, timeout_s: Optional[float] = None, **kw: Any) -> Any:
            if action.submit:
                return self._end("")
            out, code = self._episode.run(action.command)
            self._state.step_count = self._episode.steps
            if self._episode.masked or self._episode.steps >= self._max_steps:
                return self._end(out, code)
            return ShellObservation(output=out, exit_code=code, done=False, reward=None)

        def _end(self, out: str, code: Optional[int] = None) -> Any:
            reward, info = self._episode.grade()
            self._episode.close()
            return ShellObservation(output=out, exit_code=code, done=True, reward=reward,
                                    failure_class=info.get("failure_class", "ok"), masked=bool(info.get("masked")),
                                    metadata={k: v for k, v in info.items() if k in ("metrics", "steps", "grader_id")})

        @property
        def state(self) -> Any:
            return self._state

        def close(self) -> None:
            self._episode.close()

    _CLASSES.update(ShellAction=ShellAction, ShellObservation=ShellObservation,
                    Kosa8ShellEnvironment=Kosa8ShellEnvironment)
    return _CLASSES


def __getattr__(name: str) -> Any:
    if name in ("ShellAction", "ShellObservation", "Kosa8ShellEnvironment"):
        return _classes()[name]
    raise AttributeError(name)


def make_environment(cloud: Any, base: Any, tasks: Union[Sequence[Any], Callable[[], Any], None] = None,
                     **kw: Any) -> Any:
    """A ``Kosa8ShellEnvironment``: ``reset()`` gets a fresh sandbox (fork of
    ``base``), ``step(ShellAction(command=...))`` runs a command,
    ``ShellAction(submit=True)`` grades it (``grade_cmd``/``files``/``reward``
    as in :meth:`kosa8.rl.Run.map`)."""
    return _classes()["Kosa8ShellEnvironment"](cloud, base, tasks, **kw)


def create_app(
    cloud_factory: Callable[[], Any],
    base: Any,
    tasks: Union[Sequence[Any], Callable[[], Any], None] = None,
    *,
    max_concurrent_envs: int = 64,
    **kw: Any,
) -> Any:
    """An OpenEnv FastAPI app serving a kosa8 environment per session
    (``max_concurrent_envs`` at once, each with its own sandbox)."""
    es = _require()
    c = _classes()
    cloud = cloud_factory()
    return es.create_app(lambda: c["Kosa8ShellEnvironment"](cloud, base, tasks, **kw), c["ShellAction"],
                         c["ShellObservation"], max_concurrent_envs=max_concurrent_envs)
