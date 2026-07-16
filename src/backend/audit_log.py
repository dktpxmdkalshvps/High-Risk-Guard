"""REQ-005: hash-chained audit log, SQLite-backed.

Every terminal outcome (approved, rejected, or Fail-Closed abort) gets one
append-only row. Each row's `entry_hash` is a SHA-256 of its own fields
*plus* the previous row's `entry_hash` ("prev_hash"), so altering or
deleting a past row breaks the chain for every row after it — detectable
by re-walking the table and recomputing hashes (see `verify_chain`).

Deliberately NOT done here (see SRS 4장 "미해결 한계"):
  - No infrastructure-level WORM storage. This is an ordinary SQLite file;
    someone with filesystem/DB-admin access could still edit it directly
    and recompute a consistent-looking chain from that point forward.
    Hash chaining only protects against *casual* tampering (editing a row
    without touching everything after it), not a determined DB admin.
  - No application-level DELETE statement or endpoint exists anywhere in
    this codebase for this table — intentionally, per REQ-005.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "audit_log.sqlite3"
GENESIS_HASH = "0" * 64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    status TEXT NOT NULL,
    result_reason TEXT,
    approver_id TEXT NOT NULL,
    mock_biometric_verified_at TEXT,
    sensor_samples_hash TEXT NOT NULL,
    target_angle REAL,
    recorded_at TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    entry_hash TEXT NOT NULL
);
"""
# No DROP/DELETE statements exist in this module by design (REQ-005).


@dataclass
class AuditEntryInput:
    request_id: str
    file_path: str
    status: str  # "approved" | "rejected"
    result_reason: str | None
    approver_id: str
    mock_biometric_verified_at: str | None
    sensor_samples: list[dict]  # raw client-submitted timeseries; we store only its hash
    target_angle: float | None


def hash_samples(samples: list[dict]) -> str:
    """SHA-256 of the canonical JSON of the raw sensor timeseries.

    The SRS is explicit that only the hash is meant for long-term storage
    (센서 원본 데이터는 해시 처리 후 저장) — the raw samples themselves are
    not persisted by this module."""
    canonical = json.dumps(samples, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _entry_hash(prev_hash: str, row: dict) -> str:
    canonical = json.dumps(row, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((prev_hash + "|" + canonical).encode("utf-8")).hexdigest()


class AuditLog:
    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = str(db_path) if db_path is not None else str(DEFAULT_DB_PATH)
        conn = self._connect()
        try:
            conn.execute(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _last_hash(self, conn: sqlite3.Connection) -> str:
        row = conn.execute(
            "SELECT entry_hash FROM audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else GENESIS_HASH

    def record(self, entry: AuditEntryInput) -> dict:
        conn = self._connect()
        try:
            prev_hash = self._last_hash(conn)
            recorded_at = datetime.now(timezone.utc).isoformat()
            row = {
                "request_id": entry.request_id,
                "file_path": entry.file_path,
                "status": entry.status,
                "result_reason": entry.result_reason,
                "approver_id": entry.approver_id,
                "mock_biometric_verified_at": entry.mock_biometric_verified_at,
                "sensor_samples_hash": hash_samples(entry.sensor_samples),
                "target_angle": entry.target_angle,
                "recorded_at": recorded_at,
            }
            entry_hash = _entry_hash(prev_hash, row)
            conn.execute(
                """INSERT INTO audit_log
                   (request_id, file_path, status, result_reason, approver_id,
                    mock_biometric_verified_at, sensor_samples_hash, target_angle,
                    recorded_at, prev_hash, entry_hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row["request_id"], row["file_path"], row["status"], row["result_reason"],
                    row["approver_id"], row["mock_biometric_verified_at"],
                    row["sensor_samples_hash"], row["target_angle"], row["recorded_at"],
                    prev_hash, entry_hash,
                ),
            )
            conn.commit()
            row["prev_hash"] = prev_hash
            row["entry_hash"] = entry_hash
            return row
        finally:
            conn.close()

    def all_entries(self) -> list[dict]:
        conn = self._connect()
        try:
            cols = [
                "id", "request_id", "file_path", "status", "result_reason", "approver_id",
                "mock_biometric_verified_at", "sensor_samples_hash", "target_angle",
                "recorded_at", "prev_hash", "entry_hash",
            ]
            rows = conn.execute(f"SELECT {', '.join(cols)} FROM audit_log ORDER BY id ASC").fetchall()
            return [dict(zip(cols, r)) for r in rows]
        finally:
            conn.close()

    def verify_chain(self) -> tuple[bool, int | None]:
        """Recompute every row's hash from its stored fields and confirm
        prev_hash/entry_hash links are unbroken. Returns (ok, first_broken_id).
        This is the actual tamper-detection mechanism REQ-005 asks for —
        not just "we store a hash," but "we can prove after the fact
        whether the chain is intact."""
        entries = self.all_entries()
        expected_prev = GENESIS_HASH
        for e in entries:
            row = {
                "request_id": e["request_id"],
                "file_path": e["file_path"],
                "status": e["status"],
                "result_reason": e["result_reason"],
                "approver_id": e["approver_id"],
                "mock_biometric_verified_at": e["mock_biometric_verified_at"],
                "sensor_samples_hash": e["sensor_samples_hash"],
                "target_angle": e["target_angle"],
                "recorded_at": e["recorded_at"],
            }
            if e["prev_hash"] != expected_prev:
                return False, e["id"]
            recomputed = _entry_hash(expected_prev, row)
            if recomputed != e["entry_hash"]:
                return False, e["id"]
            expected_prev = e["entry_hash"]
        return True, None
