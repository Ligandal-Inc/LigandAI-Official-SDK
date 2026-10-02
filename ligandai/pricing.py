# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Fold pricing — the ONE place the SDK states what a fold costs.

Since 2026-10-02 a fold costs **10 credits ($0.10)** at the default 4
trajectories. It cost 100 credits ($1.00) before. ``1 credit = 1 cent``
(:data:`CREDITS_PER_USD` = 100), and that conversion did not change.

Structure (carried over unchanged, with the base reduced):

* the price is PER FOLD and scales with the trajectory count, proportionally —
  :data:`CREDITS_PER_TRAJECTORY` is derived as
  ``CREDITS_PER_FOLD_DEFAULT / TRAJECTORIES_DEFAULT`` (2.5 credits);
* a fold never costs less than :data:`FOLD_MIN_CREDITS` (the default price), so a
  single-trajectory fold costs the same 10 credits as a 4-trajectory one;
* ``sampling_steps`` above :data:`FOLD_STEP_BASE` multiply the cost by
  ``ceil(steps / 50)`` — 100 steps is 2x, 200 steps is 4x;
* peptide GENERATION (:data:`CREDITS_PER_GENERATED_PEPTIDE`) and DeltaForge
  scoring (:data:`CREDITS_PER_SCORED_DESIGN`) are priced separately and were
  not touched.

⛔ THE PLATFORM IS AUTHORITATIVE. Everything here is a LOCAL FALLBACK for
offline arithmetic — the pre-flight balance check, ``CampaignSpec`` budget
validation, docstring and example figures. Anything that can ask the server
should: ``client.peptides.estimate_cost()`` (``GET /api/billing/estimate``),
``client.ensemble.estimate()`` (``POST /api/v1/cofold/estimate``) and the
``total_cost_credits`` a submitted batch returns are the numbers that get
charged. The ensemble co-fold is priced per engine by the server and is NOT
derived from this module at all.

Every figure in the README, the examples and the skills is derived from the
constants below; ``tests/unit/test_fold_pricing.py`` fails when they disagree.
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
]

#: Platform rate: 100 credits = $1.00, i.e. 1 credit = 1 cent. Unchanged.
CREDITS_PER_USD: int = 100

#: Boltz-2 diffusion trajectories per fold when the caller does not say.
TRAJECTORIES_DEFAULT: int = 4

#: Credits for ONE default fold (4 trajectories, 50 sampling steps): $0.10.
CREDITS_PER_FOLD_DEFAULT: int = 10

#: Minimum credits for any one fold. Equals the default price, so folding with
#: fewer trajectories than the default is not cheaper.
FOLD_MIN_CREDITS: int = CREDITS_PER_FOLD_DEFAULT

#: Credits per trajectory (list rate): 2.5. Derived, never typed.
CREDITS_PER_TRAJECTORY: float = CREDITS_PER_FOLD_DEFAULT / TRAJECTORIES_DEFAULT

#: Sampling steps the base price is quoted at.
FOLD_STEP_BASE: int = 50

#: Peptide generation, credits per displayed peptide. Not part of the fold price.
CREDITS_PER_GENERATED_PEPTIDE: int = 100

#: DeltaForge / LigandIQ scoring, credits per design. Not part of the fold price.
CREDITS_PER_SCORED_DESIGN: int = 25


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

    ``max(FOLD_MIN_CREDITS, ceil(trajectories * 2.5 * step_multiplier))`` —
    computed in integers so 2.5 never meets a float rounding edge. Rounds UP: a
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
