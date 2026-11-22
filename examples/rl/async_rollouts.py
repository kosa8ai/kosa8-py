"""The same loop under asyncio, for trainers whose rollout code is async
(verifiers, SkyRL, verl agent loops): AsyncCloud + AsyncRun.

    pip install 'kosa8[async]'   # pooled httpx client (optional)
"""

import asyncio

import kosa8
from kosa8.rl import AsyncRun

from _tasks import make_tasks, policy  # the toy task of quickstart.py

tasks = make_tasks(32)


async def solve(sandbox, task):
    await sandbox.files.write("/workspace/solution.py", policy(task["prompt"]))


async def main():
    async with kosa8.AsyncCloud() as cloud:
        async with AsyncRun(cloud, "async-grpo", lease_s=900) as run:
            base = await cloud.sandboxes.create(template="python")
            for step in range(3):
                results = await run.map(base, tasks, solve, group_size=8, concurrency=128,
                                        grade_cmd=["python", "/grader/test.py"])
                # Masked rollouts (infra failures) have reward None and mask False: drop them from the loss.
                print(step, results.mean_reward, sum(results.mask), "of", len(results), "train")


asyncio.run(main())
