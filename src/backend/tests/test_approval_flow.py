"""Integration test for the REQ-002~004 WebSocket round trip
(backend.app): create request -> push risk info -> enforce the step1
minimum-exposure wait -> push target angle -> validate gesture -> push
final result. Confirms state sync end-to-end, not just the pieces.

created_at is nudged backward directly on the stored request instead of
sleeping STEP1_MIN_SECONDS in real time, to keep the test fast while still
exercising the real server-side time check (not bypassing it).
"""
import random

from fastapi.testclient import TestClient

from backend import app as app_module
from backend.approval_store import STEP1_MIN_SECONDS

client = TestClient(app_module.app)


def _create_request(**overrides):
    body = {
        "file_path": "src/wallet/transfer.py",
        "diff_summary": "transfer() 함수의 수수료 계산 로직 변경",
        "matched_keywords": ["transfer"],
        "matched_paths": ["/src/wallet/"],
    }
    body.update(overrides)
    resp = client.post("/api/requests", json=body)
    assert resp.status_code == 200
    return resp.json()


def _force_step1_wait_elapsed(request_id: str):
    """Fast-forward past STEP1_MIN_SECONDS without a real sleep."""
    req = app_module.store.get(request_id)
    req.created_at -= STEP1_MIN_SECONDS + 0.5


def _natural_hold_samples(target_angle: float, seed: int = 1):
    rng = random.Random(seed)
    samples = []
    t = 0.0
    while t < 1500:
        angle = target_angle * (t / 1500) + rng.uniform(-0.6, 0.6)
        samples.append({"t_ms": t, "gamma": angle})
        t += 50
    hold_start = t
    while t < hold_start + 1200:
        samples.append({"t_ms": t, "gamma": target_angle + rng.uniform(-0.7, 0.7)})
        t += 50
    hold_end = t
    return samples, hold_start, hold_end


def test_full_round_trip_approves_a_genuine_hold():
    created = _create_request()
    request_id = created["request_id"]

    with client.websocket_connect(f"/ws/{request_id}") as ws:
        state = ws.receive_json()
        assert state["type"] == "state"
        assert state["status"] == "pending_ack"
        assert state["risk"]["matched_keywords"] == ["transfer"]
        # REQ-002/REQ-003 must never be shown together.
        assert "target_angle" not in state

        # Attempting to skip straight to a gesture before step1 is acked.
        ws.send_json({"type": "gesture_result", "samples": [], "hold_start_t_ms": 0, "hold_end_t_ms": 0})
        early_gesture = ws.receive_json()
        assert early_gesture == {"type": "error", "reason": "wrong_phase"}

        # Ack too early (REQ-002: cannot be skipped).
        ws.send_json({"type": "step1_ack"})
        too_early = ws.receive_json()
        assert too_early["type"] == "error"
        assert too_early["reason"] == "step1_too_early"
        assert too_early["remaining_seconds"] > 0

        _force_step1_wait_elapsed(request_id)
        ws.send_json({"type": "step1_ack"})
        advanced = ws.receive_json()
        assert advanced["type"] == "state"
        assert advanced["status"] == "awaiting_gesture"
        assert 15.0 <= abs(advanced["target_angle"]) <= 25.0
        # Step 1's risk-detail fields are still present but the gesture
        # step's own fields must now also be there — sequential, not merged
        # into a single combined screen.
        assert advanced["hold_duration_ms"] == 1000.0

        target_angle = advanced["target_angle"]
        samples, hold_start, hold_end = _natural_hold_samples(target_angle)
        ws.send_json({
            "type": "gesture_result",
            "samples": samples,
            "hold_start_t_ms": hold_start,
            "hold_end_t_ms": hold_end,
        })
        result = ws.receive_json()
        assert result["type"] == "state"
        assert result["status"] == "approved"

    polled = client.get(f"/api/requests/{request_id}").json()
    assert polled["status"] == "approved"


def test_flat_gesture_is_rejected_over_the_wire():
    created = _create_request()
    request_id = created["request_id"]

    with client.websocket_connect(f"/ws/{request_id}") as ws:
        ws.receive_json()  # initial state
        _force_step1_wait_elapsed(request_id)
        ws.send_json({"type": "step1_ack"})
        advanced = ws.receive_json()
        target_angle = advanced["target_angle"]

        flat_samples = [{"t_ms": i * 50, "gamma": target_angle} for i in range(30)]
        ws.send_json({
            "type": "gesture_result",
            "samples": flat_samples,
            "hold_start_t_ms": 0,
            "hold_end_t_ms": flat_samples[-1]["t_ms"],
        })
        result = ws.receive_json()
        assert result["status"] == "rejected"
        assert result["result_reason"] == "no_movement_detected"

    polled = client.get(f"/api/requests/{request_id}").json()
    assert polled["status"] == "rejected"
    assert polled["result_reason"] == "no_movement_detected"


def test_gesture_timeout_triggers_fail_closed():
    """REQ-006: reaching the gesture phase and then submitting nothing at
    all (no gesture_result, ever) must not leave the request hanging
    forever — it auto-aborts once GESTURE_TIMEOUT_SECONDS elapses. Uses
    the real server-side timeout (shortened to 0.3s for tests via
    conftest.py's env var), not a simulated one."""
    created = _create_request()
    request_id = created["request_id"]

    with client.websocket_connect(f"/ws/{request_id}") as ws:
        ws.receive_json()  # initial state
        _force_step1_wait_elapsed(request_id)
        ws.send_json({"type": "step1_ack"})
        advanced = ws.receive_json()
        assert advanced["status"] == "awaiting_gesture"

        # Send nothing. The server's own receive-timeout should fire.
        timed_out = ws.receive_json()
        assert timed_out["status"] == "rejected"
        assert timed_out["result_reason"] == "timeout_fail_closed"

    polled = client.get(f"/api/requests/{request_id}").json()
    assert polled["status"] == "rejected"
    assert polled["result_reason"] == "timeout_fail_closed"


def test_disconnect_before_completion_triggers_fail_closed():
    """REQ-006: the socket dropping mid-flow (network loss, app killed,
    etc.) must abort the request rather than leave it pending forever."""
    created = _create_request()
    request_id = created["request_id"]

    with client.websocket_connect(f"/ws/{request_id}") as ws:
        ws.receive_json()  # initial state
        _force_step1_wait_elapsed(request_id)
        ws.send_json({"type": "step1_ack"})
        advanced = ws.receive_json()
        assert advanced["status"] == "awaiting_gesture"
        # Deliberately not sending gesture_result -- just drop the socket
        # by leaving the `with` block, simulating a lost connection.

    polled = client.get(f"/api/requests/{request_id}").json()
    assert polled["status"] == "rejected"
    assert polled["result_reason"] == "disconnected_fail_closed"
