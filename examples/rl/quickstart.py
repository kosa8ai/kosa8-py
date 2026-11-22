"""kosa8 RL quickstart: one GRPO-style step of 64 rollouts, graded in sealed forks.

    pip install kosa8
    export KOSA8_API_KEY=k8c_live_...
    python quickstart.py

Each task asks for ``add(a, b)`` in /workspace/solution.py. The grader
(/grader/test.py) is hidden: kosa8 writes it only into a sealed, offline
fork of the solved sandbox, so the agent can never read or edit it. Swap
``policy`` for your model. The run shows up at app.kosa8.com -> RL runs.
"""

import random

import kosa8
from kosa8.rl import Run

GRADER = """
import json, sys
sys.path.insert(0, "/workspace")
try:
    from solution import add
    cases = [(1, 2), (-3, 3), (10, 5), (0, 0)]
    passed = sum(add(a, b) == a + b for a, b in cases)
except Exception:
    passed = 0
print(json.dumps({"reward": passed / 4, "metrics": {"passed": passed}}))
"""

tasks = [
    {"id": f"add-{i}", "prompt": "Write add(a, b) in /workspace/solution.py", "files": {"/grader/test.py": GRADER}}
    for i in range(16)
]


def policy(prompt: str) -> str:
    """Stand-in for your model: right most of the time."""
    return "def add(a, b):\n    return a + b\n" if random.random() < 0.7 else "def add(a, b):\n    return a - b\n"


def solve(sandbox, task):
    """The agent's turn, in its own fork of the base sandbox."""
    sandbox.files.write("/workspace/solution.py", policy(task["prompt"]))
    return {"meta": {"model": "toy"}}


cloud = kosa8.Cloud()
with Run(cloud, "my-grpo-run") as run:
    base = cloud.sandboxes.create(template="python")
    results = run.map(base, tasks, solve, grade_cmd=["python", "/grader/test.py"], concurrency=64)

print(f"mean reward {results.mean_reward}, by class {results.by_class}")
for group in results.groups()[:3]:
    print([r.reward for r in group])
