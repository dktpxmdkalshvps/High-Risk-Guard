"""Root conftest: always loaded first by pytest regardless of which
subdirectory's tests run or in what order, so this is the one place that
can reliably set env vars *before* backend.app (and its module-level
AuditLog singleton) gets imported by any test file in either
backend/tests/ or mcp_server/tests/.

Without this, importing backend.app for the first time would create the
real audit_log.sqlite3 that ships in the repo as a side effect of module
import, and REQ-006's 10s default gesture timeout would make Fail-Closed
tests slow.
"""
import os
import tempfile

_TEST_AUDIT_DB = os.path.join(tempfile.mkdtemp(prefix="high-risk-guard-test-audit-"), "test_audit.sqlite3")
os.environ.setdefault("HIGH_RISK_GUARD_AUDIT_DB_PATH", _TEST_AUDIT_DB)
os.environ.setdefault("HIGH_RISK_GUARD_GESTURE_TIMEOUT_SECONDS", "0.3")
os.environ.setdefault("HIGH_RISK_GUARD_MCP_POLL_INTERVAL_SECONDS", "0.05")
