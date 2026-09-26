# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Unit tests for the two sibling developability scorers. [bd-dre-c80f4.26.2]

Covers the SDK surface added so all three of Andre's asks are CALLABLE:

  - ``client.gut_stability.chameleonicity``   → POST /api/scoring/chameleonicity
  - ``client.gut_stability.chameleonicity_local``
  - ``client.gut_stability.protein_gut_stability``
        → POST /api/scoring/protein-gut-stability
  - ``client.gut_stability.protein_gut_stability_local``
  - ``client.gut_stability.score`` on a route that IS mounted (the existing
    file pins the not-mounted case; this one pins that a mounted route returns
    results rather than a refusal)

⛔ WHAT IS MOCKED AND WHAT IS REAL. The three Express routes are not mounted on
any running deployment while this file is written, so every REMOTE test here
drives a mocked httpx transport: it pins the request body, the envelope check
and the error mapping, and it does NOT demonstrate a live round trip. The LOCAL
tests are real — they import the platform modules and run the actual scorers.

⛔ THE THREE INVARIANTS THIS FILE EXISTS TO DEFEND
  1. Chameleonicity on a LINEAR construct is ``applicable=False, index=None``.
     Never 0.0 — that would read as "a bad chameleon" instead of "the question
     does not apply", and the key is never dropped either.
  2. Cyclicity comes from the REALISED construct via
     ``resolve_construct_modifications``, three-valued, not from a request flag.
     ``cyclized`` is worth +0.18, the largest rescue term.
  3. Protein-gut without a usable structure is one of three named
     INDETERMINATE verdicts with ``accessible_*`` ABSENT — never zero, and never
     the flattering "fully shielded" reading that unmapped-as-buried arithmetic
     would produce.
"""

from __future__ import annotations

import json as _json
import pathlib

import pytest
from pytest_httpx import HTTPXMock

from ligandai import AsyncLigandAI, LigandAI
from ligandai.errors import LigandAIParameterNotForwarded, LigandAIValidationError
from ligandai.resources.gut_stability import (
    CHAMELEONICITY_MODULE,
    CHAMELEONICITY_SCORING_PATH,
    GI_STABILITY_SCORING_PATH,
    INDETERMINATE_VERDICTS,
    PLATFORM_MODULE_PATH_ENV,
    PROTEIN_GUT_STABILITY_SCORING_PATH,
    STRUCTURE_FORMATS,
    VALID_CYCLIC_MODES,
    LigandAIPlatformModuleUnavailable,
    _import_platform_module,
)

BASE = "http://api.ligandai.test"

#: The directory the platform modules live in on a development machine. The
#: published SDK carries NO default for this — see PLATFORM_MODULE_PATH_ENV.
PLATFORM_MODULES = pathlib.Path("/mnt/backup/LIGANDAI_ALPHA_V2/modal_workers")

needs_platform_modules = pytest.mark.skipif(
    not PLATFORM_MODULES.is_dir(),
    reason="platform modules not on this machine (published SDK checkout)",
)

#: GnRH (gonadorelin), the same peptide the peptide-scale tests use.
GNRH = "QHWSYGLRPG"

#: Trypsin (K/R) and chymotrypsin (F/W/Y) P1 residues, so both protease panels
#: see sites on the same chain.
PROTEIN_SEQ = "AKFRAWKYRAFKRWAYKRF"

#: ⛔ Deliberately NOT 1, so the offset arithmetic is actually exercised.
#: ``residue_offset`` is the STRUCTURE RESIDUE NUMBER OF SEQUENCE POSITION 1, so
#: a chain first modelled at 29 takes 29 — not 28.
FIRST_RESNUM = 29
CORRECT_OFFSET = FIRST_RESNUM

_THREE_LETTER = {
    "A": "ALA",
    "K": "LYS",
    "F": "PHE",
    "R": "ARG",
    "W": "TRP",
    "Y": "TYR",
}


def synthetic_pdb(seq: str = PROTEIN_SEQ, first: int = FIRST_RESNUM) -> str:
    """An extended CA/CB chain, 6 A apart along x.

    ⛔ A COORDINATE FIXTURE FOR A PARSER, NOT SCIENTIFIC DATA. It is built here
    (the same way the platform module's own tests build theirs) rather than
    pointed at a repo data file, so the frame guard is exercised without
    depending on a path that can move. The 6 A spacing keeps neighbour counts
    low so residues read as EXPOSED and the ``accessible_*`` branch actually
    populates — the branch the no-structure case cannot reach. No number derived
    from it is reported as a result about any real molecule.
    """
    lines: list[str] = []
    serial = 1
    for i, aa in enumerate(seq):
        resnum = first + i
        x = 6.0 * i
        for atom, elem, dx, dy in (("CA", "C", 0.0, 0.0), ("CB", "C", 0.0, 1.5)):
            lines.append(
                f"ATOM  {serial:>5} {atom:<4}{_THREE_LETTER[aa]:>4} A{resnum:>4}    "
                f"{x + dx:>8.3f}{dy:>8.3f}{0.0:>8.3f}"
                f"{1.00:>6.2f}{0.00:>6.2f}          {elem:>2}"
            )
            serial += 1
    lines.append("TER")
    lines.append("END")
    return "\n".join(lines) + "\n"


@pytest.fixture
def gi_client() -> LigandAI:
    return LigandAI(api_key="lgai_ent_test", base_url=BASE, max_retries=1)


@pytest.fixture
def platform_modules(monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """Point the local paths at the platform checkout, as a customer would."""
    monkeypatch.setenv(PLATFORM_MODULE_PATH_ENV, str(PLATFORM_MODULES))
    return PLATFORM_MODULES


def structure_record(**overrides: object) -> dict[str, object]:
    rec: dict[str, object] = {
        "sequence": PROTEIN_SEQ,
        "label": "coordinate fixture",
        "chain_id": "A",
        "residue_offset": CORRECT_OFFSET,
        "structure_content": synthetic_pdb(),
        "structure_format": "pdb",
    }
    rec.update(overrides)
    return rec


# ── registration ─────────────────────────────────────────────────────────────


def test_the_new_methods_are_registered_without_losing_the_old_ones(
    gi_client: LigandAI,
) -> None:
    """⛔ NO REGRESSION. The pre-existing surface must still be there — this
    resource already shipped, and a customer pinned to it must not lose a method
    because two more were added."""
    existing = (
        "score",
        "score_local",
        "generation_settings",
        "entitlement",
        "read_sidecar",
        "declaration",
    )
    added = (
        "chameleonicity",
        "chameleonicity_local",
        "protein_gut_stability",
        "protein_gut_stability_local",
    )
    for name in existing + added:
        assert hasattr(gi_client.gut_stability, name), name

    async_client = AsyncLigandAI(api_key="lgai_ent_test", base_url=BASE)
    for name in existing + added:
        assert hasattr(async_client.gut_stability, name), name


def test_the_error_class_is_a_top_level_export() -> None:
    import ligandai

    assert ligandai.LigandAIPlatformModuleUnavailable is (
        LigandAIPlatformModuleUnavailable
    )
    assert "LigandAIPlatformModuleUnavailable" in ligandai.__all__


def test_paths_are_the_frozen_contract() -> None:
    assert GI_STABILITY_SCORING_PATH == "/api/scoring/gut-stability"
    assert CHAMELEONICITY_SCORING_PATH == "/api/scoring/chameleonicity"
    assert PROTEIN_GUT_STABILITY_SCORING_PATH == "/api/scoring/protein-gut-stability"
    assert STRUCTURE_FORMATS == {"pdb", "ent", "cif", "mmcif", "mcif", "pdbx"}
    assert VALID_CYCLIC_MODES == {"none", "disulfide", "head_tail_contact", "lactam"}
    # ⛔ Three distinct codes, matched exactly. A prefix match on
    # "INDETERMINATE_" would make "you gave me no structure" and "your
    # residue_offset is wrong" the same answer, and they need different fixes.
    assert INDETERMINATE_VERDICTS == (
        "INDETERMINATE_NO_STRUCTURE",
        "INDETERMINATE_FRAME_MISMATCH",
        "INDETERMINATE_NO_ACCESSIBILITY",
    )
    assert len(set(INDETERMINATE_VERDICTS)) == 3


# ── INVARIANT 1 — a linear construct is NOT-APPLICABLE, never 0.0 ────────────


@needs_platform_modules
def test_linear_construct_is_not_applicable_and_index_is_none_not_zero(
    gi_client: LigandAI, platform_modules: pathlib.Path
) -> None:
    """⛔ THE INVARIANT. Chameleonicity is a property of a constrained
    macrocycle. A linear peptide has no ring to close a transannular network
    across and no collapsed state to switch into, so the quantity is UNDEFINED.
    0.0 would read as "a bad chameleon"; None means "the question does not
    apply"."""
    out = gi_client.gut_stability.chameleonicity_local([GNRH])
    rec = out["results"][0]

    assert rec["applicable"] is False
    # The key is PRESENT and None. Not dropped, not 0.0, not falsy-by-accident.
    assert "index" in rec
    assert rec["index"] is None
    assert rec["index"] != 0.0
    # ⛔ And it is not merely falsy-looking: a 0.0 would satisfy `not index`
    # while meaning something completely different.
    assert not isinstance(rec["index"], float)
    assert rec["reason"] == "not_a_cyclic_construct"
    assert "undefined rather than low" in rec["explanation"]

    assert out["batch"]["applicable"] == 0
    assert out["batch"]["not_applicable"] == 1
    assert "NOT 0.0" in out["batch"]["not_applicable_note"]


@needs_platform_modules
def test_a_linear_record_carries_no_index_bearing_terms(
    gi_client: LigandAI, platform_modules: pathlib.Path
) -> None:
    """A not-applicable record must not carry the per-term breakdown either —
    a `terms` block would invite a caller to recompute an index that does not
    exist."""
    rec = gi_client.gut_stability.chameleonicity_local([GNRH])["results"][0]
    assert "terms" not in rec
    assert "band" not in rec


@needs_platform_modules
def test_declared_linear_and_never_supplied_are_told_apart(
    gi_client: LigandAI, platform_modules: pathlib.Path
) -> None:
    """⛔ Both land in applicable=False, and they are NOT the same fact. An
    absent construct leaves cyclization UNKNOWN (cyclized_state None, the rescue
    term named in rescue_terms_omitted); an explicit linear declaration is
    False. Collapsing them would hide that nobody ever said."""
    unknown = gi_client.gut_stability.chameleonicity_local([GNRH])["results"][0]
    assert unknown["applicable"] is False
    assert unknown["construct"]["cyclized_state"] is None
    assert any(
        "cyclized" in term for term in unknown["construct"]["rescue_terms_omitted"]
    )

    declared_linear = gi_client.gut_stability.chameleonicity_local(
        [GNRH], constructs={"cyclic_mode": "none", "declared": {"cyclized": False}}
    )["results"][0]
    assert declared_linear["applicable"] is False
    assert declared_linear["construct"]["cyclized_state"] is False
    assert declared_linear["construct"]["cyclized_evidence"] == (
        "explicitly_declared_by_caller"
    )


# ── INVARIANT 2 — cyclicity comes from the realised construct ────────────────


@needs_platform_modules
def test_a_cyclic_construct_scores_and_names_its_route(
    gi_client: LigandAI, platform_modules: pathlib.Path
) -> None:
    out = gi_client.gut_stability.chameleonicity_local(
        [GNRH],
        constructs={
            "cyclic_mode": "lactam",
            "declared": {"cyclized": True, "n_methyl_count": 3, "d_residue_count": 2},
        },
    )
    rec = out["results"][0]
    assert rec["applicable"] is True
    assert isinstance(rec["index"], float)
    assert 0.0 <= rec["index"] <= 1.0
    assert rec["band"] in {
        "chameleonic_preconditions_met",
        "partial_preconditions",
        "preconditions_absent",
    }
    # The route is the REALISED one, derived by the construct resolver.
    assert rec["cyclization_route"] == "head_to_tail_amide"
    assert rec["construct"]["cyclized_state"] is True
    assert out["batch"]["applicable"] == 1


@needs_platform_modules
def test_scorer_kwargs_are_the_bridge_for_nmethyl_and_d_residues(
    gi_client: LigandAI, platform_modules: pathlib.Path
) -> None:
    """⛔ n_methyl_count / d_residue_count are read off the resolver's
    scorer_kwargs, not re-derived from the request — the resolver is where the
    realised construct is known. N-methylation removes backbone NH donors, so a
    declared count has to reach the donor arithmetic."""
    kw = {"cyclic_mode": "lactam", "declared": {"cyclized": True}}
    plain = gi_client.gut_stability.chameleonicity_local([GNRH], constructs=kw)[
        "results"
    ][0]
    methylated = gi_client.gut_stability.chameleonicity_local(
        [GNRH], constructs={**kw, "declared": {"cyclized": True, "n_methyl_count": 4}}
    )["results"][0]

    assert plain["backbone"]["n_methyl_effective"] == 0
    assert methylated["backbone"]["n_methyl_effective"] == 4
    assert (
        methylated["backbone"]["backbone_nh_donors"]
        < plain["backbone"]["backbone_nh_donors"]
    )
    assert methylated["index"] > plain["index"]


@needs_platform_modules
def test_cyclicity_is_not_taken_from_a_bare_request_flag(
    gi_client: LigandAI, platform_modules: pathlib.Path
) -> None:
    """⛔ THE +0.18 GUARD. `cyclized` is the largest rescue term in the sibling
    gut scorer, so it must never be a flag a caller can just set. Here the
    realised construct decides: a modality/cyclic_mode that resolves to no
    cyclic route leaves the peptide not-applicable even though the caller asked
    about chameleonicity."""
    out = gi_client.gut_stability.chameleonicity_local(
        [GNRH], constructs={"cyclic_mode": "none", "modality": "linear_l"}
    )
    rec = out["results"][0]
    assert rec["applicable"] is False
    assert rec["construct"]["cyclized_state"] is False
    assert rec["construct"]["cyclization_route"] == "none"


@needs_platform_modules
def test_a_disulfide_route_does_not_imply_terminal_protection(
    gi_client: LigandAI, platform_modules: pathlib.Path
) -> None:
    """A terminal disulfide bridges the Cys side chains while both termini stay
    free, so it must not buy the head-to-tail terminal-protection credits."""
    rec = gi_client.gut_stability.chameleonicity_local(
        ["CQHWSYGLRPGC"], constructs={"cyclic_mode": "disulfide"}
    )["results"][0]
    prov = rec["construct"]["provenance"]
    assert prov["n_terminal_protected"] == "default_unprotected"
    assert prov["c_terminal_protected"] == "default_unprotected"


# ── INVARIANT 3 — protein-gut absence is absence ─────────────────────────────


@needs_platform_modules
def test_no_structure_is_indeterminate_with_accessible_absent(
    gi_client: LigandAI, platform_modules: pathlib.Path
) -> None:
    """⛔ THE INVARIANT. Sequence motif counts alone cannot tell a stable folded
    protein from a labile one — mature FSH-beta reads 1.75 motif sites per
    residue from sequence and is a clinical protein. So no structure means no
    folded-state verdict, and the accessible_* fields are ABSENT, not zero."""
    out = gi_client.gut_stability.protein_gut_stability_local(
        [{"sequence": PROTEIN_SEQ, "label": "sequence only"}]
    )
    rec = out["results"][0]

    assert rec["verdict"] == "INDETERMINATE_NO_STRUCTURE"
    for panel in ("gastric", "intestinal"):
        p = rec[panel]
        # The sequence count IS present — it is the unfolded bound.
        assert p["sequence_motif_sites"] > 0
        # ⛔ accessible_sites is None, and the per-residue / weighted / shielded
        # fields are ABSENT ENTIRELY. A 0 in any of them would be the most
        # flattering verdict the module can emit, from having measured nothing.
        assert p["accessible_sites"] is None
        assert "accessible_sites_per_residue" not in p
        assert "exposure_weighted_sites" not in p
        assert "shielded_fraction" not in p
        assert "top_exposed_sites" not in p
        assert "ABSENT" in p["accessible_absent_reason"]
        assert "not zero" in p["accessible_absent_reason"]

    assert out["batch"]["indeterminate_no_structure"] == 1
    assert out["batch"]["indeterminate_frame_mismatch"] == 0


@needs_platform_modules
def test_a_wrong_residue_offset_is_a_frame_mismatch_not_fully_shielded(
    gi_client: LigandAI, platform_modules: pathlib.Path
) -> None:
    """⛔ THE SILENT-WRONG-ANSWER GUARD. residue_offset is the structure residue
    number of sequence position 1, so this chain (first modelled residue 29)
    needs 29. With 1, every motif site lands outside the modelled numbering. The
    arithmetic would otherwise report accessible_sites=0 and
    shielded_fraction=1.0 — 'fully shielded' — and score the BEST verdict from
    having measured nothing."""
    good = gi_client.gut_stability.protein_gut_stability_local(
        [structure_record(residue_offset=CORRECT_OFFSET)]
    )["results"][0]
    bad = gi_client.gut_stability.protein_gut_stability_local(
        [structure_record(residue_offset=1)]
    )["results"][0]

    assert good["verdict"] not in INDETERMINATE_VERDICTS
    assert good["intestinal"]["sites_mapped_to_structure"] > 0
    assert good["intestinal"]["accessible_sites"] is not None

    assert bad["verdict"] == "INDETERMINATE_FRAME_MISMATCH"
    assert bad["intestinal"]["frame_mismatch"] is True
    assert bad["intestinal"]["sites_mapped_to_structure"] == 0
    assert bad["intestinal"]["accessible_sites"] is None
    # ⛔ The flattering fields are ABSENT, not 1.0 / 0.
    assert "shielded_fraction" not in bad["intestinal"]
    assert "accessible_sites_per_residue" not in bad["intestinal"]
    assert "residue_offset" in bad["intestinal"]["accessible_absent_reason"]


@needs_platform_modules
def test_residue_offset_is_echoed_so_the_scored_frame_is_never_in_doubt(
    gi_client: LigandAI, platform_modules: pathlib.Path
) -> None:
    rec = gi_client.gut_stability.protein_gut_stability_local(
        [structure_record()]
    )["results"][0]
    assert rec["residue_offset_echo"] == CORRECT_OFFSET
    assert rec["structure"]["residue_offset"] == CORRECT_OFFSET
    assert rec["structure_supplied"] is True
    assert rec["structure_source"] == "structure_content"


@needs_platform_modules
def test_shielded_fraction_is_over_mapped_sites_only(
    gi_client: LigandAI, platform_modules: pathlib.Path
) -> None:
    """Dividing by the sequence count would credit unmapped sites as shielded
    and drift optimistic exactly as structural coverage falls."""
    panel = gi_client.gut_stability.protein_gut_stability_local(
        [structure_record()]
    )["results"][0]["intestinal"]
    assert "unmapped sites are EXCLUDED" in panel["shielded_fraction_basis"]
    assert panel["mapped_fraction"] == pytest.approx(1.0)
    assert panel["sites_unmapped"] == 0


@needs_platform_modules
def test_biobetter_targets_rank_by_accessibility_and_propose_nothing(
    gi_client: LigandAI, platform_modules: pathlib.Path
) -> None:
    """Ranked by accessibility x motif strength — mutating a buried site spends
    a variant on a bond no protease can reach — and the substitution is
    deliberately NOT proposed."""
    rec = gi_client.gut_stability.protein_gut_stability_local(
        [structure_record()], top_n=3
    )["results"][0]
    targets = rec["biobetter_targets"]
    assert 0 < len(targets) <= 3
    priorities = [t["priority"] for t in targets]
    assert priorities == sorted(priorities, reverse=True)
    for t in targets:
        assert t["substitution"].startswith("NOT PROPOSED")
        assert t["exposure"] > 0.0


@needs_platform_modules
def test_structure_path_is_accepted_on_the_local_path_only(
    gi_client: LigandAI, platform_modules: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """The file is on the caller's own machine here, so reading it is exactly
    what they asked for. The HTTP route rejects a server-side path."""
    path = tmp_path / "fixture.pdb"
    path.write_text(synthetic_pdb())
    rec = gi_client.gut_stability.protein_gut_stability_local(
        [
            {
                "sequence": PROTEIN_SEQ,
                "chain_id": "A",
                "residue_offset": CORRECT_OFFSET,
                "structure_path": str(path),
            }
        ]
    )["results"][0]
    assert rec["structure_source"] == "structure_path"
    assert rec["verdict"] not in INDETERMINATE_VERDICTS


@needs_platform_modules
def test_pdb_id_is_refused_on_the_local_path(
    gi_client: LigandAI, platform_modules: pathlib.Path
) -> None:
    """Fetching a PDB id is network egress and the local path is offline by
    contract — it must say so rather than quietly scoring sequence-only."""
    with pytest.raises(LigandAIValidationError) as exc:
        gi_client.gut_stability.protein_gut_stability_local(
            [{"sequence": PROTEIN_SEQ, "pdb_id": "1ABC"}]
        )
    assert "no network I/O" in str(exc.value)


# ── the declaration blocks ride on every response ────────────────────────────


@needs_platform_modules
def test_chameleonicity_declaration_is_the_modules_own_verbatim(
    gi_client: LigandAI, platform_modules: pathlib.Path
) -> None:
    """⛔ Read off the module, never retyped in the SDK — a second copy of the
    words would drift out of sync with the claim it qualifies."""
    module = _import_platform_module(
        CHAMELEONICITY_MODULE, needed_by="test", platform_module_path=str(PLATFORM_MODULES)
    )
    out = gi_client.gut_stability.chameleonicity_local([GNRH])

    assert out["declaration"] == module.DECLARATION
    assert out["results"][0]["declaration"] == module.DECLARATION

    decl = out["declaration"]
    assert decl["quantity"] == "rule_based_conformational_amphipathicity_index"
    assert decl["not_a_measured_permeability"] is True
    assert decl["not_an_oral_bioavailability_prediction"] is True
    assert decl["not_a_3d_calculation"] is True
    assert decl["no_assay_data_was_fitted"] is True
    assert "NOT_APPLICABLE on a linear construct" in decl["applicability"]


@needs_platform_modules
def test_protein_gut_declaration_states_the_formulation_caveat(
    gi_client: LigandAI, platform_modules: pathlib.Path
) -> None:
    out = gi_client.gut_stability.protein_gut_stability_local(
        [{"sequence": PROTEIN_SEQ}]
    )
    decl = out["declaration"]
    assert decl["quantity"] == "structure_weighted_rule_counts"
    assert decl["not_a_measured_degradation_rate"] is True
    assert decl["not_a_fraction_absorbed_prediction"] is True
    assert decl["not_a_ph_denaturation_model"] is True
    assert decl["not_a_neural_network"] is True
    assert "FORMULATION problem" in decl["oral_delivery_caveat"]
    assert "folded" in decl["folded_vs_unfolded"].lower()
    # The frame contract is part of the declaration, not a footnote.
    assert "INDETERMINATE_FRAME_MISMATCH" in decl["frame_check"]
    assert out["results"][0]["declaration"] == decl


def test_the_peptide_declaration_still_carries_the_unapplied_logistic(
    gi_client: LigandAI,
) -> None:
    """⛔ descriptor_logistic_baseline_not_applied survives on the peptide
    scorer. The 200-descriptor GI logistic (ROC-AUC 0.8112 gastric / 0.7435
    intestinal) needs whole-molecule RDKit descriptors and CANNOT be evaluated
    on a bare sequence, so it stays exposed only under that key."""
    decl = gi_client.gut_stability.score_local([GNRH])["declaration"]
    baseline = decl["descriptor_logistic_baseline_not_applied"]
    assert baseline["Gastric"]["roc_auc"] == pytest.approx(0.8112, abs=1e-4)
    assert baseline["Intestinal"]["roc_auc"] == pytest.approx(0.7435, abs=1e-4)
    assert baseline["cannot_be_evaluated_on"] == "a bare designed amino-acid sequence"
    assert "descriptor_logistic_baseline" not in decl


# ── batch shape on all three ─────────────────────────────────────────────────


@needs_platform_modules
def test_batch_shape_on_chameleonicity(
    gi_client: LigandAI, platform_modules: pathlib.Path
) -> None:
    seqs = [GNRH, "CQHWSYGLRPGC", "AAAKKKFFF"]
    out = gi_client.gut_stability.chameleonicity_local(
        seqs,
        constructs=[
            None,
            {"cyclic_mode": "disulfide", "declared": {"cyclized": True}},
            {"cyclic_mode": "none"},
        ],
    )
    assert len(out["results"]) == 3
    assert out["batch"]["n"] == 3
    assert out["batch"]["applicable"] + out["batch"]["not_applicable"] == 3
    assert out["batch"]["applicable"] == 1
    assert [r["sequence"] for r in out["results"]] == seqs


@needs_platform_modules
def test_batch_shape_on_protein_gut(
    gi_client: LigandAI, platform_modules: pathlib.Path
) -> None:
    out = gi_client.gut_stability.protein_gut_stability_local(
        [
            {"sequence": PROTEIN_SEQ, "label": "no structure"},
            structure_record(label="with structure"),
            structure_record(label="wrong frame", residue_offset=1),
        ]
    )
    assert len(out["results"]) == 3
    assert out["batch"]["n"] == 3
    assert out["batch"]["indeterminate_no_structure"] == 1
    assert out["batch"]["indeterminate_frame_mismatch"] == 1
    assert out["batch"]["indeterminate_total"] == 2
    assert [r["label"] for r in out["results"]] == [
        "no structure",
        "with structure",
        "wrong frame",
    ]


def test_batch_shape_on_the_peptide_scorer(gi_client: LigandAI) -> None:
    out = gi_client.gut_stability.score_local([GNRH, "", "AKFRAWKYR"])
    assert out["batch"]["n_input"] == 3
    assert out["batch"]["n_scored"] == 2
    assert out["batch"]["n_absent"] == 1


@needs_platform_modules
def test_a_single_sequence_and_a_single_record_are_normalized_to_a_batch(
    gi_client: LigandAI, platform_modules: pathlib.Path
) -> None:
    assert len(gi_client.gut_stability.chameleonicity_local(GNRH)["results"]) == 1
    single = gi_client.gut_stability.protein_gut_stability_local(
        {"sequence": PROTEIN_SEQ}
    )
    assert len(single["results"]) == 1


# ── local provenance ─────────────────────────────────────────────────────────


@needs_platform_modules
def test_local_results_say_they_were_computed_locally(
    gi_client: LigandAI, platform_modules: pathlib.Path
) -> None:
    """A local record and a platform record must never be indistinguishable in
    a customer's files."""
    for out in (
        gi_client.gut_stability.chameleonicity_local([GNRH]),
        gi_client.gut_stability.protein_gut_stability_local([{"sequence": PROTEIN_SEQ}]),
    ):
        assert out["computed_by"] == "ligandai_sdk_local_platform_module"
        assert out["platform_module"]["file"]
    cham = gi_client.gut_stability.chameleonicity_local([GNRH])
    assert cham["results"][0]["computed_by"] == "ligandai_sdk_local_platform_module"
    assert cham["platform_module"]["construct_resolver"] == "gut_stability_penalties"


@needs_platform_modules
def test_the_local_paths_need_no_network_and_no_api_key(
    monkeypatch: pytest.MonkeyPatch, platform_modules: pathlib.Path
) -> None:
    """⛔ OFFLINE BY CONTRACT. Built with no api_key and with the HTTP transport
    replaced by something that raises — if either local path touched the wire
    this would blow up rather than pass."""
    monkeypatch.delenv("LIGANDAI_API_KEY", raising=False)
    monkeypatch.delenv("LIGANDAL_API_KEY", raising=False)
    monkeypatch.delenv("LIGANDAI_TEST_API_KEY", raising=False)
    client = LigandAI(api_key="lgai_ent_test", base_url=BASE)

    def _explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a local scorer must not make an HTTP request")

    monkeypatch.setattr(client._transport, "request", _explode)

    cham = client.gut_stability.chameleonicity_local(
        [GNRH], platform_module_path=str(PLATFORM_MODULES)
    )
    assert cham["results"][0]["applicable"] is False
    protein = client.gut_stability.protein_gut_stability_local(
        [{"sequence": PROTEIN_SEQ}], platform_module_path=str(PLATFORM_MODULES)
    )
    assert protein["results"][0]["verdict"] == "INDETERMINATE_NO_STRUCTURE"
    # And the pinned in-SDK peptide port, which needs no platform module at all.
    assert client.gut_stability.score_local([GNRH])["results"][0]["status"] == "present"


# ── the platform modules being absent is an explicit error ───────────────────


def test_an_absent_platform_module_raises_instead_of_scoring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⛔ 'The scorer is not installed' and 'this molecule scored badly' are
    different facts. A pip-installed customer has no platform modules, and a
    package that answered the first with a number would manufacture the
    second."""
    monkeypatch.delenv(PLATFORM_MODULE_PATH_ENV, raising=False)
    with pytest.raises(LigandAIPlatformModuleUnavailable) as exc:
        _import_platform_module(
            "chameleonicity_absent_in_every_install_c80f4",
            needed_by="gut_stability.chameleonicity_local()",
        )
    err = exc.value
    assert err.module == "chameleonicity_absent_in_every_install_c80f4"
    assert err.needed_by == "gut_stability.chameleonicity_local()"
    assert err.env_var == PLATFORM_MODULE_PATH_ENV
    assert err.searched == ()
    # The message has to be actionable, not just a stack trace.
    assert "not importable" in str(err)
    assert PLATFORM_MODULE_PATH_ENV in str(err)
    assert "platform_module_path=" in str(err)
    assert "REMOTE method" in str(err)


def test_the_absence_error_is_also_an_importerror() -> None:
    """Matches the SDK's convention for a missing local dependency (matplotlib,
    numpy), so `except ImportError` keeps working, while anything that surfaces
    the message gets the full explanation."""
    assert issubclass(LigandAIPlatformModuleUnavailable, ImportError)
    from ligandai.errors import LigandAIError

    assert issubclass(LigandAIPlatformModuleUnavailable, LigandAIError)


def test_the_search_path_has_no_baked_in_internal_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⛔ A published wheel must never hand a customer an internal filesystem
    path — the same reason GI_SOURCE drops the platform module's `ported_from`
    key. So the default search path is EMPTY."""
    from ligandai.resources.gut_stability import _platform_search_dirs

    monkeypatch.delenv(PLATFORM_MODULE_PATH_ENV, raising=False)
    assert _platform_search_dirs() == []

    source = pathlib.Path(
        "/mnt/backup/ligandai-python-sdk/ligandai/resources/gut_stability.py"
    )
    if source.is_file():
        blob = source.read_text()
        for fragment in ("/mnt/backup", "/home/dre", "LIGANDAI_ALPHA_V2"):
            assert fragment not in blob, fragment


@needs_platform_modules
def test_importing_a_platform_module_does_not_leave_syspath_mutated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A permanent sys.path mutation would make "the module is absent" depend on
    which SDK call ran first in the process — order-dependent absence is exactly
    what an explicit-absence contract exists to prevent."""
    import sys

    monkeypatch.delenv(PLATFORM_MODULE_PATH_ENV, raising=False)
    before = list(sys.path)
    _import_platform_module(
        CHAMELEONICITY_MODULE,
        needed_by="test",
        platform_module_path=str(PLATFORM_MODULES),
    )
    assert str(PLATFORM_MODULES) not in set(sys.path) - set(before)


# ── the remote paths (MOCKED transport — no live server was involved) ────────

_CHAM_PAYLOAD = {
    "success": True,
    "count": 1,
    "results": [
        {
            "sequence": GNRH,
            "applicable": False,
            "index": None,
            "reason": "not_a_cyclic_construct",
            "declaration": {"quantity": "rule_based_conformational_amphipathicity_index"},
        }
    ],
    "declaration": {"quantity": "rule_based_conformational_amphipathicity_index"},
    "batch": {"n": 1, "applicable": 0, "not_applicable": 1},
}

_PROTEIN_PAYLOAD = {
    "success": True,
    "count": 1,
    "results": [
        {
            "label": None,
            "length": len(PROTEIN_SEQ),
            "verdict": "INDETERMINATE_NO_STRUCTURE",
            "intestinal": {
                "sequence_motif_sites": 18,
                "accessible_sites": None,
                "accessible_absent_reason": "no structure supplied — ABSENT, not zero",
            },
            "declaration": {"quantity": "structure_weighted_rule_counts"},
        }
    ],
    "declaration": {"quantity": "structure_weighted_rule_counts"},
    "batch": {"n": 1, "indeterminate_no_structure": 1},
}


def test_chameleonicity_posts_the_batch_and_returns_the_envelope(
    httpx_mock: HTTPXMock, gi_client: LigandAI
) -> None:
    """MOCKED. Pins the request body and the envelope pass-through; the route is
    not mounted on any deployment yet, so this is not a live round trip."""
    httpx_mock.add_response(
        url=f"{BASE}{CHAMELEONICITY_SCORING_PATH}", json=_CHAM_PAYLOAD
    )
    out = gi_client.gut_stability.chameleonicity(
        GNRH, constructs={"cyclic_mode": "lactam"}
    )
    # ⛔ index stays None across the wire — no coercion on the way back either.
    assert out["results"][0]["index"] is None
    assert out["results"][0]["applicable"] is False
    assert out["declaration"]["quantity"] == (
        "rule_based_conformational_amphipathicity_index"
    )

    body = _json.loads(httpx_mock.get_request().read())
    assert body["sequences"] == [GNRH]
    # ⛔ Resolved to a list ALIGNED to sequences before it goes out. Forwarding
    # the caller's shared object would let the route resolve the alignment a
    # second time, and two resolutions of one side-channel is how a construct
    # ends up applied to the wrong sequence.
    assert body["constructs"] == [{"cyclic_mode": "lactam"}]


def test_protein_gut_reads_a_local_path_and_never_sends_it(
    httpx_mock: HTTPXMock, gi_client: LigandAI, tmp_path: pathlib.Path
) -> None:
    """⛔ MOCKED, and this is the security-relevant assertion: a server-side
    path in a request body is an arbitrary-file-read primitive, and the route
    rejects it. The SDK reads the caller's file HERE and forwards the content."""
    path = tmp_path / "fixture.pdb"
    path.write_text(synthetic_pdb())
    httpx_mock.add_response(
        url=f"{BASE}{PROTEIN_GUT_STABILITY_SCORING_PATH}", json=_PROTEIN_PAYLOAD
    )

    out = gi_client.gut_stability.protein_gut_stability(
        [
            {
                "sequence": PROTEIN_SEQ,
                "chain_id": "A",
                "residue_offset": CORRECT_OFFSET,
                "structure_path": str(path),
            }
        ],
        top_n=7,
    )
    assert out["results"][0]["verdict"] == "INDETERMINATE_NO_STRUCTURE"

    body = _json.loads(httpx_mock.get_request().read())
    rec = body["records"][0]
    assert "structure_path" not in rec
    assert str(path) not in _json.dumps(body)
    assert rec["structure_content"].startswith("ATOM")
    assert rec["structure_format"] == "pdb"
    assert rec["chain_id"] == "A"
    assert rec["residue_offset"] == CORRECT_OFFSET
    assert body["top_n"] == 7


def test_protein_gut_forwards_a_pdb_id_for_the_server_to_fetch(
    httpx_mock: HTTPXMock, gi_client: LigandAI
) -> None:
    """MOCKED. Network egress belongs in one place — the API layer."""
    httpx_mock.add_response(
        url=f"{BASE}{PROTEIN_GUT_STABILITY_SCORING_PATH}", json=_PROTEIN_PAYLOAD
    )
    gi_client.gut_stability.protein_gut_stability(
        [
            {
                "sequence": PROTEIN_SEQ,
                "pdb_id": "4MQW",
                "chain_id": "A",
                "residue_offset": 29,
            }
        ]
    )
    rec = _json.loads(httpx_mock.get_request().read())["records"][0]
    assert rec["pdb_id"] == "4MQW"
    assert "structure_content" not in rec


def test_score_returns_results_when_the_route_is_mounted(
    httpx_mock: HTTPXMock, gi_client: LigandAI
) -> None:
    """⛔ DELIVERABLE C.1 — no hardcoded refusal in the success path. The
    existing file pins the NOT-mounted case; this pins that a mounted route's
    results come back. MOCKED: the route 404s on the running server until a
    deploy."""
    httpx_mock.add_response(
        url=f"{BASE}{GI_STABILITY_SCORING_PATH}",
        json={
            "success": True,
            "count": 1,
            "results": [
                {"sequence": GNRH, "status": "present", "gi_balance_score": 0.402}
            ],
            "declaration": {"quantity": "rule_count_normalized"},
            "batch": {"n_input": 1, "n_scored": 1, "n_absent": 0},
        },
    )
    out = gi_client.gut_stability.score([GNRH])
    assert out["results"][0]["gi_balance_score"] == pytest.approx(0.402)
    assert out["batch"]["n_scored"] == 1


@pytest.mark.parametrize("status", [404, 405, 501])
@pytest.mark.parametrize(
    ("path", "method", "local_name"),
    [
        (CHAMELEONICITY_SCORING_PATH, "chameleonicity", "chameleonicity_local"),
        (
            PROTEIN_GUT_STABILITY_SCORING_PATH,
            "protein_gut_stability",
            "protein_gut_stability_local",
        ),
    ],
)
def test_an_unmounted_route_names_its_local_equivalent(
    httpx_mock: HTTPXMock,
    gi_client: LigandAI,
    status: int,
    path: str,
    method: str,
    local_name: str,
) -> None:
    httpx_mock.add_response(
        url=f"{BASE}{path}", status_code=status, json={"message": "Not Found"}
    )
    args = (
        [GNRH] if method == "chameleonicity" else [{"sequence": PROTEIN_SEQ}]
    )
    with pytest.raises(LigandAIParameterNotForwarded) as exc:
        getattr(gi_client.gut_stability, method)(args)
    assert "not mounted" in str(exc.value)
    assert local_name in str(exc.value)
    assert exc.value.override == f"use {local_name}()"


@pytest.mark.parametrize(
    ("path", "method"),
    [
        (CHAMELEONICITY_SCORING_PATH, "chameleonicity"),
        (PROTEIN_GUT_STABILITY_SCORING_PATH, "protein_gut_stability"),
    ],
)
def test_an_spa_shell_200_counts_as_unmounted(
    httpx_mock: HTTPXMock, gi_client: LigandAI, path: str, method: str
) -> None:
    """⛔ An unmounted path on a single-page-app server answers 200 with the app
    shell, not a 404. A 2xx body with no results list and no declaration cannot
    have come from the scorer."""
    httpx_mock.add_response(url=f"{BASE}{path}", json={"ok": True})
    args = [GNRH] if method == "chameleonicity" else [{"sequence": PROTEIN_SEQ}]
    with pytest.raises(LigandAIParameterNotForwarded):
        getattr(gi_client.gut_stability, method)(args)


# ── entitlement: a refusal is raised, never a zeroed score ───────────────────


@pytest.mark.parametrize(
    ("path", "method"),
    [
        (GI_STABILITY_SCORING_PATH, "score"),
        (CHAMELEONICITY_SCORING_PATH, "chameleonicity"),
        (PROTEIN_GUT_STABILITY_SCORING_PATH, "protein_gut_stability"),
    ],
)
def test_a_403_is_raised_not_turned_into_a_score(
    httpx_mock: HTTPXMock, gi_client: LigandAI, path: str, method: str
) -> None:
    """⛔ THE ENTITLEMENT INVARIANT. A non-entitled caller must be REFUSED, not
    quietly handed a number. Three distinct things must not collapse into one:
    the route is absent (ParameterNotForwarded), the gate refused (403), and the
    scorer ran (results). A 403 rewritten as 'use the local path' would tell a
    customer their account is fine when it is not."""
    httpx_mock.add_response(
        url=f"{BASE}{path}",
        status_code=403,
        json={
            "error": "tier_required",
            "message": "gi_stability module access required",
            "requiredTier": "enterprise",
            "currentTier": "pro",
        },
    )
    args = (
        [GNRH]
        if method in {"score", "chameleonicity"}
        else [{"sequence": PROTEIN_SEQ}]
    )
    with pytest.raises(Exception) as exc:
        getattr(gi_client.gut_stability, method)(args)

    # Raised, and NOT rewritten into the route-absent refusal.
    assert not isinstance(exc.value, LigandAIParameterNotForwarded)
    assert getattr(exc.value, "status_code", None) == 403
    assert getattr(exc.value, "required_tier", None) == "enterprise"


@pytest.mark.parametrize(
    ("path", "method"),
    [
        (CHAMELEONICITY_SCORING_PATH, "chameleonicity"),
        (PROTEIN_GUT_STABILITY_SCORING_PATH, "protein_gut_stability"),
    ],
)
def test_a_plain_403_still_raises_without_a_tier_hint(
    httpx_mock: HTTPXMock, gi_client: LigandAI, path: str, method: str
) -> None:
    """A module-grant refusal carries no requiredTier. It must still raise."""
    httpx_mock.add_response(
        url=f"{BASE}{path}", status_code=403, json={"message": "Forbidden"}
    )
    args = [GNRH] if method == "chameleonicity" else [{"sequence": PROTEIN_SEQ}]
    with pytest.raises(Exception) as exc:
        getattr(gi_client.gut_stability, method)(args)
    assert not isinstance(exc.value, LigandAIParameterNotForwarded)
    assert getattr(exc.value, "status_code", None) == 403


# ── client-side validation, before any request is sent ───────────────────────


def test_chameleonicity_validates_before_any_request(gi_client: LigandAI) -> None:
    with pytest.raises(LigandAIValidationError):
        gi_client.gut_stability.chameleonicity([])
    with pytest.raises(LigandAIValidationError, match="cyclic_mode"):
        gi_client.gut_stability.chameleonicity(
            [GNRH], constructs={"cyclic_mode": "head_to_tail"}
        )
    with pytest.raises(LigandAIValidationError):
        gi_client.gut_stability.chameleonicity(
            [GNRH], constructs={"declared": {"n_methyl_count": "three"}}
        )
    with pytest.raises(LigandAIValidationError, match="length"):
        gi_client.gut_stability.chameleonicity([GNRH, GNRH], constructs=[{}])
    with pytest.raises(LigandAIValidationError):
        gi_client.gut_stability.chameleonicity(["A" * 3] * 501)


def test_a_miskeyed_construct_map_is_refused_not_shared(gi_client: LigandAI) -> None:
    """⛔ A {sequence: construct} map whose keys do not match the batch would
    otherwise fall through to 'one shared construct' and silently apply the
    wrong cyclicity to every sequence. cyclized is worth +0.18."""
    with pytest.raises(LigandAIValidationError) as exc:
        gi_client.gut_stability.chameleonicity(
            [GNRH], constructs={GNRH.lower(): {"cyclic_mode": "lactam"}}
        )
    assert "keyed by the sequences" in str(exc.value)


def test_a_correctly_keyed_construct_map_is_resolved_onto_the_batch(
    httpx_mock: HTTPXMock, gi_client: LigandAI
) -> None:
    httpx_mock.add_response(
        url=f"{BASE}{CHAMELEONICITY_SCORING_PATH}", json=_CHAM_PAYLOAD
    )
    gi_client.gut_stability.chameleonicity(
        [GNRH, "AAAKKK", "CCWWFF"],
        constructs={
            # ⛔ Deliberately DIFFERENT per sequence and given out of order, so a
            # swapped alignment fails this test instead of passing by symmetry.
            "AAAKKK": {"cyclic_mode": "disulfide"},
            GNRH: {"cyclic_mode": "lactam"},
        },
    )
    body = _json.loads(httpx_mock.get_request().read())
    # An aligned list is the ONE shape with no ambiguity between "a map keyed by
    # sequence" and "one shared construct whose keys happen to look like
    # sequences". The unlisted sequence stays ABSENT (null), which leaves its
    # cyclization UNKNOWN rather than asserting linear.
    assert body["sequences"] == [GNRH, "AAAKKK", "CCWWFF"]
    assert body["constructs"] == [
        {"cyclic_mode": "lactam"},
        {"cyclic_mode": "disulfide"},
        None,
    ]


# ── ONE spelling for declared chemistry, flat or nested ──────────────────────


@needs_platform_modules
def test_flat_and_nested_chemistry_give_the_same_chameleonicity_record(
    gi_client: LigandAI, platform_modules: pathlib.Path
) -> None:
    """⛔ THE SILENT-IGNORE GUARD. The two platform ops once wanted opposite
    shapes for the same input and each ignored the other with no error: flat keys
    on chameleonicity left backbone NH donors at 9 / index 0.6947 where the
    nested spelling gave 6 / 0.8487. One normalizer now handles both.

    ⛔ AND the second assertion is the one that matters: a test that only checked
    "both shapes return a record" would pass just as well on two equally-IGNORED
    inputs — which is exactly how the defect survived. So this also pins that the
    chemistry MOVES the number."""
    flat = gi_client.gut_stability.chameleonicity_local(
        [GNRH],
        constructs={"cyclic_mode": "lactam", "cyclized": True, "n_methyl_count": 4},
    )["results"][0]
    nested = gi_client.gut_stability.chameleonicity_local(
        [GNRH],
        constructs={
            "cyclic_mode": "lactam",
            "declared": {"cyclized": True, "n_methyl_count": 4},
        },
    )["results"][0]
    no_chemistry = gi_client.gut_stability.chameleonicity_local(
        [GNRH], constructs={"cyclic_mode": "lactam", "declared": {"cyclized": True}}
    )["results"][0]

    # Same spelling, same answer.
    assert flat["index"] == nested["index"]
    assert flat["backbone"] == nested["backbone"]
    assert flat["terms"] == nested["terms"]

    # ⛔ And the chemistry is not being ignored by BOTH paths.
    assert flat["backbone"]["n_methyl_effective"] == 4
    assert no_chemistry["backbone"]["n_methyl_effective"] == 0
    assert flat["index"] != no_chemistry["index"]
    assert (
        flat["backbone"]["backbone_nh_donors"]
        < no_chemistry["backbone"]["backbone_nh_donors"]
    )


def test_a_nested_value_wins_a_conflict_with_a_flat_one(
    gi_client: LigandAI,
) -> None:
    """The nested spelling is the more specific statement, so it wins — and the
    resolution is DETERMINISTIC rather than dict-ordering dependent."""
    from ligandai.resources.gut_stability import _merge_declared

    merged = _merge_declared({"n_methyl_count": 1, "declared": {"n_methyl_count": 5}})
    assert merged["n_methyl_count"] == 5
    # An unsupplied key stays unsupplied, so the scorer applies its own
    # conservative default rather than one invented here.
    assert "cyclized" not in merged
    assert _merge_declared(None) == {}
    assert _merge_declared({}) == {}


@needs_platform_modules
def test_flat_and_nested_chemistry_agree_on_the_peptide_scorer_too(
    gi_client: LigandAI, platform_modules: pathlib.Path
) -> None:
    """Same one normalizer on score_local, so the SDK cannot develop the very
    split it just removed a layer down."""
    mods = {"cyclized": True, "n_terminal_protected": True, "n_methyl_count": 3}
    flat = gi_client.gut_stability.score_local([GNRH], modifications=mods)["results"][0]
    nested = gi_client.gut_stability.score_local(
        [GNRH], modifications={"declared": mods}
    )["results"][0]
    bare = gi_client.gut_stability.score_local([GNRH])["results"][0]

    assert flat["modifications"] == nested["modifications"]
    assert flat["gi_balance_score"] == nested["gi_balance_score"]
    # And the chemistry is doing something — a rescue of 0.10 + 0.18 + 0.075.
    assert flat["gi_balance_score"] > bare["gi_balance_score"]
    assert flat["modification_rescue"] > 0


def test_both_chemistry_spellings_go_out_on_the_wire(
    httpx_mock: HTTPXMock, gi_client: LigandAI
) -> None:
    """A deployment whose op reads only one spelling still sees the chemistry.
    Sending one and hoping is how the silent-ignore defect survived."""
    httpx_mock.add_response(
        url=f"{BASE}{CHAMELEONICITY_SCORING_PATH}", json=_CHAM_PAYLOAD
    )
    gi_client.gut_stability.chameleonicity(
        [GNRH], constructs={"cyclic_mode": "lactam", "n_methyl_count": 4}
    )
    construct = _json.loads(httpx_mock.get_request().read())["constructs"][0]
    assert construct["n_methyl_count"] == 4
    assert construct["declared"]["n_methyl_count"] == 4
    assert construct["cyclic_mode"] == "lactam"

    httpx_mock.reset()
    httpx_mock.add_response(
        url=f"{BASE}{GI_STABILITY_SCORING_PATH}",
        json={
            "success": True,
            "count": 1,
            "results": [{"sequence": GNRH, "status": "present"}],
            "declaration": {"quantity": "rule_count_normalized"},
            "batch": {"n_input": 1},
        },
    )
    gi_client.gut_stability.score([GNRH], modifications={"declared": {"cyclized": True}})
    mods = _json.loads(httpx_mock.get_request().read())["modifications"]
    assert mods["cyclized"] is True
    assert mods["declared"]["cyclized"] is True


def test_a_declared_only_modification_record_is_not_read_as_a_sequence_map(
    gi_client: LigandAI,
) -> None:
    """⛔ `{"declared": {...}}` used to fail the "is this a modification record"
    shape test, fall through to the sequence-keyed branch, and resolve to NO
    modifications at all — silently."""
    from ligandai.resources.gut_stability import _mods_for

    resolved = _mods_for(0, GNRH, {"declared": {"cyclized": True, "n_methyl_count": 2}})
    assert resolved["cyclized"] is True
    assert resolved["n_methyl_count"] == 2


def test_protein_gut_validates_the_frame_before_any_request(
    gi_client: LigandAI, tmp_path: pathlib.Path
) -> None:
    path = tmp_path / "fixture.pdb"
    path.write_text(synthetic_pdb())

    with pytest.raises(LigandAIValidationError, match="records"):
        gi_client.gut_stability.protein_gut_stability([])
    with pytest.raises(LigandAIValidationError, match="sequence"):
        gi_client.gut_stability.protein_gut_stability([{"label": "no sequence"}])

    # ⛔ chain_id and residue_offset are both required once a structure exists.
    with pytest.raises(LigandAIValidationError, match="chain_id"):
        gi_client.gut_stability.protein_gut_stability(
            [{"sequence": PROTEIN_SEQ, "structure_path": str(path), "residue_offset": 29}]
        )
    with pytest.raises(LigandAIValidationError) as exc:
        gi_client.gut_stability.protein_gut_stability(
            [{"sequence": PROTEIN_SEQ, "structure_path": str(path), "chain_id": "A"}]
        )
    # The message has to teach the convention, because the failure is silent.
    assert "STRUCTURE RESIDUE NUMBER OF SEQUENCE POSITION 1" in str(exc.value)
    assert "not 28" in str(exc.value).replace("NOT 28", "not 28")


def test_protein_gut_refuses_two_structure_sources(gi_client: LigandAI) -> None:
    with pytest.raises(LigandAIValidationError, match="more than one structure"):
        gi_client.gut_stability.protein_gut_stability(
            [
                {
                    "sequence": PROTEIN_SEQ,
                    "pdb_id": "4MQW",
                    "structure_content": synthetic_pdb(),
                    "chain_id": "A",
                    "residue_offset": 29,
                }
            ]
        )


def test_protein_gut_rejects_a_bad_pdb_id_and_a_bad_format(
    gi_client: LigandAI,
) -> None:
    with pytest.raises(LigandAIValidationError, match="pdb_id"):
        gi_client.gut_stability.protein_gut_stability(
            [
                {
                    "sequence": PROTEIN_SEQ,
                    "pdb_id": "4MQWX",
                    "chain_id": "A",
                    "residue_offset": 29,
                }
            ]
        )
    with pytest.raises(LigandAIValidationError, match="structure_format"):
        gi_client.gut_stability.protein_gut_stability(
            [
                {
                    "sequence": PROTEIN_SEQ,
                    "structure_content": synthetic_pdb(),
                    "structure_format": "xyz",
                    "chain_id": "A",
                    "residue_offset": 29,
                }
            ]
        )


def test_an_unreadable_structure_path_fails_before_the_request(
    gi_client: LigandAI, tmp_path: pathlib.Path
) -> None:
    """⛔ A missing file is not a measurement. It must not degrade into a
    sequence-only score that silently drops the accessibility question."""
    with pytest.raises(LigandAIValidationError, match="does not exist"):
        gi_client.gut_stability.protein_gut_stability(
            [
                {
                    "sequence": PROTEIN_SEQ,
                    "structure_path": str(tmp_path / "absent.pdb"),
                    "chain_id": "A",
                    "residue_offset": 29,
                }
            ]
        )
    unknown = tmp_path / "fixture.xyz"
    unknown.write_text(synthetic_pdb())
    with pytest.raises(LigandAIValidationError, match="structure_format"):
        gi_client.gut_stability.protein_gut_stability(
            [
                {
                    "sequence": PROTEIN_SEQ,
                    "structure_path": str(unknown),
                    "chain_id": "A",
                    "residue_offset": 29,
                }
            ]
        )


def test_top_n_is_clamped_not_rejected(
    httpx_mock: HTTPXMock, gi_client: LigandAI
) -> None:
    httpx_mock.add_response(
        url=f"{BASE}{PROTEIN_GUT_STABILITY_SCORING_PATH}", json=_PROTEIN_PAYLOAD
    )
    gi_client.gut_stability.protein_gut_stability(
        [{"sequence": PROTEIN_SEQ}], top_n=9_999
    )
    assert _json.loads(httpx_mock.get_request().read())["top_n"] == 100

    with pytest.raises(LigandAIValidationError, match="top_n"):
        gi_client.gut_stability.protein_gut_stability(
            [{"sequence": PROTEIN_SEQ}], top_n="ten"  # type: ignore[arg-type]
        )


def test_protein_gut_refuses_an_oversized_batch(gi_client: LigandAI) -> None:
    with pytest.raises(LigandAIValidationError, match="maximum 25"):
        gi_client.gut_stability.protein_gut_stability(
            [{"sequence": PROTEIN_SEQ}] * 26
        )


# ── async surface ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_async_chameleonicity_posts_and_keeps_index_none(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        url=f"{BASE}{CHAMELEONICITY_SCORING_PATH}", json=_CHAM_PAYLOAD
    )
    async with AsyncLigandAI(api_key="lgai_ent_test", base_url=BASE) as client:
        out = await client.gut_stability.chameleonicity([GNRH])
    assert out["results"][0]["index"] is None


@pytest.mark.asyncio
async def test_async_protein_gut_unmounted_names_the_local_path(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        url=f"{BASE}{PROTEIN_GUT_STABILITY_SCORING_PATH}",
        status_code=404,
        json={"message": "Not Found"},
    )
    async with AsyncLigandAI(api_key="lgai_ent_test", base_url=BASE) as client:
        with pytest.raises(LigandAIParameterNotForwarded) as exc:
            await client.gut_stability.protein_gut_stability(
                [{"sequence": PROTEIN_SEQ}]
            )
    assert "protein_gut_stability_local" in str(exc.value)


@needs_platform_modules
@pytest.mark.asyncio
async def test_the_async_local_paths_are_the_same_functions(
    platform_modules: pathlib.Path,
) -> None:
    """⛔ SHARED, NOT RE-TYPED. The local paths do no I/O, so the async resource
    reuses the same functions rather than carrying coroutine copies — which is
    what stops the sync and async surfaces drifting into two rule sets."""
    from ligandai.resources.gut_stability import AsyncGutStability, GutStability

    assert AsyncGutStability.chameleonicity_local is GutStability.chameleonicity_local
    assert (
        AsyncGutStability.protein_gut_stability_local
        is GutStability.protein_gut_stability_local
    )

    async with AsyncLigandAI(api_key="lgai_ent_test", base_url=BASE) as client:
        out = client.gut_stability.chameleonicity_local([GNRH])
    assert out["results"][0]["index"] is None
