# Copyright (c) 2026 kosa8. Licensed under the MIT License; see LICENSE.
"""Drop-in shims for code written against other sandbox SDKs.

Change one import and the same code runs on kosa8 Cloud::

    from e2b import Sandbox                       # before
    from kosa8.compat.e2b import Sandbox          # after

    from daytona import Daytona                   # before
    from kosa8.compat.daytona import Daytona      # after

    import modal                                  # before
    import kosa8.compat.modal as modal            # after

Every shim calls :class:`kosa8.Kosa8Cloud` (``KOSA8_API_KEY`` from the
environment, ``KOSA8_CLOUD_URL`` for another address). A call kosa8 cannot do
raises :class:`Unsupported`, which names the method and what to use on kosa8
instead; a shim never pretends a call worked.

Things that differ everywhere, because kosa8 Cloud works this way:

* Each command runs in a fresh container and only ``/workspace`` persists
  between commands, so relative paths are put under ``/workspace``.
* A buffered command runs at most 90 seconds; run longer work in the
  background (E2B ``background=True``, Modal's entrypoint) and poll it.
* Output arrives when a command ends, not as it is printed; callbacks such as
  E2B's ``on_stdout`` are called once, with all of it.

Every shim object keeps the kosa8 object it wraps as ``.kosa8``
(a :class:`kosa8.CloudSandbox`) for anything the other SDK has no word for.
"""

from __future__ import annotations

import functools
import posixpath
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..cloud import Kosa8Cloud
from ..errors import Kosa8Error

__all__ = ["Unsupported", "set_default_cloud", "get_cloud"]


class Unsupported(Kosa8Error, NotImplementedError):
    """A method of another SDK that kosa8 cannot do.

    ``method`` is the method as the other SDK spells it (``e2b.Sandbox.pty``),
    ``alternative`` what to use on kosa8 instead.
    """

    def __init__(self, method: str, alternative: str, reason: str = "") -> None:
        why = f": {reason}" if reason else ""
        super().__init__(f"{method} is not supported by kosa8{why}. On kosa8: {alternative}", code="unsupported")
        self.method = method
        self.alternative = alternative


_default_cloud: Optional[Kosa8Cloud] = None


def set_default_cloud(cloud: Optional[Kosa8Cloud]) -> None:
    """Make every shim use ``cloud`` (for a custom address, timeout or
    ``opener``); ``None`` goes back to a ``Kosa8Cloud()`` per call."""
    global _default_cloud
    _default_cloud = cloud


def get_cloud(api_key: Optional[str] = None) -> Kosa8Cloud:
    """The cloud a shim talks to: the one given to :func:`set_default_cloud`,
    else ``Kosa8Cloud(api_key)`` (``KOSA8_API_KEY`` when ``api_key`` is None)."""
    if _default_cloud is not None and api_key is None:
        return _default_cloud
    if _default_cloud is not None:
        return Kosa8Cloud(api_key, base_url=_default_cloud.base_url, timeout=_default_cloud.timeout,
                          opener=_default_cloud._open)
    return Kosa8Cloud(api_key)


# --- helpers the shims share ---------------------------------------------------

WORKSPACE = "/workspace"


def abs_path(path: str) -> str:
    """A relative path is under /workspace, the folder kosa8 keeps between commands."""
    if not path:
        raise ValueError("a path is required")
    if path.startswith("/"):
        return path
    p = path[2:] if path.startswith("./") else path
    return posixpath.join(WORKSPACE, p)


def in_dir(argv: Sequence[str], cwd: Optional[str]) -> List[str]:
    """``argv`` run in ``cwd`` (relative to /workspace)."""
    if not cwd:
        return list(argv)
    return ["sh", "-c", 'cd "$1" || exit 1; shift; exec "$@"', "sh", abs_path(cwd), *argv]


def bash_or_sh(cmd: str) -> List[str]:
    """``cmd`` through bash when the image has it, else sh (the base template is Alpine)."""
    return ["sh", "-c", 'if command -v bash >/dev/null 2>&1; then exec bash -l -c "$1"; fi; exec sh -c "$1"', "sh", cmd]


def is_kosa8_url(url: str) -> bool:
    host = urllib.parse.urlparse(url if "//" in url else "https://" + url).hostname or ""
    return host in ("localhost", "127.0.0.1", "::1") or host == "kosa8.com" or host.endswith(".kosa8.com")


def check_url(method: str, url: Optional[str]) -> None:
    """Refuse another provider's address rather than send a kosa8 key there."""
    if url and not is_kosa8_url(url):
        raise Unsupported(
            method,
            "leave it out; kosa8 reads KOSA8_CLOUD_URL (default https://api.kosa8.com)",
            f"{url} is not a kosa8 address and the kosa8 API key is never sent to it",
        )


def given(v: Any) -> bool:
    """An argument the caller set: not None, False or empty (0 counts as set)."""
    if v is None or v is False:
        return False
    return not (isinstance(v, (list, tuple, dict, set, str)) and len(v) == 0)


def stat_entry(box: Any, path: str) -> Optional[Dict[str, Any]]:
    """One file's entry from its folder's listing, or None when it is not there."""
    p = abs_path(path).rstrip("/") or "/"
    if p == "/":
        return {"name": "/", "path": "/", "type": "dir"}
    parent, name = posixpath.split(p)
    from ..errors import Kosa8NotFoundError

    try:
        entries = box.files.list(parent or "/")
    except Kosa8NotFoundError:
        return None
    for e in entries:
        if e.get("name") == name or e.get("path") == p:
            return e
    return None


class Hybrid:
    """A method callable on the class (with an id first) and on an instance,
    the way E2B's ``Sandbox.kill(sandbox_id)`` and ``sandbox.kill()`` both work."""

    def __init__(self, on_instance: Callable[..., Any], on_class: Callable[..., Any]) -> None:
        self._inst = on_instance
        self._cls = on_class
        functools.update_wrapper(self, on_instance)

    def __get__(self, obj: Any, typ: Optional[type] = None) -> Callable[..., Any]:
        if obj is None:
            return functools.partial(self._cls, typ)
        return functools.partial(self._inst, obj)


def translating(convert: Callable[[Kosa8Error], Optional[BaseException]]) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Raise the other SDK's exception for a kosa8 one (the kosa8 error is its
    ``__cause__``). :class:`Unsupported` goes through as it is."""

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def run(*a: Any, **kw: Any) -> Any:
            try:
                return fn(*a, **kw)
            except Unsupported:
                raise
            except Kosa8Error as e:
                other = convert(e)
                if other is None:
                    raise
                raise other from e

        return run

    return deco

