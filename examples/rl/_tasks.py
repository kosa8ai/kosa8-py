"""The toy task the RL examples share: write add(a, b); a hidden grader scores it."""

import random

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

PROMPT = "Write add(a, b) in /workspace/solution.py"


def make_tasks(n):
    return [{"id": f"add-{i}", "prompt": PROMPT, "files": {"/grader/test.py": GRADER}} for i in range(n)]


def policy(prompt):
    """Stand-in for your model: right most of the time."""
    return "def add(a, b):\n    return a + b\n" if random.random() < 0.7 else "def add(a, b):\n    return a - b\n"
