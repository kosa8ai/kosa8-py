# Copyright (c) 2026 kosa8. Licensed under the MIT License; see LICENSE.
"""Run one example script against the shims: ``python _script.py FILE``.
Calls Modal ``@app.local_entrypoint()`` functions afterwards, as ``modal run`` would."""

import os
import runpy
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import alias  # noqa: E402  (patches the SDKs before the example imports them)

path = sys.argv[1]
sys.argv = [path]
sys.path.insert(0, os.path.dirname(os.path.abspath(path)))
runpy.run_path(path, run_name="__main__")
for fn in alias.ENTRYPOINTS:
    fn()
