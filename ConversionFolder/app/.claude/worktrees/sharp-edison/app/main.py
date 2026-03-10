"""
Informatica Conversion Tool — FastAPI Application Entry Point
"""
import asyncio
import logging
import os
import time
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from dotenv import load_dotenv

load_dotenv()

from backend.config import settings as _cfg          # centralised config — must be first
from backend.db.database import init_db, DB_PATH
from backend.routes import router
from backend.logger import configure_app_logging
from backend.auth import (
    is_authenticated, check_password,
    create_session_token, COOKIE_NAME, SESSION_HOURS,
    SECRET_KEY,
)
from backend.limiter import login_limiter
from backend.cleanup import run_cleanup_loop, run_watchdog_loop

_startup_log = logging.getLogger("conversion.startup")

TEMPLATES = Path(__file__).parent / "frontend" / "templates"


_APP_START_TIME = time.monotonic()


@asynccontextmanager
async def lifespan(app: FastAPI):
    log_level = _cfg.log_level
    configure_app_logging(log_level)
    await init_db()

    # ── Security startup guards ────────────────────────────────────────────
    if SECRET_KEY == "change-me-in-production-please":
        if _cfg.app_password:
            # APP_PASSWORD is set → this is a real deployment. Hard-fail so the
            # operator cannot accidentally ship with a token-forgeable secret key.
            raise RuntimeError(
                "FATAL: SECRET_KEY is the default insecure placeholder while "
                "APP_PASSWORD is set (production mode). Generate a strong key with:\n"
                "  python -c \"import secrets; print(secrets.token_hex(32))\"\n"
                "and set SECRET_KEY in your .env file. Refusing to start."
            )
        else:
            _startup_log.warning(
                "SECURITY WARNING: SECRET_KEY is the default insecure value. "
                "Set a strong random SECRET_KEY in your .env before any non-local deployment."
            )
    if not _cfg.app_password:
        _startup_log.warning(
            "SECURITY WARNING: APP_PASSWORD is not set. "
            "The application is running in open-access dev mode — all requests are unauthenticated. "
            "Set APP_PASSWORD in your .env for any non-local deployment."
        )

    # ── Stuck-job recovery ─────────────────────────────────────────────────
    # Jobs left in mid-pipeline states (parsing, classifying, documenting,
    # verifying, converting) across a server restart can never complete —
    # their asyncio tasks are gone.  Mark them FAILED so the UI shows them
    # as actionable (delete + re-upload) rather than spinning forever.
    from backend.db.database import recover_stuck_jobs
    recovered = await recover_stuck_jobs()
    if recovered:
        _startup_log.warning(
            "Startup recovery: marked %d stuck job(s) as FAILED "
            "(were mid-pipeline when server last stopped). "
            "Delete and re-upload to retry.",
            len(recovered),
        )

    # ── GAP #13 — Model deprecation + API key check ───────────────────────
    try:
        import anthropic as _anthropic
        _probe = _anthropic.AsyncAnthropic(api_key=_cfg.anthropic_api_key)
        await _probe.messages.create(
            model=_cfg.claude_model, max_tokens=1,
            messages=[{"role": "user", "content": "ping"}],
        )
    except _anthropic.NotFoundError:
        _startup_log.error(
            "MODEL DEPRECATED: '%s' returned 404 — update claude_model in .env "
            "to a current model string. All jobs will fail until this is fixed.",
            _cfg.claude_model,
        )
    except _anthropic.AuthenticationError:
        _startup_log.error(
            "API KEY INVALID: Anthropic rejected the key in ANTHROPIC_API_KEY. "
            "All jobs will fail until a valid key is provided in .env."
        )
    except _anthropic.PermissionDeniedError:
        _startup_log.error(
            "API PERMISSION DENIED: the configured API key lacks required access. "
            "All jobs will fail until key permissions are corrected."
        )
    except Exception as _probe_exc:
        # Genuine network/rate-limit/timeout — non-fatal; will surface on first job
        _startup_log.warning(
            "Startup API probe inconclusive (%s: %s) — will retry on first job.",
            type(_probe_exc).__name__, str(_probe_exc)[:120],
        )

    # ── Start background job cleanup loop ─────────────────────────────────
    _bg_cleanup = asyncio.create_task(run_cleanup_loop())
    _bg_cleanup.set_name("cleanup_loop")

    # ── GAP #16 — Start stuck-job timeout watchdog ────────────────────────
    _bg_watchdog = asyncio.create_task(run_watchdog_loop())
    _bg_watchdog.set_name("watchdog_loop")

    _bg_tasks = [_bg_cleanup, _bg_watchdog]

    # ── GAP #15 — Graceful shutdown ───────────────────────────────────────
    yield

    # Cancel background loops
    for _bg in _bg_tasks:
        _bg.cancel()
    await asyncio.gather(*_bg_tasks, return_exceptions=True)
    _startup_log.info("Shutdown: background loops cancelled.")

    # Cancel any in-flight pipeline tasks so they don't outlive the process
    from backend.routes import _active_tasks
    if _active_tasks:
        _startup_log.info("Shutdown: cancelling %d active pipeline task(s)…", len(_active_tasks))
        for _task in list(_active_tasks.values()):
            _task.cancel()
        await asyncio.gather(*_active_tasks.values(), return_exceptions=True)
        _startup_log.info("Shutdown: all pipeline tasks cancelled.")


app = FastAPI(
    title="Informatica Conversion Tool",
    description="Converts Informatica PowerCenter mappings to Python, PySpark, or dbt",
    version="2.6.1",
    lifespan=lifespan,
    # Hide docs behind auth in production — set SHOW_DOCS=false in .env
    docs_url="/docs" if _cfg.show_docs else None,
    redoc_url=None,
)

# ── CORS — restrict to same-origin by default ────────────
# Allow additional origins via CORS_ORIGINS="https://your.domain,https://other.domain"
_cors_origins_env = _cfg.cors_origins
_allowed_origins: list[str] = (
    [o.strip() for o in _cors_origins_env.split(",") if o.strip()]
    if _cors_origins_env
    else []  # empty → same-origin only (browser enforces; no CORS headers emitted)
)
if _allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type", "Authorization"],
    )
    _startup_log.info("CORS enabled for origins: %s", _allowed_origins)
else:
    _startup_log.info("CORS: same-origin only (CORS_ORIGINS not set; no cross-origin headers emitted)")

# ── Static files (always public — just CSS/JS assets) ────
static_dir = Path(__file__).parent / "frontend" / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# ── Login page ────────────────────────────────────────────
@app.get("/login")
async def login_page(request: Request):
    if is_authenticated(request):
        return RedirectResponse("/", status_code=302)
    return FileResponse(str(TEMPLATES / "login.html"))


@app.get("/health")
async def health_check():
    """Lightweight health check — used by load balancers and uptime monitors."""
    import aiosqlite
    db_ok = False
    try:
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute("SELECT 1")
        db_ok = True
    except Exception:
        pass
    return JSONResponse({
        "status":         "ok" if db_ok else "degraded",
        "version":        "2.3.0",
        "uptime_seconds": round(time.monotonic() - _APP_START_TIME, 1),
        "db":             "ok" if db_ok else "error",
    }, status_code=200 if db_ok else 503)


@app.post("/login")
async def login_submit(
    request: Request,
    password: str = Form(...),
    _rl: None = Depends(login_limiter),
):
    if check_password(password):
        token = create_session_token()
        response = RedirectResponse("/", status_code=302)
        response.set_cookie(
            key=COOKIE_NAME,
            value=token,
            httponly=True,
            samesite="lax",
            max_age=SESSION_HOURS * 3600,
            secure=_cfg.https,
        )
        return response
    return RedirectResponse("/login?error=1", status_code=302)


@app.get("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(COOKIE_NAME)
    return response


# ── Protected API routes ──────────────────────────────────
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path

    # Always allow: health check, login page, static assets, favicon
    if (path == "/health" or
        path.startswith("/login") or
        path.startswith("/static") or
        path == "/favicon.ico"):
        return await call_next(request)

    # Check authentication for everything else
    if not is_authenticated(request):
        # API calls → 401 JSON
        if path.startswith("/api/"):
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        # UI/browser requests → redirect to login
        return RedirectResponse(f"/login", status_code=302)

    return await call_next(request)


# ── API routes ────────────────────────────────────────────
app.include_router(router)


# ── Serve the main UI (catch-all, auth enforced by middleware) ──
@app.get("/")
@app.get("/{path:path}")
async def serve_ui(path: str = ""):
    index = TEMPLATES / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return {"message": "Informatica Conversion Tool API", "docs": "/docs"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
