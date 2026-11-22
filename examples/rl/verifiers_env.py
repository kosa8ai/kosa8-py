"""Prime Intellect verifiers (v1) running its rollouts in kosa8 sandboxes.

kosa8 plugs in as a custom runtime; nothing in the environment changes.
In the environment's config:

    [env.agent.runtime]
    type = "custom"
    import_path = "kosa8.rl.verifiers.Kosa8Backend"
    template = "python"        # or image = "ghcr.io/acme/task:1.0"
    lease_s = 3600             # a crashed eval cannot leak sandboxes

    export KOSA8_API_KEY=k8c_live_...

This script checks the runtime end to end without verifiers' CLI: it starts
one, runs a command, writes and reads a file, and cleans up.
"""

import asyncio

from kosa8.rl.verifiers import Kosa8Backend, Kosa8RuntimeConfig


async def main():
    config = Kosa8RuntimeConfig(import_path="kosa8.rl.verifiers.Kosa8Backend", template="python")
    backend = Kosa8Backend(config)
    await backend.start()
    try:
        runtime = backend.bind()
        await runtime.write("hello.txt", b"hi from verifiers")
        print((await runtime.run(["cat", "hello.txt"], {})).stdout)
    finally:
        await backend.stop()


asyncio.run(main())
