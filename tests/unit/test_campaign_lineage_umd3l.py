# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""A round-2 design names the round-1 lead it came from. bd-dre-umd3l.

The recursive campaign ran end to end and produced, for a customer, a second
round of designs with NOTHING tying any of them to the first round's leads:
``CampaignRound``, ``SelectivityDelta`` and ``report()`` carried no parent,
no parent round, and no comparison. "Is this variant better than what it came
from" — the question a refinement round exists to answer — was not writable
from the returned data.

⛔ WHERE THE LINEAGE WAS LOST. It was never RECORDED, not dropped in a response
shape. The served generator takes no parent or seed sequence — neither the
generation request body nor its response carries one — and
``RecursiveCampaign._generate()`` reseeds only by AVOIDANCE. So there is no
server-side link to plumb through; a link exists only where the caller states
one, and this module's job is to carry it faithfully and to refuse to invent
one where it does not exist.

These tests cover the capability and the seven ways carrying a parent link goes
silently wrong:

1. a parent named but never refolded, so the "improvement" is measured across
   two different pose sets;
2. a pool-level steer written down as a per-child parent it cannot support;
3. a difference inside the panel's own noise band reported as an improvement;
4. "we could not compare" collapsing into "no difference";
5. a parent control counted as a new survivor, inflating the round's yield;
6. the pre-fold predicted ipSAE column (>1.0) standing in for the measured
   ``peptide_ipsae``;
7. a Kd quoted off a leg below the 0.67 gate, where DeltaForge scores nothing.

No network: the fold submitter and the metric surface are stubbed, so the whole
loop runs offline and burns nothing.
"""

from __future__ import annotations

import pytest

from ligandai.errors import LigandAIValidationError
from ligandai.resources.campaigns import Campaigns

# Round-1 leads and their round-2 children.
P1 = "ACDEFGHIKLMNP"
P2 = "WYVTSRQNKHGFE"
C1 = "ACDEFGHIKLMNW"   # P1 with the last residue changed
C2 = "WYVTSRQNKHGFA"   # P2 with the last residue changed

TARGET = "ENPP3"
OFF = ["ENPP1", "ENPP6"]

#: The platform's own gate: below it DeltaForge returns no Kd at all.
GATE = 0.67


# ─────────────────────────────────────────────────────────────────────────────
# Fakes
# ─────────────────────────────────────────────────────────────────────────────


class _Batch:
    def __init__(self, receptor: str, seqs, draw: int):
        self.jobs = [
            {"job_id": f"{receptor}|{draw}|{s}", "peptide_index": i, "sequence": s}
            for i, s in enumerate(seqs)
        ]

    def wait(self, **_kw):
        return []


class _Peptides:
    def __init__(self):
        self.calls: list[dict] = []

    def fold_batch(self, *, peptides, target_gene, **kw):
        self.calls.append({"receptor": target_gene, "peptides": list(peptides), **kw})
        draw = sum(1 for c in self.calls if c["receptor"] == target_gene) - 1
        return _Batch(target_gene, peptides, draw)

    def folded(self, receptor: str = TARGET) -> set[str]:
        """Every sequence submitted against one receptor."""
        out: set[str] = set()
        for c in self.calls:
            if c["receptor"] == receptor:
                out.update(c["peptides"])
        return out


class _Evaluator:
    """``peptide_ipsae`` and Kd per ``(sequence, receptor)``, with jitter."""

    def __init__(self, ipsae: dict, kd: dict | None = None, jitter: float = 0.0,
                 kd_jitter: float = 0.0):
        self.ipsae = ipsae
        self.kd = kd or {}
        self.jitter = jitter
        self.kd_jitter = kd_jitter

    def filter_folds(self, ids, criteria=None, deltaforge_scores=None):
        rows = []
        for jid in ids:
            recep, draw, seq = jid.split("|")
            d = int(draw)
            sign = 1.0 if d % 2 else -1.0
            v = self.ipsae[(seq, recep)] + sign * self.jitter
            m = {
                "peptide_ipsae": v,
                "peptide_ipsae_status": "ok",
                "iptm": 0.80,
                "plddt_mean": 88.0,
                "scaffold_mediated": False,
                "kd_ipsae_gate": GATE,
                "peptide_length": len(seq),
            }
            kd = self.kd.get((seq, recep))
            if kd is not None:
                m.update({
                    "kd_nm": kd * (1.0 + sign * self.kd_jitter),
                    "kd_status": "ok",
                    "delta_g": -9.0,
                    "kd_scorer": "deltaforge_v10_2_unified_parallel",
                    "kd_scorer_version": "10.2.0",
                    "kd_aggregate_method": "boltzmann_parallel",
                })
            rows.append({
                "fold_job_id": jid,
                "quality_tier": {"verdict": "great_plus"},
                "metrics": m,
            })
        return {"results": rows}


class _Client:
    def __init__(self, evaluator):
        self.peptides = _Peptides()
        self.evaluator = evaluator


def _ipsae(**per_seq) -> dict:
    """``{seq: (on_target, off1, off2)}`` → the ``(seq, receptor)`` map."""
    out: dict[tuple[str, str], float] = {}
    for seq, vals in per_seq.items():
        on, o1, o2 = vals
        out[(seq, TARGET)] = on
        out[(seq, OFF[0])] = o1
        out[(seq, OFF[1])] = o2
    return out


def _run(evaluator, *, band=0.05, target_leg_min=0.66, **kw):
    client = _Client(evaluator)
    camp = Campaigns(transport=None, client=client)  # type: ignore[arg-type]
    run = camp.recursive(
        target=TARGET, off_targets=OFF, band=band, target_leg_min=target_leg_min, **kw
    )
    return client, run


#: A default world: both leads bind the target and neither off-target.
BASE = _ipsae(
    **{
        P1: (0.90, 0.30, 0.20),
        P2: (0.88, 0.28, 0.22),
        C1: (0.95, 0.30, 0.20),   # clearly better than P1
        C2: (0.881, 0.28, 0.22),  # a hair better than P2 — inside any band
    }
)


# ─────────────────────────────────────────────────────────────────────────────
# 1 — the capability: a child names its parent AND the parent's round
# ─────────────────────────────────────────────────────────────────────────────


def test_a_round_two_design_names_the_round_one_lead_it_came_from():
    _client, run = _run(_Evaluator(BASE))
    r1 = run.run_round(sequences=[P1, P2], confirm=False)
    assert r1.index == 1

    r2 = run.run_round(sequences=[C1, C2], derived_from={C1: P1, C2: P2},
                       confirm=False)
    rows = {r.sequence: r for r in r2.complete}

    # The row itself names the parent and the ROUND the parent came from. A bare
    # parent string is not enough: "better than what it came from" is only
    # readable against a specific measurement.
    assert rows[C1].parent == P1
    assert rows[C1].parent_round == 1
    assert rows[C1].lineage_origin == "derived"
    assert rows[C2].parent == P2
    assert rows[C2].parent_round == 1

    lin = r2.parent_of(C1)
    assert lin is not None and lin.parent == P1 and lin.parent_round == 1
    assert lin.has_parent is True

    # And the campaign can walk the chain back.
    chain = run.ancestry(C1)
    assert [c.sequence for c in chain] == [C1, P1]


def test_the_parent_is_refolded_in_the_same_panel_as_its_children():
    client, run = _run(_Evaluator(BASE))
    run.run_round(sequences=[P1], confirm=False)
    n_before = len(client.peptides.calls)

    r2 = run.run_round(sequences=[C1], derived_from={C1: P1}, confirm=False)

    # Round 2's on-target submission carries the CHILD and its PARENT together.
    round2_calls = client.peptides.calls[n_before:]
    on_target = [c for c in round2_calls if c["receptor"] == TARGET]
    assert on_target, "round 2 folded nothing against the target"
    assert set(on_target[0]["peptides"]) == {C1, P1}
    assert r2.parent_controls == [P1]

    c = r2.comparison_for(C1)
    assert c is not None and c.same_panel is True
    assert c.parent == P1 and c.parent_round == 1
    assert rows_of(r2)[C1].parent_in_panel is True


def rows_of(rnd):
    return {r.sequence: r for r in rnd.complete}


# ─────────────────────────────────────────────────────────────────────────────
# 2 — ⛔ the comparison is fair or it is NOT OFFERED
# ─────────────────────────────────────────────────────────────────────────────


def test_a_parent_not_refolded_in_this_panel_yields_a_refusal_not_a_delta():
    _client, run = _run(_Evaluator(BASE))
    run.run_round(sequences=[P1], confirm=False)
    r2 = run.run_round(sequences=[C1], derived_from={C1: P1},
                       refold_parents=False, confirm=False)

    c = r2.comparison_for(C1)
    assert c is not None
    assert c.same_panel is False
    assert c.verdict == "NOT_COMPARABLE"
    # ⛔ Every number is withheld. A cross-round difference is not computed at
    # all, let alone reported.
    assert c.delta_improvement is None
    assert c.child_delta is None
    assert c.parent_delta is None
    assert c.kd_fold_change is None
    assert c.child_target_kd_nm is None
    assert c.parent_target_kd_nm is None
    # And it says WHY, in terms of the fold conditions.
    assert "same panel" in (c.not_comparable_reason or "").lower()
    assert "earlier round" in (c.not_comparable_reason or "").lower()

    # The round surfaces it rather than burying it in an aggregate.
    assert [x.sequence for x in r2.not_comparable_to_parent] == [C1]
    assert r2.improved_over_parent == []
    assert any("NO fair" in n or "not comparable" in n for n in r2.notes)


def test_a_child_whose_parent_is_absent_is_indeterminate_not_equal_to_it():
    # "We could not look" and "we looked and saw no difference" must not collapse.
    _client, run = _run(_Evaluator(BASE))
    run.run_round(sequences=[P1], confirm=False)
    run.run_round(sequences=[C1], derived_from={C1: P1},
                  refold_parents=False, confirm=False)
    counts = run.report()["rounds"][1]["vs_parent"]
    assert counts["NOT_COMPARABLE"] == 1
    assert counts["INDISTINGUISHABLE"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# 3 — a pool-level steer does not become a per-child parent
# ─────────────────────────────────────────────────────────────────────────────


def test_a_pool_level_steer_does_not_invent_a_per_child_parent():
    _client, run = _run(_Evaluator(BASE))
    run.run_round(sequences=[P1, P2], confirm=False)
    r2 = run.run_round(sequences=[C1, C2], steered_by=[P1, P2], confirm=False)

    lin = r2.parent_of(C1)
    assert lin is not None
    assert lin.origin == "reseeded_pool"
    # ⛔ No individual parent is named, because the generator has no per-design
    # parent operator and guessing one would be a fabricated link.
    assert lin.parent is None
    assert lin.has_parent is False
    assert set(lin.parent_cohort) == {P1, P2}

    c = r2.comparison_for(C1)
    assert c is not None
    assert c.baseline_kind == "cohort_best"
    assert c.parent is None
    assert c.same_panel is True
    # The cohort baseline names the specific design that SET it.
    assert c.delta_baseline_from in {P1, P2}
    assert c.delta_baseline_round == 1


# ─────────────────────────────────────────────────────────────────────────────
# 4 — an undeclared later round is a de novo draw, and says so
# ─────────────────────────────────────────────────────────────────────────────


def test_an_undeclared_round_two_is_reported_as_de_novo_not_a_refinement():
    _client, run = _run(_Evaluator(BASE))
    run.run_round(sequences=[P1, P2], confirm=False)
    r2 = run.run_round(sequences=[C1, C2], confirm=False)

    assert all(lin.origin == "de_novo" for lin in r2.lineage.values())
    assert r2.vs_parent == []
    assert any("DE NOVO" in n for n in r2.notes)
    rep = run.report()
    assert rep["lineage"]["generator_accepts_a_parent_sequence"] is False
    assert rep["lineage"]["rounds_with_declared_lineage"] == []
    assert rep["lineage"]["n_designs_with_a_named_parent"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# 5 — resolvability: an improvement inside the band is not an improvement
# ─────────────────────────────────────────────────────────────────────────────


def test_a_resolvable_improvement_over_a_same_panel_parent_reads_BETTER():
    _client, run = _run(_Evaluator(BASE))
    run.run_round(sequences=[P1], confirm=False)
    r2 = run.run_round(sequences=[C1], derived_from={C1: P1}, confirm=False)
    c = r2.comparison_for(C1)
    assert c is not None and c.same_panel is True
    assert c.child_delta == pytest.approx(0.95 - 0.30)
    assert c.parent_delta == pytest.approx(0.90 - 0.30)
    assert c.delta_improvement == pytest.approx(0.05, abs=1e-9)
    assert c.delta_improvement_resolvable is True
    assert c.verdict == "BETTER"
    assert r2.improved_over_parent == [C1]


def test_an_improvement_inside_the_measured_band_is_INDISTINGUISHABLE():
    # jitter=0.02 → a 0.04 per-leg range, so the band on a DIFFERENCE is wider
    # than the 0.001 this child gains.
    _client, run = _run(_Evaluator(BASE, jitter=0.02))
    run.run_round(sequences=[P2], replicates=2, confirm=False)
    r2 = run.run_round(sequences=[C2], derived_from={C2: P2}, replicates=2,
                       confirm=False)
    c = r2.comparison_for(C2)
    assert c is not None and c.same_panel is True
    assert c.band is not None and c.band_source == "measured"
    assert abs(c.delta_improvement) < c.band
    assert c.delta_improvement_resolvable is False
    assert c.verdict == "INDISTINGUISHABLE"
    assert r2.improved_over_parent == []


# ─────────────────────────────────────────────────────────────────────────────
# 6 — metric discipline: peptide_ipsae, bounded by 1.0, gated at 0.67
# ─────────────────────────────────────────────────────────────────────────────


def test_every_sampled_peptide_ipsae_is_at_most_one():
    _client, run = _run(_Evaluator(BASE))
    run.run_round(sequences=[P1], confirm=False)
    r2 = run.run_round(sequences=[C1], derived_from={C1: P1}, confirm=False)
    sampled = [
        v for row in (r2.panel.to_rows() if r2.panel else [])
        if (v := row["peptide_ipsae"]) is not None
    ]
    assert sampled and max(sampled) <= 1.0
    c = r2.comparison_for(C1)
    assert c is not None
    assert c.child_target_ipsae is not None and c.child_target_ipsae <= 1.0
    assert c.parent_target_ipsae is not None and c.parent_target_ipsae <= 1.0


def test_a_prefold_predicted_ipsae_above_one_refuses_the_comparison():
    # ⛔ peptide_ipsae is bounded by 1.0. A value above it is the PRE-FOLD
    # predicted column, which was never measured and is not this metric.
    bad = dict(BASE)
    bad[(C1, TARGET)] = 1.42
    _client, run = _run(_Evaluator(bad))
    run.run_round(sequences=[P1], confirm=False)
    r2 = run.run_round(sequences=[C1], derived_from={C1: P1}, confirm=False)
    c = r2.comparison_for(C1)
    assert c is not None
    assert c.verdict == "NOT_COMPARABLE"
    assert "above 1.0" in (c.not_comparable_reason or "")
    assert c.delta_improvement is None


def test_a_kd_is_compared_only_above_the_ipsae_gate():
    # The child's on-target interface sits BELOW 0.67: DeltaForge scores an
    # interface and there is none, so no Kd comparison is offered — and that is
    # not the same as the child being weaker.
    ip = dict(BASE)
    ip[(C1, TARGET)] = 0.55
    kd = {(P1, TARGET): 20.0, (C1, TARGET): 5.0,
          (P1, OFF[0]): 900.0, (C1, OFF[0]): 900.0,
          (P1, OFF[1]): 900.0, (C1, OFF[1]): 900.0}
    _client, run = _run(_Evaluator(ip, kd=kd, jitter=0.005, kd_jitter=0.02),
                        target_leg_min=0.50)
    run.run_round(sequences=[P1], replicates=2, confirm=False)
    r2 = run.run_round(sequences=[C1], derived_from={C1: P1}, replicates=2,
                       confirm=False)
    c = r2.comparison_for(C1)
    assert c is not None and c.same_panel is True
    assert c.ipsae_gate == pytest.approx(GATE)
    assert c.kd_fold_change is None
    assert "gate" in (c.kd_not_compared_reason or "")


def test_a_same_panel_kd_improvement_is_quoted_against_its_own_band():
    kd = {(P1, TARGET): 100.0, (C1, TARGET): 4.0,
          (P1, OFF[0]): 900.0, (C1, OFF[0]): 900.0,
          (P1, OFF[1]): 900.0, (C1, OFF[1]): 900.0}
    _client, run = _run(_Evaluator(BASE, kd=kd, jitter=0.005, kd_jitter=0.02))
    run.run_round(sequences=[P1], replicates=2, confirm=False)
    r2 = run.run_round(sequences=[C1], derived_from={C1: P1}, replicates=2,
                       confirm=False)
    c = r2.comparison_for(C1)
    assert c is not None and c.same_panel is True
    assert c.child_target_kd_nm is not None and c.parent_target_kd_nm is not None
    # parent Kd / child Kd — >1 means the child is tighter.
    assert c.kd_fold_change == pytest.approx(100.0 / 4.0, rel=0.05)
    assert c.kd_band_log10 is not None
    assert c.kd_improvement_resolvable is True
    assert c.verdict == "BETTER"
    # Both Kd baselines name the design and the round they came from.
    assert c.kd_baseline_from == P1
    assert c.kd_baseline_round == 1


# ─────────────────────────────────────────────────────────────────────────────
# 7 — a parent control is a baseline, never a new survivor
# ─────────────────────────────────────────────────────────────────────────────


def test_a_parent_control_is_not_counted_as_a_new_survivor():
    _client, run = _run(_Evaluator(BASE))
    r1 = run.run_round(sequences=[P1], confirm=False)
    assert r1.survivors == [P1]
    r2 = run.run_round(sequences=[C1], derived_from={C1: P1}, confirm=False)
    # P1 cleared the panel again — it is the baseline, not a round-2 hit.
    assert P1 in {r.sequence for r in r2.complete}
    assert P1 not in r2.survivors
    assert r2.survivors == [C1]
    assert r2.parent_of(P1).origin == "parent_control"
    assert r2.parent_of(P1).parent_round == 1


def test_a_hard_negative_parent_is_not_refolded_and_its_child_is_not_compared():
    _client, run = _run(_Evaluator(BASE))
    run.run_round(sequences=[P1], confirm=False)
    run.avoidance.hard_negatives.append(P1)
    r2 = run.run_round(sequences=[C1], derived_from={C1: P1}, confirm=False)
    assert r2.parent_controls == []
    c = r2.comparison_for(C1)
    assert c is not None and c.verdict == "NOT_COMPARABLE"
    assert "hard negative" in (c.not_comparable_reason or "")


# ─────────────────────────────────────────────────────────────────────────────
# 8 — a link to a design this round never folds is refused at the call
# ─────────────────────────────────────────────────────────────────────────────


def test_derived_from_needs_the_designs_it_describes():
    _client, run = _run(_Evaluator(BASE))
    with pytest.raises(LigandAIValidationError, match="pass sequences="):
        run.run_round(derived_from={C1: P1}, confirm=False)


def test_derived_from_naming_a_child_not_in_sequences_is_refused():
    _client, run = _run(_Evaluator(BASE))
    with pytest.raises(LigandAIValidationError, match="not in sequences"):
        run.run_round(sequences=[C1], derived_from={C2: P1}, confirm=False)


def test_a_design_is_not_its_own_parent():
    _client, run = _run(_Evaluator(BASE))
    with pytest.raises(LigandAIValidationError, match="not its own parent"):
        run.run_round(sequences=[C1], derived_from={C1: C1}, confirm=False)


# ─────────────────────────────────────────────────────────────────────────────
# 9 — the report is quotable, and it states what it could not compare
# ─────────────────────────────────────────────────────────────────────────────


def test_report_carries_lineage_and_the_refused_comparisons():
    _client, run = _run(_Evaluator(BASE))
    run.run_round(sequences=[P1, P2], confirm=False)
    run.run_round(sequences=[C1, C2], derived_from={C1: P1, C2: P2}, confirm=False)
    rep = run.report()

    assert rep["lineage"]["rounds_with_declared_lineage"] == [2]
    assert rep["lineage"]["n_designs_with_a_named_parent"] == 2
    assert rep["lineage"]["n_comparisons_against_a_same_panel_parent"] == 2
    assert rep["lineage"]["n_comparisons_refused_as_unfair"] == 0

    r2 = rep["rounds"][1]
    assert r2["lineage_origins"]["derived"] == 2
    assert r2["lineage_origins"]["parent_control"] == 2
    assert r2["n_parent_controls"] == 2
    assert set(r2["parent_controls_refolded_in_this_panel"]) == {P1, P2}
    assert r2["vs_parent"]["BETTER"] >= 1

    one = {c["sequence"]: c for c in r2["parent_comparisons"]}[C1]
    assert one["parent"] == P1
    assert one["parent_round"] == 1
    assert one["same_panel"] is True
    assert one["peptide_ipsae_gate"] == pytest.approx(GATE)


def test_lineage_rows_export_one_row_per_design():
    _client, run = _run(_Evaluator(BASE))
    run.run_round(sequences=[P1], confirm=False)
    r2 = run.run_round(sequences=[C1], derived_from={C1: P1}, confirm=False)
    rows = {r["sequence"]: r for r in r2.lineage_rows()}
    assert set(rows) == {C1, P1}
    assert rows[C1]["parent"] == P1
    assert rows[C1]["parent_round"] == 1
    assert rows[C1]["parent_in_panel"] is True
    assert rows[P1]["is_parent_control"] is True
    assert rows[P1]["origin"] == "parent_control"


def test_lineage_objects_are_top_level_exports():
    import ligandai
    from ligandai.resources.campaigns import Lineage as _L
    from ligandai.resources.campaigns import ParentComparison as _PC

    assert ligandai.Lineage is _L
    assert ligandai.ParentComparison is _PC
    assert "Lineage" in ligandai.__all__
    assert "ParentComparison" in ligandai.__all__


def test_a_parent_control_is_never_served_from_the_dedupe_cache():
    # ⛔ A control served from the 24h dedupe carries an EARLIER round's pose
    # set while reading as a same-panel one — the exact unfairness the control
    # exists to remove.
    client, run = _run(_Evaluator(BASE))
    run.run_round(sequences=[P1], confirm=False)
    n_before = len(client.peptides.calls)
    r2 = run.run_round(sequences=[C1], derived_from={C1: P1}, confirm=False)
    first = client.peptides.calls[n_before]
    assert first["force_resubmit"] is True
    assert any("force_fresh=True" in n for n in r2.notes)


def test_an_explicit_force_fresh_choice_is_not_overridden():
    client, run = _run(_Evaluator(BASE))
    run.run_round(sequences=[P1], confirm=False)
    n_before = len(client.peptides.calls)
    run.run_round(sequences=[C1], derived_from={C1: P1}, confirm=False,
                  panel_kwargs={"force_fresh": False})
    assert client.peptides.calls[n_before]["force_resubmit"] is False


def test_a_lead_resubmitted_in_its_own_steered_pool_is_not_its_own_baseline():
    # ⛔ Comparing a design against itself reads INDISTINGUISHABLE, which is a
    # measurement of nothing wearing a verdict's name.
    _client, run = _run(_Evaluator(BASE))
    run.run_round(sequences=[P1, P2], confirm=False)
    r2 = run.run_round(sequences=[P1, C1], steered_by=[P1, P2], confirm=False)
    self_cmp = r2.comparison_for(P1)
    assert self_cmp is not None
    assert self_cmp.delta_baseline_from != P1
    assert self_cmp.delta_baseline_from == P2
