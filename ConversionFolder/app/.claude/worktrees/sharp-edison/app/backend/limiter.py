"""
In-memory sliding-window rate limiter — FastAPI Depends injection.

Replaces slowapi to avoid the decorator-wrapping bug that causes
FastAPI to lose UploadFile type annotations on file-upload routes
(ForwardRef('UploadFile') FastAPIError on startup).

Usage
-----
    from .limiter import jobs_limiter, login_limiter
    from fastapi import Depends

    @router.post("/jobs")
    async def create_job(
        file: UploadFile = File(...),
        _rl: None = Depends(jobs_limiter),   # rate-limited, no signature impact
    ):

Limits are configurable via environment variables (read once at startup):
    RATE_LIMIT_JOBS   — job creation  (default: "20/minute")
    RATE_LIMIT_LOGIN  — login POST    (default: "5/minute")

Format: "<count>/<unit>"  where unit is second | minute | hour | day
Examples: "20/minute", "100/hour", "5/minute"
"""
import asyncio
import os
import time
from collections import defaultdict
from typing import Dict, List

from fastapi import HTTPException, Request

# ── Limit strings ─────────────────────────────────────────────────────────────

from .config import settings as _cfg
RATE_LIMIT_JOBS  = _cfg.rate_limit_jobs
RATE_LIMIT_LOGIN = _cfg.rate_limit_login

_PERIOD_SECONDS: dict[str, int] = {
    "second": 1,
    "minute": 60,
    "hour":   3_600,
    "day":    86_400,
}


def _parse(limit_str: str) -> tuple[int, int]:
    """'20/minute' → (20, 60)"""
    try:
        count_s, unit = limit_str.split("/", 1)
        return int(count_s.strip()), _PERIOD_SECONDS[unit.strip().lower()]
    except (ValueError, KeyError):
        return 20, 60   # safe default


# ── Limiter class ─────────────────────────────────────────────────────────────

class RateLimiter:
    """
    Sliding-window rate limiter as a FastAPI callable dependency.

    Each instance maintains its own per-IP window dict.  Create one
    instance per limit tier and inject with Depends().
    """

    def __init__(self, limit_str: str) -> None:
        self.max_calls, self.period = _parse(limit_str)
        self._windows: Dict[str, List[float]] = defaultdict(list)
        # asyncio.Lock prevents two concurrent coroutines from both reading count N,
        # both passing the < max_calls check, and both appending — bypassing the limit.
        self._lock = asyncio.Lock()

    async def __call__(self, request: Request) -> None:
        ip = request.client.host if request.client else "unknown"
        now = time.monotonic()

        async with self._lock:
            # Sliding window — discard timestamps older than the period
            window = [t for t in self._windows[ip] if now - t < self.period]

            if len(window) >= self.max_calls:
                raise HTTPException(
                    status_code=429,
                    detail=(
                        f"Rate limit exceeded — maximum {self.max_calls} requests "
                        f"per {self.period}s from this IP. Please wait and retry."
                    ),
                )

            window.append(now)
            self._windows[ip] = window


# ── Singleton instances ───────────────────────────────────────────────────────
# Created once at import time; shared across all requests for the same tier.

jobs_limiter  = RateLimiter(RATE_LIMIT_JOBS)
login_limiter = RateLimiter(RATE_LIMIT_LOGIN)
