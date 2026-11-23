# Copyright (c) 2026 kosa8. Licensed under the MIT License; see LICENSE.
"""Point another SDK's names at the kosa8 shims, so that SDK's own tests and
examples run unmodified against kosa8 Cloud.

Importing this module (directly, or as a pytest plugin: ``-p alias``) imports
the original package (``e2b``, ``e2b_code_interpreter``, ``daytona``,
``modal``) and replaces its top-level names with the shim's. Code that does
``from e2b import Sandbox`` then gets :class:`kosa8.compat.e2b.Sandbox`; names
the shim does not have (``Template``, ``Volume``, ...) stay the original's.
``KOSA8_COMPAT_PROVIDERS`` (comma list) picks the packages; all by default.

Every sandbox a shim creates is appended to ``KOSA8_COMPAT_RECORD`` (a file),
so the harness can delete what a failed test left behind.
"""

from __future__ import annotations

import importlib
import os
from typing import Any, Callable, Dict, List

import kosa8.compat as compat

SHIMS = {
    "e2b": "kosa8.compat.e2b",
    "e2b_code_interpreter": "kosa8.compat.e2b_code_interpreter",
    "daytona": "kosa8.compat.daytona",
    "modal": "kosa8.compat.modal",
}

#: Entry points a harness can call after running an example as a script
#: (Modal's ``@app.local_entrypoint()``).
ENTRYPOINTS: List[Callable[..., Any]] = []


def _record(create: Callable[..., Any]) -> Callable[..., Any]:
    path = os.environ.get("KOSA8_COMPAT_RECORD")

    def wrapped(*a: Any, **kw: Any) -> Any:
        box = create(*a, **kw)
        if path:
            with open(path, "a") as f:
                f.write(box.id + "\n")
        return box

    return wrapped


def install(providers: List[str]) -> Dict[str, str]:
    """Patch each provider's package; answers {provider: what happened}."""
    done: Dict[str, str] = {}
    recorded = _record(compat.create_sandbox)
    for p in providers:
        shim = importlib.import_module(SHIMS[p])
        if hasattr(shim, "create_sandbox"):
            shim.create_sandbox = recorded  # type: ignore[attr-defined]
        try:
            real = importlib.import_module(p)
        except ImportError:
            import sys

            sys.modules[p] = shim
            done[p] = "shim only (original not installed)"
            continue
        names = [n for n in getattr(shim, "__all__", dir(shim)) if not n.startswith("_")]
        for n in names:
            setattr(real, n, getattr(shim, n))
        if p == "modal":
            _modal_entrypoints(shim)
        if p == "daytona":
            _daytona_v0_types(real)
        done[p] = f"{len(names)} names from {SHIMS[p]}"
    return done


def _daytona_v0_types(real: Any) -> None:
    """Daytona 2.x moved the 0.x names (``Image``, ``PtySize``, ...) under
    ``daytona.v0``; the 0.x examples import them from the root. Names the shim
    does not define come from there (types only; every call goes to the shim)."""
    import importlib

    v0 = importlib.import_module("daytona.v0")
    real.__getattr__ = lambda name: getattr(v0, name)


def _modal_entrypoints(shim: Any) -> None:
    """``@app.local_entrypoint()`` records the function for the harness to call
    (what ``modal run`` does); ``@app.function`` is a Modal Function, which the
    shim does not mirror."""
    def local_entrypoint(self: Any, *a: Any, **kw: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            ENTRYPOINTS.append(fn)
            return fn

        return deco

    def function(self: Any, *a: Any, **kw: Any) -> Any:
        raise compat.Unsupported("modal App.function (Modal Functions)", "run the code in a sandbox: Sandbox.create(...).exec(...)")

    shim.App.local_entrypoint = local_entrypoint
    if not hasattr(shim.App, "function"):
        shim.App.function = function


_wanted = [p.strip() for p in os.environ.get("KOSA8_COMPAT_PROVIDERS", ",".join(SHIMS)).split(",") if p.strip()]
INSTALLED = install(_wanted)
