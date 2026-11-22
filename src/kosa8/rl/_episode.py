# Copyright (c) 2026 kosa8. Licensed under the MIT License; see LICENSE.
"""One multi-turn episode in one sandbox: the core the Gymnasium, OpenEnv
and TRL adapters share."""

from __future__ import annotations

from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple, Union

from ..cloud import CloudSandbox, _quiet_delete
from ..errors import Kosa8Error, classify_failure
from ._rollouts import DEFAULT_MASK, RolloutResult, _from_output, _maker, task_files

#: Most characters of command output handed back as an observation.
OBS_LIMIT = 16384


def prompt_of(task: Any) -> str:
    if isinstance(task, Mapping):
        for key in ("prompt", "problem_statement", "question", "instruction"):
            v = task.get(key)
            if isinstance(v, str):
                return v
    return task if isinstance(task, str) else ""


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + f"\n... [{len(text) - limit} characters cut] ...\n" + text[-half:]


class Episode:
    """A sandbox for one task at a time: ``reset(task)`` gets a fresh one
    (fork of ``base``, or created), ``run(cmd)`` acts in it, ``grade()`` scores
    it in a sealed fork, ``close()`` deletes it."""

    def __init__(
        self,
        cloud: Any,
        base: Any,
        *,
        grade_cmd: Optional[Union[str, Sequence[str]]] = None,
        files: Any = None,
        grade_timeout_s: Optional[float] = None,
        reward: Optional[Callable[..., Any]] = None,
        setup: Optional[Callable[[CloudSandbox, Any], Optional[str]]] = None,
        run_id: Optional[str] = None,
        lease_s: Optional[int] = None,
        obs_limit: int = OBS_LIMIT,
        mask: Sequence[str] = DEFAULT_MASK,
    ) -> None:
        self.cloud = cloud
        self.base = base
        self.grade_cmd = grade_cmd
        self.files = files
        self.grade_timeout_s = grade_timeout_s
        self.reward_fn = reward
        self.setup = setup
        self.run_id = run_id
        self.lease_s = lease_s
        self.obs_limit = obs_limit
        self.mask = tuple(mask)
        self.box: Optional[CloudSandbox] = None
        self.task: Any = None
        self.steps = 0
        self.failure_class = "ok"
        self.error: Optional[str] = None
        if not isinstance(base, CloudSandbox):
            self._make = _maker(cloud, base, run_id, lease_s)

    def reset(self, task: Any) -> str:
        """A fresh sandbox for ``task``; returns the first observation (the
        ``setup`` hook's, else the task's prompt)."""
        self.close()
        self.task, self.steps, self.failure_class, self.error = task, 0, "ok", None
        if isinstance(self.base, CloudSandbox):
            kids = self.base.fork(1, run_id=self.run_id, lease_s=self.lease_s)
            self.box = kids[0]
        else:
            self.box = self._make(task)
        if self.setup is not None:
            obs = self.setup(self.box, task)
            if obs is not None:
                return obs
        return prompt_of(task)

    def run(self, command: Union[str, Sequence[str]]) -> Tuple[str, Optional[int]]:
        """Run a command; returns its output (stdout, then stderr) and exit code.
        A kosa8 failure is caught: the text says what happened and
        :attr:`failure_class` is set (``infra`` means mask this episode)."""
        if self.box is None:
            raise RuntimeError("call reset() first")
        self.steps += 1
        try:
            r = self.box.exec(command)
        except Kosa8Error as e:
            fc, _ = classify_failure(e)
            if fc != "timeout":
                self.failure_class, self.error = fc, f"{type(e).__name__}: {e}"
            return f"[kosa8: {e}]", None
        out = r.stdout + (("\n" if r.stdout and r.stderr else "") + r.stderr if r.stderr else "")
        return clip(out, self.obs_limit), r.exit_code

    @property
    def masked(self) -> bool:
        return self.failure_class in self.mask

    def grade(self) -> Tuple[Optional[float], Dict[str, Any]]:
        """``(reward, info)``; ``reward`` is ``None`` when the episode is masked."""
        info: Dict[str, Any] = {"failure_class": self.failure_class, "steps": self.steps}
        if self.box is None or self.masked:
            info["masked"] = True
            return None, info
        try:
            if self.grade_cmd is not None:
                files = self.files(self.task) if callable(self.files) else (self.files or task_files(self.task))
                g = self.box.grade(self.grade_cmd, files=files, timeout_s=self.grade_timeout_s, run_id=self.run_id)
                info.update(failure_class=g.failure_class, metrics=g.metrics, exit=g.exit_code, grader_id=g.grader_id)
                reward: Optional[float] = g.reward
            elif self.reward_fn is not None:
                holder = RolloutResult(task=self.task, group=0, index=0)
                if not _from_output(holder, self.reward_fn(self.box, self.task, None)):
                    raise TypeError("reward= must return a number or {'reward': x}")
                reward = holder.reward
            else:
                raise ValueError("give grade_cmd= or reward=")
        except Kosa8Error as e:
            fc, _ = classify_failure(e)
            self.failure_class, self.error = fc, f"{type(e).__name__}: {e}"
            info["failure_class"] = fc
            reward = None
        info["masked"] = info["failure_class"] in self.mask
        return (None if info["masked"] else reward), info

    def close(self) -> None:
        box, self.box = self.box, None
        if box is not None:
            _quiet_delete(box)
