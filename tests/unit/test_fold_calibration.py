# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Lock the per-engine fold-score standing contract — RAW is never adjusted.

These tests pin the behaviors the charts and the comparison dashboard rely on:
raw values are returned byte-identical by :func:`standing` / :func:`engine_agreement`;
Boltz-2 inflation shows up as a LOWER within-engine percentile for the same-
looking-high raw; DeltaForge dG is per-engine (the same raw dG lands at different
percentiles across engines with different dG distributions); cohorts below
``MIN_DISTRIBUTION_SAMPLES`` yield percentile ``None`` (no synthetic fill); and
agreement is decided on percentile spread.

All fixtures are explicit literal fold records representing plausible real
outputs — never random fill.
"""

from __future__ import annotations

from ligandai.fold_calibration import (
    MIN_DISTRIBUTION_SAMPLES,
    build_distributions,
    engine_agreement,
    standing,
)

# A 20-sequence cohort matching the verified semantics in the locked contract.
# esmfold2 / protenix run lower; boltz2 runs HIGH (it inflates interface
# confidence). The raws are explicit literals, not generated fill. esmfold2 0.50
# and protenix 0.45 sit at the TOP of their own cohorts (within-engine pct
# ~0.975), while boltz2 0.71 is only MID within its inflated cohort (~0.525) —
# the same-looking-high Boltz-2 raw has the LOWER within-engine standing.
_ESM = [0.30, 0.32, 0.34, 0.36, 0.38, 0.40, 0.41, 0.42, 0.43, 0.44,
        0.45, 0.46, 0.47, 0.48, 0.485, 0.49, 0.492, 0.495, 0.498, 0.50]
_PROT = [0.26, 0.28, 0.30, 0.32, 0.34, 0.36, 0.37, 0.38, 0.39, 0.40,
         0.41, 0.42, 0.43, 0.44, 0.445, 0.447, 0.448, 0.449, 0.4495, 0.45]
_BOLTZ = [0.55, 0.58, 0.60, 0.62, 0.64, 0.66, 0.67, 0.68, 0.69, 0.70,
          0.71, 0.72, 0.73, 0.74, 0.75, 0.78, 0.80, 0.82, 0.84, 0.86]


def _iptm_distributions() -> object:
    records: list[dict[str, object]] = []
    for engine, raws in (("esmfold2", _ESM), ("protenix", _PROT), ("boltz2", _BOLTZ)):
        records.extend({"engine": engine, "iptm": raw} for raw in raws)
    return build_distributions(records, metrics=("iptm",))


def test_standing_returns_raw_unchanged() -> None:
    """standing carries the raw value byte-identical; it never adjusts it."""
    distributions = _iptm_distributions()
    info = standing(distributions, "boltz2", "iptm", 0.71)
    assert info is not None
    assert info.raw == 0.71  # exactly as supplied
    assert info.engine == "boltz2"
    assert info.percentile is not None


def test_boltz2_inflation_is_lower_within_engine_percentile() -> None:
    """A high-looking Boltz-2 raw sits lower within its own (inflated) cohort.

    Boltz-2 raw 0.71 is the median-ish of its high cohort, while esmfold2 raw
    0.50 and protenix 0.45 sit high within their lower cohorts — so the same-
    looking-high Boltz-2 value has the LOWER within-engine percentile.
    """
    distributions = _iptm_distributions()
    boltz = standing(distributions, "boltz2", "iptm", 0.71)
    esm = standing(distributions, "esmfold2", "iptm", 0.50)
    prot = standing(distributions, "protenix", "iptm", 0.45)
    assert boltz is not None and esm is not None and prot is not None
    assert boltz.percentile is not None and esm.percentile is not None
    # Boltz-2's higher raw lands at a lower within-engine percentile.
    assert boltz.percentile < esm.percentile
    assert esm.percentile > 0.8  # near the top of esmfold2's own cohort


def test_engine_agreement_decides_on_percentile_spread() -> None:
    """A wide raw spread that maps to a wide percentile spread => disagreement.

    Per the verified semantics: boltz2 raw 0.71 lands ~70th within its cohort
    while esmfold2 0.50 and protenix 0.45 land ~97th within theirs — so the
    engines DISAGREE on standing, with boltz2 worst and esmfold2/protenix best.
    Raw values are carried through unchanged.
    """
    distributions = _iptm_distributions()
    agreement = engine_agreement(
        {"esmfold2": 0.50, "protenix": 0.45, "boltz2": 0.71},
        "iptm",
        distributions,
    )
    assert agreement is not None
    # Raw carried through byte-identical.
    assert agreement.raw == {"esmfold2": 0.50, "protenix": 0.45, "boltz2": 0.71}
    assert agreement.agree is False
    assert agreement.best_engine == "esmfold2"
    assert agreement.worst_engine == "boltz2"
    assert agreement.percentile_spread > 0.0


def test_agreement_can_agree_despite_raw_spread() -> None:
    """Engines at similar within-engine standing AGREE even if raws differ.

    Pick a value near the TOP of each engine's own cohort: esmfold2 0.54,
    protenix 0.52, boltz2 0.86 — a wide raw spread, but each sits at the top
    of its own distribution, so they agree on standing.
    """
    distributions = _iptm_distributions()
    agreement = engine_agreement(
        {"esmfold2": 0.54, "protenix": 0.52, "boltz2": 0.86},
        "iptm",
        distributions,
    )
    assert agreement is not None
    assert agreement.raw_spread > 0.25  # wide raw spread
    assert agreement.percentile_spread <= 0.25  # tight standing spread
    assert agreement.agree is True


def test_deltaforge_dg_is_per_engine() -> None:
    """Same raw dG lands at different within-engine percentiles across engines.

    esmfold2's dG cohort centers more favorable (more negative) than boltz2's,
    so a raw dG of -9.0 is high standing on esmfold2 but low standing on boltz2
    (dG: lower/more-negative is better). The raw is preserved.
    """
    # esmfold2 dG cohort runs favorable (centered ~ -9.5); boltz2 runs ~ -8.0.
    esm_dg = [-7.0, -7.5, -8.0, -8.5, -9.0, -9.5, -10.0, -10.5, -11.0, -11.5]
    boltz_dg = [-5.0, -5.5, -6.0, -6.5, -7.0, -7.5, -8.0, -8.5, -9.0, -9.5]
    records: list[dict[str, object]] = []
    records.extend({"engine": "esmfold2", "deltaforge": v} for v in esm_dg)
    records.extend({"engine": "boltz2", "deltaforge": v} for v in boltz_dg)
    distributions = build_distributions(records, metrics=("deltaforge",))

    esm = standing(distributions, "esmfold2", "deltaforge", -9.0)
    boltz = standing(distributions, "boltz2", "deltaforge", -9.0)
    assert esm is not None and boltz is not None
    assert esm.raw == -9.0 and boltz.raw == -9.0  # raw unchanged, per engine
    assert esm.percentile is not None and boltz.percentile is not None
    # -9.0 is mid/low standing on the favorable esmfold2 cohort but high on the
    # less-favorable boltz2 cohort — per-engine, not a shared band.
    assert boltz.percentile > esm.percentile


def test_below_floor_yields_no_percentile() -> None:
    """An engine below MIN_DISTRIBUTION_SAMPLES gets raw but no percentile."""
    records = [{"engine": "esmfold2", "iptm": 0.5} for _ in range(MIN_DISTRIBUTION_SAMPLES - 1)]
    distributions = build_distributions(records, metrics=("iptm",))
    info = standing(distributions, "esmfold2", "iptm", 0.5)
    assert info is not None
    assert info.raw == 0.5  # raw still shown
    assert info.percentile is None  # no synthetic fill
    assert info.label is None
    assert not info.ok
    assert info.n == MIN_DISTRIBUTION_SAMPLES - 1


def test_missing_value_returns_no_percentile() -> None:
    """A non-finite / missing raw never fabricates a percentile."""
    distributions = _iptm_distributions()
    assert distributions.percentile("esmfold2", "iptm", None) is None
    assert distributions.percentile("esmfold2", "iptm", float("nan")) is None
