"""
Simple session-based authentication.
Password is set via APP_PASSWORD in .env.
Sessions are signed cookies using itsdangerous.
"""
from __future__ import annotations
import os
import hashlib
import hmac
from datetime import datetime, timedelta

from fastapi import Request, HTTPException
from fastapi.responses import RedirectResponse
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

# ── Config ────────────────────────────────────
SECRET_KEY   = os.environ.get("SECRET_KEY",   "change-me-in-production-please")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
SESSION_HOURS = int(os.environ.get("SESSION_HOURS", "8"))
COOKIE_NAME  = "ict_session"

_signer = URLSafeTimedSerializer(SECRET_KEY)

# Public paths that don't require auth
PUBLIC_PATHS = {"/login", "/static", "/favicon.ico"}


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def create_session_token() -> str:
    return _signer.dumps({"auth": True, "ts": datetime.utcnow().isoformat()})


def verify_session_token(token: str) -> bool:
    try:
        _signer.loads(token, max_age=SESSION_HOURS * 3600)
        return True
    except (BadSignature, SignatureExpired):
        return False


def check_password(submitted: str) -> bool:
    if not APP_PASSWORD:
        # No password set — allow all (dev mode)
        return True
    return hmac.compare_digest(
        _hash_password(submitted),
        _hash_password(APP_PASSWORD)
    )


def is_authenticated(request: Request) -> bool:
    if not APP_PASSWORD:
        return True  # Dev mode — no password set
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return False
    return verify_session_token(token)


def require_auth(request: Request):
    """Dependency — raises 401 if not authenticated."""
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
