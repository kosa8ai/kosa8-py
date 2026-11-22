# Copyright (c) 2026 kosa8. Licensed under the MIT License; see LICENSE.
"""kosa8 for RL training: thousands of agent rollouts that do not leak
sandboxes, grade in sealed forks, mask infra failures, and show up on
app.kosa8.com (RL runs).

>>> import kosa8
>>> from kosa8.rl import Run
>>> cloud = kosa8.Cloud()
>>> with Run(cloud, "my-grpo-run") as run:
...     base = cloud.sandboxes.create(template="python")
...     results = run.map(base, tasks, solve, grade_cmd=["python", "/grader/test.py"], concurrency=64)

- :class:`Run` / :class:`AsyncRun`: a recorded run; ``map`` runs one step.
- :func:`map_rollouts`: the asyncio rollout engine on its own.
- :func:`lease` / :class:`Lease`: heartbeats while you run, drain when you stop.
- Adapters, each importing its framework only when used:
  :mod:`kosa8.rl.gym` (Gymnasium), :mod:`kosa8.rl.openenv` (OpenEnv),
  :mod:`kosa8.rl.verifiers` (Prime Intellect verifiers v1 runtime),
  :mod:`kosa8.rl.trl` (TRL ``environment_factory``), :mod:`kosa8.rl.nemo_gym`
  (NeMo Gym sandbox provider), :mod:`kosa8.rl.swebench` (SWE-bench instances).
"""

from ..errors import FAILURE_CLASSES, classify_failure
from ._lease import DEFAULT_LEASE_S, Lease, lease
from ._rollouts import DEFAULT_MASK, RolloutResult, Rollouts, map_rollouts
from ._run import AsyncRun, Run

__all__ = [
    "Run",
    "AsyncRun",
    "Lease",
    "lease",
    "map_rollouts",
    "RolloutResult",
    "Rollouts",
    "DEFAULT_MASK",
    "DEFAULT_LEASE_S",
    "FAILURE_CLASSES",
    "classify_failure",
]
