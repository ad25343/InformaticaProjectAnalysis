"""
Claude API retry helper — exponential backoff on transient errors.

Usage:
    from .retry import claude_with_retry

    result = await claude_with_retry(
        lambda: client.messages.create(...),
        label="conversion Pass 1",
    )
"""
from __future__ import annotations
import asyncio
import logging
import random

import anthropic

log = logging.getLogger("conversion.retry")

# Status codes that are safe to retry (rate limit, overloaded, transient server errors)
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 529}


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, (
        anthropic.RateLimitError,
        anthropic.APIConnectionError,
        anthropic.InternalServerError,
    )):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code in _RETRYABLE_STATUS_CODES
    return False


async def claude_with_retry(
    call_fn,
    *,
    max_attempts: int = 3,
    base_delay: float = 10.0,
    label: str = "",
):
    """
    Call an async callable with exponential backoff on retryable Claude API errors.

    Args:
        call_fn:      Zero-argument async callable wrapping the Claude API call.
        max_attempts: Total attempts before re-raising (default 3).
        base_delay:   Base delay in seconds; actual delay = base * 2^attempt + jitter.
        label:        Human-readable label for log messages.

    Raises:
        The last exception if all attempts fail or the error is non-retryable.
    """
    for attempt in range(max_attempts):
        try:
            return await call_fn()
        except Exception as exc:
            non_retryable = not _is_retryable(exc)
            last_attempt   = attempt == max_attempts - 1

            if non_retryable or last_attempt:
                raise

            delay = base_delay * (2 ** attempt) + random.uniform(0, 2)
            log.warning(
                "claude_retry: %s — attempt %d/%d failed (%s: %s). Retrying in %.1fs.",
                label or "call", attempt + 1, max_attempts,
                type(exc).__name__, str(exc)[:120], delay,
            )
            await asyncio.sleep(delay)
