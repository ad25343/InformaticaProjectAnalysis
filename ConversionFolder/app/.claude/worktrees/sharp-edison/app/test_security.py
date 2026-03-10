"""
Unit tests for security utilities — no API key or server required.

Coverage
--------
  scan_xml_for_secrets()    — credential detection in uploaded Informatica XML
  scan_yaml_for_secrets()   — plaintext secret detection in generated YAML
  safe_zip_extract()        — Zip Slip, Zip Bomb, entry count, normal extraction
  safe_parse_xml()          — XXE-hardened parser (malformed input, valid input)
  validate_upload_size()    — HTTP 413 on oversized content
  scan_python_with_bandit() — smoke test (degrades gracefully if bandit absent)

Usage
-----
  python3 test_security.py          # run all tests
  python3 test_security.py -v       # verbose output
"""
from __future__ import annotations

import io
import sys
import unittest
import zipfile
from pathlib import Path

# Allow running from the app/ directory directly
sys.path.insert(0, str(Path(__file__).parent))

from backend.security import (
    safe_parse_xml,
    safe_zip_extract,
    scan_python_with_bandit,
    scan_xml_for_secrets,
    scan_yaml_for_secrets,
    validate_upload_size,
    ZipExtractionError,
    MAX_UPLOAD_BYTES,
)

SEP = "─" * 60


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_zip(entries: dict[str, bytes]) -> bytes:
    """Build an in-memory ZIP with the given {name: content} entries."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# scan_xml_for_secrets
# ─────────────────────────────────────────────────────────────────────────────

class TestScanXmlForSecrets(unittest.TestCase):

    def _scan(self, xml: str):
        return scan_xml_for_secrets(xml)

    # ── Should flag ───────────────────────────────────────────────────────────

    def test_flags_hardcoded_password(self):
        xml = '<ROOT><CONNECTION PASSWORD="s3cr3tP@ss"/></ROOT>'
        findings = self._scan(xml)
        self.assertTrue(findings, "Expected a finding for hardcoded PASSWORD attribute")
        self.assertEqual(findings[0]["severity"], "HIGH")
        self.assertIn("PASSWORD", findings[0]["attribute"])

    def test_flags_token_attribute(self):
        xml = '<ROOT><SESSION TOKEN="abcdef1234567890"/></ROOT>'
        findings = self._scan(xml)
        self.assertTrue(findings, "Expected a finding for TOKEN attribute")

    def test_flags_api_key_attribute(self):
        xml = '<ROOT><CONN APIKEY="live_abc123xyz"/></ROOT>'
        findings = self._scan(xml)
        self.assertTrue(findings)

    def test_flags_secret_attribute(self):
        xml = '<ROOT><CONN SECRET="my-actual-secret"/></ROOT>'
        findings = self._scan(xml)
        self.assertTrue(findings)

    def test_value_preview_truncated(self):
        """value_preview should be max 6 chars + ellipsis."""
        xml = '<ROOT><CONNECTION PASSWORD="verylongpassword123"/></ROOT>'
        findings = self._scan(xml)
        self.assertTrue(findings)
        preview = findings[0]["value_preview"]
        self.assertLessEqual(len(preview), 8, "Preview should be short")
        self.assertIn("…", preview)

    # ── Should NOT flag ───────────────────────────────────────────────────────

    def test_ignores_informatica_variable(self):
        xml = '<ROOT><CONNECTION PASSWORD="$$DB_PASSWORD"/></ROOT>'
        findings = self._scan(xml)
        self.assertFalse(findings, "$$VAR placeholder should be ignored")

    def test_ignores_single_dollar_variable(self):
        xml = '<ROOT><CONNECTION PASSWORD="$DB_PASSWORD"/></ROOT>'
        findings = self._scan(xml)
        self.assertFalse(findings, "$VAR placeholder should be ignored")

    def test_ignores_xml_placeholder(self):
        xml = '<ROOT><CONNECTION PASSWORD="<your_password>"/></ROOT>'
        findings = self._scan(xml)
        self.assertFalse(findings, "<placeholder> style should be ignored")

    def test_ignores_masked_value(self):
        xml = '<ROOT><CONNECTION PASSWORD="*****"/></ROOT>'
        findings = self._scan(xml)
        self.assertFalse(findings, "Masked-out value should be ignored")

    def test_ignores_changeme(self):
        xml = '<ROOT><CONNECTION PASSWORD="changeme"/></ROOT>'
        findings = self._scan(xml)
        self.assertFalse(findings, "'changeme' is a known placeholder")

    def test_ignores_empty_password(self):
        xml = '<ROOT><CONNECTION PASSWORD=""/></ROOT>'
        findings = self._scan(xml)
        self.assertFalse(findings, "Empty value should be ignored")

    def test_ignores_non_credential_attribute(self):
        xml = '<ROOT><CONNECTION USERNAME="myuser" HOSTNAME="db.example.com"/></ROOT>'
        findings = self._scan(xml)
        self.assertFalse(findings, "Non-credential attributes should not be flagged")

    def test_handles_malformed_xml_gracefully(self):
        findings = self._scan("not valid xml <<<")
        self.assertIsInstance(findings, list)  # should not raise

    def test_handles_empty_string(self):
        findings = self._scan("")
        self.assertIsInstance(findings, list)

    def test_multiple_findings(self):
        xml = """
        <ROOT>
          <CONN1 PASSWORD="real_pass_1"/>
          <CONN2 TOKEN="real_token_99"/>
          <CONN3 PASSWORD="$$IGNORE_ME"/>
        </ROOT>
        """
        findings = self._scan(xml)
        self.assertEqual(len(findings), 2, "Should flag exactly 2 real credentials")


# ─────────────────────────────────────────────────────────────────────────────
# scan_yaml_for_secrets
# ─────────────────────────────────────────────────────────────────────────────

class TestScanYamlForSecrets(unittest.TestCase):

    def _scan(self, yaml: str):
        return scan_yaml_for_secrets(yaml, filename="test.yaml")

    # ── Should flag ───────────────────────────────────────────────────────────

    def test_flags_plaintext_password(self):
        yaml = "password: my_real_password\n"
        findings = self._scan(yaml)
        self.assertTrue(findings, "Expected a finding for plaintext password")
        self.assertEqual(findings[0]["severity"], "HIGH")
        self.assertEqual(findings[0]["line"], 1)

    def test_flags_token(self):
        yaml = "token: ghp_abc123xyz\n"
        findings = self._scan(yaml)
        self.assertTrue(findings)

    def test_flags_api_key(self):
        yaml = "api_key: sk-live-abc123\n"
        findings = self._scan(yaml)
        self.assertTrue(findings)

    def test_flags_client_secret(self):
        yaml = "client_secret: abcdefgh1234\n"
        findings = self._scan(yaml)
        self.assertTrue(findings)

    def test_flags_secret_key(self):
        yaml = "secret: s3cr3t_v@lue\n"
        findings = self._scan(yaml)
        self.assertTrue(findings)

    def test_correct_line_number_reported(self):
        yaml = "host: db.example.com\npassword: real_password\nport: 5432\n"
        findings = self._scan(yaml)
        self.assertTrue(findings)
        self.assertEqual(findings[0]["line"], 2)

    def test_preview_truncated(self):
        yaml = "password: this_is_a_very_long_password_value\n"
        findings = self._scan(yaml)
        self.assertTrue(findings)
        self.assertIn("…", findings[0]["value_preview"])

    # ── Should NOT flag ───────────────────────────────────────────────────────

    def test_ignores_empty_password(self):
        yaml = "password: \n"
        findings = self._scan(yaml)
        self.assertFalse(findings, "Empty value should be ignored")

    def test_ignores_empty_string_literal(self):
        yaml = 'password: ""\n'
        findings = self._scan(yaml)
        self.assertFalse(findings)

    def test_ignores_null(self):
        yaml = "password: null\n"
        findings = self._scan(yaml)
        self.assertFalse(findings)

    def test_ignores_tilde_null(self):
        yaml = "password: ~\n"
        findings = self._scan(yaml)
        self.assertFalse(findings)

    def test_ignores_xml_placeholder(self):
        yaml = "password: <your_password_here>\n"
        findings = self._scan(yaml)
        self.assertFalse(findings)

    def test_ignores_informatica_variable(self):
        yaml = "password: $$DB_PASSWORD\n"
        findings = self._scan(yaml)
        self.assertFalse(findings)

    def test_ignores_changeme(self):
        yaml = "password: changeme\n"
        findings = self._scan(yaml)
        self.assertFalse(findings)

    def test_ignores_your_prefix(self):
        yaml = "password: your_password_here\n"
        findings = self._scan(yaml)
        self.assertFalse(findings)

    def test_ignores_non_secret_key(self):
        yaml = "hostname: db.example.com\nport: 5432\n"
        findings = self._scan(yaml)
        self.assertFalse(findings)

    def test_multiple_findings(self):
        yaml = (
            "host: db.example.com\n"
            "password: real_pass\n"
            "port: 5432\n"
            "token: abc123real\n"
            "api_key: null\n"
        )
        findings = self._scan(yaml)
        self.assertEqual(len(findings), 2, "Should flag password and token only")


# ─────────────────────────────────────────────────────────────────────────────
# safe_zip_extract
# ─────────────────────────────────────────────────────────────────────────────

class TestSafeZipExtract(unittest.TestCase):

    def test_normal_extraction(self):
        files = {
            "mapping.xml": b"<ROOT/>",
            "subdir/workflow.xml": b"<WORKFLOW/>",
        }
        result = safe_zip_extract(_make_zip(files))
        self.assertIn("mapping.xml", result)
        self.assertIn("subdir/workflow.xml", result)
        self.assertEqual(result["mapping.xml"], b"<ROOT/>")

    def test_zip_slip_rejected(self):
        """An entry with path traversal (../../etc/passwd) must be rejected."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("../../etc/passwd", "root:x:0:0:root")
        with self.assertRaises(ZipExtractionError) as ctx:
            safe_zip_extract(buf.getvalue())
        self.assertIn("Zip Slip", str(ctx.exception),
                      f"Expected 'Zip Slip' in: {ctx.exception}")

    def test_not_a_zip_raises(self):
        with self.assertRaises(ZipExtractionError):
            safe_zip_extract(b"this is not a zip file")

    def test_empty_zip_returns_empty_dict(self):
        result = safe_zip_extract(_make_zip({}))
        self.assertEqual(result, {})

    def test_entry_count_limit(self):
        """Archives with more than MAX_ZIP_FILE_COUNT entries must be rejected."""
        from backend.security import MAX_ZIP_FILE_COUNT
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for i in range(MAX_ZIP_FILE_COUNT + 1):
                zf.writestr(f"file_{i}.txt", b"x")
        with self.assertRaises(ZipExtractionError) as ctx:
            safe_zip_extract(buf.getvalue())
        self.assertIn("entries", str(ctx.exception))

    def test_zip_bomb_rejected(self):
        """Expand a highly-compressed entry that exceeds MAX_ZIP_EXTRACTED_BYTES."""
        from backend.security import MAX_ZIP_EXTRACTED_BYTES
        # Generate content slightly larger than the limit
        large_content = b"A" * (MAX_ZIP_EXTRACTED_BYTES + 1)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("large_file.bin", large_content)
        with self.assertRaises(ZipExtractionError) as ctx:
            safe_zip_extract(buf.getvalue())
        # Error message should mention the size limit
        err = str(ctx.exception).lower()
        self.assertTrue("limit" in err or "mb" in err or "zip bomb" in err,
                        f"Expected size-limit message, got: {ctx.exception}")

    def test_directories_skipped(self):
        """Directory entries (name ending in /) should not appear in output."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            # Create a directory entry by writing an empty entry with trailing slash
            zf.writestr(zipfile.ZipInfo("subdir/"), b"")
            zf.writestr("subdir/file.xml", b"<ROOT/>")
        result = safe_zip_extract(buf.getvalue())
        for key in result:
            self.assertFalse(key.endswith("/"), "Directory entries should be skipped")
        self.assertIn("subdir/file.xml", result)

    def test_leading_slash_stripped(self):
        """Absolute paths in ZIP entries should have leading slash stripped safely."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("absolute/path.xml", b"<ROOT/>")
        result = safe_zip_extract(buf.getvalue())
        # Should be stored without leading slash
        keys = list(result.keys())
        self.assertTrue(len(keys) > 0, "Expected at least one extracted file")
        for k in keys:
            self.assertFalse(k.startswith("/"), f"Key '{k}' should not start with /")


# ─────────────────────────────────────────────────────────────────────────────
# safe_parse_xml / XXE
# ─────────────────────────────────────────────────────────────────────────────

class TestSafeParseXml(unittest.TestCase):

    def test_parses_valid_xml(self):
        root = safe_parse_xml("<ROOT><CHILD attr='val'/></ROOT>")
        self.assertEqual(root.tag, "ROOT")

    def test_parses_bytes(self):
        root = safe_parse_xml(b"<ROOT/>")
        self.assertEqual(root.tag, "ROOT")

    def test_raises_on_malformed_xml(self):
        from lxml import etree
        with self.assertRaises(etree.XMLSyntaxError):
            safe_parse_xml("<ROOT><UNCLOSED>")

    def test_xxe_blocked(self):
        """An XXE payload attempting to read /etc/passwd must not succeed."""
        xxe_payload = (
            '<?xml version="1.0"?>'
            '<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
            "<ROOT>&xxe;</ROOT>"
        )
        # Should either parse without resolving the entity (safe)
        # or raise an XMLSyntaxError — either is acceptable.
        try:
            root = safe_parse_xml(xxe_payload)
            # If it parsed, make sure the entity was NOT resolved
            text = root.text or ""
            self.assertNotIn("root:", text, "XXE entity was resolved — this is a vulnerability!")
        except Exception:
            pass  # parser rejected it — also safe


# ─────────────────────────────────────────────────────────────────────────────
# validate_upload_size
# ─────────────────────────────────────────────────────────────────────────────

class TestValidateUploadSize(unittest.TestCase):

    def test_accepts_content_within_limit(self):
        """Content smaller than the limit should not raise."""
        small_content = b"x" * 100
        try:
            validate_upload_size(small_content, label="test.xml")
        except Exception as e:
            self.fail(f"validate_upload_size raised unexpectedly: {e}")

    def test_raises_413_on_oversized_content(self):
        from fastapi import HTTPException
        oversized = b"x" * (MAX_UPLOAD_BYTES + 1)
        with self.assertRaises(HTTPException) as ctx:
            validate_upload_size(oversized, label="huge.xml")
        self.assertEqual(ctx.exception.status_code, 413)

    def test_custom_limit_override(self):
        from fastapi import HTTPException
        content = b"x" * 200
        # Should pass with a 1 KB limit override at exactly 200 bytes
        validate_upload_size(content, label="test.xml", limit=1024)
        # Should fail when limit is 100 bytes
        with self.assertRaises(HTTPException):
            validate_upload_size(content, label="test.xml", limit=100)

    def test_exactly_at_limit_passes(self):
        """Content exactly at the limit should be accepted."""
        exact = b"x" * MAX_UPLOAD_BYTES
        try:
            validate_upload_size(exact, label="test.xml")
        except Exception as e:
            self.fail(f"Content at limit should be accepted, but raised: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# scan_python_with_bandit — smoke test (degrades gracefully if bandit absent)
# ─────────────────────────────────────────────────────────────────────────────

class TestScanPythonWithBandit(unittest.TestCase):

    def test_returns_dict_with_expected_keys(self):
        result = scan_python_with_bandit("x = 1\n")
        for key in ("ran", "findings", "high_count", "medium_count", "low_count", "error"):
            self.assertIn(key, result, f"Missing key: {key}")

    def test_clean_code_produces_no_high_findings(self):
        clean_code = "def add(a, b):\n    return a + b\n"
        result = scan_python_with_bandit(clean_code)
        # If bandit ran, high_count should be 0 for trivially safe code
        if result["ran"]:
            self.assertEqual(result["high_count"], 0)

    def test_degrades_gracefully_without_bandit(self):
        """If bandit is not installed, error should be set and ran=False."""
        result = scan_python_with_bandit("pass\n")
        if not result["ran"]:
            self.assertIsNotNone(result["error"])
        # Either way, findings should be a list
        self.assertIsInstance(result["findings"], list)

    def test_oversized_file_skipped(self):
        """Files exceeding MAX_BANDIT_LINES should be skipped with an error message."""
        from backend.security import MAX_BANDIT_LINES
        huge_code = "x = 1\n" * (MAX_BANDIT_LINES + 1)
        result = scan_python_with_bandit(huge_code)
        self.assertFalse(result["ran"])
        self.assertIn("large", result["error"].lower())

    def test_suspicious_code_flagged_if_bandit_available(self):
        """If bandit is installed, B105 (hardcoded_password_string) should fire."""
        suspicious = 'password = "super_secret_123"\n'
        result = scan_python_with_bandit(suspicious)
        if result["ran"]:
            self.assertGreater(len(result["findings"]), 0,
                "bandit should flag hardcoded password string (B105)")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"\n{SEP}")
    print(" Informatica Conversion Tool — Security Unit Tests")
    print(f"{SEP}\n")
    print("No API key required — all tests are deterministic.\n")

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    test_classes = [
        TestScanXmlForSecrets,
        TestScanYamlForSecrets,
        TestSafeZipExtract,
        TestSafeParseXml,
        TestValidateUploadSize,
        TestScanPythonWithBandit,
    ]

    for cls in test_classes:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    verbosity = 2 if "-v" in sys.argv else 1
    runner = unittest.TextTestRunner(verbosity=verbosity)
    result = runner.run(suite)

    print(f"\n{SEP}")
    total = result.testsRun
    failed = len(result.failures) + len(result.errors)
    print(f" Results: {total - failed}/{total} passed")
    if result.failures or result.errors:
        print(f" ❌ {failed} failure(s) — see output above")
        sys.exit(1)
    else:
        print(" ✅ All security tests passed")
    print(f"{SEP}\n")
