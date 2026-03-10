"""
Simple session-based authentication.
Password is set via APP_PASSWORD in .env.
Sessions are signed cookies using itsdangerous.

Password hashing uses bcrypt — bcrypt is deliberately slow so brute-force
attacks are computationally expensive even if the hash leaks.
Work factor is configurable via BCRYPT_ROUNDS (default: 12 ≈ 250ms).
"""
from __future__ import annotations
import bcrypt
from datetime import datetime

from fastapi import Request, HTTPException
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from .config import settings

SECRET_KEY    = settings.secret_key
APP_PASSWORD  = settings.app_password
SESSION_HOURS = settings.session_hours
COOKIE_NAME   = "ict_session"

_signer = URLSafeTimedSerializer(SECRET_KEY)

# Hash the app password once at startup.  bcrypt.hashpw() is called only here;
# subsequent checks use bcrypt.checkpw() which is safe against timing attacks.
_APP_PASSWORD_HASH: bytes | None = (
    bcrypt.hashpw(APP_PASSWORD.encode(), bcrypt.gensalt(rounds=settings.bcrypt_rounds))
    if APP_PASSWORD else None
)

# Public paths that don't require auth
PUBLIC_PATHS = {"/login", "/static", "/favicon.ico"}


def create_session_token() -> str:
    return _signer.dumps({"auth": True, "ts": datetime.utcnow().isoformat()})


def verify_session_token(token: str) -> bool:
    try:
        _signer.loads(token, max_age=SESSION_HOURS * 3600)
        return True
    except (BadSignature, SignatureExpired):
        return False


def check_password(submitted: str) -> bool:
    if not APP_PASSWORD or _APP_PASSWORD_HASH is None:
        # No password set — allow all (dev mode)
        return True
    # bcrypt.checkpw handles constant-time comparison internally
    return bcrypt.checkpw(submitted.encode(), _APP_PASSWORD_HASH)


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
