# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Thermodynamic (DeltaForge Kd / ΔG) primitives for the selection loops.

Every selection surface in this SDK — the selectivity panel, the co-optimization
aggregate, the refinement verdict — was ipSAE-only. ipSAE says an interface was
*modelled*; it does not say how tightly anything binds, and the deliverable
customers buy is stated in molar units. This module holds the shared rules that
let Kd travel through those loops without acquiring a meaning it has not earned.

⛔ THE FIVE RULES, AND WHY EACH EXISTS
------------------------------------

1. **GATE BEFORE YOU SCORE.** DeltaForge scores an INTERFACE. Below a
   peptide-interface ipSAE of :data:`VALID_IPSAE` there is no interface to
   score, so the number it returns is about noise. Gate on
   ``peptide_ipsae`` (the ``ipsae_d0res`` binder metric), never on whole-complex
   ``overall_ipsae``, then score what survives. A computed ipSAE of ``0.0`` is a
   MEASUREMENT and gates out on its value; a ``None`` is an absent measurement
   and is a different state entirely (see rule 5).

2. **log10(Kd) IS SYMMETRIC WITHIN A design × target CELL, SO USE THE
   GEOMETRIC MEAN.** (Median skew −0.060 over 469 cells.) A median over the 2–4
   poses a cell actually has is an *optimistic order statistic*, not a noisy
   one — it is biased low and leave-one-out cannot detect that, because dropping
   a point barely moves an order statistic. :func:`geometric_mean_kd` is the
   estimator; :func:`kd_bootstrap_ci` is the interval that must travel with it.

3. **NEVER QUOTE A RATIO ON AN UNCONFIRMED DENOMINATOR.** Selectivity is
   ``min(off-target Kd) / on-target Kd``, so the on-target leg is the
   denominator and its error is the ratio's error. A screen-depth on-target Kd
   runs ~22x optimistic (measured: median 1.03 nM at 4 diffusion samples under a
   MIN_IN_DOMAIN=2 gate, versus 22.43 nM on independent re-fold; only 3 of 12
   held within 2x). An unconfirmed denominator therefore yields NO ratio —
   :func:`selectivity_ratio` returns ``ratio=None`` with a reason code. Not a
   ratio with a caveat attached: a caveat is discarded the first time the number
   is copied into a slide.

4. **DIRECTION IS PART OF THE METRIC.** ipSAE is higher-is-better; Kd is
   lower-is-better. "Worst case across targets" is therefore ``min`` for one and
   ``max`` for the other, and "clears the floor" is ``>=`` for one and ``<=``
   for the other. Passing a Kd into an aggregator that assumes ipSAE's direction
   returns a confidently inverted verdict and no field contradicts it. Nothing
   here infers direction from a value's magnitude or a field's name — the caller
   names it with :data:`MetricDirection`.

5. **AN ABSENT MEASUREMENT IS NOT A SCORE OF ZERO.** For Kd this is sharper than
   for ipSAE: 0 nM is *infinitely tight binding*, so defaulting a missing Kd to
   zero does not merely dilute a mean, it manufactures the best result in the
   set and sorts it to the top. Absent Kd ⇒ the row is INCOMPLETE and belongs in
   the partial list, never in a verdict.

Refs: bd-LIGANDAI_ALPHA_V2-jb73b.
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from ligandai.errors import LigandAIValidationError

__all__ = [
    "VALID_IPSAE",
    "V102_SCORER_NAMES",
    "V102_SCORER_VERSION_PREFIX",
    "kd_provenance_ok",
    "CONFIRM_MIN_DRAWS",
    "MetricDirection",
    "METRIC_DIRECTIONS",
    "DIRECTION_BY_METRIC",
    "geometric_mean_kd",
    "kd_bootstrap_ci",
    "kd_to_delta_g",
    "delta_g_to_kd",
    "SelectivityRatio",
    "selectivity_ratio",
    "worst_case",
    "clears_floor",
    "expected_false_positives",
    "multiplicity_note",
]

#: Peptide-interface ipSAE below which there is no interface for DeltaForge to
#: score. Applied to ``peptide_ipsae`` (``ipsae_d0res``), NEVER to whole-complex
#: ``overall_ipsae`` — those are different numbers and the whole-complex one is
#: systematically higher, so gating on it lets un-scoreable rows through.
VALID_IPSAE: float = 0.67

#: Independent fold submissions required before an on-target Kd may serve as the
#: DENOMINATOR of a selectivity ratio. One screen-depth draw is not a
#: confirmation of itself: re-folding the same design independently moved the
#: cohort median from 1.03 nM to 22.43 nM (~22x), and only 3 of 12 designs held
#: within 2x. See rule 3 in the module docstring.
CONFIRM_MIN_DRAWS: int = 2

#: Which way "better" points for a metric. There is no default and nothing here
#: guesses: a Kd aggregated under ipSAE's direction produces an inverted verdict
#: that no response field contradicts.
MetricDirection = Literal["higher_is_better", "lower_is_better"]

METRIC_DIRECTIONS: tuple[str, ...] = ("higher_is_better", "lower_is_better")

#: Direction for the metrics these loops actually carry. Used to CHECK a
#: caller-supplied direction against the metric they named, never to infer one
#: silently from a field name.
DIRECTION_BY_METRIC: dict[str, MetricDirection] = {
    "peptide_ipsae": "higher_is_better",
    "ipsae": "higher_is_better",
    "iptm": "higher_is_better",
    "plddt": "higher_is_better",
    "kd_nm": "lower_is_better",
    "delta_g": "lower_is_better",
}

#: Scorer names the production v10.2 router reports. An admissible Kd must carry
#: one of these (or a ``scorer_version`` starting with
#: :data:`V102_SCORER_VERSION_PREFIX`).
V102_SCORER_NAMES: frozenset[str] = frozenset({
    "deltaforge_v10_2_unified_parallel",
    "deltaforge_v10_boltz2_calibrated_gbr",
})

#: ``scorer_version`` prefix the v10.2 surface returns, e.g.
#: ``"v10_2_unified_parallel_2026_05_08"``.
V102_SCORER_VERSION_PREFIX: str = "v10_2_unified_parallel"


def kd_provenance_ok(row: Mapping[str, Any]) -> tuple[bool, str]:
    """Is this Kd attributable to the PRODUCTION v10.2 scorer?

    Returns ``(ok, reason)``.

    ⛔ DO NOT ASSERT ``calibration_head`` — VERIFIED ON PRODUCTION 2026-09-23.
    It is not a v10.2 field: in ``deltaforge_v10_client.py`` only the **V9HQ**
    branch ever sets ``calibration_head="v9hq_xray"``, the v10/v10.2 result
    dicts never set it, it is absent from every ``batch_score_fold`` response,
    and ``server/fold-thermo-join.ts`` records the column as NULL on all 43,668
    persisted rows. An implementation that required it would refuse EVERY real
    measurement while looking rigorous — a gate that rejects 100% of valid input
    is indistinguishable from one that was never wired up.

    What the API actually exposes, and what this checks instead, is
    ``scorer_version`` / ``scorer``. That still does the job the
    ``calibration_head`` assertion was reaching for: it separates the production
    v10.2 router from ``compute_deltaforge_energy`` (the V9 path, whose bin
    coefficients carry a 6.44 kcal/mol discontinuity at chain length 40 and a
    wrong-signed entropy term below it — the same 4,255 structures gave 0 versus
    32 single-digit-nM designs between the two).

    ⛔ Also refuses a row the platform has marked non-quotable. DeltaForge's Kd
    does not rank on every interface class (TCR-pMHC r=+0.02; symmetric /
    multi-component r=−0.17, i.e. worse than ignoring it), so the server
    withholds the quote and says why. A withheld number is a decision to
    respect, not a null to fill in.
    """
    if row.get("affinity_quotable") is False:
        code = row.get("affinity_reason_code") or "affinity_not_quotable"
        return False, f"withheld by the platform affinity gate: {code}"
    version = row.get("scorer_version")
    if isinstance(version, str) and version.startswith(V102_SCORER_VERSION_PREFIX):
        return True, f"scorer_version={version}"
    scorer = row.get("scorer") or row.get("affinity_scorer")
    if isinstance(scorer, str) and scorer in V102_SCORER_NAMES:
        return True, f"scorer={scorer}"
    return False, (
        f"not attributable to the production v10.2 scorer "
        f"(scorer_version={version!r}, scorer={scorer!r}); expected a version "
        f"starting {V102_SCORER_VERSION_PREFIX!r} or one of "
        f"{sorted(V102_SCORER_NAMES)}"
    )


# Gas constant × 298.15 K, in kcal/mol — the temperature DeltaForge calibrates at.
_RT_KCAL: float = 0.001987204258640832 * 298.15


def _check_direction(direction: str) -> MetricDirection:
    if direction not in METRIC_DIRECTIONS:
        raise LigandAIValidationError(
            f"unknown metric direction {direction!r}; use one of "
            f"{list(METRIC_DIRECTIONS)}. ipSAE is 'higher_is_better'; Kd and ΔG "
            f"are 'lower_is_better'. There is no default — a Kd aggregated under "
            f"ipSAE's direction returns an inverted verdict silently."
        )
    return direction  # type: ignore[return-value]


# ─────────────────────────────────────────────────────────────────────────────
# Point estimate and interval
# ─────────────────────────────────────────────────────────────────────────────


def geometric_mean_kd(draws: Sequence[float]) -> float:
    """Geometric mean of Kd draws — the estimator for a log-symmetric quantity.

    ⛔ NOT the median. Over 469 design × target cells the within-cell log10(Kd)
    distribution is symmetric (median skew −0.060), which makes the mean of the
    logs the efficient estimator and the median of 2–4 poses an optimistic order
    statistic that a leave-one-out check cannot flag.

    Raises on an empty sequence — an absent measurement is not a Kd of zero — and
    on a non-positive draw, which has no logarithm and cannot be a dissociation
    constant.
    """
    vals = [float(v) for v in draws]
    if not vals:
        raise LigandAIValidationError(
            "geometric_mean_kd got no draws. An absent measurement is not a Kd "
            "of zero — 0 nM is infinitely tight binding, so defaulting it would "
            "manufacture the best result in the set. The cell is incomplete."
        )
    bad = [v for v in vals if not math.isfinite(v) or v <= 0.0]
    if bad:
        raise LigandAIValidationError(
            f"Kd draws must be finite and > 0 nM; got {bad[:4]}. A non-positive "
            f"or non-finite Kd is a scorer failure or a unit error, not a "
            f"measurement, and must not be aggregated."
        )
    return float(math.exp(statistics.fmean(math.log(v) for v in vals)))


def kd_bootstrap_ci(
    draws: Sequence[float],
    *,
    confidence: float = 0.90,
    n_boot: int = 2000,
    rng_seed: int = 0,
) -> tuple[float, float] | None:
    """Percentile bootstrap CI for the geometric mean, resampled in log space.

    Returns ``None`` with fewer than :data:`CONFIRM_MIN_DRAWS` draws. An interval
    derived from a single draw is not an interval — it would be the point itself,
    and reporting it as a CI states a precision that was never measured.

    ``rng_seed`` is fixed by default so a reported interval is reproducible from
    the recorded draws.
    """
    vals = [float(v) for v in draws]
    if len(vals) < CONFIRM_MIN_DRAWS:
        return None
    if not 0.0 < confidence < 1.0:
        raise LigandAIValidationError("confidence must be in (0, 1)")
    bad = [v for v in vals if not math.isfinite(v) or v <= 0.0]
    if bad:
        raise LigandAIValidationError(
            f"Kd draws must be finite and > 0 nM; got {bad[:4]}"
        )
    logs = [math.log(v) for v in vals]
    rng = random.Random(rng_seed)
    n = len(logs)
    means = sorted(
        statistics.fmean(rng.choice(logs) for _ in range(n)) for _ in range(n_boot)
    )
    lo_i = int((1.0 - confidence) / 2.0 * n_boot)
    hi_i = min(n_boot - 1, int((1.0 + confidence) / 2.0 * n_boot))
    return (float(math.exp(means[lo_i])), float(math.exp(means[hi_i])))


def kd_to_delta_g(kd_nm: float) -> float:
    """ΔG (kcal/mol) from Kd in NANOMOLAR. The unit is in the parameter name."""
    if not math.isfinite(kd_nm) or kd_nm <= 0.0:
        raise LigandAIValidationError(f"kd_nm must be finite and > 0; got {kd_nm!r}")
    return float(_RT_KCAL * math.log(kd_nm * 1e-9))


def delta_g_to_kd(delta_g: float) -> float:
    """Kd in NANOMOLAR from ΔG in kcal/mol."""
    if not math.isfinite(delta_g):
        raise LigandAIValidationError(f"delta_g must be finite; got {delta_g!r}")
    return float(math.exp(delta_g / _RT_KCAL) * 1e9)


# ─────────────────────────────────────────────────────────────────────────────
# Direction-aware combination
# ─────────────────────────────────────────────────────────────────────────────


def worst_case(
    per_key: Mapping[str, float], direction: MetricDirection
) -> tuple[float, str]:
    """The weakest value and the key holding it, under ``direction``.

    For ``higher_is_better`` that is the ``min``; for ``lower_is_better`` (Kd, ΔG)
    it is the ``max``. Returns the limiting key under both, because "which target
    is holding this design back" is the actionable half of the answer.
    """
    _check_direction(direction)
    if not per_key:
        raise LigandAIValidationError(
            "worst_case got no values. An absent measurement is not a score of "
            "zero — the row is incomplete."
        )
    if direction == "higher_is_better":
        limiting = min(per_key, key=lambda k: per_key[k])
    else:
        limiting = max(per_key, key=lambda k: per_key[k])
    return float(per_key[limiting]), limiting


def clears_floor(value: float, floor: float, direction: MetricDirection) -> bool:
    """``value >= floor`` for higher-is-better, ``value <= floor`` for Kd/ΔG."""
    _check_direction(direction)
    return value >= floor if direction == "higher_is_better" else value <= floor


# ─────────────────────────────────────────────────────────────────────────────
# Selectivity ratio
# ─────────────────────────────────────────────────────────────────────────────

#: Why a selectivity ratio was withheld. A reason code, not free text, so the
#: caller can branch on it instead of pattern-matching a sentence.
RatioReason = Literal[
    "ok",
    "on_target_unconfirmed",
    "on_target_missing",
    "off_targets_missing",
    "on_target_not_gated",
]


@dataclass(frozen=True)
class SelectivityRatio:
    """``min(off-target Kd) / on-target Kd``, or an explicit refusal to state one.

    ⛔ ``ratio is None`` is the normal, expected outcome for a screen-depth panel
    and is NOT an error. Read :attr:`quotable` before reading :attr:`ratio`.
    """

    ratio: float | None
    reason: RatioReason
    on_target_kd_nm: float | None
    tightest_off_target: str | None
    tightest_off_target_kd_nm: float | None
    off_target_kd_nm: dict[str, float] = field(default_factory=dict)
    on_target_n_draws: int = 0
    on_target_ci_nm: tuple[float, float] | None = None
    note: str = ""

    @property
    def quotable(self) -> bool:
        """True only when a ratio was actually computed on a confirmed denominator."""
        return self.ratio is not None and self.reason == "ok"

    def __repr__(self) -> str:  # pragma: no cover - repr only
        if self.ratio is None:
            return f"<SelectivityRatio WITHHELD [{self.reason}]>"
        return (
            f"<SelectivityRatio {self.ratio:.1f}x vs {self.tightest_off_target} "
            f"(on-target {self.on_target_kd_nm:.3g} nM, n={self.on_target_n_draws})>"
        )


def selectivity_ratio(
    *,
    on_target_kd_nm: float | None,
    off_target_kd_nm: Mapping[str, float],
    on_target_n_draws: int,
    on_target_ci_nm: tuple[float, float] | None = None,
    on_target_gated: bool = True,
    min_draws: int = CONFIRM_MIN_DRAWS,
) -> SelectivityRatio:
    """Selectivity as the platform defines it: ``min(off Kd) / on-target Kd``.

    The ratio is WITHHELD — ``ratio=None`` plus a reason code — rather than
    caveated whenever it would rest on something that cannot carry it:

    ``on_target_missing``
        No on-target Kd. There is no denominator, and a missing one is not 1.0.
    ``on_target_not_gated``
        The on-target leg fell below :data:`VALID_IPSAE`, so DeltaForge scored a
        non-interface. The denominator is a number about noise.
    ``on_target_unconfirmed``
        Fewer than ``min_draws`` independent draws. Screen-depth on-target Kd
        runs ~22x optimistic, and a ~22x error in the denominator is a ~22x error
        in the ratio — which is the entire claimed effect for most designs.
    ``off_targets_missing``
        No off-target Kd at all. A "selectivity" with nothing to be selective
        against is not a margin.

    A partial off-target panel still yields a ratio, but it is an UPPER bound by
    construction (the minimum over a subset can only fall as comparators arrive),
    and :attr:`SelectivityRatio.note` says so.
    """
    offs = {k: float(v) for k, v in off_target_kd_nm.items()
            if isinstance(v, (int, float)) and math.isfinite(float(v)) and float(v) > 0}
    tightest = min(offs, key=lambda k: offs[k]) if offs else None
    tightest_kd = offs[tightest] if tightest is not None else None

    def _no(reason: RatioReason, note: str) -> SelectivityRatio:
        return SelectivityRatio(
            ratio=None, reason=reason, on_target_kd_nm=on_target_kd_nm,
            tightest_off_target=tightest, tightest_off_target_kd_nm=tightest_kd,
            off_target_kd_nm=offs, on_target_n_draws=on_target_n_draws,
            on_target_ci_nm=on_target_ci_nm, note=note,
        )

    if on_target_kd_nm is None:
        return _no("on_target_missing",
                   "No on-target Kd: there is no denominator. A missing "
                   "measurement is not a ratio of 1.")
    if not on_target_gated:
        return _no("on_target_not_gated",
                   f"On-target peptide ipSAE below {VALID_IPSAE}: DeltaForge "
                   f"scored a non-interface, so the denominator is not an affinity.")
    if on_target_n_draws < min_draws:
        return _no("on_target_unconfirmed",
                   f"On-target leg has {on_target_n_draws} independent draw(s); "
                   f"{min_draws} required. A screen-depth on-target Kd runs ~22x "
                   f"optimistic, and that error passes straight into the ratio.")
    if tightest is None or tightest_kd is None:
        return _no("off_targets_missing",
                   "No off-target Kd measured: there is nothing to be selective "
                   "against.")

    return SelectivityRatio(
        ratio=float(tightest_kd / float(on_target_kd_nm)),
        reason="ok",
        on_target_kd_nm=float(on_target_kd_nm),
        tightest_off_target=tightest,
        tightest_off_target_kd_nm=tightest_kd,
        off_target_kd_nm=offs,
        on_target_n_draws=on_target_n_draws,
        on_target_ci_nm=on_target_ci_nm,
        note=f"min over {len(offs)} off-target(s); an upper bound until every "
             f"comparator is present.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Multiplicity
# ─────────────────────────────────────────────────────────────────────────────


def expected_false_positives(n_tests: int, alpha: float = 0.05) -> float:
    """``alpha * n`` — how many nominal hits a null panel produces by itself.

    Ranking N designs is N tests. Compare the nominal hit count against this
    before believing any of them.
    """
    if n_tests < 0:
        raise LigandAIValidationError("n_tests must be >= 0")
    if not 0.0 < alpha < 1.0:
        raise LigandAIValidationError("alpha must be in (0, 1)")
    return float(alpha) * float(n_tests)


def multiplicity_note(n_hits: int, n_tests: int, alpha: float = 0.05) -> dict[str, Any]:
    """The multiplicity readout that must travel with any ranked hit count."""
    expected = expected_false_positives(n_tests, alpha)
    return {
        "n_tests": int(n_tests),
        "n_nominal_hits": int(n_hits),
        "alpha": float(alpha),
        "expected_false_positives": round(expected, 2),
        "exceeds_chance": bool(n_hits > expected),
        "note": (
            f"{n_hits} nominal hit(s) over {n_tests} test(s); chance alone yields "
            f"~{expected:.2f} at alpha={alpha}. "
            + ("The count exceeds chance." if n_hits > expected else
               "⛔ The count does NOT exceed chance — this panel supports no hit claim.")
        ),
    }
