# Security Policy

## Reporting a Vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

If you discover a security issue in this project, report it privately:

1. Open a [GitHub Security Advisory](https://github.com/ad25343/InformaticaConversion/security/advisories/new) on this repository.
2. Alternatively, contact the maintainer directly via GitHub: [@ad25343](https://github.com/ad25343).

Include as much detail as you can:
- A description of the vulnerability and its potential impact
- Steps to reproduce or a proof-of-concept (without active exploitation)
- Any relevant log output or error messages
- The version or commit where you observed the issue

You will receive an acknowledgement within **72 hours** and a resolution update within **14 days** for confirmed issues.

---

## Scope

The following are **in scope** for this policy:

| Area | Examples |
|------|---------|
| Input handling | XXE injection, Zip Slip, path traversal via uploaded files |
| Authentication | Brute force, session fixation, auth bypass |
| Dependency CVEs | Vulnerabilities in pinned packages in `requirements.txt` |
| Secrets exposure | Hardcoded credentials in source code or generated output |
| Rate limiting | Endpoints that could be abused to incur API costs |
| Generated code | Security issues Claude introduces into PySpark/dbt/Python output |
| Security gate bypass | Logic flaws that allow the security review gate (Step 9) to be skipped |

The following are **out of scope**:

- Findings already covered by automated scanning (CVEs flagged in CI `pip-audit` runs)
- Social engineering of maintainers
- Issues requiring physical access to the host machine
- Denial of service via the Claude API itself (report to [Anthropic](https://www.anthropic.com/security))

---

## Current Security Architecture

Key protections implemented in this version:

| Threat | Mitigation |
|--------|-----------|
| XXE injection | `safe_xml_parser()` — DTD loading and entity resolution disabled on every lxml parse |
| Zip Slip | `safe_zip_extract()` — entry paths normalised with `posixpath.normpath` and checked against virtual root |
| Zip Bomb | `safe_zip_extract()` — total extracted bytes and entry count capped by env-configurable limits |
| Upload abuse | `validate_upload_size()` — HTTP 413 on every upload stream; configurable via `MAX_UPLOAD_MB` |
| Credentials in XML | `scan_xml_for_secrets()` — scans uploaded Informatica XML for plaintext passwords before processing |
| Rate limiting | `slowapi` — `POST /api/jobs`, `POST /api/jobs/zip`, `POST /login` rate-limited per IP |
| Insecure generated code | Step 8 security scan — bandit (Python), YAML regex scan, Claude review (all stacks) |
| Security gate | Step 9 human review — reviewer must explicitly APPROVE, ACKNOWLEDGE, REQUEST_FIX, or FAIL before pipeline continues; auto-proceeds only when scan is fully clean (APPROVED recommendation) |
| Secrets in generated tests | Step 11 test files re-scanned; findings merged into Step 8 security report before Gate 3 |
| Recurring bad patterns in generated code | Security KB (`security_rules.yaml` + `security_patterns.json`) — 17 standing rules and auto-learned patterns from prior Gate 2 findings injected into every Step 7 conversion prompt (v2.2) |
| Default credentials | Startup warnings logged if `SECRET_KEY` or `APP_PASSWORD` are not set |
| Session security | `httponly` + `samesite=lax` cookies; `secure` flag enabled when `HTTPS=true` |

### Security Review Decisions (Step 9)

When the automated security scan (Step 8) finds issues, the pipeline pauses at Step 9
and waits for a human decision. Four outcomes are possible:

| Decision | Meaning | Pipeline Effect |
|----------|---------|----------------|
| APPROVED | Scan was clean, or reviewer confirms no action needed | Proceeds to Step 10 |
| ACKNOWLEDGED | Issues noted; risk accepted with documented rationale | Proceeds to Step 10 with finding on record |
| REQUEST_FIX | Send findings back to Step 7 for Claude to address, then re-scan | Re-runs Steps 7→8→Gate 2; max 2 rounds; auto-proceeds if re-scan is clean |
| FAILED | Findings are unacceptable for this mapping | Job blocked permanently |

The reviewer, their role, their decision, and any notes are stored in the job record
(`security_sign_off`) and included in the downloadable Markdown and PDF reports.

After every APPROVED or ACKNOWLEDGED decision, the scan findings are automatically
recorded in the Security Knowledge Base (`security_patterns.json`). The patterns grow
in weight with each recurrence and are injected into all future Step 7 conversion prompts,
so issues seen on one job are proactively avoided on the next.

---

## Disclosure Timeline

| Day | Action |
|-----|--------|
| 0 | Report received |
| 1–3 | Acknowledgement sent to reporter |
| 1–7 | Issue assessed and severity assigned |
| 7–14 | Fix developed and reviewed |
| 14 | Fix released; reporter notified |
| 14+ | Public disclosure (coordinated with reporter) |

For CRITICAL findings we aim for a fix within **7 days**.

---

## Supported Versions

Only the latest release on the `main` branch receives security fixes. Older commits are not patched.
