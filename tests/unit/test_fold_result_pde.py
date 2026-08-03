# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""FoldResult must surface PDE (predicted distance error) WITH its provenance.

Regression cover for bd-LIGANDAI_ALPHA_V2-du0xv (migration 170).

THE RULES, in the same spirit as the peptide_ipsae anti-coalescing cover:

1. ``interface_pde_min`` is the interface measurement. It must NEVER be
   back-filled from ``gpde`` or ``interface_gpde`` — those are complex-level
   statistics, so substituting one reports a different quantity under a name
   that promises the interface. A missing value means "not computed", not zero.

2. A PDE number is uninterpretable without ``pde_engine`` / ``pde_kind`` /
   ``pde_scale``. Boltz-2's complex_pde/complex_ipde scale has never been
   verified against the Angstrom-scale gPDE that OpenDDE/Protenix emit, and a
   PAE-derived proxy is a different quantity from a native token_pair_pde read.
   The tags therefore travel with the value everywhere it is surfaced.

3. Cross-engine ranking keys on ``pde_rankable`` only — non-None if and only if
   ``pde_kind == 'native'`` AND ``pde_scale == 'angstrom'``. The flat
   ``metrics`` dict, which strips tags by construction, carries that key and
   nothing else PDE-shaped.

4. LOWER IS BETTER, the opposite direction to iPSAE/iPTM/pLDDT.
"""

from __future__ import annotations

import pytest

from ligandai.resources.peptides import _parse_fold
from ligandai.types import PdeScores


# ─── Basic surfacing ────────────────────────────────────────────────────────


def test_parse_fold_surfaces_pde_with_tags() -> None:
    fold = _parse_fold(
        {
            "jobId": "fold_pde_ok",
            "status": "completed",
            "ipsae": 0.93,
            "gpde": 3.36,
            "interfaceGpde": 4.10,
            "interfacePdeMin": 1.82,
            "pdeRankable": 1.82,
            "pdeEngine": "protenix",
            "pdeKind": "native",
            "pdeScale": "angstrom",
        }
    )
    assert fold.gpde == pytest.approx(3.36)
    assert fold.interface_gpde == pytest.approx(4.10)
    assert fold.interface_pde_min == pytest.approx(1.82)
    assert fold.pde_rankable == pytest.approx(1.82)
    assert fold.pde_engine == "protenix"
    assert fold.pde_kind == "native"
    assert fold.pde_scale == "angstrom"
    # ipSAE still surfaced alongside — PDE is an additional signal, not a swap.
    assert fold.ipsae == pytest.approx(0.93)


def test_parse_fold_accepts_snake_case_aliases() -> None:
    """Server payloads vary between camelCase and snake_case; both must parse."""
    fold = _parse_fold(
        {
            "jobId": "fold_pde_snake",
            "interface_pde_min": 2.4,
            "interface_gpde": 3.1,
            "pde_engine": "opendde",
            "pde_kind": "native",
            "pde_scale": "angstrom",
        }
    )
    assert fold.interface_pde_min == pytest.approx(2.4)
    assert fold.interface_gpde == pytest.approx(3.1)
    assert fold.pde_engine == "opendde"


def test_parse_fold_reads_pde_from_nested_result_node() -> None:
    """_parse_fold probes payload / result / output_data in priority order."""
    fold = _parse_fold(
        {
            "jobId": "fold_nested",
            "result": {
                "interfacePdeMin": 1.55,
                "pdeEngine": "protenix",
                "pdeKind": "native",
                "pdeScale": "angstrom",
            },
        }
    )
    assert fold.interface_pde_min == pytest.approx(1.55)
    assert fold.pde is not None
    assert fold.pde.cross_engine_rank_value == pytest.approx(1.55)


# ─── ⛔ Anti-coalescing ─────────────────────────────────────────────────────


def test_interface_pde_min_never_falls_back_to_gpde() -> None:
    """⛔ The core anti-regression. A fold with a complex-level gPDE but no
    interface measurement must report None for the interface value."""
    fold = _parse_fold(
        {
            "jobId": "fold_complex_only",
            "gpde": 3.36,
            "pdeEngine": "protenix",
            "pdeKind": "native",
            "pdeScale": "angstrom",
        }
    )
    assert fold.interface_pde_min is None, (
        "interface_pde_min must stay None when not computed — back-filling it "
        "from the complex-level gpde reports a whole-complex statistic under a "
        "name that promises the binder<->receptor interface"
    )
    assert fold.gpde == pytest.approx(3.36)
    # ...and the cross-engine value must not be conjured from gpde either.
    assert fold.pde is not None
    assert fold.pde.cross_engine_rank_value is None


def test_interface_pde_min_never_falls_back_to_interface_gpde() -> None:
    """interface_gpde is the interface MEAN; interface_pde_min is the MIN. The
    mean scored AUROC 0.755 against the min's 0.806 on the 199-fold holdout —
    they are different measurements and one is not a stand-in for the other."""
    fold = _parse_fold(
        {
            "jobId": "fold_mean_only",
            "interfaceGpde": 4.10,
            "pdeEngine": "protenix",
            "pdeKind": "native",
            "pdeScale": "angstrom",
        }
    )
    assert fold.interface_pde_min is None
    assert fold.interface_gpde == pytest.approx(4.10)


def test_zero_pde_is_preserved_not_treated_as_missing() -> None:
    """A perfectly-predicted contact scores a genuine 0.0 Å — a real
    measurement, not an absence."""
    fold = _parse_fold(
        {
            "jobId": "fold_zero_pde",
            "interfacePdeMin": 0.0,
            "pdeEngine": "protenix",
            "pdeKind": "native",
            "pdeScale": "angstrom",
        }
    )
    assert fold.interface_pde_min == pytest.approx(0.0)
    assert fold.pde is not None
    assert fold.pde.cross_engine_rank_value == pytest.approx(0.0)


def test_fold_without_pde_reports_none_everywhere() -> None:
    fold = _parse_fold({"jobId": "fold_no_pde", "ipsae": 0.93, "iptm": 0.88})
    assert fold.gpde is None
    assert fold.interface_pde_min is None
    assert fold.pde_rankable is None
    assert fold.pde is None, "no PDE reported must yield no PDE block at all"
    assert fold.pde_interpretation is None


# ─── ⛔ Cross-engine gate ───────────────────────────────────────────────────


def test_proxy_kind_is_not_cross_engine_rankable() -> None:
    """A PAE-derived surrogate is a different quantity from a native
    token_pair_pde read; it must never enter a cross-engine ordering."""
    fold = _parse_fold(
        {
            "jobId": "fold_proxy",
            "interfacePdeMin": 2.1,
            "pdeEngine": "esmfold2",
            "pdeKind": "proxy",
            "pdeScale": "angstrom",
        }
    )
    assert fold.interface_pde_min == pytest.approx(2.1)
    assert fold.pde_rankable is None
    assert fold.pde is not None
    assert fold.pde.is_cross_engine_comparable is False
    assert fold.pde.cross_engine_rank_value is None


def test_unverified_scale_is_not_cross_engine_rankable() -> None:
    """Boltz-2's complex_pde/complex_ipde scale is not established against the
    Angstrom-scale gPDE OpenDDE/Protenix emit, so it cannot be compared."""
    fold = _parse_fold(
        {
            "jobId": "fold_boltz2",
            "interfacePdeMin": 1.4,
            "pdeEngine": "boltz2",
            "pdeKind": "native",
            "pdeScale": "unverified",
        }
    )
    assert fold.interface_pde_min == pytest.approx(1.4)
    assert fold.pde_rankable is None
    assert fold.pde is not None
    assert fold.pde.cross_engine_rank_value is None
    # The raw value is still reachable — legitimate within a single engine.
    assert fold.pde.interface_pde_min == pytest.approx(1.4)
    assert fold.pde.engine == "boltz2"


def test_rankable_is_recomputed_on_payloads_predating_the_column() -> None:
    """Older payloads carry the components but no generated column. The
    native+angstrom rule is applied here rather than left to the caller."""
    fold = _parse_fold(
        {
            "jobId": "fold_legacy",
            "interfacePdeMin": 1.9,
            "pdeEngine": "protenix",
            "pdeKind": "native",
            "pdeScale": "angstrom",
        }
    )
    assert fold.pde_rankable == pytest.approx(1.9)


def test_untagged_pde_is_never_promoted_to_rankable() -> None:
    """A PDE number with no kind/scale is uninterpretable. Absent tags must not
    be optimistically read as native+angstrom."""
    fold = _parse_fold({"jobId": "fold_untagged", "interfacePdeMin": 1.7})
    assert fold.interface_pde_min == pytest.approx(1.7)
    assert fold.pde_rankable is None
    assert fold.pde is not None
    assert fold.pde.is_cross_engine_comparable is False
    assert fold.pde.cross_engine_rank_value is None


# ─── metrics dict ──────────────────────────────────────────────────────────


def test_metrics_carries_only_the_self_qualifying_pde_scalar() -> None:
    """`metrics` is a flat dict[str, float] that strips tags by construction, so
    only pde_rankable — non-None iff native+angstrom — may live there."""
    fold = _parse_fold(
        {
            "jobId": "fold_metrics_pde",
            "ipsae": 0.93,
            "gpde": 3.36,
            "interfaceGpde": 4.10,
            "interfacePdeMin": 1.82,
            "pdeEngine": "protenix",
            "pdeKind": "native",
            "pdeScale": "angstrom",
        }
    )
    assert fold.metrics is not None
    assert fold.metrics["pde_rankable"] == pytest.approx(1.82)
    for stripped in ("gpde", "interface_gpde", "interface_pde_min"):
        assert stripped not in fold.metrics, (
            f"{stripped} must NOT be back-filled into the flat metrics dict — "
            "there it would carry no engine/kind/scale, and a caller comparing "
            "it across engines would be mixing incompatible scales"
        )


def test_metrics_omits_pde_entirely_for_unverified_scale() -> None:
    fold = _parse_fold(
        {
            "jobId": "fold_metrics_boltz2",
            "iptm": 0.88,
            "interfacePdeMin": 1.4,
            "pdeEngine": "boltz2",
            "pdeKind": "native",
            "pdeScale": "unverified",
        }
    )
    assert fold.metrics is not None
    assert "pde_rankable" not in fold.metrics
    assert "interface_pde_min" not in fold.metrics
    # iPTM still present — only the PDE keys are withheld.
    assert fold.metrics["iptm"] == pytest.approx(0.88)


# ─── Direction + units ─────────────────────────────────────────────────────


def test_direction_is_stated_not_inferred() -> None:
    """PDE runs opposite to ipSAE/iPTM/pLDDT, so the direction is on the wire."""
    fold = _parse_fold(
        {
            "jobId": "fold_direction",
            "interfacePdeMin": 1.82,
            "pdeEngine": "protenix",
            "pdeKind": "native",
            "pdeScale": "angstrom",
        }
    )
    assert fold.pde_interpretation == "lower_is_better"
    assert fold.pde_units == "angstrom"


def test_units_reflect_unverified_scale() -> None:
    fold = _parse_fold(
        {
            "jobId": "fold_units_unverified",
            "interfacePdeMin": 1.4,
            "pdeEngine": "boltz2",
            "pdeKind": "native",
            "pdeScale": "unverified",
        }
    )
    assert fold.pde_units == "unverified"
    assert fold.pde_interpretation == "lower_is_better"


def test_server_supplied_interpretation_wins() -> None:
    fold = _parse_fold(
        {
            "jobId": "fold_server_tags",
            "interfacePdeMin": 1.82,
            "pdeKind": "native",
            "pdeScale": "angstrom",
            "pdeInterpretation": "lower_is_better",
            "pdeUnits": "angstrom",
        }
    )
    assert fold.pde_interpretation == "lower_is_better"
    assert fold.pde_units == "angstrom"


# ─── PdeScores model ───────────────────────────────────────────────────────


def test_pde_scores_bundles_value_with_tags() -> None:
    block = PdeScores(
        interfacePdeMin=1.82,
        pdeEngine="protenix",
        pdeKind="native",
        pdeScale="angstrom",
    )
    assert block.is_cross_engine_comparable is True
    assert block.cross_engine_rank_value == pytest.approx(1.82)
    assert bool(block) is True


def test_pde_scores_is_falsy_when_no_number_present() -> None:
    """Tags with no measurement are not a PDE result."""
    block = PdeScores(pdeEngine="protenix", pdeKind="native", pdeScale="angstrom")
    assert bool(block) is False


def test_pde_scores_accepts_field_names_as_well_as_aliases() -> None:
    block = PdeScores(interface_pde_min=2.0, kind="native", scale="angstrom")
    assert block.cross_engine_rank_value == pytest.approx(2.0)


# ─── search() request mapping ──────────────────────────────────────────────


def test_search_signature_exposes_pde_filters() -> None:
    """pde_max / pde_engine must reach the wire as snake_case query params, and
    `sort='pde'` must be a documented key. Signature-level cover: the mapping is
    hand-written per call site, so a param added to the docstring but not to the
    params dict would ship as a silent no-op."""
    import inspect

    from ligandai.resources.peptides import Peptides

    sig = inspect.signature(Peptides.search)
    assert "pde_max" in sig.parameters
    assert "pde_engine" in sig.parameters

    src = inspect.getsource(Peptides.search)
    assert 'params["pde_max"] = pde_max' in src
    assert 'params["pde_engine"] = pde_engine' in src


def test_async_search_signature_matches_sync() -> None:
    import inspect

    from ligandai.resources.peptides import AsyncPeptides

    sig = inspect.signature(AsyncPeptides.search)
    assert "pde_max" in sig.parameters
    assert "pde_engine" in sig.parameters
    src = inspect.getsource(AsyncPeptides.search)
    assert 'params["pde_max"] = pde_max' in src
    assert 'params["pde_engine"] = pde_engine' in src
