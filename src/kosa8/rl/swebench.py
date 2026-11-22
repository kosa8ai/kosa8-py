# Copyright (c) 2026 kosa8. Licensed under the MIT License; see LICENSE.
"""SWE-bench on kosa8: start an instance's image, apply a patch, grade with
the hidden tests in a sealed fork.

    from kosa8.rl import Run, swebench

    tasks = swebench.tasks(instances)            # rows of princeton-nlp/SWE-bench_Verified
    with Run(cloud, "swe-grpo") as run:
        results = run.map(lambda t: swebench.create(cloud, t["instance"]), tasks, solve,
                          grade_cmd=swebench.GRADE_CMD, timeout_s=1200)

Each task carries its hidden grader files (the instance's ``test_patch`` and
a runner script); the agent's sandbox never sees them, and grading runs with
no network. The default test command is pytest over ``FAIL_TO_PASS`` and
``PASS_TO_PASS`` (reward 1.0 only if all pass), which fits the pytest repos;
for django (``tests/runtests.py``), sympy (``bin/test``) and others pass
``test_cmd`` (``{tests}`` is replaced by the quoted test names), or take it
from the ``swebench`` package's own test spec. No SWE-bench import is needed.

Sandboxes are ``persistent`` (one long-lived container), so changes outside
``/workspace`` (the repository is at ``/testbed``) stay between commands.
The hidden files (test patch and runner) are at most 64 and 8 MiB in all,
and the grader's command runs at most 90 s (a buffered exec). :func:`grade`
raises :class:`GradeTooLong` for a test run that needs longer: split the
instance's tests (``test_cmd`` over a part of ``FAIL_TO_PASS`` /
``PASS_TO_PASS``) into several grades. Longer grading comes with the
upcoming kosa8 Jobs API. In :meth:`kosa8.rl.Run.map` such a rollout is
``timeout`` (``reward=None``), not reward 0.
"""

from __future__ import annotations

import json
import shlex
from typing import Any, Dict, List, Mapping, Optional, Sequence

from ..cloud import EXEC_LIMIT_S
from ..errors import Kosa8ExecTimeoutError

_SPLIT_HINT = (
    "the sealed grader runs at most {s} s: split the instance's tests into several grades "
    "(test_cmd over part of FAIL_TO_PASS / PASS_TO_PASS each), or run the long suite in the "
    "sandbox with exec_background; longer grading comes with the upcoming kosa8 Jobs API"
)


class GradeTooLong(Kosa8ExecTimeoutError):
    """The instance's tests need longer than the sealed grader's 90 s."""

#: Where SWE-bench images keep the repository.
REPO_DIR = "/testbed"
#: The grader command for tasks made by :func:`tasks`.
GRADE_CMD = ["sh", "/grader/run.sh"]
#: pytest over the instance's tests.
PYTEST_CMD = "python -m pytest -rA -p no:cacheprovider -x {tests}"


def instance_image(instance: Mapping[str, Any], *, namespace: str = "swebench", arch: str = "x86_64",
                   tag: str = "latest") -> str:
    """The instance's evaluation image, as SWE-bench names them on Docker Hub
    (``swebench/sweb.eval.x86_64.django_1776_django-11099:latest``)."""
    iid = str(instance["instance_id"]).replace("__", "_1776_").lower()
    return f"docker.io/{namespace}/sweb.eval.{arch}.{iid}:{tag}"


def _tests(v: Any) -> List[str]:
    if isinstance(v, str):
        v = json.loads(v) if v.strip().startswith("[") else [v]
    return [str(t) for t in (v or [])]


def create(cloud: Any, instance: Mapping[str, Any], *, size: str = "2x4", image: Optional[str] = None,
           **kw: Any) -> Any:
    """A running sandbox of the instance's image (persistent, so ``/testbed`` edits last)."""
    kw.setdefault("persistent", True)
    return cloud.sandboxes.create(image=image or instance_image(instance), size=size, **kw)


def apply_patch(sandbox: Any, patch: str, *, repo_dir: str = REPO_DIR) -> Any:
    """Apply a unified diff in the repository; returns the exec result (exit 0 when it applied)."""
    if not patch.strip():
        raise ValueError("the patch is empty")
    sandbox.files.write("/workspace/.kosa8/patch.diff", patch)
    return sandbox.exec(f"cd {shlex.quote(repo_dir)} && git apply -v /workspace/.kosa8/patch.diff")


def grader_files(instance: Mapping[str, Any], *, test_cmd: Optional[str] = None,
                 repo_dir: str = REPO_DIR) -> Dict[str, str]:
    """The hidden files that grade ``instance``: its test patch and ``/grader/run.sh``.

    The script refuses an empty diff (an instance that passes with no change
    is not a solve), applies the test patch, runs the tests and prints
    ``{"reward": 1.0 | 0.0, "metrics": {...}}``."""
    tests = _tests(instance.get("FAIL_TO_PASS")) + _tests(instance.get("PASS_TO_PASS"))
    quoted = " ".join(shlex.quote(t) for t in tests)
    cmd = (test_cmd or PYTEST_CMD).replace("{tests}", quoted)
    script = f"""#!/bin/sh
cd {shlex.quote(repo_dir)} || exit 2
if git diff --quiet HEAD; then
  echo '{{"reward": 0.0, "metrics": {{"empty_patch": 1}}}}'
  exit 0
fi
git apply -v /grader/test.patch || {{ echo '{{"reward": 0.0, "metrics": {{"test_patch_failed": 1}}}}'; exit 0; }}
{cmd}
rc=$?
if [ "$rc" -eq 0 ]; then r=1.0; else r=0.0; fi
echo "{{\\"reward\\": $r, \\"metrics\\": {{\\"tests\\": {len(tests)}, \\"exit\\": $rc}}}}"
exit 0
"""
    return {"/grader/test.patch": str(instance.get("test_patch") or ""), "/grader/run.sh": script}


def tasks(instances: Sequence[Mapping[str, Any]], *, test_cmd: Optional[str] = None) -> List[Dict[str, Any]]:
    """Tasks for :meth:`kosa8.rl.Run.map`: ``id``, ``prompt`` (the problem
    statement), hidden ``files`` and the ``instance`` itself."""
    return [{"id": i["instance_id"], "prompt": i.get("problem_statement", ""),
             "files": grader_files(i, test_cmd=test_cmd), "instance": dict(i)} for i in instances]


def grade(sandbox: Any, instance: Mapping[str, Any], *, patch: Optional[str] = None, test_cmd: Optional[str] = None,
          timeout_s: Optional[float] = 90, run_id: Optional[str] = None,
          rollout: Optional[Mapping[str, Any]] = None) -> Any:
    """Grade the sandbox's ``/testbed`` (after applying ``patch``, if given)
    with the instance's hidden tests, in a sealed fork; a :class:`~kosa8.GradeResult`.

    Raises :class:`GradeTooLong` (a :class:`~kosa8.Kosa8ExecTimeoutError`)
    when ``timeout_s`` is over 90 s or the tests ran past it."""
    if timeout_s is not None and timeout_s > EXEC_LIMIT_S:
        raise GradeTooLong(f"timeout_s={timeout_s} is over the limit: " + _SPLIT_HINT.format(s=EXEC_LIMIT_S),
                           code="exec_timeout")
    if patch is not None:
        r = apply_patch(sandbox, patch)
        if r.exit_code != 0:
            raise ValueError(f"the patch does not apply: {r.stderr.strip()[-500:]}")
    g = sandbox.grade(GRADE_CMD, files=grader_files(instance, test_cmd=test_cmd), timeout_s=timeout_s,
                      run_id=run_id, rollout=rollout)
    if g.failure_class == "timeout":
        raise GradeTooLong(f"the tests of {instance.get('instance_id', 'this instance')} did not finish: "
                           + _SPLIT_HINT.format(s=timeout_s or EXEC_LIMIT_S), code="exec_timeout")
    return g
