# Copyright (c) 2026 kosa8. Licensed under the MIT License; see LICENSE.
"""One definition for a sync and an async group of kosa8 Cloud calls.

A group's methods check their arguments, then ``return self._call(method,
path, body, then=...)``: ``then`` turns the API's answer into what the method
returns. :class:`Calls` runs the request at once; :class:`AsyncCalls` makes
``_call`` a coroutine, so ``class AsyncX(AsyncCalls, X)`` is the async twin of
``X`` with every method awaitable and nothing written twice. Arguments are
checked when the method is called, before anything is sent, in both.
"""

from __future__ import annotations

import urllib.parse
from typing import Any, Callable, Dict, Mapping, Optional


def q(v: Any) -> str:
    """Percent-encode one path segment."""
    return urllib.parse.quote(str(v), safe="")


def query(params: Mapping[str, Any]) -> str:
    """``?a=1&b=x`` from the params that are not None (``""`` when none are)."""
    out: Dict[str, str] = {}
    for k, v in params.items():
        if v is None:
            continue
        if isinstance(v, bool):
            v = "1" if v else "0"
        elif isinstance(v, (list, tuple)):
            v = ",".join(str(x) for x in v)
        out[k] = str(v)
    return ("?" + urllib.parse.urlencode(out)) if out else ""


def key(name: str, default: Any = None) -> Callable[[Any], Any]:
    """``then`` that answers ``answer[name]`` (``default`` when absent)."""
    def pick(answer: Any) -> Any:
        if isinstance(answer, dict) and name in answer:
            return answer[name]
        return default() if callable(default) else default
    return pick


def as_dict(answer: Any) -> Dict[str, Any]:
    return answer if isinstance(answer, dict) else {}


class Calls:
    """A group of calls on a :class:`~kosa8.Kosa8Cloud`."""

    def __init__(self, cloud: Any) -> None:
        self._cloud = cloud

    def _call(self, method: str, path: str, body: Any = None, *, then: Optional[Callable[[Any], Any]] = None,
              **kw: Any) -> Any:
        answer = self._cloud._request(method, path, body, **kw)
        return then(answer) if then is not None else answer


class AsyncCalls(Calls):
    """The same group on a :class:`~kosa8.AsyncCloud`: every call is a coroutine."""

    async def _call(self, method: str, path: str, body: Any = None, *,  # type: ignore[override]
                    then: Optional[Callable[[Any], Any]] = None, **kw: Any) -> Any:
        answer = await self._cloud._request(method, path, body, **kw)
        return then(answer) if then is not None else answer
