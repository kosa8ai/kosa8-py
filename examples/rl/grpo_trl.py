"""TRL GRPOTrainer with kosa8 sandboxes as its environment.

    pip install kosa8 trl datasets
    export KOSA8_API_KEY=k8c_live_...

The model gets two tools, bash(command) and write_file(path, content), in its
own sandbox per generation (a fork of a prepared base). reward_func grades
each one in a sealed fork with the row's hidden grader file; an infra failure
returns None, which TRL leaves out of the loss instead of scoring 0.
"""

import kosa8
from datasets import Dataset
from trl import GRPOConfig, GRPOTrainer

from kosa8.rl import Run
from kosa8.rl.trl import environment_factory, reward_func

from _tasks import GRADER, PROMPT  # the toy task of quickstart.py

cloud = kosa8.Cloud()
with Run(cloud, "trl-grpo-add") as run:  # labels, leases and drains every sandbox below
    base = cloud.sandboxes.create(template="python")
    Env = environment_factory(cloud, base, grade_cmd=["python", "/grader/test.py"], run_id=run.id)
    prompt = [{"role": "user", "content": PROMPT + ", then stop."}]
    dataset = Dataset.from_dict({"prompt": [prompt] * 256, "files": [{"/grader/test.py": GRADER}] * 256})
    trainer = GRPOTrainer(
        model="Qwen/Qwen3-0.6B",
        train_dataset=dataset,
        reward_funcs=reward_func,
        environment_factory=Env,
        args=GRPOConfig(num_generations=8, max_completion_length=1024, max_steps=20),
    )
    trainer.train()
