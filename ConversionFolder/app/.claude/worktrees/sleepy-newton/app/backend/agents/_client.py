"""
Shared Anthropic client factory.

All agents MUST construct their AsyncAnthropic client through make_client()
so that the per-call timeout is applied consistently from config.  Direct
`anthropic.AsyncAnthropic(api_key=...)` calls bypass the timeout and allow
the pipeline to hang indefinitely if the Anthropic API becomes unresponsive.

Usage:
    from ._client import make_client
    client = make_client()
"""
from __future__ import annotations

import anthropic

from ..config import settings as _cfg


def make_client() -> anthropic.AsyncAnthropic:
    """Return a configured AsyncAnthropic client with a hard per-call timeout."""
    return anthropic.AsyncAnthropic(
        api_key=_cfg.anthropic_api_key,
        timeout=float(_cfg.agent_timeout_secs),
    )


def make_sync_client() -> anthropic.Anthropic:
    """Return a configured synchronous Anthropic client with a hard per-call timeout."""
    return anthropic.Anthropic(
        api_key=_cfg.anthropic_api_key,
        timeout=float(_cfg.agent_timeout_secs),
    )
