# Copyright (c) 2026 kosa8. Licensed under the MIT License; see LICENSE.
"""kosa8 sandboxes for TRL's ``GRPOTrainer(environment_factory=...)``.

Checked against TRL's OpenEnv guide (TRL 1.18, 2026-11): the trainer makes
one environment per generation from a class it calls with no arguments,
calls ``reset(**row)`` with the dataset row's columns at the start of each
episode, exposes every other public method as a tool (built from its
docstring's ``Args:``), feeds a tool's exception back to the model, and
passes the instances to reward functions as ``environments``. A reward of
``None`` is treated as NaN (left out), which is how a masked rollout is
kept out of the loss here.

    from kosa8.rl.trl import environment_factory, reward_func

    Env = environment_factory(lambda: kosa8.Cloud(), base, grade_cmd=["python", "/grader/test.py"])
    trainer = GRPOTrainer(model=..., train_dataset=ds, reward_funcs=reward_func, environment_factory=Env)

Each dataset row is the task: its ``files`` column (hidden grader files) is
used by the grader and never shown to the model. The sandbox of an episode
is deleted when it is graded, on the next ``reset``, and at exit.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, List, Optional, Sequence, Union

from ._episode import Episode


def environment_factory(
    cloud: Union[Any, Callable[[], Any]],
    base: Any,
    *,
    grade_cmd: Optional[Union[str, Sequence[str]]] = None,
    files: Any = None,
    reward: Optional[Callable[..., Any]] = None,
    setup: Optional[Callable[[Any, Any], Optional[str]]] = None,
    run_id: Optional[str] = None,
    lease_s: Optional[int] = None,
    write_tool: bool = True,
) -> type:
    """A class to pass as ``environment_factory``. ``cloud`` is a client or a
    function making one (made once, shared). Its tools are ``bash(command)``
    and, with ``write_tool``, ``write_file(path, content)``."""
    lock = threading.Lock()
    shared: dict = {}

    def get_cloud() -> Any:
        with lock:
            if "c" not in shared:
                shared["c"] = cloud() if callable(cloud) and not hasattr(cloud, "sandboxes") else cloud
            return shared["c"]

    class Kosa8Env:
        def __init__(self) -> None:
            self._episode = Episode(get_cloud(), base, grade_cmd=grade_cmd, files=files, reward=reward,
                                    setup=setup, run_id=run_id, lease_s=lease_s)
            self._reward: Optional[float] = None
            self._graded = False
            self._info: dict = {}

        def reset(self, **kwargs: Any) -> Optional[str]:
            self._reward, self._graded, self._info = None, False, {}
            return self._episode.reset(dict(kwargs)) or None

        def bash(self, command: str) -> str:
            """
            Run a shell command in your sandbox.

            Args:
                command: The command, run with sh -c. Files you keep go in /workspace.

            Returns:
                The command's output and exit code.
            """
            out, code = self._episode.run(command)
            if self._episode.masked:
                raise RuntimeError("the sandbox failed; the episode is over")
            return f"{out}\n[exit {code}]"

        def _grade(self) -> Optional[float]:
            if not self._graded:
                self._reward, self._info = self._episode.grade()
                self._graded = True
                self._episode.close()
            return self._reward

        def __del__(self) -> None:  # pragma: no cover (GC timing)
            try:
                self._episode.close()
            except Exception:  # noqa: BLE001
                pass

    if write_tool:
        def write_file(self: Any, path: str, content: str) -> str:
            """
            Write a text file in your sandbox.

            Args:
                path: Absolute path, e.g. /workspace/solution.py
                content: The whole file.

            Returns:
                What was written.
            """
            box = self._episode.box
            if box is None:
                raise RuntimeError("no sandbox: the episode has not started")
            info = box.files.write(path, content)
            return f"wrote {info.get('size', len(content))} bytes to {path}"

        Kosa8Env.write_file = write_file  # type: ignore[attr-defined]

    Kosa8Env.__name__ = Kosa8Env.__qualname__ = "Kosa8Env"
    return Kosa8Env


def reward_func(environments: List[Any], **kwargs: Any) -> List[Optional[float]]:
    """Grade each episode (once) and return its reward; ``None`` (left out of
    the loss) for a masked episode."""
    return [env._grade() for env in environments]


def failure_classes(environments: List[Any]) -> List[str]:
    """Each episode's failure class after :func:`reward_func`, for logging."""
    return [env._info.get("failure_class", env._episode.failure_class) for env in environments]
