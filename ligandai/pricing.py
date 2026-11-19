# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Local pricing mirrors for offline budgets; server quotes are authoritative.

A default fold (4 trajectories, 50 steps) costs 20 credits ($0.20): 5 per
trajectory, with a 20-credit minimum PER FOLD. Steps above 50 scale the rate
by ``ceil(steps / 50)``. One credit remains one cent.

PTF generation is 10 credits per peptide, plus 5 each for immunogenicity and
serum-stability guidance, capped at 20 per peptide. Quality guidance and
LigandIQ fold ranking add zero. There is no volume discount or PTF setup fee.
The standalone LigandForge setup fee does not belong in these PTF estimates.

Standalone LigandIQ scoring costs 5 per peptide with a 20-credit minimum PER
JOB. This floor is not a generation surcharge and does not apply to every
other route that uses the nominal scoring rate. Campaign rates are optimistic
budget inputs, not billing receipts; scoring fan-out and route minima can add
cost. Ensemble co-fold prices come from the server, not from this module.

Use ``client.peptides.estimate_cost()`` or ``client.synthesis.estimate_cost()``
for generation/folding quotes and ``client.ensemble.estimate()`` for ensembles.
Submitted jobs and the server ledger determine actual account charges.
"""

from __future__ import annotations

__all__ = [
    "CREDITS_PER_USD",
    "TRAJECTORIES_DEFAULT",
    "CREDITS_PER_FOLD_DEFAULT",
    "CREDITS_PER_TRAJECTORY",
    "FOLD_MIN_CREDITS",
    "FOLD_STEP_BASE",
    "CREDITS_PER_GENERATED_PEPTIDE",
    "CREDITS_PER_SCORED_DESIGN",
    "fold_step_multiplier",
    "fold_credits",
    "fold_batch_credits",
    "credits_to_usd",
    "GENERATION_GUIDANCE_SURCHARGE",
    "GENERATION_PER_PEPTIDE_CAP",
    "SCORE_MIN_CHARGE_PEPTIDES",
    "SCORE_MIN_CHARGE",
    "generation_credits",
    "ligandiq_score_credits",
]

#: Platform rate: 100 credits = $1.00, i.e. 1 credit = 1 cent. Unchanged.
CREDITS_PER_USD: int = 100

#: Boltz-2 diffusion trajectories per fold when the caller does not say.
TRAJECTORIES_DEFAULT: int = 4

#: Credits for ONE default fold (4 trajectories, 50 sampling steps): $0.20.
#: Mirrors server FOLD_DEFAULT_CREDITS = FOLD_PER_TRAJECTORY (5) x 4 (shared/forge-pricing.ts).
CREDITS_PER_FOLD_DEFAULT: int = 20

#: Minimum credits for any one fold. Equals the default price, so folding with
#: fewer trajectories than the default is not cheaper.
FOLD_MIN_CREDITS: int = CREDITS_PER_FOLD_DEFAULT

#: Credits per trajectory (list rate): 5. Derived, never typed.
CREDITS_PER_TRAJECTORY: float = CREDITS_PER_FOLD_DEFAULT / TRAJECTORIES_DEFAULT

#: Sampling steps the base price is quoted at.
FOLD_STEP_BASE: int = 50

#: Peptide generation, credits per displayed peptide. Not part of the fold price.
CREDITS_PER_GENERATED_PEPTIDE: int = 10  # server GENERATION_BASE_PER_PEPTIDE ($0.10), flat, no volume ladder

#: DeltaForge / LigandIQ scoring, credits per design. Not part of the fold price.
#: Mirrors server LIGANDIQ_PER_PEPTIDE_SCORE (DeltaForge imports the same value).
CREDITS_PER_SCORED_DESIGN: int = 5


GENERATION_GUIDANCE_SURCHARGE: int = 5
GENERATION_PER_PEPTIDE_CAP: int = 20
SCORE_MIN_CHARGE_PEPTIDES: int = 4
SCORE_MIN_CHARGE: int = SCORE_MIN_CHARGE_PEPTIDES * CREDITS_PER_SCORED_DESIGN


def generation_credits(
    count: int,
    *,
    immunogenicity: bool = False,
    serum_stability: bool = False,
    quality_guided: bool = False,
    ligandiq_fold_ranking: bool = False,
) -> int:
    """PTF generation only; no setup fee, folding or standalone scoring.

    ``quality_guided`` and ``ligandiq_fold_ranking`` are included at no extra
    charge. The cap is applied per peptide before multiplying by the count.
    """
    rate = CREDITS_PER_GENERATED_PEPTIDE + GENERATION_GUIDANCE_SURCHARGE * (
        int(bool(immunogenicity)) + int(bool(serum_stability))
    )
    return max(0, int(count)) * min(GENERATION_PER_PEPTIDE_CAP, rate)


def ligandiq_score_credits(count: int) -> int:
    """One standalone LigandIQ scoring job, with its per-job minimum.

    Zero inputs mean no job. Independent jobs each pay their own minimum.
    """
    n = max(0, int(count))
    return max(SCORE_MIN_CHARGE, n * CREDITS_PER_SCORED_DESIGN) if n else 0


def fold_step_multiplier(sampling_steps: int | None = None) -> int:
    """Cost multiplier for ``sampling_steps``: ``max(1, ceil(steps / 50))``.

    ``None`` / ``0`` mean "the default" and cost 1x. Fewer than 50 steps is never
    a discount.
    """
    steps = int(sampling_steps) if sampling_steps else FOLD_STEP_BASE
    return max(1, -(-steps // FOLD_STEP_BASE))


def fold_credits(
    trajectories: int | None = None,
    sampling_steps: int | None = None,
) -> int:
    """Local estimate of one fold, in whole credits.

    ``max(FOLD_MIN_CREDITS, ceil(trajectories * 5 * step_multiplier))`` —
    computed in integers so the rate never meets a float rounding edge. Rounds UP: a
    pre-flight check should over-state by a credit, never under-state.
    """
    n = int(trajectories) if trajectories else TRAJECTORIES_DEFAULT
    n = max(1, n)
    mult = fold_step_multiplier(sampling_steps)
    scaled = -(-(n * mult * CREDITS_PER_FOLD_DEFAULT) // TRAJECTORIES_DEFAULT)
    return max(FOLD_MIN_CREDITS, scaled)


def fold_batch_credits(
    count: int,
    trajectories: int | None = None,
    sampling_steps: int | None = None,
) -> int:
    """``count`` independent folds. The minimum applies PER FOLD, so this is
    ``count * fold_credits(...)``, never a floor on the batch total."""
    return max(0, int(count)) * fold_credits(trajectories, sampling_steps)


def credits_to_usd(credits: float) -> float:
    """Dollar value of ``credits`` at the platform rate (1 credit = 1 cent)."""
    return round(credits / CREDITS_PER_USD, 2)
