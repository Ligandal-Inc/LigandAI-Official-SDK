# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Tests for the optional matplotlib fold charts (raw-preserving).

Chart rendering requires matplotlib (the ``viz`` extra). Each rendering test
skips cleanly when matplotlib is absent. The fold records below are explicit
literals representing plausible per-engine/per-seed outputs — never random fill.
The charts plot RAW values in native units; the only cross-engine footing is the
within-engine percentile annotation. No value is rescaled, and the cross-engine
linked-line chart never collapses engines onto a single shared raw y-axis.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ligandai.fold_charts import (
    FoldComparison,
    build_fold_comparison,
    distribution_figure,
    linked_line_figure,
)

# Explicit literal fold records: two sequences folded by three engines across
# several seeds. Boltz-2 raw values run higher (it inflates interface
# confidence) — the charts show that raw spread directly without correcting it.
_FOLD_RECORDS = [
    {"sequence": "ACDEFGHIK", "engine": "esmfold2", "seed": 0, "iptm": 0.46, "ipsae": 0.51, "plddt": 82.0, "delta_g": -8.1},
    {"sequence": "ACDEFGHIK", "engine": "esmfold2", "seed": 1, "iptm": 0.49, "ipsae": 0.55, "plddt": 84.0, "delta_g": -8.4},
    {"sequence": "ACDEFGHIK", "engine": "protenix", "seed": 0, "iptm": 0.48, "ipsae": 0.50, "plddt": 80.0, "delta_g": -7.9},
    {"sequence": "ACDEFGHIK", "engine": "protenix", "seed": 1, "iptm": 0.44, "ipsae": 0.47, "plddt": 79.0, "delta_g": -7.6},
    {"sequence": "ACDEFGHIK", "engine": "boltz2", "seed": 0, "iptm": 0.71, "ipsae": 0.70, "plddt": 83.0, "delta_g": -8.6},
    {"sequence": "ACDEFGHIK", "engine": "boltz2", "seed": 1, "iptm": 0.69, "ipsae": 0.68, "plddt": 82.0, "delta_g": -8.2},
    {"sequence": "LMNPQRSTV", "engine": "esmfold2", "seed": 0, "iptm": 0.33, "ipsae": 0.38, "plddt": 70.0, "delta_g": -5.1},
    {"sequence": "LMNPQRSTV", "engine": "boltz2", "seed": 0, "iptm": 0.58, "ipsae": 0.61, "plddt": 72.0, "delta_g": -5.4},
]

# A larger single-engine cohort so a within-engine distribution can form (>= the
# MIN_DISTRIBUTION_SAMPLES floor). Distinct sequences, one esmfold2 fold each.
_ESM_COHORT = [
    {"sequence": f"SEQ{index:02d}", "engine": "esmfold2", "iptm": 0.30 + 0.02 * index}
    for index in range(12)
]


def test_build_fold_comparison_groups_by_sequence_engine_seed() -> None:
    """Normalization preserves per-seed rows and resolves engines/metrics."""
    comparison = build_fold_comparison(_FOLD_RECORDS)
    assert isinstance(comparison, FoldComparison)
    assert set(comparison.engines()) == {"esmfold2", "protenix", "boltz2"}
    assert comparison.sequences() == ["ACDEFGHIK", "LMNPQRSTV"]
    # Three esmfold2 ipTM raws (2 for seq1 + 1 for seq2).
    assert len(comparison.raw_values("esmfold2", "iptm")) == 3
    # per_engine_raw averages the seeds into one RAW value per engine.
    per_engine = comparison.per_engine_raw("ACDEFGHIK", "iptm")
    assert per_engine["esmfold2"] == pytest.approx((0.46 + 0.49) / 2)
    assert per_engine["boltz2"] == pytest.approx((0.71 + 0.69) / 2)
    # Boltz-2's raw is higher than esmfold2's — shown directly, not corrected.
    assert per_engine["boltz2"] > per_engine["esmfold2"]


def test_build_fold_comparison_passthrough() -> None:
    """A FoldComparison passed in is returned unchanged."""
    comparison = build_fold_comparison(_FOLD_RECORDS)
    assert build_fold_comparison(comparison) is comparison


def test_distributions_are_within_engine_and_data_derived() -> None:
    """Within-engine distributions come from the cohort's own raw folds."""
    comparison = build_fold_comparison(_ESM_COHORT, metrics=["iptm"])
    distributions = comparison.distributions(["iptm"])
    # 12 esmfold2 folds -> a usable distribution; below the floor -> None.
    assert distributions.n("esmfold2", "iptm") == 12
    # A high raw sits high within esmfold2's own distribution.
    top = distributions.percentile("esmfold2", "iptm", 0.52)
    assert top is not None and top > 0.8
    # An engine with no folds has no percentile (no synthetic fill).
    assert distributions.percentile("boltz2", "iptm", 0.52) is None


def test_distribution_figure_returns_four_panels_and_saves(tmp_path: Path) -> None:
    """4-panel distribution figure renders RAW values and writes a file."""
    pytest.importorskip("matplotlib")
    save_path = tmp_path / "dist.png"
    fig = distribution_figure(_FOLD_RECORDS, save_path=save_path)
    try:
        # 4 requested metrics -> 4 populated axes (2x2 grid).
        populated = [ax for ax in fig.axes if ax.get_title()]
        assert len(populated) == 4
        # The ipTM panel's plotted data must reach Boltz-2's raw ~0.71 — proof
        # the axis is native/raw, not a 0..1 calibrated scale.
        iptm_ax = next(ax for ax in populated if ax.get_title().lower().startswith("iptm"))
        _, top = iptm_ax.get_ylim()
        assert top > 0.6
        assert save_path.exists()
        assert save_path.stat().st_size > 0
    finally:
        import matplotlib.pyplot as plt

        plt.close(fig)


def test_distribution_figure_by_engine(tmp_path: Path) -> None:
    """by='engine' yields one panel per engine."""
    pytest.importorskip("matplotlib")
    fig = distribution_figure(_FOLD_RECORDS, by="engine")
    try:
        populated = [ax for ax in fig.axes if ax.get_title()]
        assert len(populated) == 3
    finally:
        import matplotlib.pyplot as plt

        plt.close(fig)


def test_linked_line_uses_independent_raw_axes_not_shared(tmp_path: Path) -> None:
    """Cross-engine linked lines use ONE host with per-engine native ranges.

    There must be exactly one axes (no shared raw y-axis collapsing the engines),
    and the host's y-axis must be the normalized-fraction frame (0..1) used to
    overlay independent native ranges — never a shared raw axis nor a calibrated
    one. Per-engine native ranges differ (Boltz-2 runs higher).
    """
    pytest.importorskip("matplotlib")
    import matplotlib.pyplot as plt

    for mode in ("per-sequence", "aggregate", "best"):
        save_path = tmp_path / f"linked_{mode}.png"
        fig = linked_line_figure(_FOLD_RECORDS, metric="iptm", mode=mode, save_path=save_path)
        try:
            # Single host axes -> engines are not stacked on a shared raw axis.
            assert len(fig.axes) == 1
            host = fig.axes[0]
            # The host frame is the normalized 0..1 overlay, NOT raw and NOT a
            # 0..1 *calibrated* score (it carries no raw y tick labels).
            assert host.get_ylim() == (0.0, 1.0)
            assert host.get_yticks().tolist() == []
            assert save_path.exists()
        finally:
            plt.close(fig)


def test_linked_line_independent_axes_preserve_native_ranges() -> None:
    """Each engine's native raw range is preserved independently (no rescale)."""
    pytest.importorskip("matplotlib")
    import matplotlib.pyplot as plt

    comparison = build_fold_comparison(_FOLD_RECORDS, metrics=["iptm"])
    esm_vals = comparison.raw_values("esmfold2", "iptm")
    boltz_vals = comparison.raw_values("boltz2", "iptm")
    # The engines occupy different native raw ranges — the chart shows that.
    assert max(boltz_vals) > max(esm_vals)
    fig = linked_line_figure(_FOLD_RECORDS, metric="iptm", mode="per-sequence")
    try:
        assert len(fig.axes) == 1
    finally:
        plt.close(fig)


def test_charts_raise_clear_error_without_matplotlib(monkeypatch: pytest.MonkeyPatch) -> None:
    """When matplotlib is unavailable the user gets an actionable ImportError."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name.startswith("matplotlib"):
            raise ImportError("No module named 'matplotlib'")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ImportError, match=r"ligandai\[viz\]"):
        distribution_figure(_FOLD_RECORDS)
