"""REQ-004: reject simple taps and mechanical/macro repetition.

The mobile screen streams the *entire* gesture-phase sensor session (from
the moment the target-angle screen appears, not just the final "hold"
window) to the backend, and the backend — not the phone — makes the
accept/reject call. This is deliberate: a client that wants to fake success
can always claim "I held it," so the decision has to be based on whether
the submitted sample stream *looks physically plausible*, not on a
client-reported boolean.

Two rule-based heuristics, both intentionally simple (no ML, consistent
with the SRS's "MVP-level, not a formal biometric system" framing):

1. no_movement_detected — a human tilting a phone from rest (~0 deg) up to
   a 15-25 deg target necessarily produces a wide spread of angle values
   over the session. A session whose entire angle spread is near-zero
   means the device never actually moved — e.g. a script that jumps
   straight to a fixed value and reports it as a "hold," or someone
   tapping a button while the phone sits flat on a table. We flag this via
   the population standard deviation of gamma across the *whole* session
   (not just the hold slice, where low variance is expected and desired).

2. mechanical_pattern_detected — human hand tremor/adjustment while
   aiming for a target is noisy: the time between direction changes and
   the size of each swing varies. A macro/servo replaying a fixed
   oscillation produces near-identical period and amplitude on every
   cycle. We detect local extrema (peaks/troughs) in the angle sequence
   and, if there are enough of them to judge a pattern, compute the
   coefficient of variation (stdev / mean) of both the inter-peak time
   gaps and the peak-to-peak amplitudes. Suspiciously uniform values
   (low CV) on both axes indicate a mechanical source rather than a hand.

Both thresholds are conservative, hand-picked constants for an MVP demo —
not fit against real user data. That is called out here rather than
presented as calibrated.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass

# --- REQ-004 thresholds (documented, not fit against real data) ---
MIN_SAMPLES = 8  # below this we can't judge a "pattern" at all
FLAT_STDEV_THRESHOLD_DEG = 1.5  # whole-session stdev below this => "never moved"
MIN_EXTREMA_FOR_PATTERN_CHECK = 6  # need this many peaks/troughs to judge periodicity
MECHANICAL_CV_THRESHOLD = 0.10  # coefficient of variation below this => "too uniform to be a hand"


@dataclass
class Sample:
    t_ms: float  # time since gesture phase started, milliseconds
    gamma: float  # DeviceOrientationEvent.gamma, degrees


@dataclass
class GestureValidationResult:
    accepted: bool
    reason: str | None = None  # None when accepted


def _stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    return statistics.pstdev(values)


def _coefficient_of_variation(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    mean = statistics.fmean(values)
    if abs(mean) < 1e-9:
        return None
    return statistics.pstdev(values) / abs(mean)


def _find_extrema_indices(gammas: list[float]) -> list[int]:
    """Indices where the direction of travel reverses (local peak/trough)."""
    extrema = []
    prev_sign = 0
    for i in range(1, len(gammas)):
        delta = gammas[i] - gammas[i - 1]
        if delta == 0:
            continue
        sign = 1 if delta > 0 else -1
        if prev_sign != 0 and sign != prev_sign:
            extrema.append(i - 1)
        prev_sign = sign
    return extrema


def _looks_mechanical(samples: list[Sample]) -> bool:
    gammas = [s.gamma for s in samples]
    times = [s.t_ms for s in samples]
    extrema_idx = _find_extrema_indices(gammas)
    if len(extrema_idx) < MIN_EXTREMA_FOR_PATTERN_CHECK:
        return False  # not enough oscillation to judge one way or the other

    peak_times = [times[i] for i in extrema_idx]
    peak_gammas = [gammas[i] for i in extrema_idx]

    inter_peak_gaps = [b - a for a, b in zip(peak_times, peak_times[1:])]
    amplitudes = [abs(b - a) for a, b in zip(peak_gammas, peak_gammas[1:])]

    gap_cv = _coefficient_of_variation(inter_peak_gaps)
    amp_cv = _coefficient_of_variation(amplitudes)
    if gap_cv is None or amp_cv is None:
        return False

    return gap_cv < MECHANICAL_CV_THRESHOLD and amp_cv < MECHANICAL_CV_THRESHOLD


def validate_gesture(
    samples: list[Sample],
    *,
    target_angle: float,
    tolerance: float,
    hold_duration_ms: float,
    hold_start_t_ms: float,
    hold_end_t_ms: float,
) -> GestureValidationResult:
    """Authoritative REQ-003/REQ-004 check, run server-side on the full
    client-submitted sample stream for one gesture-phase session."""
    if len(samples) < MIN_SAMPLES:
        return GestureValidationResult(False, "insufficient_samples")

    if hold_end_t_ms - hold_start_t_ms < hold_duration_ms:
        return GestureValidationResult(False, "hold_too_short")

    hold_samples = [s for s in samples if hold_start_t_ms <= s.t_ms <= hold_end_t_ms]
    if not hold_samples:
        return GestureValidationResult(False, "hold_too_short")

    lo, hi = target_angle - tolerance, target_angle + tolerance
    if any(not (lo <= s.gamma <= hi) for s in hold_samples):
        return GestureValidationResult(False, "hold_out_of_range")

    all_gammas = [s.gamma for s in samples]
    if _stdev(all_gammas) < FLAT_STDEV_THRESHOLD_DEG:
        return GestureValidationResult(False, "no_movement_detected")

    if _looks_mechanical(samples):
        return GestureValidationResult(False, "mechanical_pattern_detected")

    return GestureValidationResult(True, None)
