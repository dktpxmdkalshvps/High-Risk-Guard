"""high-risk-guard MCP server.

Exposes two tools:

  - `scan_high_risk`: REQ-001 detection only (unchanged since Phase 1).
    Useful for a quick dry check without triggering an actual approval
    request.
  - `request_high_risk_approval`: the full Phase 2/3 round trip. Runs
    REQ-001 detection; if the change is high-risk, creates an approval
    request on the backend, waits for the approver to go through the
    REQ-002~004 mobile flow, and returns the outcome. This is what Codex
    should actually call before applying a high-risk change — see
    skills/high-risk-guard/SKILL.md.

This process (the MCP server, spawned by Codex over stdio) and the FastAPI
backend (backend/app.py, run separately via uvicorn) are two different
processes. They only talk over HTTP — this file never imports backend.*
directly — which is also why REQ-006 Fail-Closed has to be enforced
backend-side (in app.py) rather than here: this process has no way to know
if the backend/approver ever comes back.
"""
from __future__ import annotations

import os
import time

import httpx
from mcp.server.fastmcp import FastMCP

from mcp_server.detector import scan_change

mcp = FastMCP("high-risk-guard")

BACKEND_BASE_URL = os.environ.get("HIGH_RISK_GUARD_BACKEND_URL", "http://127.0.0.1:8000")
MCP_POLL_INTERVAL_SECONDS = float(os.environ.get("HIGH_RISK_GUARD_MCP_POLL_INTERVAL_SECONDS", "1.0"))
# Bounded wait for THIS tool call, distinct from REQ-006's own server-side
# Fail-Closed timeout (GESTURE_TIMEOUT_SECONDS, enforced in backend/app.py
# once the gesture screen is reached). This one exists purely so the MCP
# tool call itself can't block Codex forever if the approver never even
# opens/acks the request in the first place — a phase REQ-006 has no
# server-side timeout for.
MCP_POLL_MAX_SECONDS = float(os.environ.get("HIGH_RISK_GUARD_MCP_POLL_MAX_SECONDS", "120.0"))


def _make_http_client() -> httpx.Client:
    """Split out so tests can monkeypatch this to point at an in-process
    ASGI transport instead of doing real network I/O against a live
    uvicorn process."""
    return httpx.Client(base_url=BACKEND_BASE_URL, timeout=10.0)


@mcp.tool()
def scan_high_risk(file_path: str, content: str) -> dict:
    """Scan a proposed code change for REQ-001 high-risk signals only
    (no approval request is created). Returns `is_high_risk`,
    `matched_keywords`, `matched_paths`."""
    return scan_change(file_path, content).to_dict()


@mcp.tool()
def request_high_risk_approval(file_path: str, content: str, diff_summary: str | None = None) -> dict:
    """Run REQ-001 detection and, if high-risk, block until a human
    approves or rejects via the physical-gesture mobile flow (or the
    request is auto-aborted by REQ-006 Fail-Closed).

    Args:
        file_path: Path of the file being modified.
        content: New file content or diff text being applied.
        diff_summary: Human-readable summary shown on the REQ-002 forced
            risk-exposure screen. Defaults to a truncated `content` if omitted.

    Returns:
        A dict with `approved` (bool — the only field the caller must act
        on), `status`, `reason`, and (when a request was actually created)
        `request_id` / `approve_url`. The caller MUST NOT proceed with the
        change unless `approved` is exactly True.
    """
    detection = scan_change(file_path, content)
    if not detection.is_high_risk:
        return {"approved": True, "status": "not_high_risk", "reason": None}

    body = {
        "file_path": file_path,
        "diff_summary": diff_summary or content[:2000],
        "matched_keywords": detection.matched_keywords,
        "matched_paths": detection.matched_paths,
    }

    with _make_http_client() as client:
        created_resp = client.post("/api/requests", json=body)
        created_resp.raise_for_status()
        created = created_resp.json()
        request_id = created["request_id"]
        approve_url = str(client.base_url) + created["approve_url"].lstrip("/")

        deadline = time.monotonic() + MCP_POLL_MAX_SECONDS
        while time.monotonic() < deadline:
            poll_resp = client.get(f"/api/requests/{request_id}")
            poll_resp.raise_for_status()
            state = poll_resp.json()
            status = state.get("status")
            if status in ("approved", "rejected"):
                return {
                    "approved": status == "approved",
                    "status": status,
                    "reason": state.get("result_reason"),
                    "request_id": request_id,
                    "approve_url": approve_url,
                }
            time.sleep(MCP_POLL_INTERVAL_SECONDS)

        # The approver never reached a terminal outcome within our own
        # polling budget. Fail-Closed locally (approved=False) AND tell the
        # backend to close the request out server-side — otherwise its
        # record would sit in PENDING_ACK/AWAITING_GESTURE forever, and a
        # late approval minutes later would get logged in the REQ-005
        # audit trail as "approved" for a change Codex already gave up on.
        try:
            abandon_resp = client.post(f"/api/requests/{request_id}/abandon")
            abandon_resp.raise_for_status()
            abandoned_state = abandon_resp.json()
            reason = abandoned_state.get("result_reason", "abandoned_no_response")
        except httpx.HTTPError:
            # Best-effort: even if telling the backend fails (network blip,
            # backend restarted, ...), we still must not return approved.
            reason = "abandoned_no_response"

    return {
        "approved": False,
        "status": "rejected",
        "reason": reason,
        "request_id": request_id,
        "approve_url": approve_url,
    }


if __name__ == "__main__":
    mcp.run()
