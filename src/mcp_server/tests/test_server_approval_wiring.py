"""Tests that request_high_risk_approval (mcp_server.server) actually
wires REQ-001 detection through to a real backend approval flow and back
— not just that the pieces exist in isolation.

A real uvicorn server (backend.app.app, the exact same ASGI app used in
production) is started on a loopback port for this test module.
request_high_risk_approval talks to it over real HTTP, exactly as it would
against a production backend — no ASGI-transport shortcuts. The approver's
side of the WebSocket flow is driven on a background thread using
FastAPI's TestClient (which shares the same in-process `store`/`app`
objects the live server is also using), while the main thread runs
request_high_risk_approval's polling loop for real.
"""
import json
import random
import socket
import threading
import time
import urllib.request

import pytest
import uvicorn
from fastapi.testclient import TestClient

from backend import app as backend_app_module
from backend.approval_store import STEP1_MIN_SECONDS
from mcp_server import server as mcp_server_module

ws_client = TestClient(backend_app_module.app)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live_backend():
    port = _free_port()
    config = uvicorn.Config(backend_app_module.app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)

    original_base_url = mcp_server_module.BACKEND_BASE_URL
    mcp_server_module.BACKEND_BASE_URL = f"http://127.0.0.1:{port}"
    try:
        yield mcp_server_module.BACKEND_BASE_URL
    finally:
        mcp_server_module.BACKEND_BASE_URL = original_base_url
        server.should_exit = True
        thread.join(timeout=5)


def test_not_high_risk_change_is_approved_without_creating_a_request(live_backend, monkeypatch):
    def _fail(*a, **k):
        raise AssertionError("must not contact the backend for a non-high-risk change")
    monkeypatch.setattr(mcp_server_module, "_make_http_client", _fail)

    result = mcp_server_module.request_high_risk_approval(
        "src/utils/formatter.py", "def format_name(a, b): return f'{a} {b}'"
    )
    assert result == {"approved": True, "status": "not_high_risk", "reason": None}


def _play_approver_side(request_id: str, samples_fn):
    """Runs on a background thread: connects as the mobile approver would,
    fast-forwards the REQ-002 wait, acks, and submits a gesture."""
    with ws_client.websocket_connect(f"/ws/{request_id}") as ws:
        ws.receive_json()  # initial state
        req = backend_app_module.store.get(request_id)
        req.created_at -= STEP1_MIN_SECONDS + 0.5
        ws.send_json({"type": "step1_ack"})
        advanced = ws.receive_json()
        target_angle = advanced["target_angle"]

        samples, hold_start, hold_end = samples_fn(target_angle)
        ws.send_json({
            "type": "gesture_result",
            "samples": samples,
            "hold_start_t_ms": hold_start,
            "hold_end_t_ms": hold_end,
        })
        ws.receive_json()  # final result; the polling loop under test reads state via REST


def _natural_hold(target_angle, seed=1):
    rng = random.Random(seed)
    samples, t = [], 0.0
    while t < 1500:
        samples.append({"t_ms": t, "gamma": target_angle * (t / 1500) + rng.uniform(-0.6, 0.6)})
        t += 50
    hold_start = t
    while t < hold_start + 1200:
        samples.append({"t_ms": t, "gamma": target_angle + rng.uniform(-0.7, 0.7)})
        t += 50
    return samples, hold_start, t


def _wait_for_new_request_id(existing_ids, timeout_s=5.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        new_ids = set(backend_app_module.store._requests.keys()) - existing_ids
        if new_ids:
            return next(iter(new_ids))
        time.sleep(0.02)
    return None


def test_high_risk_change_approved_end_to_end(live_backend):
    """The Codex-facing tool call blocks (polling, over real HTTP against
    a real uvicorn server) while a separate "approver" thread completes
    the real REQ-002~004 mobile flow, then the tool call must observe the
    approval and return approved=True."""
    captured = {}

    def run_tool():
        captured["result"] = mcp_server_module.request_high_risk_approval(
            "src/wallet/transfer.py",
            "def transfer(a, b, amt): pass",
            diff_summary="수수료 계산 로직 변경",
        )

    existing_ids = set(backend_app_module.store._requests.keys())
    tool_thread = threading.Thread(target=run_tool)
    tool_thread.start()

    request_id = _wait_for_new_request_id(existing_ids)
    assert request_id is not None, "request_high_risk_approval never created a request"

    _play_approver_side(request_id, _natural_hold)
    tool_thread.join(timeout=10)
    assert not tool_thread.is_alive(), "tool call did not return in time"

    result = captured["result"]
    assert result["approved"] is True
    assert result["status"] == "approved"
    assert result["request_id"] == request_id


def test_high_risk_change_rejected_end_to_end_when_gesture_is_flat(live_backend):
    captured = {}

    def run_tool():
        captured["result"] = mcp_server_module.request_high_risk_approval(
            "src/wallet/transfer.py", "def transfer(a, b, amt): pass"
        )

    existing_ids = set(backend_app_module.store._requests.keys())
    tool_thread = threading.Thread(target=run_tool)
    tool_thread.start()

    request_id = _wait_for_new_request_id(existing_ids)
    assert request_id is not None

    def _flat(target_angle):
        samples = [{"t_ms": i * 50, "gamma": target_angle} for i in range(30)]
        return samples, 0, samples[-1]["t_ms"]

    _play_approver_side(request_id, _flat)
    tool_thread.join(timeout=10)
    assert not tool_thread.is_alive()

    result = captured["result"]
    assert result["approved"] is False
    assert result["status"] == "rejected"
    assert result["reason"] == "no_movement_detected"


def test_abandoned_when_approver_never_responds(live_backend, monkeypatch):
    """If nobody ever acts on the request, request_high_risk_approval must
    not just give up locally -- it has to close the backend record out too
    (POST /abandon), or the request would sit open forever and a much
    later approval could get logged as "approved" for work Codex already
    treated as rejected. MCP_POLL_MAX_SECONDS is shortened for the test;
    the mechanism under test is the same either way."""
    monkeypatch.setattr(mcp_server_module, "MCP_POLL_MAX_SECONDS", 0.3)
    monkeypatch.setattr(mcp_server_module, "MCP_POLL_INTERVAL_SECONDS", 0.05)

    result = mcp_server_module.request_high_risk_approval(
        "src/wallet/transfer.py", "def transfer(a, b, amt): pass"
    )
    assert result["approved"] is False
    assert result["status"] == "rejected"
    assert result["reason"] == "abandoned_no_response"
    request_id = result["request_id"]

    with urllib.request.urlopen(f"{live_backend}/api/requests/{request_id}") as resp:
        polled = json.loads(resp.read())
    assert polled["status"] == "rejected"
    assert polled["result_reason"] == "abandoned_no_response"


def test_late_approval_attempt_after_abandonment_is_rejected(live_backend, monkeypatch):
    """Once a request has been abandoned, it must stay closed: (a) a
    freshly-opened mobile page for that request_id should immediately see
    the terminal "rejected/abandoned" state, and (b) even an
    already-open, stale connection that only now tries to send step1_ack
    must be refused, not advanced into the gesture phase."""
    monkeypatch.setattr(mcp_server_module, "MCP_POLL_MAX_SECONDS", 0.3)
    monkeypatch.setattr(mcp_server_module, "MCP_POLL_INTERVAL_SECONDS", 0.05)

    result = mcp_server_module.request_high_risk_approval(
        "src/wallet/transfer.py", "def transfer(a, b, amt): pass"
    )
    request_id = result["request_id"]
    assert result["reason"] == "abandoned_no_response"

    with ws_client.websocket_connect(f"/ws/{request_id}") as ws:
        state = ws.receive_json()
        assert state["status"] == "rejected"
        assert state["result_reason"] == "abandoned_no_response"

        ws.send_json({"type": "step1_ack"})
        late_ack = ws.receive_json()
        assert late_ack == {
            "type": "error",
            "reason": "request_already_closed",
            "status": "rejected",
            "result_reason": "abandoned_no_response",
        }
