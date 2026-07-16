"""REQ-004 verification: run N synthetic "macro / simple-tap" gesture
attempts through the real backend.gesture_validator.validate_gesture and
report exactly how many were rejected -- "N건 중 M건", not a percentage.

None of these attempts represent a genuine human hold (see
backend/tests/test_gesture_validator.py::test_steady_confident_hold_accepted
for the gray-zone case that *should* pass) -- they're all constructed to
look like either a phone that never moved, or a macro/servo replaying a
fixed pattern. If any of these slip through as "accepted", that's a real
gap this script is meant to surface, not something to explain away.

Run from src/: `python scripts/verify_req004_macro_rejection.py`
"""
import math
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.gesture_validator import Sample, validate_gesture  # noqa: E402

TARGET_ANGLE = 20.0
TOLERANCE = 3.0
HOLD_DURATION_MS = 1000.0


def _flat(offset_deg=0.0, target=TARGET_ANGLE, n=30, step_ms=50):
    samples = [Sample(t_ms=i * step_ms, gamma=target + offset_deg) for i in range(n)]
    return samples, 0, samples[-1].t_ms


def _flat_with_tiny_noise(noise=0.02, target=TARGET_ANGLE, n=30, step_ms=50, seed=1):
    import random
    rng = random.Random(seed)
    samples = [Sample(t_ms=i * step_ms, gamma=target + rng.uniform(-noise, noise)) for i in range(n)]
    return samples, 0, samples[-1].t_ms


def _periodic(period_samples, n=40, amplitude=None, target=TARGET_ANGLE, step_ms=50, shape="sine"):
    amplitude = amplitude if amplitude is not None else target / 2
    samples = []
    t = 0.0
    for i in range(n):
        if shape == "sine":
            g = amplitude * (1 + math.sin(2 * math.pi * i / period_samples))
        else:  # triangle wave
            phase = (i % period_samples) / period_samples
            tri = 1 - abs(2 * phase - 1)
            g = amplitude * 2 * tri
        samples.append(Sample(t_ms=t, gamma=g))
        t += step_ms
    hold_start = t
    for _ in range(24):
        samples.append(Sample(t_ms=t, gamma=target))
        t += step_ms
    return samples, hold_start, t


CASES = [
    ("고정값 탭 (양의 목표각)", lambda: _flat(target=20.0)),
    ("고정값 탭 (음의 목표각)", lambda: _flat(target=-22.0)),
    ("고정값 탭 + 미세 잡음(0.02deg, 진짜 흔들림 아님)", lambda: _flat_with_tiny_noise(noise=0.02)),
    ("순간 점프 후 완전 고정 유지", lambda: _flat(offset_deg=0.0, target=18.0, n=26)),
    ("완벽한 사인파 (주기 8샘플)", lambda: _periodic(8, shape="sine")),
    ("완벽한 사인파 (주기 4샘플, 더 빠름)", lambda: _periodic(4, shape="sine")),
    ("완벽한 삼각파 (주기 6샘플)", lambda: _periodic(6, shape="triangle")),
    ("완벽한 삼각파 (주기 10샘플)", lambda: _periodic(10, shape="triangle")),
    ("고정값 탭, 목표각 근접 다른 값", lambda: _flat(target=16.5)),
    ("고정값 탭, 목표각 근접 다른 값 2", lambda: _flat(target=24.0)),
]


def main() -> int:
    rejected = 0
    print(f"{'#':<3} {'결과':<6} {'케이스':<45} {'사유'}")
    print("-" * 100)
    for i, (label, gen) in enumerate(CASES, 1):
        samples, hold_start, hold_end = gen()
        target = samples[-1].gamma if hold_end == samples[-1].t_ms else TARGET_ANGLE
        # Use the actual hold-segment average as the "claimed" target angle,
        # mirroring what a real client would report as its target.
        hold_samples = [s for s in samples if s.t_ms >= hold_start]
        claimed_target = sum(s.gamma for s in hold_samples) / len(hold_samples) if hold_samples else TARGET_ANGLE

        result = validate_gesture(
            samples,
            target_angle=claimed_target,
            tolerance=TOLERANCE,
            hold_duration_ms=HOLD_DURATION_MS,
            hold_start_t_ms=hold_start,
            hold_end_t_ms=hold_end,
        )
        mark = "거부" if not result.accepted else "승인(!)"
        if not result.accepted:
            rejected += 1
        print(f"{i:<3} {mark:<6} {label:<45} {result.reason or '(REQ-004를 통과함)'}")

    print("-" * 100)
    print(f"결과: 매크로/단순 탭 시도 {len(CASES)}건 중 {rejected}건 거부됨")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
