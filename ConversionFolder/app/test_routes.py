"""
API route tests using FastAPI TestClient.

No live server needed — all tests run in-process.
No ANTHROPIC_API_KEY needed — pipeline is not triggered (jobs are created
but the orchestrator background task is not awaited).

Run:  python3 test_routes.py [-v]
"""
import asyncio
import io
import os
import sys
import unittest
from pathlib import Path

# ── env before backend imports ─────────────────────────────────────────────────
os.environ["SECRET_KEY"]   = "test-route-secret-key-32-chars!!"
os.environ["APP_PASSWORD"] = "route-test-password"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///test_routes_tmp.db"

sys.path.insert(0, str(Path(__file__).parent))

from fastapi.testclient import TestClient

# Build the app (mirrors main.py but without the lifespan startup overhead)
from backend.config  import settings
from backend.routes  import router
from backend.auth    import (
    create_session_token, check_password,
    COOKIE_NAME, is_authenticated,
)
from backend.limiter import jobs_limiter, login_limiter, RateLimiter

import fastapi
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# ── Minimal test app (no lifespan so DB init doesn't block) ───────────────────
_test_app = FastAPI(title="ICT Test")
_test_app.include_router(router)


@_test_app.middleware("http")
async def _auth_middleware(request: fastapi.Request, call_next):
    """Mirrors the auth guard in main.py for protected-route tests."""
    public = {"/api/health", "/login", "/favicon.ico"}
    if request.url.path in public or request.url.path.startswith("/static"):
        return await call_next(request)
    if not is_authenticated(request):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    return await call_next(request)


# ── Initialise the test DB once ────────────────────────────────────────────────
async def _init():
    from backend.db.database import init_db
    await init_db()


asyncio.get_event_loop().run_until_complete(_init())

# ── Shared sample XML ──────────────────────────────────────────────────────────
SAMPLE_XML = (
    Path(__file__).parent / "sample_xml" / "sample_mapping.xml"
).read_bytes()

MINIMAL_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<POWERMART CREATION_DATE="01/01/2024" REPOSITORY_NAME="TEST">
  <REPOSITORY NAME="TEST">
    <FOLDER NAME="TEST">
      <MAPPING NAME="m_TEST_MAPPING" ISVALID="YES">
        <TRANSFORMATION NAME="SQ_SOURCE" TYPE="Source Qualifier"/>
      </MAPPING>
    </FOLDER>
  </REPOSITORY>
</POWERMART>
"""


def _auth_cookie() -> dict:
    """Return a cookie dict for an authenticated session."""
    return {COOKIE_NAME: create_session_token()}


# ══════════════════════════════════════════════════════════════════════════════
# 1. Health check
# ══════════════════════════════════════════════════════════════════════════════

class TestHealthCheck(unittest.TestCase):

    def setUp(self):
        self.client = TestClient(_test_app, raise_server_exceptions=False)

    def test_health_returns_200(self):
        r = self.client.get("/api/health")
        self.assertEqual(r.status_code, 200)

    def test_health_body_structure(self):
        r = self.client.get("/api/health")
        body = r.json()
        self.assertIn("status",          body)
        self.assertIn("version",         body)
        self.assertIn("uptime_seconds",  body)

    def test_health_version_is_string(self):
        r = self.client.get("/api/health")
        self.assertIsInstance(r.json()["version"], str)
        self.assertTrue(r.json()["version"].startswith("2."))

    def test_health_no_auth_needed(self):
        """Health check must be publicly accessible (used by load balancers)."""
        r = self.client.get("/api/health")
        self.assertNotEqual(r.status_code, 401)


# ══════════════════════════════════════════════════════════════════════════════
# 2. Auth — protected routes
# ══════════════════════════════════════════════════════════════════════════════

class TestAuthMiddleware(unittest.TestCase):

    def setUp(self):
        self.client = TestClient(_test_app, raise_server_exceptions=False)

    def test_protected_route_without_cookie_returns_401(self):
        r = self.client.get("/api/jobs")
        self.assertEqual(r.status_code, 401)

    def test_protected_route_with_valid_cookie_passes_auth(self):
        r = self.client.get("/api/jobs", cookies=_auth_cookie())
        # 200 or 404 are both fine — 401 is not
        self.assertNotEqual(r.status_code, 401)

    def test_tampered_cookie_returns_401(self):
        bad_cookie = {COOKIE_NAME: "tampered.token.value"}
        r = self.client.get("/api/jobs", cookies=bad_cookie)
        self.assertEqual(r.status_code, 401)

    def test_health_accessible_without_auth(self):
        r = self.client.get("/api/health")
        self.assertNotEqual(r.status_code, 401)


# ══════════════════════════════════════════════════════════════════════════════
# 3. Job creation — validation
# ══════════════════════════════════════════════════════════════════════════════

class TestJobCreation(unittest.TestCase):

    def setUp(self):
        self.client  = TestClient(_test_app, raise_server_exceptions=False)
        self.cookies = _auth_cookie()

    def test_create_job_with_valid_xml_returns_200(self):
        r = self.client.post(
            "/api/jobs",
            files={"file": ("test.xml", io.BytesIO(SAMPLE_XML), "text/xml")},
            cookies=self.cookies,
        )
        self.assertIn(r.status_code, (200, 202))

    def test_create_job_returns_job_id(self):
        r = self.client.post(
            "/api/jobs",
            files={"file": ("test.xml", io.BytesIO(SAMPLE_XML), "text/xml")},
            cookies=self.cookies,
        )
        body = r.json()
        self.assertIn("job_id", body)
        self.assertRegex(
            body["job_id"],
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
        )

    def test_create_job_with_non_xml_extension_returns_400(self):
        r = self.client.post(
            "/api/jobs",
            files={"file": ("mapping.csv", io.BytesIO(b"a,b,c"), "text/plain")},
            cookies=self.cookies,
        )
        self.assertEqual(r.status_code, 400)

    def test_create_job_with_empty_file_returns_400(self):
        r = self.client.post(
            "/api/jobs",
            files={"file": ("empty.xml", io.BytesIO(b""), "text/xml")},
            cookies=self.cookies,
        )
        self.assertEqual(r.status_code, 400)

    def test_create_job_with_non_xml_content_returns_400(self):
        r = self.client.post(
            "/api/jobs",
            files={"file": ("fake.xml", io.BytesIO(b"not xml content"), "text/xml")},
            cookies=self.cookies,
        )
        self.assertEqual(r.status_code, 400)

    def test_create_job_without_auth_returns_401(self):
        r = self.client.post(
            "/api/jobs",
            files={"file": ("test.xml", io.BytesIO(MINIMAL_XML), "text/xml")},
        )
        self.assertEqual(r.status_code, 401)

    def test_create_job_with_minimal_valid_xml(self):
        r = self.client.post(
            "/api/jobs",
            files={"file": ("minimal.xml", io.BytesIO(MINIMAL_XML), "text/xml")},
            cookies=self.cookies,
        )
        self.assertIn(r.status_code, (200, 202))


# ══════════════════════════════════════════════════════════════════════════════
# 4. Job ID validation
# ══════════════════════════════════════════════════════════════════════════════

class TestJobIdValidation(unittest.TestCase):

    def setUp(self):
        self.client  = TestClient(_test_app, raise_server_exceptions=False)
        self.cookies = _auth_cookie()

    def test_valid_uuid_accepted(self):
        """A properly formatted UUID should pass ID validation (may 404 if job not found)."""
        r = self.client.get(
            "/api/jobs/00000000-0000-0000-0000-000000000000",
            cookies=self.cookies,
        )
        # 404 (job not found) is OK; 400 (bad format) is not
        self.assertNotEqual(r.status_code, 400)

    def test_path_traversal_job_id_rejected(self):
        r = self.client.get(
            "/api/jobs/../../../etc/passwd",
            cookies=self.cookies,
        )
        self.assertIn(r.status_code, (400, 404, 422))

    def test_sql_injection_job_id_rejected(self):
        r = self.client.get(
            "/api/jobs/1; DROP TABLE jobs; --",
            cookies=self.cookies,
        )
        self.assertIn(r.status_code, (400, 404, 422))

    def test_short_garbage_job_id_rejected(self):
        r = self.client.get(
            "/api/jobs/notauuid",
            cookies=self.cookies,
        )
        self.assertIn(r.status_code, (400, 422))


# ══════════════════════════════════════════════════════════════════════════════
# 5. Content-type enforcement
# ══════════════════════════════════════════════════════════════════════════════

class TestContentTypeEnforcement(unittest.TestCase):

    def setUp(self):
        self.client  = TestClient(_test_app, raise_server_exceptions=False)
        self.cookies = _auth_cookie()

    def test_pdf_content_type_rejected(self):
        r = self.client.post(
            "/api/jobs",
            files={"file": ("mapping.xml", io.BytesIO(b"%PDF-1.4"), "application/pdf")},
            cookies=self.cookies,
        )
        self.assertEqual(r.status_code, 415)

    def test_octet_stream_accepted(self):
        """Clients sending application/octet-stream for XML should be allowed."""
        r = self.client.post(
            "/api/jobs",
            files={"file": ("test.xml", io.BytesIO(MINIMAL_XML), "application/octet-stream")},
            cookies=self.cookies,
        )
        self.assertIn(r.status_code, (200, 202))


# ══════════════════════════════════════════════════════════════════════════════
# 6. Rate limiter — unit-level (not via HTTP to avoid flakiness)
# ══════════════════════════════════════════════════════════════════════════════

class TestRateLimiterIntegration(unittest.TestCase):
    """
    Tests the RateLimiter class in isolation rather than through HTTP
    to avoid test-order flakiness from the shared singleton instances.
    """

    def _make_req(self, ip="10.0.0.99"):
        from unittest.mock import MagicMock
        req = MagicMock()
        req.client.host = ip
        return req

    def test_limiter_allows_burst_then_blocks(self):
        from fastapi import HTTPException
        limiter = RateLimiter("3/minute")
        req = self._make_req()
        for _ in range(3):
            asyncio.get_event_loop().run_until_complete(limiter(req))
        with self.assertRaises(HTTPException) as ctx:
            asyncio.get_event_loop().run_until_complete(limiter(req))
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertIn("Rate limit exceeded", ctx.exception.detail)

    def test_login_limiter_is_stricter_than_jobs_limiter(self):
        """login_limiter (5/min) should have a lower max than jobs_limiter (20/min)."""
        self.assertLess(login_limiter.max_calls, jobs_limiter.max_calls)

    def test_limiter_detail_message_contains_limit_info(self):
        from fastapi import HTTPException
        limiter = RateLimiter("1/minute")
        req = self._make_req(ip="192.168.1.200")
        asyncio.get_event_loop().run_until_complete(limiter(req))
        with self.assertRaises(HTTPException) as ctx:
            asyncio.get_event_loop().run_until_complete(limiter(req))
        self.assertIn("1", ctx.exception.detail)


# ══════════════════════════════════════════════════════════════════════════════
# Runner
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main(verbosity=2)
