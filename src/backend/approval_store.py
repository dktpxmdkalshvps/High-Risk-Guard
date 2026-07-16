"""REQ-002/003 state machine + in-memory session store.

Single-process, in-memory only. This matches the SRS's stated scope (3장:
"동시 접속자 수... 단일 팀(승인자 1~2인) 기준") — no external DB, no
multi-worker deployment. Restarting the backend loses all pending requests,
which is acceptable for this MVP (Phase 3 adds persistent, hash-chained
*audit* logging on top of this — this store is just live session state).
"""
from __future__ import annotations

import os
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

# REQ-002: minimum forced-exposure time before the "next" step can be
# reached at all. Kept as a named constant, not a magic number, exactly so
# it's easy to retune. Overridable via env var for fast verification runs.
STEP1_MIN_SECONDS = float(os.environ.get("HIGH_RISK_GUARD_STEP1_MIN_SECONDS", "5.0"))

# REQ-003: random target angle window and how tightly it must be held.
TARGET_ANGLE_MIN_DEG = 15.0
TARGET_ANGLE_MAX_DEG = 25.0
ANGLE_TOLERANCE_DEG = 3.0
HOLD_DURATION_MS = 1000.0

# REQ-006 (Fail-Closed): once the gesture screen is reached, this is how
# long the approver has to complete a valid hold before the request is
# auto-aborted. Overridable via env var so verification scripts / tests
# don't have to block for the real default.
GESTURE_TIMEOUT_SECONDS = float(os.environ.get("HIGH_RISK_GUARD_GESTURE_TIMEOUT_SECONDS", "10.0"))


class ApprovalStatus(str, Enum):
    PENDING_ACK = "pending_ack"          # step 1: forced risk exposure
    AWAITING_GESTURE = "awaiting_gesture"  # step 2: physical gesture
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass
class RiskInfo:
    file_path: str
    diff_summary: str
    matched_keywords: list[str] = field(default_factory=list)
    matched_paths: list[str] = field(default_factory=list)


@dataclass
class ApprovalRequest:
    request_id: str
    risk: RiskInfo
    status: ApprovalStatus = ApprovalStatus.PENDING_ACK
    created_at: float = field(default_factory=time.monotonic)
    target_angle: float = 0.0  # signed degrees; sign encodes tilt direction
    target_direction: str = "right"  # "left" or "right", derived from target_angle's sign
    result_reason: str | None = None
    gesture_phase_started_at: float | None = None  # set on PENDING_ACK -> AWAITING_GESTURE
    # MOCK ONLY: no real biometric (FaceID/etc) integration exists in this
    # MVP. We stamp this the moment the approver is confirmed present and
    # engaged (successful step1_ack), as a stand-in for "a real biometric
    # check would have happened around here." See SRS 3장 "보안 수준의
    # 정직한 정의" — this is NOT a substitute for actual biometric proof.
    mock_biometric_verified_at: str | None = None

    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.created_at

    def gesture_phase_elapsed_seconds(self) -> float:
        if self.gesture_phase_started_at is None:
            return 0.0
        return time.monotonic() - self.gesture_phase_started_at


def _pick_target_angle() -> tuple[float, str]:
    magnitude = random.uniform(TARGET_ANGLE_MIN_DEG, TARGET_ANGLE_MAX_DEG)
    direction = random.choice(["left", "right"])
    signed = magnitude if direction == "right" else -magnitude
    return signed, direction


class ApprovalStore:
    def __init__(self) -> None:
        self._requests: dict[str, ApprovalRequest] = {}

    def create(self, risk: RiskInfo) -> ApprovalRequest:
        target_angle, direction = _pick_target_angle()
        req = ApprovalRequest(
            request_id=str(uuid.uuid4()),
            risk=risk,
            target_angle=target_angle,
            target_direction=direction,
        )
        self._requests[req.request_id] = req
        return req

    def get(self, request_id: str) -> ApprovalRequest | None:
        return self._requests.get(request_id)

    def try_ack_step1(self, request_id: str) -> tuple[bool, float]:
        """Attempt to advance PENDING_ACK -> AWAITING_GESTURE.

        Returns (ok, remaining_seconds). The 5s clock starts at request
        creation (server time), not at WS-connect time, so a client cannot
        shorten the window by delaying its connection — but this also means
        the countdown the UI shows on connect must account for time already
        elapsed. remaining_seconds is 0 when ok is True.
        """
        req = self.get(request_id)
        if req is None:
            return False, 0.0
        if req.status != ApprovalStatus.PENDING_ACK:
            return False, 0.0
        remaining = STEP1_MIN_SECONDS - req.elapsed_seconds()
        if remaining > 0:
            return False, remaining
        req.status = ApprovalStatus.AWAITING_GESTURE
        req.gesture_phase_started_at = time.monotonic()
        req.mock_biometric_verified_at = datetime.now(timezone.utc).isoformat()
        return True, 0.0

    def finalize(self, request_id: str, accepted: bool, reason: str | None) -> ApprovalRequest | None:
        req = self.get(request_id)
        if req is None:
            return None
        if req.status != ApprovalStatus.AWAITING_GESTURE:
            return None
        req.status = ApprovalStatus.APPROVED if accepted else ApprovalStatus.REJECTED
        req.result_reason = reason
        return req

    def abort(self, request_id: str, reason: str) -> ApprovalRequest | None:
        """REQ-006 Fail-Closed: force a non-terminal request to REJECTED,
        regardless of which phase it's in (PENDING_ACK or AWAITING_GESTURE).
        No-op if already terminal (APPROVED/REJECTED) — never overwrites a
        real outcome."""
        req = self.get(request_id)
        if req is None:
            return None
        if req.status in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED):
            return req
        req.status = ApprovalStatus.REJECTED
        req.result_reason = reason
        return req
