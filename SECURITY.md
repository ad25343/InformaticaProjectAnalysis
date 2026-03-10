# Security Policy

## Reporting Vulnerabilities

If you discover a security vulnerability, please report it via
[GitHub Issues](https://github.com/ad25343/InformaticaProjectAnalysis/issues).

## Security Architecture

Security is infrastructure, not a feature layer.

### Input Validation

| Threat | Defence |
|---|---|
| XML External Entity (XXE) | All XML parsing uses `safe_parse_xml()` with DTD loading and entity resolution disabled |
| Path Traversal | Folder scanning resolves all paths relative to the configured root; symlinks are rejected |
| Zip Slip | ZIP extraction validates every entry path before write |
| Zip Bomb | ZIP extraction caps total bytes and entry count |
| Oversized uploads | File size validated before processing |
| Malformed project config | YAML parsed with `yaml.safe_load()`; schema validated before any processing |

### Infrastructure

| Threat | Defence |
|---|---|
| Hardcoded secrets | Startup warning if `SECRET_KEY` is default; all credentials via environment variables |
| Unauthenticated access | Session-cookie middleware on all non-static routes |
| CORS misconfiguration | No CORS headers by default (same-origin only); opt-in via `CORS_ORIGINS` |
| Dependency CVEs | `pip-audit` in CI; dependencies pinned in `requirements.txt` |

### Generated Output

| Threat | Defence |
|---|---|
| Strategy JSON injection | All string values in strategy output are sanitized before serialization |
| Path traversal in exports | Export paths validated; no user-controlled path components |
| Sensitive data in logs | XML content and credentials are never logged; structured logging only |

### API Security

| Header | Value |
|---|---|
| `X-Content-Type-Options` | `nosniff` |
| `X-Frame-Options` | `DENY` |
| `X-XSS-Protection` | `1; mode=block` |
| `Referrer-Policy` | `strict-origin-when-cross-origin` |
| `Content-Security-Policy` | `default-src 'self'` |
| `Strict-Transport-Security` | Added when `HTTPS=true` |

## Dependencies

All dependencies are pinned to exact versions. `pip-audit` runs in CI on every push
to detect known vulnerabilities.
