"""Unit tests for REQ-005 (backend.audit_log): hash chaining and
tamper detection. Each test uses its own temp SQLite file (pytest's
tmp_path) so tests don't share state or leave files behind.
"""
import sqlite3

from backend.audit_log import GENESIS_HASH, AuditEntryInput, AuditLog


def _entry(request_id="req-1", status="approved", reason=None):
    return AuditEntryInput(
        request_id=request_id,
        file_path="src/wallet/transfer.py",
        status=status,
        result_reason=reason,
        approver_id="approver-demo",
        mock_biometric_verified_at="2026-07-10T00:00:00+00:00",
        sensor_samples=[{"t_ms": 0, "gamma": 1.0}, {"t_ms": 50, "gamma": 2.0}],
        target_angle=18.5,
    )


def test_first_entry_chains_to_genesis(tmp_path):
    log = AuditLog(tmp_path / "audit.sqlite3")
    row = log.record(_entry())
    assert row["prev_hash"] == GENESIS_HASH
    assert len(row["entry_hash"]) == 64  # sha256 hex digest length
    ok, broken_id = log.verify_chain()
    assert ok is True
    assert broken_id is None


def test_multiple_entries_link_in_order(tmp_path):
    log = AuditLog(tmp_path / "audit.sqlite3")
    first = log.record(_entry(request_id="req-1"))
    second = log.record(_entry(request_id="req-2", status="rejected", reason="timeout_fail_closed"))
    third = log.record(_entry(request_id="req-3"))

    assert second["prev_hash"] == first["entry_hash"]
    assert third["prev_hash"] == second["entry_hash"]

    ok, broken_id = log.verify_chain()
    assert ok is True
    assert broken_id is None


def test_tampering_with_a_row_breaks_the_chain(tmp_path):
    db_path = tmp_path / "audit.sqlite3"
    log = AuditLog(db_path)
    log.record(_entry(request_id="req-1"))
    log.record(_entry(request_id="req-2", status="rejected", reason="hold_out_of_range"))
    log.record(_entry(request_id="req-3"))

    tampered_id = next(e["id"] for e in log.all_entries() if e["request_id"] == "req-2")

    # Tamper directly via raw SQL (bypassing the AuditLog API entirely —
    # exactly the "casual tampering" scenario hash chaining is meant to
    # catch, per the module's documented limitation). Actually change the
    # value (rejected -> approved), not overwrite it with the same value.
    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE audit_log SET status = 'approved' WHERE request_id = 'req-2'")
    conn.commit()
    conn.close()

    ok, broken_id = log.verify_chain()
    assert ok is False
    assert broken_id == tampered_id


def test_no_delete_api_exists():
    """REQ-005: no application-level deletion of audit rows."""
    public_methods = [name for name in dir(AuditLog) if not name.startswith("_")]
    forbidden = {"delete", "remove", "clear", "truncate", "drop"}
    assert not (forbidden & set(m.lower() for m in public_methods)), public_methods
