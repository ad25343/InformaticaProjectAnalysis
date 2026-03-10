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

from backend.db.database import init_db, DB_PATH
from backend.routes import router
from backend.logger import configure_app_logging
from backend.auth import (
    is_authenticated, check_password,
    create_session_token, COOKIE_NAME, SESSION_HOURS,
    SECRET_KEY,
)
from backend.limiter import login_limiter
from backend.cleanup import run_cleanup_loop

_startup_log = logging.getLogger("conversion.startup")

TEMPLATES = Path(__file__).parent / "frontend" / "templates"


_APP_START_TIME = time.monotonic()


@asynccontextmanager
async def lifespan(app: FastAPI):
    log_level = os.environ.get("LOG_LEVEL", "INFO")
    configure_app_logging(log_level)
    await init_db()

    # ── Security startup warnings ──────────────────────────────────────────
    if SECRET_KEY == "change-me-in-production-please":
        _startup_log.warning(
            "SECURITY WARNING: SECRET_KEY is set to the default insecure value. "
            "Set a strong random SECRET_KEY in your .env before deploying to production."
        )
    if not os.environ.get("APP_PASSWORD"):
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

    # ── Start background job cleanup loop ─────────────────────────────────
    asyncio.create_task(run_cleanup_loop())

    yield


app = FastAPI(
    title="Informatica Conversion Tool",
    description="Converts Informatica PowerCenter mappings to Python, PySpark, or dbt",
    version="1.1.0",
    lifespan=lifespan,
    # Hide docs behind auth in production — set SHOW_DOCS=false in .env
    docs_url="/docs" if os.environ.get("SHOW_DOCS", "true").lower() != "false" else None,
    redoc_url=None,
)

# ── CORS — restrict to same-origin by default ────────────
# Allow additional origins via CORS_ORIGINS="https://your.domain,https://other.domain"
_cors_origins_env = os.environ.get("CORS_ORIGINS", "")
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
        "version":        "1.1.0",
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
            secure=os.environ.get("HTTPS", "false").lower() == "true",
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
