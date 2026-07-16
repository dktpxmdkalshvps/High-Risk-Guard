"""FastAPI + WebSocket backend for REQ-002~006.

Round trip:

  1. Something (an MCP tool call, a verification script, ...) POSTs risk
     info to /api/requests -> gets back a request_id + mobile URL.
  2. The mobile screen (mobile/approve.html) opens that URL, connects to
     /ws/{request_id}, and is pushed the risk summary (REQ-002 data).
  3. After the client-enforced + server-enforced STEP1_MIN_SECONDS, the
     client sends step1_ack; the server advances state and pushes the
     random target angle (REQ-003 data) — never both steps at once.
  4. The client streams DeviceOrientation samples for the whole gesture
     phase and, once it locally detects a 1.0s in-range hold, sends
     gesture_result; the server authoritatively re-validates via
     gesture_validator (REQ-004) and pushes back approved/rejected.
  5. REQ-006 Fail-Closed: if the WebSocket disconnects, or no valid
     gesture arrives within GESTURE_TIMEOUT_SECONDS of entering the
     gesture phase, the request is force-aborted to REJECTED — never left
     hanging, never defaults to approved.
  6. Every terminal outcome (approved/rejected, including Fail-Closed
     aborts) is written to the REQ-005 hash-chained audit log.
  7. GET /api/requests/{id} lets any other process (the MCP server) poll
     the final outcome — this is how scan/approval results get back to
     Codex.
  8. POST /api/requests/{id}/abandon: the *caller's* Fail-Closed. REQ-006's
     own timeout only covers the gesture phase — there is deliberately no
     server-side timeout on PENDING_ACK (an approver is allowed to take
     their time reading the risk summary). But an MCP tool call still has
     to return eventually, so mcp_server.server bounds its own wait
     (MCP_POLL_MAX_SECONDS) and, if that expires with the request still
     open, calls this endpoint to explicitly close it out server-side as
     REJECTED("abandoned_no_response") — instead of just giving up locally
     and leaving the backend record (and REQ-005 audit trail) sitting in
     limbo, which would let a late approval minutes later get logged as
     "approved" for work Codex had already treated as rejected.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend.approval_store import (
    ANGLE_TOLERANCE_DEG,
    GESTURE_TIMEOUT_SECONDS,
    HOLD_DURATION_MS,
    STEP1_MIN_SECONDS,
    ApprovalStatus,
    ApprovalStore,
    RiskInfo,
)
from backend.audit_log import AuditEntryInput, AuditLog
from backend.gesture_validator import Sample, validate_gesture

app = FastAPI(title="high-risk-guard backend")
store = ApprovalStore()
# Overridable so tests / verification scripts don't write into the real
# audit DB file that ships in the repo.
audit_log = AuditLog(os.environ.get("HIGH_RISK_GUARD_AUDIT_DB_PATH"))

MOBILE_DIR = Path(__file__).resolve().parent.parent / "mobile"
app.mount("/mobile", StaticFiles(directory=str(MOBILE_DIR)), name="mobile")

DEFAULT_APPROVER_ID = "approver-demo"
# Used for the audit-log row written by /abandon: no human approver acted
# on this request at all, so there is no real approver_id for it.
SYSTEM_APPROVER_ID = "system:mcp-poll-timeout"


@app.get("/")
def root():
    return RedirectResponse(url="/mobile/approve.html")


class CreateRequestBody(BaseModel):
    file_path: str
    diff_summary: str
    matched_keywords: list[str] = []
    matched_paths: list[str] = []


@app.post("/api/requests")
def create_request(body: CreateRequestBody):
    risk = RiskInfo(
        file_path=body.file_path,
        diff_summary=body.diff_summary,
        matched_keywords=body.matched_keywords,
        matched_paths=body.matched_paths,
    )
    req = store.create(risk)
    return {
        "request_id": req.request_id,
        "approve_url": f"/mobile/approve.html?request_id={req.request_id}",
        "status": req.status.value,
    }


@app.get("/api/requests/{request_id}")
def get_request(request_id: str):
    req = store.get(request_id)
    if req is None:
        return {"error": "not_found"}
    return {
        "request_id": req.request_id,
        "status": req.status.value,
        "result_reason": req.result_reason,
    }


@app.post("/api/requests/{request_id}/abandon")
def abandon_request(request_id: str):
    """Caller-side Fail-Closed (see module docstring, point 8). Idempotent:
    if the request already reached a real terminal outcome (approved,
    rejected, or already abandoned) before this call arrives — e.g. the
    approver finished the gesture in the same instant the MCP poll gave
    up — store.abort() is a no-op and that real outcome is left alone. We
    only write a new audit row when this call is the thing that actually
    closes the request out, so a race never produces two conflicting
    terminal audit entries for the same request_id."""
    req = store.get(request_id)
    if req is None:
        return {"error": "not_found"}
    was_already_terminal = req.status in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED)
    req = store.abort(request_id, "abandoned_no_response")
    if not was_already_terminal:
        _write_audit_entry(req, SYSTEM_APPROVER_ID, samples=[])
    return {
        "request_id": req.request_id,
        "status": req.status.value,
        "result_reason": req.result_reason,
    }


def _state_message(req) -> dict:
    msg = {
        "type": "state",
        "status": req.status.value,
        "risk": {
            "file_path": req.risk.file_path,
            "diff_summary": req.risk.diff_summary,
            "matched_keywords": req.risk.matched_keywords,
            "matched_paths": req.risk.matched_paths,
        },
        "step1_min_seconds": STEP1_MIN_SECONDS,
        "elapsed_seconds": req.elapsed_seconds(),
    }
    if req.status == ApprovalStatus.AWAITING_GESTURE:
        msg["target_angle"] = req.target_angle
        msg["target_direction"] = req.target_direction
        msg["tolerance"] = ANGLE_TOLERANCE_DEG
        msg["hold_duration_ms"] = HOLD_DURATION_MS
        msg["gesture_timeout_seconds"] = GESTURE_TIMEOUT_SECONDS
    if req.status in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED):
        msg["result_reason"] = req.result_reason
    return msg


def _write_audit_entry(req, approver_id: str, samples: list[dict]) -> None:
    """Only called for terminal outcomes (approved/rejected, including
    Fail-Closed aborts) — REQ-005 logs the *outcome* of a request, not
    every intermediate state transition."""
    audit_log.record(AuditEntryInput(
        request_id=req.request_id,
        file_path=req.risk.file_path,
        status=req.status.value,
        result_reason=req.result_reason,
        approver_id=approver_id,
        mock_biometric_verified_at=req.mock_biometric_verified_at,
        sensor_samples=samples,
        target_angle=req.target_angle if req.gesture_phase_started_at else None,
    ))


@app.websocket("/ws/{request_id}")
async def approval_ws(websocket: WebSocket, request_id: str, approver_id: str = DEFAULT_APPROVER_ID):
    await websocket.accept()
    req = store.get(request_id)
    if req is None:
        await websocket.send_json({"type": "error", "reason": "not_found"})
        await websocket.close()
        return

    await websocket.send_json(_state_message(req))

    try:
        while True:
            # REQ-006 Fail-Closed: once the gesture phase has started, cap
            # how long we'll wait for a valid gesture_result. This is a
            # *receive* timeout, not a background timer, so it only ever
            # fires while we're actually waiting on this specific socket.
            if req.status == ApprovalStatus.AWAITING_GESTURE:
                remaining = GESTURE_TIMEOUT_SECONDS - req.gesture_phase_elapsed_seconds()
                if remaining <= 0:
                    req = store.abort(request_id, "timeout_fail_closed")
                    _write_audit_entry(req, approver_id, samples=[])
                    await websocket.send_json(_state_message(req))
                    break
                try:
                    msg = await asyncio.wait_for(websocket.receive_json(), timeout=remaining)
                except asyncio.TimeoutError:
                    req = store.abort(request_id, "timeout_fail_closed")
                    _write_audit_entry(req, approver_id, samples=[])
                    await websocket.send_json(_state_message(req))
                    break
            else:
                msg = await websocket.receive_json()

            msg_type = msg.get("type")

            # A request can be closed out from *outside* this loop between
            # two messages on the same socket — most notably /abandon
            # (REQ-006's caller-side Fail-Closed) firing while this
            # approver is still sitting on an old screen. Surface that
            # plainly instead of letting it look like "you were too early"
            # or a generic phase mismatch.
            req = store.get(request_id)
            if req.status in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED) and msg_type in (
                "step1_ack", "gesture_result"
            ):
                await websocket.send_json({
                    "type": "error",
                    "reason": "request_already_closed",
                    "status": req.status.value,
                    "result_reason": req.result_reason,
                })
                continue

            if msg_type == "step1_ack":
                ok, remaining = store.try_ack_step1(request_id)
                req = store.get(request_id)
                if ok:
                    await websocket.send_json(_state_message(req))
                else:
                    await websocket.send_json({
                        "type": "error",
                        "reason": "step1_too_early",
                        "remaining_seconds": remaining,
                    })

            elif msg_type == "gesture_result":
                if req.status != ApprovalStatus.AWAITING_GESTURE:
                    await websocket.send_json({"type": "error", "reason": "wrong_phase"})
                    continue
                raw_samples = msg.get("samples", [])
                samples = [Sample(t_ms=s["t_ms"], gamma=s["gamma"]) for s in raw_samples]
                result = validate_gesture(
                    samples,
                    target_angle=req.target_angle,
                    tolerance=ANGLE_TOLERANCE_DEG,
                    hold_duration_ms=HOLD_DURATION_MS,
                    hold_start_t_ms=msg.get("hold_start_t_ms", 0),
                    hold_end_t_ms=msg.get("hold_end_t_ms", 0),
                )
                req = store.finalize(request_id, result.accepted, result.reason)
                _write_audit_entry(req, approver_id, samples=raw_samples)
                await websocket.send_json(_state_message(req))

            else:
                await websocket.send_json({"type": "error", "reason": "unknown_message_type"})

    except WebSocketDisconnect:
        # REQ-006 Fail-Closed: a request that hasn't reached a terminal
        # outcome when its socket drops is aborted, not left pending.
        req = store.get(request_id)
        if req is not None and req.status not in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED):
            req = store.abort(request_id, "disconnected_fail_closed")
            _write_audit_entry(req, approver_id, samples=[])
        return
