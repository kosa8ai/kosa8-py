"""Grade SWE-bench patches on kosa8: the instance's image, your patch, the
hidden tests in a sealed, offline fork. Masked results are infra failures.

    pip install kosa8 datasets
    python swebench_grade.py
"""

import kosa8
from datasets import load_dataset

from kosa8.rl import Run, swebench

instances = [i for i in load_dataset("princeton-nlp/SWE-bench_Verified", split="test")
             if i["repo"] in ("psf/requests", "pallets/flask")][:8]
tasks = swebench.tasks(instances)


def solve(sandbox, task):
    """Your agent goes here; this one submits the gold patch to check the harness."""
    r = swebench.apply_patch(sandbox, task["instance"]["patch"])
    return {"meta": {"applied": r.exit_code == 0}}


cloud = kosa8.Cloud()
with Run(cloud, "swebench-gold-check", project="swe") as run:
    results = run.map(lambda task: swebench.create(cloud, task["instance"]), tasks, solve,
                      grade_cmd=swebench.GRADE_CMD, grade_timeout_s=900, timeout_s=1200, concurrency=8)

for r in results:
    print(r.task["id"], r.reward, r.failure_class, "masked" if r.masked else "")
