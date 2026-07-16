"""REQ-006 (Fail-Closed) verification: starts the real backend
(backend.app:app) as an actual subprocess -- not an in-process ASGI
shortcut -- and drives two real WebSocket clients against it to confirm:

  A) an abrupt disconnect mid-flow (before any gesture is submitted)
     auto-aborts the request, rather than leaving it pending forever.
  B) letting the real GESTURE_TIMEOUT_SECONDS elapse without submitting a
     valid gesture auto-aborts the request via the server's own timeout
     (not a client-side giveup).

Each check reports Pass/Fail, honestly -- no partial credit, no rounding.

Run from src/: `python scripts/verify_req006_fail_closed.py`
(waits for the real default timeouts -- takes roughly 2x(STEP1_MIN_SECONDS
+ GESTURE_TIMEOUT_SECONDS) seconds, ~30s with the SRS defaults.)
"""
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

SRC_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC_DIR))

import websockets  # noqa: E402
from backend.approval_store import GESTURE_TIMEOUT_SECONDS, STEP1_MIN_SECONDS  # noqa: E402

PORT = 8971
BASE_URL = f"http://127.0.0.1:{PORT}"
WS_URL = f"ws://127.0.0.1:{PORT}"


def _http_post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        BASE_URL + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def _http_get(path: str) -> dict:
    with urllib.request.urlopen(BASE_URL + path) as resp:
        return json.loads(resp.read())


def _wait_for_server(timeout_s=15) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(BASE_URL + "/api/requests/health-probe", timeout=1)
            return True
        except urllib.error.HTTPError:
            return True  # server responded, even with an error status
        except Exception:
            time.sleep(0.3)
    return False


def _create_request() -> str:
    created = _http_post("/api/requests", {
        "file_path": "src/wallet/transfer.py",
        "diff_summary": "REQ-006 검증용 요청",
        "matched_keywords": ["transfer"],
        "matched_paths": ["/src/wallet/"],
    })
    return created["request_id"]


async def _advance_to_gesture_phase(request_id: str):
    ws = await websockets.connect(f"{WS_URL}/ws/{request_id}")
    await ws.recv()  # initial state (pending_ack)
    await asyncio.sleep(STEP1_MIN_SECONDS + 0.5)
    await ws.send(json.dumps({"type": "step1_ack"}))
    advanced = json.loads(await ws.recv())
    assert advanced["status"] == "awaiting_gesture", advanced
    return ws


async def check_disconnect_fail_closed() -> tuple[bool, dict]:
    request_id = _create_request()
    ws = await _advance_to_gesture_phase(request_id)
    await ws.close()  # abrupt disconnect; no gesture_result ever sent
    await asyncio.sleep(1.0)  # give the server a moment to notice
    polled = _http_get(f"/api/requests/{request_id}")
    ok = polled["status"] == "rejected" and polled["result_reason"] == "disconnected_fail_closed"
    return ok, polled


async def check_gesture_timeout_fail_closed() -> tuple[bool, dict]:
    request_id = _create_request()
    ws = await _advance_to_gesture_phase(request_id)
    # Send nothing at all; wait for the server's own GESTURE_TIMEOUT_SECONDS
    # receive-timeout to fire and push the abort state to us.
    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=GESTURE_TIMEOUT_SECONDS + 5))
    await ws.close()
    ok = msg.get("status") == "rejected" and msg.get("result_reason") == "timeout_fail_closed"
    polled = _http_get(f"/api/requests/{request_id}")
    ok = ok and polled["status"] == "rejected" and polled["result_reason"] == "timeout_fail_closed"
    return ok, polled


async def run_checks():
    results = []
    ok, detail = await check_disconnect_fail_closed()
    results.append(("A) WebSocket 강제 단절 시 Fail-Closed", ok, detail))

    ok, detail = await check_gesture_timeout_fail_closed()
    results.append((f"B) 제스처 타임아웃({GESTURE_TIMEOUT_SECONDS:.0f}초) 시 Fail-Closed", ok, detail))
    return results


def main() -> int:
    env = os.environ.copy()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.app:app",
         "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
        cwd=str(SRC_DIR),
        env=env,
    )
    try:
        if not _wait_for_server():
            print("결과: FAIL — 백엔드 서버가 기동되지 않았습니다.")
            return 1

        print(f"(STEP1_MIN_SECONDS={STEP1_MIN_SECONDS:.1f}s, "
              f"GESTURE_TIMEOUT_SECONDS={GESTURE_TIMEOUT_SECONDS:.1f}s 기준으로 실제 대기하며 검증합니다)")
        results = asyncio.run(run_checks())

        print("-" * 90)
        all_pass = True
        for label, ok, detail in results:
            status = "Pass" if ok else "Fail"
            all_pass = all_pass and ok
            print(f"[{status}] {label}")
            print(f"    -> {detail}")
        print("-" * 90)
        print(f"결과: {'PASS' if all_pass else 'FAIL'} "
              f"({sum(1 for _, ok, _ in results if ok)}/{len(results)}건 통과)")
        return 0 if all_pass else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
