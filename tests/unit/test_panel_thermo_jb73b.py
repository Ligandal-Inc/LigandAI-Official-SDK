# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""The panel carries Kd beside ipSAE. bd-LIGANDAI_ALPHA_V2-jb73b.2.

Selectivity is ``min(off-target Kd) / on-target Kd``. A panel that reports only
``peptide_ipsae`` cannot express that sentence, so ``fold_panel`` was returning
rows the deliverable is not writable from.

These tests cover the capability and the eight ways carrying a second objective
goes silently wrong:

1. a computed ``0.0`` read through a truthiness test and dropped as "missing";
2. "below the ipSAE gate" and "never scored" collapsing into one empty cell;
3. the ipSAE argmax reused as the Kd argmin — they are different receptors and
   the Kd one is the risk;
4. a ratio minimised over a SUBSET of off-targets reported as a verdict;
5. a zero on-target Kd dividing into ``inf`` and reading as perfect selectivity;
6. a linear mean over log-distributed draws, which the weakest draw dominates;
7. paying to DeltaForge-score a fold the platform will refuse to quote;
8. ``affinity_quotable=False`` — a WITHHELD number — read as a zero or an error.

No network: the transport, the fold submitter, the metric surface and the
batch scorer are all stubbed, so the whole loop runs offline and burns nothing.
"""

from __future__ import annotations

import math

import pytest

from ligandai.resources.campaigns import (
    Campaigns,
    PanelLeg,
    PanelResult,
)

A = "ACDEFGHIKLMNP"
B = "WYVTSRQNKHGFE"

#: The gate the platform applies before DeltaForge is allowed to quote.
GATE = 0.67


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures — rows shaped exactly like the metrics object Worker A emits
# ─────────────────────────────────────────────────────────────────────────────


def _metrics(
    *,
    peptide_ipsae=0.80,
    peptide_ipsae_status="ok",
    kd_nm=10.0,
    delta_g=-8.5,
    kd_status="ok",
    kd_reason=None,
    kd_scorer="deltaforge_v10_2_unified_parallel",
    kd_scorer_version="10.2.0",
    peptide_length=14,
    scaffold_mediated=False,
) -> dict:
    """One ``metrics`` object, verbatim in the server's field names."""
    return {
        "peptide_ipsae": peptide_ipsae,
        "peptide_ipsae_status": peptide_ipsae_status,
        "iptm": 0.80,
        "plddt_mean": 88.0,
        "scaffold_mediated": scaffold_mediated,
        "delta_g": delta_g,
        "kd_nm": kd_nm,
        "kd_status": kd_status,
        "kd_reason": kd_reason,
        "kd_scorer": kd_scorer,
        "kd_scorer_version": kd_scorer_version,
        "kd_aggregate_method": "boltzmann_parallel",
        "kd_ipsae_gate": GATE,
        "peptide_length": peptide_length,
    }


class _FakeEvaluator:
    """``filter_folds`` over a fixed ``{fold_job_id: metrics}`` map."""

    def __init__(self, rows: dict[str, dict]):
        self.rows = rows
        self.calls: list[dict] = []

    def filter_folds(self, ids, criteria=None, deltaforge_scores=None):
        self.calls.append({
            "ids": list(ids),
            "criteria": criteria,
            "deltaforge_scores": deltaforge_scores,
        })
        merged = {
            str(d.get("fold_job_id")): d for d in (deltaforge_scores or [])
        }
        out = []
        for jid in ids:
            m = dict(self.rows[jid])
            over = merged.get(jid)
            if over is not None:
                # The server merges the caller's pre-computed scores OVER the
                # joined value; mirror that, including the status flip.
                m["delta_g"] = over.get("delta_g")
                m["kd_nm"] = over.get("kd_nm")
                m["kd_status"] = "ok" if isinstance(over.get("kd_nm"), float) else m.get("kd_status")
            out.append({
                "fold_job_id": jid,
                "quality_tier": {"verdict": "great_plus"},
                "metrics": m,
            })
        return {"results": out}


class _FakeDeltaForge:
    """``batch_score_fold`` returning the documented envelope."""

    def __init__(self, results: dict[str, dict]):
        self.results = results
        self.calls: list[list[str]] = []

    def batch_score_fold(self, fold_job_ids, **_kw):
        self.calls.append(list(fold_job_ids))
        rows = [dict(self.results[j], foldJobId=j) for j in fold_job_ids]
        return {
            "success": True,
            "scored": sum(1 for r in rows if r.get("kd_nm") is not None),
            "failed": sum(1 for r in rows if r.get("error")),
            "results": rows,
            "errors": [],
        }


class _FakeClient:
    def __init__(self, evaluator=None, deltaforge=None):
        self.evaluator = evaluator
        self.deltaforge = deltaforge
        self.peptides = None


def _camp(evaluator=None, deltaforge=None) -> Campaigns:
    return Campaigns(  # type: ignore[arg-type]
        transport=None, client=_FakeClient(evaluator, deltaforge)
    )


def _hydrate(rows: dict[str, dict], *, legs=None, job_index=None) -> PanelResult:
    """Run ``_hydrate_metrics`` over a one-leg panel and hand it back."""
    legs = legs or {(A, "ENPP3"): PanelLeg(sequence=A, receptor="ENPP3",
                                           fold_job_ids=list(rows))}
    job_index = job_index or {j: (A, "ENPP3") for j in rows}
    panel = PanelResult(
        target="ENPP3", off_targets=["ENPP1"], legs=legs, requested_draws=1,
    )
    _camp(_FakeEvaluator(rows))._hydrate_metrics(panel, job_index)
    return panel


def _panel(spec, *, target="ENPP3", off_targets=("ENPP1", "ENPP6"), draws=1):
    """A PanelResult from ``{(seq, receptor): {...leg fields...}}``."""
    legs: dict[tuple[str, str], PanelLeg] = {}
    for (seq, recep), cell in spec.items():
        ipsae = list(cell.get("ipsae", [0.80]))
        kd = list(cell.get("kd", []))
        legs[(seq, recep)] = PanelLeg(
            sequence=seq,
            receptor=recep,
            peptide_ipsae_draws=ipsae,
            scaffold_mediated_draws=[False] * len(ipsae),
            kd_nm_draws=kd,
            delta_g_draws=list(cell.get("delta_g", [])),
            kd_not_computed=int(cell.get("kd_not_computed", 0)),
            kd_statuses=list(cell.get("kd_statuses", [])),
            kd_reasons=list(cell.get("kd_reasons", [])),
            peptide_length=cell.get("peptide_length", len(seq)),
            kd_scorers=list(cell.get("kd_scorers", ["deltaforge_v10_2_unified_parallel"] * len(kd))),
            kd_scorer_versions=list(cell.get("kd_scorer_versions", ["10.2.0"] * len(kd))),
            kd_ipsae_gate=cell.get("kd_ipsae_gate", GATE),
        )
    return PanelResult(
        target=target, off_targets=list(off_targets), legs=legs,
        requested_draws=draws, statistic="median",
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. A computed 0.0 is a MEASUREMENT
# ─────────────────────────────────────────────────────────────────────────────


def test_a_computed_zero_kd_is_recorded_as_a_draw():
    panel = _hydrate({"j0": _metrics(kd_nm=0.0)})
    leg = panel.leg(A, "ENPP3")
    assert leg is not None
    # ⛔ NEVER `assert leg.kd_nm_draws` — 0.0 is falsy and that assertion would
    # pass only when the value were wrong.
    assert leg.kd_nm_draws == [0.0]
    assert leg.kd_not_computed == 0


def test_a_boolean_is_not_a_kd_measurement():
    panel = _hydrate({"j0": _metrics(kd_nm=True)})
    leg = panel.leg(A, "ENPP3")
    assert leg is not None
    assert leg.kd_nm_draws == []
    assert leg.kd_not_computed == 1


# ─────────────────────────────────────────────────────────────────────────────
# 2. Below the gate is a STATED reason, not an empty cell
# ─────────────────────────────────────────────────────────────────────────────


def test_below_the_ipsae_gate_is_indeterminate_and_says_so_verbatim():
    reason = "peptide_ipsae 0.41 < gate 0.67; DeltaForge scores an interface"
    panel = _hydrate({"j0": _metrics(
        peptide_ipsae=0.41, kd_nm=None, delta_g=None,
        kd_status="below_ipsae_gate", kd_reason=reason,
    )})
    leg = panel.leg(A, "ENPP3")
    assert leg is not None
    assert leg.kd_nm_draws == []
    assert leg.kd_not_computed == 1
    assert leg.kd_statuses == ["below_ipsae_gate"]
    # The platform's OWN words, not a reworded summary.
    assert reason in leg.kd_not_computed_reasons
    assert leg.kd_ipsae_gate == GATE
    # ...and the ipSAE itself was measured fine. The two are different facts.
    assert leg.peptide_ipsae_draws == [0.41]


def test_never_scored_and_below_the_gate_are_distinguishable():
    panel = _hydrate({
        "j0": _metrics(kd_nm=None, delta_g=None, kd_status="not_computed",
                       kd_reason="no DeltaForge score persisted for this fold"),
        "j1": _metrics(peptide_ipsae=0.40, kd_nm=None, delta_g=None,
                       kd_status="below_ipsae_gate",
                       kd_reason="peptide_ipsae 0.40 < gate 0.67"),
    })
    leg = panel.leg(A, "ENPP3")
    assert leg is not None
    assert leg.kd_not_computed == 2
    assert set(leg.kd_not_computed_statuses) == {"not_computed", "below_ipsae_gate"}


def test_the_peptide_length_confound_column_is_carried():
    panel = _hydrate({"j0": _metrics(peptide_length=21)})
    leg = panel.leg(A, "ENPP3")
    assert leg is not None
    assert leg.peptide_length == 21


def test_a_kd_draw_is_not_recorded_when_its_own_ipsae_draw_was_indeterminate():
    # A Kd aggregated over a different draw set than the ipSAE reported beside
    # it is a silent mismatch. The server contradicting itself must not produce
    # one.
    panel = _hydrate({"j0": _metrics(peptide_ipsae=None,
                                     peptide_ipsae_status="not_computed",
                                     kd_nm=5.0)})
    leg = panel.leg(A, "ENPP3")
    assert leg is not None
    assert leg.peptide_ipsae_draws == []
    assert leg.kd_nm_draws == []
    assert leg.kd_not_computed == 1


def test_two_scorers_on_one_leg_is_not_one_measurement():
    panel = _hydrate({
        "j0": _metrics(kd_scorer="deltaforge_v10_2_unified_parallel"),
        "j1": _metrics(kd_scorer="deltaforge_v9_esm"),
    })
    leg = panel.leg(A, "ENPP3")
    assert leg is not None
    assert leg.kd_scorer_conflict is True
    assert set(leg.kd_scorer_set) == {
        "deltaforge_v10_2_unified_parallel", "deltaforge_v9_esm"
    }
    # No single scorer can be named, so none is.
    assert leg.kd_scorer is None


# ─────────────────────────────────────────────────────────────────────────────
# 3. log10 aggregation — the mean is where it matters
# ─────────────────────────────────────────────────────────────────────────────


def test_the_mean_over_kd_draws_is_geometric_not_arithmetic():
    leg = PanelLeg(sequence=A, receptor="ENPP3", kd_nm_draws=[1.0, 100.0],
                   peptide_ipsae_draws=[0.8, 0.8])
    # 10.0, not 50.5. A linear mean over log-distributed draws is dominated by
    # the weakest one.
    assert leg.kd_value("mean") == pytest.approx(10.0)
    assert leg.kd_value("median") == pytest.approx(50.5)  # order statistic, unchanged


def test_the_log10_spread_is_a_fold_range_not_a_standard_error():
    leg = PanelLeg(sequence=A, receptor="ENPP3", kd_nm_draws=[1.0, 100.0],
                   peptide_ipsae_draws=[0.8, 0.8])
    assert leg.log10_kd_spread() == pytest.approx(2.0)
    assert leg.kd_spread() == pytest.approx(99.0)


def test_a_nonpositive_draw_makes_the_geometric_mean_undefined_not_zero():
    leg = PanelLeg(sequence=A, receptor="ENPP3", kd_nm_draws=[0.0, 10.0],
                   peptide_ipsae_draws=[0.8, 0.8])
    assert leg.kd_nonpositive_draws == 1
    assert leg.kd_value("mean") is None      # never 0.0, never inf
    assert leg.kd_value("min") == pytest.approx(0.0)   # the order statistic stands


# ─────────────────────────────────────────────────────────────────────────────
# 4. The selectivity ratio — the deliverable's own definition
# ─────────────────────────────────────────────────────────────────────────────


def test_kd_fold_selectivity_is_the_tightest_off_target_over_the_target():
    panel = _panel({
        (A, "ENPP3"): {"ipsae": [0.90], "kd": [1.0]},
        (A, "ENPP1"): {"ipsae": [0.70], "kd": [100.0]},
        (A, "ENPP6"): {"ipsae": [0.70], "kd": [20.0]},
    })
    row = panel.deltas(band=0.05, target_leg_min=0.66)[0][0]
    assert row.target_kd_nm == pytest.approx(1.0)
    assert row.off_target_kd_nm == {"ENPP1": 100.0, "ENPP6": 20.0}
    assert row.kd_fold_selectivity == pytest.approx(20.0)
    assert row.log10_kd_selectivity == pytest.approx(math.log10(20.0))
    assert row.kd_selectivity_status == "ok"
    assert row.kd_selectivity_resolvable is True


def test_the_kd_argmin_is_not_the_ipsae_argmax():
    # ENPP1 is the worst off-target on ipSAE; ENPP6 is the tightest on Kd.
    # Reusing one argument for the other reports the wrong receptor as the risk.
    panel = _panel({
        (A, "ENPP3"): {"ipsae": [0.90], "kd": [1.0]},
        (A, "ENPP1"): {"ipsae": [0.75], "kd": [900.0]},
        (A, "ENPP6"): {"ipsae": [0.60], "kd": [3.0]},
    })
    row = panel.deltas(band=0.05, target_leg_min=0.66)[0][0]
    assert row.worst_off_target == "ENPP1"          # ipSAE argmax
    assert row.worst_off_target_by_kd == "ENPP6"    # Kd argmin
    assert row.worst_off_target_by_kd != row.worst_off_target
    assert row.kd_fold_selectivity == pytest.approx(3.0)


def test_a_missing_off_target_kd_is_an_upper_bound_not_a_verdict():
    panel = _panel({
        (A, "ENPP3"): {"ipsae": [0.90], "kd": [1.0]},
        (A, "ENPP1"): {"ipsae": [0.70], "kd": [100.0]},
        (A, "ENPP6"): {"ipsae": [0.70], "kd": [],
                       "kd_not_computed": 1, "kd_statuses": ["not_computed"],
                       "kd_reasons": ["no DeltaForge score persisted"]},
    })
    row = panel.deltas(band=0.05, target_leg_min=0.66)[0][0]
    # ⛔ A ratio minimised over a SUBSET can only FALL when the rest arrives.
    assert row.kd_fold_selectivity is None
    assert row.kd_selectivity_resolvable is False
    assert row.kd_selectivity_status != "ok"
    assert row.kd_upper_bound == pytest.approx(100.0)
    assert row.kd_comparators_missing == ("ENPP6",)
    # The ipSAE row is still complete and still a verdict — the two objectives
    # have separate completeness.
    assert row.verdict == "PASS_BOTH"


def test_a_missing_target_kd_is_not_a_selectivity_row_at_all():
    panel = _panel({
        (A, "ENPP3"): {"ipsae": [0.90], "kd": [],
                       "kd_not_computed": 1, "kd_statuses": ["below_ipsae_gate"]},
        (A, "ENPP1"): {"ipsae": [0.70], "kd": [100.0]},
        (A, "ENPP6"): {"ipsae": [0.70], "kd": [20.0]},
    })
    row = panel.deltas(band=0.05, target_leg_min=0.66)[0][0]
    assert row.target_kd_nm is None
    assert row.kd_fold_selectivity is None
    assert row.kd_upper_bound is None
    assert row.kd_selectivity_status == "no_target_kd"


def test_a_zero_target_kd_is_not_perfect_selectivity():
    panel = _panel({
        (A, "ENPP3"): {"ipsae": [0.90], "kd": [0.0]},
        (A, "ENPP1"): {"ipsae": [0.70], "kd": [100.0]},
        (A, "ENPP6"): {"ipsae": [0.70], "kd": [20.0]},
    })
    row = panel.deltas(band=0.05, target_leg_min=0.66)[0][0]
    assert row.target_kd_nm == pytest.approx(0.0)
    assert row.kd_fold_selectivity is None          # never inf
    assert row.log10_kd_selectivity is None
    assert row.kd_selectivity_status == "target_kd_nonpositive"
    assert row.kd_selectivity_resolvable is False


def test_the_length_confound_travels_beside_every_kd_ordering():
    panel = _panel({
        (A, "ENPP3"): {"ipsae": [0.90], "kd": [1.0], "peptide_length": 12},
        (A, "ENPP1"): {"ipsae": [0.70], "kd": [100.0]},
        (A, "ENPP6"): {"ipsae": [0.70], "kd": [20.0]},
    })
    row = panel.deltas(band=0.05, target_leg_min=0.66)[0][0]
    assert row.target_peptide_length == 12
    assert row.kd_scorer == "deltaforge_v10_2_unified_parallel"
    assert row.kd_ipsae_gate == GATE


def test_deltas_still_ranks_on_the_ipsae_delta():
    # ⛔ B wins on Kd selectivity (100x vs 2x) and LOSES on Δ. The existing
    # surface ranks on Δ and must keep doing so; Kd is reported beside it.
    panel = _panel({
        (A, "ENPP3"): {"ipsae": [0.95], "kd": [50.0]},
        (A, "ENPP1"): {"ipsae": [0.20], "kd": [100.0]},
        (A, "ENPP6"): {"ipsae": [0.20], "kd": [100.0]},
        (B, "ENPP3"): {"ipsae": [0.70], "kd": [1.0]},
        (B, "ENPP1"): {"ipsae": [0.69], "kd": [100.0]},
        (B, "ENPP6"): {"ipsae": [0.69], "kd": [100.0]},
    })
    complete, _partial, _summary = panel.deltas(band=0.05, target_leg_min=0.66)
    assert [r.sequence for r in complete] == [A, B]
    assert complete[0].kd_fold_selectivity == pytest.approx(2.0)
    assert complete[1].kd_fold_selectivity == pytest.approx(100.0)


def test_the_summary_counts_why_a_kd_was_never_computed():
    panel = _panel({
        (A, "ENPP3"): {"ipsae": [0.90], "kd": [1.0]},
        (A, "ENPP1"): {"ipsae": [0.70], "kd": [],
                       "kd_not_computed": 1, "kd_statuses": ["below_ipsae_gate"],
                       "kd_reasons": ["peptide_ipsae 0.70 < gate 0.67"]},
        (A, "ENPP6"): {"ipsae": [0.70], "kd": [20.0]},
    })
    _c, _p, summary = panel.deltas(band=0.05, target_leg_min=0.66)
    assert summary["kd_not_computed_reasons"] == {"below_ipsae_gate": 1}
    assert summary["n_kd_selectivity_ok"] == 0
    assert summary["n_kd_upper_bounds_not_verdicts"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# 5. Replication over BOTH objectives
# ─────────────────────────────────────────────────────────────────────────────


def test_a_design_that_holds_on_ipsae_can_lose_on_kd_and_it_is_visible():
    panel = _panel({
        (A, "ENPP3"): {"ipsae": [0.90], "kd": [50.0]},
        (A, "ENPP1"): {"ipsae": [0.20], "kd": [60.0]},
        (A, "ENPP6"): {"ipsae": [0.20], "kd": [60.0]},
    })
    rep = panel.hold_rate([A], band=0.05, target_leg_min=0.66,
                          min_kd_fold_selectivity=10.0)
    assert rep.held == (A,)             # ipSAE held
    assert rep.kd_lost == (A,)          # 1.2x selectivity, not 10x
    assert rep.kd_held == ()
    assert rep.kd_hold_rate == 0.0
    assert "kd_fold_selectivity >= 10.0" in (rep.thermo_criterion or "")


def test_an_unmeasured_kd_is_untested_never_lost():
    panel = _panel({
        (A, "ENPP3"): {"ipsae": [0.90], "kd": []},
        (A, "ENPP1"): {"ipsae": [0.20], "kd": []},
        (A, "ENPP6"): {"ipsae": [0.20], "kd": []},
    })
    rep = panel.hold_rate([A], band=0.05, target_leg_min=0.66,
                          min_kd_fold_selectivity=10.0)
    assert rep.held == (A,)
    assert rep.kd_untested == (A,)
    assert rep.kd_lost == ()


def test_no_thermo_threshold_says_so_rather_than_inventing_one():
    panel = _panel({
        (A, "ENPP3"): {"ipsae": [0.90], "kd": [1.0]},
        (A, "ENPP1"): {"ipsae": [0.20], "kd": [100.0]},
        (A, "ENPP6"): {"ipsae": [0.20], "kd": [100.0]},
    })
    rep = panel.hold_rate([A], band=0.05, target_leg_min=0.66)
    assert rep.kd_untested == (A,)
    assert rep.kd_hold_rate is None
    assert "no thermo criterion" in (rep.thermo_criterion or "")


def test_the_kd_noise_band_is_measured_in_log10_space():
    panel = _panel({
        (A, "ENPP3"): {"ipsae": [0.90, 0.90], "kd": [1.0, 100.0]},
        (A, "ENPP1"): {"ipsae": [0.20, 0.20], "kd": [10.0, 100.0]},
        (A, "ENPP6"): {"ipsae": [0.20, 0.20], "kd": [10.0, 100.0]},
    }, draws=2)
    spread = panel.observed_kd_spread()
    assert spread["n_legs"] == 3
    assert spread["median_log10_range"] == pytest.approx(1.0)
    assert panel.cross_draw_kd_band_log10() == pytest.approx(1.0 * math.sqrt(2), abs=1e-4)


# ─────────────────────────────────────────────────────────────────────────────
# 6. score_thermo — the explicit, billed backfill
# ─────────────────────────────────────────────────────────────────────────────


def _thermo_panel():
    """Two legs: one above the gate, one below it."""
    legs = {
        (A, "ENPP3"): PanelLeg(
            sequence=A, receptor="ENPP3", fold_job_ids=["j-hi"],
            peptide_ipsae_draws=[0.90], ipsae_by_job={"j-hi": 0.90},
            kd_not_computed=1, kd_statuses=["not_computed"],
        ),
        (A, "ENPP1"): PanelLeg(
            sequence=A, receptor="ENPP1", fold_job_ids=["j-lo"],
            peptide_ipsae_draws=[0.41], ipsae_by_job={"j-lo": 0.41},
            kd_not_computed=1, kd_statuses=["below_ipsae_gate"],
        ),
    }
    return PanelResult(target="ENPP3", off_targets=["ENPP1"], legs=legs,
                       requested_draws=1)


def test_score_thermo_never_pays_to_score_a_fold_below_the_gate():
    df = _FakeDeltaForge({"j-hi": {"delta_g": -9.0, "kd_nm": 2.5,
                                   "affinity_quotable": True,
                                   "scorer_version": "10.2.0"}})
    ev = _FakeEvaluator({"j-hi": _metrics(kd_nm=2.5, delta_g=-9.0)})
    camp = _camp(ev, df)
    report = camp.score_thermo(_thermo_panel(), only_gated=True)
    # The below-gate fold was never sent.
    assert df.calls == [["j-hi"]]
    assert "j-lo" in report.skipped_below_gate
    assert report.n_skipped_below_gate == 1
    assert "0.67" in report.skipped_below_gate["j-lo"]
    assert report.n_submitted == 1


def test_score_thermo_is_never_called_implicitly_by_fold_panel():
    # ⛔ Charge-on-success. The only way to spend here is to ask for it.
    src = Campaigns.fold_panel.__doc__ or ""
    assert "score_thermo" not in Campaigns._hydrate_metrics.__code__.co_names
    assert "cost" in (Campaigns.score_thermo.__doc__ or "").lower()
    assert "credits()" in (Campaigns.score_thermo.__doc__ or "")
    assert src  # the fold-cost note is still there


def test_affinity_quotable_false_is_indeterminate_with_the_servers_own_code():
    df = _FakeDeltaForge({"j-hi": {
        "delta_g": None, "kd_nm": None,
        "affinity_quotable": False,
        "affinity_reason_code": "interface_type_anticorrelated_symmetric_multimer",
        "affinity_reason": "symmetric multimer interface; affinity is not quotable",
        "scorer_version": "10.2.0",
    }})
    ev = _FakeEvaluator({"j-hi": _metrics(kd_nm=None, delta_g=None,
                                          kd_status="scorer_not_quotable")})
    camp = _camp(ev, df)
    report = camp.score_thermo(_thermo_panel(), only_gated=True)
    assert report.indeterminate["j-hi"] == (
        "interface_type_anticorrelated_symmetric_multimer"
    )
    # Never a zero, never an error.
    assert report.deltaforge_scores == []
    assert report.errors == []
    assert report.n_scored == 0


def test_score_thermo_merges_forward_so_the_panel_carries_the_kd():
    df = _FakeDeltaForge({"j-hi": {"delta_g": -9.0, "kd_nm": 2.5,
                                   "affinity_quotable": True,
                                   "scorer_version": "10.2.0"}})
    ev = _FakeEvaluator({"j-hi": _metrics(kd_nm=None, delta_g=None,
                                          kd_status="not_computed")})
    camp = _camp(ev, df)
    panel = _thermo_panel()
    report = camp.score_thermo(panel, only_gated=True)
    assert report.deltaforge_scores == [
        {"fold_job_id": "j-hi", "delta_g": -9.0, "kd_nm": 2.5}
    ]
    assert report.merged_into_panel is True
    # ⛔ batch-score-fold does not persist, so the merge is the only way the
    # panel ever sees the number.
    assert ev.calls[-1]["deltaforge_scores"] == report.deltaforge_scores
    leg = panel.leg(A, "ENPP3")
    assert leg is not None
    assert leg.kd_nm_draws == [2.5]


def test_score_thermo_chunks_at_the_servers_own_limit():
    ids = [f"j{i}" for i in range(205)]
    legs = {
        (A, "ENPP3"): PanelLeg(
            sequence=A, receptor="ENPP3", fold_job_ids=ids,
            peptide_ipsae_draws=[0.90] * len(ids),
            ipsae_by_job={j: 0.90 for j in ids},
        )
    }
    panel = PanelResult(target="ENPP3", off_targets=["ENPP1"], legs=legs,
                        requested_draws=1)
    df = _FakeDeltaForge({j: {"delta_g": -9.0, "kd_nm": 2.5,
                              "affinity_quotable": True} for j in ids})
    ev = _FakeEvaluator({j: _metrics(kd_nm=2.5) for j in ids})
    report = _camp(ev, df).score_thermo(panel, only_gated=True)
    assert [len(c) for c in df.calls] == [200, 5]
    assert report.n_scored == 205


def test_score_thermo_does_not_re_pay_for_a_leg_that_already_has_a_kd():
    legs = {
        (A, "ENPP3"): PanelLeg(
            sequence=A, receptor="ENPP3", fold_job_ids=["j-hi"],
            peptide_ipsae_draws=[0.90], ipsae_by_job={"j-hi": 0.90},
            kd_nm_draws=[2.5], kd_by_job={"j-hi": 2.5},
        )
    }
    panel = PanelResult(target="ENPP3", off_targets=["ENPP1"], legs=legs,
                        requested_draws=1)
    df = _FakeDeltaForge({})
    report = _camp(_FakeEvaluator({}), df).score_thermo(panel)
    assert df.calls == []
    assert report.n_already_scored == 1
    assert report.n_submitted == 0
