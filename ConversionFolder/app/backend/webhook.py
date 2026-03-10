"""
Outbound webhook notifications (v2.9.0).

Fires a structured JSON POST to WEBHOOK_URL at key pipeline events:

  gate_waiting  — pipeline paused at Gate 1, 2, or 3; a human decision is required
  job_complete  — Gate 3 approved; generated code is ready for export/deployment
  job_failed    — pipeline reached a terminal FAILED or BLOCKED state

Configuration (all via .env / environment variables):
  WEBHOOK_URL            POST destination URL; empty = notifications disabled
  WEBHOOK_SECRET         Optional HMAC-SHA256 signing key (see below)
  WEBHOOK_TIMEOUT_SECS   Per-request timeout in seconds (default 10)

Signature verification (optional):
  When WEBHOOK_SECRET is set every outbound request carries an
  X-Webhook-Signature: sha256=<hex> header.  Receivers compute
  HMAC-SHA256(secret, raw_request_body) and compare with constant-time
  equality to verify the payload originated from this tool.

All failures are logged as warnings and never block the pipeline.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone

import httpx

from .config import settings

_log = logging.getLogger("conversion.webhook")

_TOOL = "Informatica Conversion Tool"


async def fire_webhook(
    event: str,
    job_id: str,
    filename: str,
    step: int,
    status: str,
    message: str,
    gate: str | None = None,
) -> None:
    """
    Fire a non-blocking, non-fatal webhook notification.

    Args:
        event:    "gate_waiting" | "job_complete" | "job_failed"
        job_id:   UUID of the job
        filename: Original mapping filename
        step:     Current pipeline step number (0–12)
        status:   Job status string (awaiting_review, complete, failed, blocked, …)
        message:  Human-readable description of the event
        gate:     Gate label, e.g. "Gate 1 — Human Sign-off" (None for non-gate events)
    """
    url = settings.webhook_url
    if not url:
        return  # Not configured — silent no-op

    payload = {
        "event":     event,
        "job_id":    job_id,
        "filename":  filename,
        "step":      step,
        "status":    status,
        "message":   message,
        "gate":      gate,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tool":      _TOOL,
        "version":   settings.app_version,
    }

    body = json.dumps(payload, default=str).encode()

    headers = {
        "Content-Type": "application/json",
        "User-Agent":   f"{_TOOL}/{settings.app_version}",
    }

    # Optional HMAC-SHA256 request signing
    if settings.webhook_secret:
        sig = hmac.new(
            settings.webhook_secret.encode(),
            body,
            hashlib.sha256,
        ).hexdigest()
        headers["X-Webhook-Signature"] = f"sha256={sig}"

    try:
        async with httpx.AsyncClient(timeout=settings.webhook_timeout_secs) as client:
            resp = await client.post(url, content=body, headers=headers)
        if resp.is_success:
            _log.info(
                "Webhook sent — event=%s job=%s http=%d",
                event, job_id, resp.status_code,
            )
        else:
            _log.warning(
                "Webhook non-2xx — event=%s job=%s http=%d body=%s",
                event, job_id, resp.status_code, resp.text[:200],
            )
    except Exception as exc:
        _log.warning(
            "Webhook failed (non-fatal) — event=%s job=%s error=%s",
            event, job_id, exc,
        )
