"""Unit tests for REQ-004 (backend.gesture_validator).

Uses synthetic sample streams built with a seeded RNG so results are
deterministic. See gesture_validator.py's module docstring for the
reasoning behind the two rejection heuristics being tested here.
"""
import math
import random

from backend.gesture_validator import Sample, validate_gesture

TARGET_ANGLE = 20.0
TOLERANCE = 3.0
HOLD_DURATION_MS = 1000.0


def _natural_hold_samples(seed: int = 42):
    """Simulates a human tilting a phone from rest (0 deg) up to the
    target over ~1.5s (with natural jitter), then holding there for
    ~1.2s (with small natural tremor) — should be accepted."""
    rng = random.Random(seed)
    samples = []
    ramp_ms = 1500
    step_ms = 50
    t = 0.0
    while t < ramp_ms:
        progress = t / ramp_ms
        angle = TARGET_ANGLE * progress + rng.uniform(-0.6, 0.6)
        samples.append(Sample(t_ms=t, gamma=angle))
        t += step_ms

    hold_start = t
    hold_ms = 1200
    while t < hold_start + hold_ms:
        angle = TARGET_ANGLE + rng.uniform(-0.7, 0.7)
        samples.append(Sample(t_ms=t, gamma=angle))
        t += step_ms
    hold_end = t
    return samples, hold_start, hold_end


def _steady_confident_hold_samples():
    """A gray-zone case: a steady hand that ramps up smoothly (near-zero
    ramp noise, strictly monotonic — no direction reversals at all in the
    ramp) and then holds almost perfectly still, with only 2 tiny
    corrective wobbles during the hold (not the 6+ needed to even run the
    periodicity check, and not perfectly uniform in size). This is meant
    to sit right at the edge of both heuristics: overall variance is not
    huge (most of the session is close to target), and there is some
    oscillation, but not enough of either to look like a macro."""
    samples = []
    t = 0.0
    step_ms = 50
    ramp_steps = 30
    for i in range(ramp_steps + 1):
        angle = TARGET_ANGLE * (i / ramp_steps)  # perfectly linear, zero noise
        samples.append(Sample(t_ms=t, gamma=angle))
        t += step_ms
    hold_start = t

    # 2 small, non-uniform corrective wobbles, then dead calm for the rest
    # of the hold — a plausible "confident, steady hand".
    wobble_offsets = [0.4, -0.3, 0.15, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                      0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                      0.0, 0.0, 0.0, 0.0]
    for offset in wobble_offsets:
        samples.append(Sample(t_ms=t, gamma=TARGET_ANGLE + offset))
        t += step_ms
    hold_end = t
    return samples, hold_start, hold_end


def test_steady_confident_hold_accepted():
    """Gray-zone check requested before Phase 3: a calm, accurate hand
    should NOT be mistaken for a flat fake or a macro just because its
    variance/CV happen to be low."""
    samples, hold_start, hold_end = _steady_confident_hold_samples()
    result = validate_gesture(
        samples,
        target_angle=TARGET_ANGLE,
        tolerance=TOLERANCE,
        hold_duration_ms=HOLD_DURATION_MS,
        hold_start_t_ms=hold_start,
        hold_end_t_ms=hold_end,
    )
    assert result.accepted is True, f"gray-zone steady hold was rejected: {result.reason}"
    assert result.reason is None


def test_natural_hold_accepted():
    samples, hold_start, hold_end = _natural_hold_samples()
    result = validate_gesture(
        samples,
        target_angle=TARGET_ANGLE,
        tolerance=TOLERANCE,
        hold_duration_ms=HOLD_DURATION_MS,
        hold_start_t_ms=hold_start,
        hold_end_t_ms=hold_end,
    )
    assert result.accepted is True
    assert result.reason is None


def test_flat_fake_rejected_as_no_movement():
    """The device never moved: every sample is (near-)identical, as if a
    script jumped straight to the target and claimed a hold, or the phone
    sat flat on a table while someone tapped a button."""
    samples = [Sample(t_ms=i * 50.0, gamma=TARGET_ANGLE + 0.01 * (i % 2)) for i in range(30)]
    result = validate_gesture(
        samples,
        target_angle=TARGET_ANGLE,
        tolerance=TOLERANCE,
        hold_duration_ms=HOLD_DURATION_MS,
        hold_start_t_ms=0,
        hold_end_t_ms=samples[-1].t_ms,
    )
    assert result.accepted is False
    assert result.reason == "no_movement_detected"


def test_perfectly_periodic_swing_rejected_as_mechanical():
    """A macro/servo swinging with fixed period and amplitude before
    settling exactly on the target — real hands wobble; this doesn't."""
    samples = []
    t = 0.0
    step_ms = 50
    # 40 samples of a perfect sine oscillation (fixed period=8 samples,
    # fixed amplitude) around the mid-point between rest and target.
    for i in range(40):
        angle = (TARGET_ANGLE / 2) * (1 + math.sin(2 * math.pi * i / 8))
        samples.append(Sample(t_ms=t, gamma=angle))
        t += step_ms
    hold_start = t
    for _ in range(24):
        samples.append(Sample(t_ms=t, gamma=TARGET_ANGLE))
        t += step_ms
    hold_end = t

    result = validate_gesture(
        samples,
        target_angle=TARGET_ANGLE,
        tolerance=TOLERANCE,
        hold_duration_ms=HOLD_DURATION_MS,
        hold_start_t_ms=hold_start,
        hold_end_t_ms=hold_end,
    )
    assert result.accepted is False
    assert result.reason == "mechanical_pattern_detected"


def test_hold_drifting_out_of_tolerance_rejected():
    samples, hold_start, hold_end = _natural_hold_samples(seed=7)
    # Push the last few "hold" samples outside the tolerance band.
    for s in samples:
        if s.t_ms >= hold_start:
            s.gamma = TARGET_ANGLE + TOLERANCE + 5.0
    result = validate_gesture(
        samples,
        target_angle=TARGET_ANGLE,
        tolerance=TOLERANCE,
        hold_duration_ms=HOLD_DURATION_MS,
        hold_start_t_ms=hold_start,
        hold_end_t_ms=hold_end,
    )
    assert result.accepted is False
    assert result.reason == "hold_out_of_range"


def test_hold_shorter_than_required_rejected():
    samples, hold_start, _ = _natural_hold_samples(seed=3)
    short_hold_end = hold_start + 400  # well under the 1000ms requirement
    result = validate_gesture(
        samples,
        target_angle=TARGET_ANGLE,
        tolerance=TOLERANCE,
        hold_duration_ms=HOLD_DURATION_MS,
        hold_start_t_ms=hold_start,
        hold_end_t_ms=short_hold_end,
    )
    assert result.accepted is False
    assert result.reason == "hold_too_short"


def test_too_few_samples_rejected():
    samples = [Sample(t_ms=i * 50.0, gamma=TARGET_ANGLE) for i in range(3)]
    result = validate_gesture(
        samples,
        target_angle=TARGET_ANGLE,
        tolerance=TOLERANCE,
        hold_duration_ms=HOLD_DURATION_MS,
        hold_start_t_ms=0,
        hold_end_t_ms=samples[-1].t_ms,
    )
    assert result.accepted is False
    assert result.reason == "insufficient_samples"
