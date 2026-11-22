# Copyright (c) 2026 kosa8. Licensed under the MIT License; see LICENSE.
"""``e2b_code_interpreter`` on kosa8 Cloud: change
``from e2b_code_interpreter import Sandbox`` to
``from kosa8.compat.e2b_code_interpreter import Sandbox``.

Mirrors ``e2b-code-interpreter`` **2.11.10** (``Sandbox.run_code``,
``create_code_context``, ``list_code_contexts``, ``remove_code_context``,
``restart_code_context`` and the ``Execution`` / ``Result`` / ``Logs``
models), on top of everything :mod:`kosa8.compat.e2b` mirrors. Sandboxes
start from kosa8's ``python`` template unless ``template=`` says otherwise.
Python cells only; other languages raise :class:`kosa8.compat.Unsupported`.
"""

from __future__ import annotations

from . import e2b as _e2b
from .e2b import *  # noqa: F401,F403
from .e2b import Context, Execution, ExecutionError, Logs, OutputMessage, Result  # noqa: F401

MIRRORS = {"e2b-code-interpreter": "2.11.10", "e2b": "2.64.0"}


class Sandbox(_e2b.Sandbox):
    """E2B's code-interpreter ``Sandbox``; its default template is kosa8's ``python``."""

    _default_template = "python"


__all__ = list(_e2b.__all__)
