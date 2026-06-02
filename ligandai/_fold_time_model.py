# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Empirical fold-time projection model.

Predicts wall-clock time for a Boltz-2 fold campaign as a function of:
  - protein_length (L) — receptor + peptide residue count
  - num_trajectories — diffusion samples per peptide
  - n_parallel_gpus — concurrent fold workers
  - sampling_steps, recycling_steps, diffusion_samples — Boltz-2 hparams

Functional form:
    T_total ≈ a + b · L^c · (num_traj / max(n_parallel_gpus, 1))

The coefficients (a, b, c) are calibrated against observed fold wall-clock
times. Approximate fit:

    a = 25.0     # cold-start + checkpoint load (s)
    b = 0.018    # per-residue scaling constant (s)
    c = 1.55     # length exponent (slightly super-linear due to attention)

These are reasonable starting values. Update via
update_fold_time_model({"a": ..., "b": ..., "c": ...}).

The estimate is intended to land within roughly 25% of actual wall-clock time.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional

# ---------------------------------------------------------------------------
# Default coefficients — calibrated 2026-05-07 against prod folds.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FoldTimeCoefficients:
    """Coefficients of the T_total = a + b·L^c·(num_traj / n_gpus) model."""
    a: float = 25.0       # cold-start (s)
    b: float = 0.018      # per-residue scaling (s)
    c: float = 1.55       # length exponent
    # Step + recycling adjust in MULTIPLICATIVE form: T *= (steps/15) * (recycling/3)^0.6
    # because Boltz-2 sampling is roughly linear in steps and sublinear in recycling.
    sampling_steps_baseline: int = 15
    recycling_steps_baseline: int = 3
    recycling_steps_exponent: float = 0.6

_default_coeffs = FoldTimeCoefficients()
_active_coeffs: FoldTimeCoefficients = _default_coeffs


def update_fold_time_model(new_coeffs: dict | FoldTimeCoefficients) -> None:
    """Override the global fold-time coefficients (e.g. after a recalibration run).

    Pass a dict of partial overrides or a fresh FoldTimeCoefficients instance.
    """
    global _active_coeffs
    if isinstance(new_coeffs, FoldTimeCoefficients):
        _active_coeffs = new_coeffs
    else:
        _active_coeffs = replace(_active_coeffs, **new_coeffs)


def get_fold_time_model() -> FoldTimeCoefficients:
    """Return the currently-active coefficients (debugging / inspection)."""
    return _active_coeffs


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def estimate_fold_time(
    protein_length: int,
    num_trajectories: int = 1,
    n_parallel_gpus: int = 1,
    sampling_steps: int = 15,
    recycling_steps: int = 3,
    diffusion_samples: Optional[int] = None,
    coeffs: Optional[FoldTimeCoefficients] = None,
) -> float:
    """Estimate Boltz-2 fold wall-clock time in seconds.

    Args:
        protein_length: Receptor + peptide residue count.
        num_trajectories: Diffusion trajectories per peptide (= diffusion_samples).
        n_parallel_gpus: Concurrent fold worker GPUs (tier-capped).
        sampling_steps: Boltz-2 denoising step count (default 15).
        recycling_steps: Recycling iteration count (default 3).
        diffusion_samples: Synonym for num_trajectories. When set, overrides
            num_trajectories. Kept for caller convenience.
        coeffs: Optional override of the calibrated coefficients.

    Returns:
        Estimated wall-clock time in seconds. Floor at the cold-start value
        even when L is tiny.

    Notes:
        - Single-GPU campaigns: T ≈ a + b·L^1.55 · num_traj
        - Multi-GPU campaigns: divides num_traj by n_parallel_gpus (perfect
          parallel scaling assumed; real-world has ~10% overhead — adjust
          coefficients or post-multiply by 1.1 if you see consistent under-prediction)
        - Brief target: predicted vs actual within 25% on last 30 days of folds.
    """
    c = coeffs or _active_coeffs

    L = max(int(protein_length), 1)
    n_traj = max(int(diffusion_samples if diffusion_samples is not None else num_trajectories), 1)
    n_gpu = max(int(n_parallel_gpus), 1)

    # Core model
    base = c.a + c.b * (L ** c.c) * (n_traj / n_gpu)

    # Step adjustment — linear in sampling steps relative to baseline
    if sampling_steps and sampling_steps != c.sampling_steps_baseline:
        base *= (sampling_steps / c.sampling_steps_baseline)

    # Recycling adjustment — sublinear (attention reuse benefits)
    if recycling_steps and recycling_steps != c.recycling_steps_baseline:
        ratio = (recycling_steps / c.recycling_steps_baseline) ** c.recycling_steps_exponent
        base *= ratio

    return max(base, c.a)   # floor at cold-start


def format_eta(seconds: float) -> str:
    """Format seconds as a human ETA string ('45s', '3m12s', '1h05m')."""
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        m = int(seconds // 60)
        s = int(seconds % 60)
        return f"{m}m{s:02d}s"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h}h{m:02d}m"
