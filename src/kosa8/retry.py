# Copyright (c) 2026 kosa8. Licensed under the MIT License; see LICENSE.
"""Retries with exponential backoff and full jitter.

Only failures that can pass are retried: ``infra`` (no answer, a 5xx, a host
or capacity problem) and rate limits (429, waiting at least the server's
``Retry-After``). Quota, payment, bad requests and your own exceptions are
raised at once.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, TypeVar

from .errors import classify_failure

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    """How often and how long to retry.

    ``attempts`` counts the first try (``1`` turns retries off). The n-th
    retry waits a random time up to ``min(max_s, base_s * 2**n)`` ("full
    jitter", so a thousand rollouts that failed together do not come back
    together), and never less than a 429's ``Retry-After`` (capped at
    ``max_retry_after_s``).
    """

    attempts: int = 3
    base_s: float = 0.5
    max_s: float = 8.0
    max_retry_after_s: float = 60.0

    def __post_init__(self) -> None:
        if not isinstance(self.attempts, int) or self.attempts < 1:
            raise ValueError("attempts must be a whole number of at least 1")
        if self.base_s < 0 or self.max_s < 0:
            raise ValueError("base_s and max_s must not be negative")

    def should_retry(self, exc: BaseException, attempt: int) -> bool:
        """Whether to try again after ``exc`` on attempt ``attempt`` (1-based)."""
        return attempt < self.attempts and classify_failure(exc)[1]

    def delay(self, exc: Optional[BaseException], attempt: int, *, rng: Optional[random.Random] = None) -> float:
        """Seconds to wait before attempt ``attempt + 1``."""
        r = rng or random
        cap = min(self.max_s, self.base_s * (2 ** max(0, attempt - 1)))
        wait = r.uniform(0, cap) if cap > 0 else 0.0
        ra = getattr(exc, "retry_after", None)
        if isinstance(ra, (int, float)) and ra > 0:
            wait = max(wait, min(float(ra), self.max_retry_after_s))
        return wait


#: No retries.
NO_RETRY = RetryPolicy(attempts=1)

#: How the retry helpers wait (seams for tests).
_sleep = time.sleep


async def _asleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def call_with_retry(
    fn: Callable[[], T],
    policy: RetryPolicy,
    *,
    sleep: Optional[Callable[[float], Any]] = None,
    on_retry: Optional[Callable[[BaseException, int, float], Any]] = None,
) -> T:
    """Call ``fn`` until it returns, retrying what ``policy`` allows."""
    attempt = 1
    while True:
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 (classified below)
            if not policy.should_retry(e, attempt):
                raise
            wait = policy.delay(e, attempt)
            if on_retry is not None:
                on_retry(e, attempt, wait)
            (sleep or _sleep)(wait)
            attempt += 1


async def acall_with_retry(
    fn: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    *,
    on_retry: Optional[Callable[[BaseException, int, float], Any]] = None,
) -> T:
    """The async :func:`call_with_retry`."""
    attempt = 1
    while True:
        try:
            return await fn()
        except Exception as e:  # noqa: BLE001
            if not policy.should_retry(e, attempt):
                raise
            wait = policy.delay(e, attempt)
            if on_retry is not None:
                on_retry(e, attempt, wait)
            await _asleep(wait)
            attempt += 1
