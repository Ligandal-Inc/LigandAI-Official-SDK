# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Unit tests for the `gut_stability` resource. [bd-dre-c80f4.1]

Covers the GI / gut-stability developability module's SDK surface:
  - ``client.gut_stability.score``             → POST /api/scoring/gut-stability
  - ``client.gut_stability.score_local``       → the same rule set, in-process
  - ``client.gut_stability.generation_settings`` → the gi_stability_enabled block
  - ``client.gut_stability.entitlement``       → why a value is absent
  - ``client.gut_stability.read_sidecar``      → the platform sidecar, or absent
  - the declaration contract, which is the point of the whole module

⛔ THE TWO WORKED EXAMPLES ARE MEASURED VALUES, NOT ILLUSTRATIONS. GnRH
(QHWSYGLRPG) bare and the same peptide cyclized with both termini protected
were scored on 2026-09-23 and are pinned here to four decimals. A change that
moves either one means the SDK's ruleset and the platform's have diverged —
which is worse than having no local scorer at all, because a customer would
then get two different numbers for one peptide and nothing would say so.
"""

from __future__ import annotations

import importlib.util
import json as _json
import pathlib

import pytest
from pytest_httpx import HTTPXMock

from ligandai import AsyncLigandAI, LigandAI
from ligandai.errors import LigandAIParameterNotForwarded, LigandAIValidationError
from ligandai.resources.gut_stability import (
    DESCRIPTOR_LOGISTIC_BASELINE,
    GI_STABILITY_CANONICAL_WIRE_KEY,
    GI_STABILITY_MODULE_GRANT_KEYS,
    GI_STABILITY_SCORING_PATH,
    GUT_STABILITY_RULESET_VERSION,
    build_gi_stability_record,
    explain_entitlement,
    gut_stability_declaration,
    length_bin_for_length,
    protease_motif_counts,
    score_gut_design,
)

BASE = "http://api.ligandai.test"

#: GnRH (gonadorelin). Chosen because its single K/R site is R-P — the proline
#: shield — so a scorer that ignored the `nxt != "P"` clause would report a
#: trypsin liability that the rules say is not there.
GNRH = "QHWSYGLRPG"


@pytest.fixture
def gi_client() -> LigandAI:
    return LigandAI(api_key="lgai_ent_test", base_url=BASE, max_retries=1)


# ── registration ─────────────────────────────────────────────────────────────


def test_gut_stability_resource_registered(gi_client: LigandAI) -> None:
    methods = (
        "score",
        "score_local",
        "generation_settings",
        "entitlement",
        "read_sidecar",
        "declaration",
    )
    assert hasattr(gi_client, "gut_stability")
    for m in methods:
        assert hasattr(gi_client.gut_stability, m), m

    async_client = AsyncLigandAI(api_key="lgai_ent_test", base_url=BASE)
    assert hasattr(async_client, "gut_stability")
    for m in methods:
        assert hasattr(async_client.gut_stability, m), m


# ── the declaration contract ─────────────────────────────────────────────────


def test_declaration_states_all_four_negatives() -> None:
    d = gut_stability_declaration()
    assert d["quantity"] == "rule_count_normalized"
    assert d["not_a_measured_rate"] is True
    assert d["not_a_survival_probability"] is True
    assert d["not_an_oral_bioavailability_prediction"] is True
    assert d["not_a_neural_network"] is True
    assert "rule counts normalized by length" in d["statement"].lower()


def test_declaration_carries_the_source_and_its_missing_sequence_column() -> None:
    src = gut_stability_declaration()["source"]
    assert src["figshare_doi"] == "10.6084/m9.figshare.25941580.v1"
    assert src["rows"] == 109
    assert src["environment_counts"] == {"Gastric": 46, "Intestinal": 63}
    assert src["descriptor_columns"] == 200
    # The reason the descriptor logistic cannot be evaluated on a sequence.
    assert src["has_amino_acid_sequence_column"] is False
    assert src["has_smiles_column"] is False


def test_descriptor_logistic_is_carried_not_applied() -> None:
    """The 200-descriptor baseline rides on the declaration as a NOT-APPLIED
    record. Its ROC-AUCs are real; they describe a model this module never runs."""
    d = gut_stability_declaration()
    baseline = d["descriptor_logistic_baseline_not_applied"]
    assert baseline is not None
    assert baseline["Gastric"]["roc_auc"] == pytest.approx(0.8112, abs=1e-4)
    assert baseline["Intestinal"]["roc_auc"] == pytest.approx(0.7435, abs=1e-4)
    assert baseline["input_requirement"] == "200 RDKit whole-molecule descriptors"
    assert baseline["cannot_be_evaluated_on"] == "a bare designed amino-acid sequence"
    # ⛔ The key NAME carries the contract: a reader who sees only the key must
    # still learn the baseline was not run. Renaming it to a neutral
    # "descriptor_logistic_baseline" would lose that.
    assert "descriptor_logistic_baseline_not_applied" in d
    assert "descriptor_logistic_baseline" not in d
    assert DESCRIPTOR_LOGISTIC_BASELINE["Gastric"]["n"] == 46
    assert DESCRIPTOR_LOGISTIC_BASELINE["Intestinal"]["n"] == 63


def test_probability_fields_are_declared_but_absent(gi_client: LigandAI) -> None:
    """⛔ sgf_stable_probability / sif_stable_probability are in shared/schema.ts
    and must NEVER be emitted. The only thing that could produce one is the
    descriptor logistic, which cannot be evaluated on a bare sequence. Writing a
    rule count into a field named `..._probability` is the mislabelling the
    declaration forbids."""
    out = gi_client.gut_stability.score_local([GNRH])
    sidecar = out["results"][0]["gi_stability_scores"]

    assert "sgf_stable_probability" not in sidecar["scores"]
    assert "sif_stable_probability" not in sidecar["scores"]
    # A measured stability CLASS would need an actual SGF/SIF assay — also absent.
    assert "sgf_stability_class" not in sidecar["classes"]
    assert "sif_stability_class" not in sidecar["classes"]
    # What IS there.
    assert set(sidecar["scores"]) == {
        "sgf_risk",
        "sif_risk",
        "gi_balance_score",
        "permeability_size_penalty",
    }


def test_sidecar_declares_zero_ranking_contribution(gi_client: LigandAI) -> None:
    out = gi_client.gut_stability.score_local([GNRH])
    gate = out["results"][0]["gi_stability_scores"]["feature_gate"]
    assert gate["default_enabled"] is False
    assert gate["enterprise_feature"] is True
    assert gate["ranking_contribution"] == 0
    assert gate["primary_rank_eligible"] is False
    assert gate["thermostability_rank_eligible"] is False
    assert gate["serum_half_life_rank_eligible"] is False


# ── the two measured worked examples ─────────────────────────────────────────


def test_gnrh_bare_matches_the_measured_values() -> None:
    """GnRH QHWSYGLRPG, no modifications. Measured 2026-09-23."""
    p = score_gut_design(GNRH)
    assert p["seq_len"] == 10
    assert p["sgf_risk"] == pytest.approx(0.860, abs=1e-4)
    assert p["sif_risk"] == pytest.approx(0.510, abs=1e-4)
    assert p["gi_balance_score"] == pytest.approx(0.402, abs=1e-4)
    assert p["gi_design_class"] == "high_gi_developability_risk"
    assert p["length_bin"] == "short_linear_oral_exploration"
    assert p["permeability_size_penalty"] == pytest.approx(0.25, abs=1e-4)


def test_gnrh_cyclized_and_protected_matches_the_measured_values() -> None:
    """Same peptide, cyclized with both termini protected. Measured 2026-09-23.

    The rescue is 0.10 (N-term) + 0.08 (C-term) + 0.18 (cyclized) = 0.36, and
    the terminal-unprotected penalty drops to 0. sif_risk floors at 0.0 rather
    than going negative."""
    p = score_gut_design(
        GNRH,
        n_terminal_protected=True,
        c_terminal_protected=True,
        cyclized=True,
    )
    assert p["sgf_risk"] == pytest.approx(0.340, abs=1e-4)
    assert p["sif_risk"] == pytest.approx(0.000, abs=1e-4)
    assert p["gi_balance_score"] == pytest.approx(0.814, abs=1e-4)
    assert p["gi_design_class"] == "balanced_gi_candidate"
    assert p["modification_rescue"] == pytest.approx(0.36, abs=1e-4)
    assert p["terminal_unprotected_penalty"] == pytest.approx(0.0, abs=1e-4)


def test_modification_flags_move_the_score_in_the_right_direction() -> None:
    bare = score_gut_design(GNRH)
    rescued = score_gut_design(
        GNRH, n_terminal_protected=True, c_terminal_protected=True, cyclized=True
    )
    assert rescued["sgf_risk"] < bare["sgf_risk"]
    assert rescued["sif_risk"] < bare["sif_risk"]
    assert rescued["gi_balance_score"] > bare["gi_balance_score"]


def test_modification_rescue_is_capped() -> None:
    """Every rescue term is individually capped and the total caps at 0.45, so
    stacking D-residues and N-methylation cannot buy unlimited rescue."""
    maxed = score_gut_design(
        GNRH,
        n_terminal_protected=True,
        c_terminal_protected=True,
        cyclized=True,
        d_residue_count=100,
        n_methyl_count=100,
    )
    assert maxed["modification_rescue"] == pytest.approx(0.45, abs=1e-6)


# ── motif counting ───────────────────────────────────────────────────────────


def test_proline_shield_suppresses_the_gnrh_trypsin_site() -> None:
    """GnRH's only K/R is the R at index 7, followed by P. The rules count a
    site only when the next residue is not proline, so trypsin burden is 0 —
    a scorer that dropped the shield clause would report 1."""
    counts = protease_motif_counts(GNRH)
    assert counts["sif_trypsin_basic"] == 0
    # W, Y, L each sit before a non-proline residue.
    assert counts["sgf_pepsin_aromatic_hydrophobic"] == 3
    assert counts["sif_chymotrypsin_aromatic_hydrophobic"] == 3
    # seq[1] is 'H', not P or A.
    assert counts["sif_dppiv_n_terminal_xp_or_xa"] == 0


def test_dppiv_is_strictly_positional() -> None:
    assert protease_motif_counts("GPGGG")["sif_dppiv_n_terminal_xp_or_xa"] == 1
    assert protease_motif_counts("GAGGG")["sif_dppiv_n_terminal_xp_or_xa"] == 1
    # A proline anywhere else is not a DPP-IV site.
    assert protease_motif_counts("GGGPG")["sif_dppiv_n_terminal_xp_or_xa"] == 0


def test_length_bins_match_the_documented_policy() -> None:
    assert length_bin_for_length(3)["name"] == "di_tripeptide_transporter_like"
    assert length_bin_for_length(10)["name"] == "short_linear_oral_exploration"
    assert length_bin_for_length(12)["name"] == "short_linear_oral_exploration"
    assert length_bin_for_length(13)["name"] == "modified_or_lumen_local"
    assert length_bin_for_length(30)["name"] == "modified_or_lumen_local"
    assert length_bin_for_length(31)["name"] == "formulation_or_gi_exposure"
    assert length_bin_for_length(400)["name"] == "formulation_or_gi_exposure"


# ── absent is absent, never zero ─────────────────────────────────────────────


def test_unusable_sequences_are_absent_not_zero(gi_client: LigandAI) -> None:
    out = gi_client.gut_stability.score_local(["", "A", "123", GNRH])
    results = out["results"]
    assert len(results) == 4

    for idx in (0, 1, 2):
        rec = results[idx]
        assert rec["status"] == "absent", idx
        assert "undefined, not zero" in rec["reason"]
        # ⛔ An absent record carries NO score fields at all — a 0.0 would read
        # as a clean bill of health.
        assert "sgf_risk" not in rec
        assert "gi_balance_score" not in rec
        assert rec["declaration"]["quantity"] == "rule_count_normalized"

    assert results[3]["status"] == "present"
    assert out["batch"]["n_input"] == 4
    assert out["batch"]["n_absent"] == 3
    assert out["batch"]["n_scored"] == 1


def test_single_residue_raises_on_the_scalar_path() -> None:
    with pytest.raises(LigandAIValidationError) as exc:
        score_gut_design("A")
    assert "undefined" in str(exc.value)
    assert "not zero" in str(exc.value)


def test_non_standard_residues_are_reported_not_silently_dropped(
    gi_client: LigandAI,
) -> None:
    out = gi_client.gut_stability.score_local(["QHWSYGLRPGXUB"])
    rec = out["results"][0]
    assert rec["status"] == "present"
    assert rec["sequence"] == GNRH
    assert rec["non_standard_residues_dropped"] == ["B", "U", "X"]


def test_empty_batch_proline_guard_is_absent_not_zero(gi_client: LigandAI) -> None:
    out = gi_client.gut_stability.score_local([""])
    guard = out["batch"]["proline_guard"]
    assert guard["status"] == "absent"
    assert "undefined, not zero" in guard["reason"]


# ── provenance ───────────────────────────────────────────────────────────────


def test_local_results_are_labelled_as_local(gi_client: LigandAI) -> None:
    """A local result and a platform result must never be indistinguishable in
    a customer's records."""
    out = gi_client.gut_stability.score_local([GNRH])
    assert out["computed_by"] == "ligandai_sdk_local_ruleset"
    assert out["ruleset_version"] == GUT_STABILITY_RULESET_VERSION
    rec = out["results"][0]
    assert rec["computed_by"] == "ligandai_sdk_local_ruleset"
    assert rec["ruleset_version"] == GUT_STABILITY_RULESET_VERSION


def test_ruleset_version_matches_the_platform_feature_version() -> None:
    sidecar = build_gi_stability_record(score_gut_design(GNRH))
    assert sidecar["feature_name"] == "gi_peptide_stability"
    assert sidecar["feature_version"] == GUT_STABILITY_RULESET_VERSION
    assert GUT_STABILITY_RULESET_VERSION == "gut_stability_v1_c80f4"


# ── the server scoring path ──────────────────────────────────────────────────

_SERVER_PAYLOAD = {
    "results": [
        {
            "sequence": GNRH,
            "status": "present",
            "seq_len": 10,
            "sgf_risk": 0.86,
            "sif_risk": 0.51,
            "gi_balance_score": 0.402,
            "gi_design_class": "high_gi_developability_risk",
            "declaration": {"quantity": "rule_count_normalized"},
        }
    ],
    "declaration": {"quantity": "rule_count_normalized"},
    "batch": {"n_input": 1, "n_scored": 1, "n_absent": 0},
}


def test_score_posts_the_batch_and_returns_the_payload(
    httpx_mock: HTTPXMock, gi_client: LigandAI
) -> None:
    httpx_mock.add_response(
        url=f"{BASE}{GI_STABILITY_SCORING_PATH}", json=_SERVER_PAYLOAD
    )
    out = gi_client.gut_stability.score(
        GNRH, modifications={"cyclized": True, "n_terminal_protected": True}
    )
    assert out["results"][0]["gi_design_class"] == "high_gi_developability_risk"

    body = _json.loads(httpx_mock.get_request().read())
    # A single sequence is normalized to a one-element list.
    assert body["sequences"] == [GNRH]
    assert body["modifications"]["cyclized"] is True


@pytest.mark.parametrize("status", [404, 405, 501])
def test_score_names_score_local_when_the_route_is_not_mounted(
    httpx_mock: HTTPXMock, gi_client: LigandAI, status: int
) -> None:
    """No Express or FastAPI route forwards to the gut-stability Modal function
    today, so this is the path a customer actually hits."""
    httpx_mock.add_response(
        url=f"{BASE}{GI_STABILITY_SCORING_PATH}",
        status_code=status,
        json={"message": "Not Found"},
    )
    with pytest.raises(LigandAIParameterNotForwarded) as exc:
        gi_client.gut_stability.score([GNRH])
    assert "score_local" in str(exc.value)
    assert "not mounted" in str(exc.value)
    assert exc.value.override == "use score_local()"


def test_score_treats_an_spa_shell_200_as_an_unmounted_route(
    httpx_mock: HTTPXMock, gi_client: LigandAI
) -> None:
    """⛔ An unmounted path on a single-page-app server answers 200 with the app
    shell, not a 404. A 2xx body with no results list and no declaration cannot
    have come from the scorer."""
    httpx_mock.add_response(
        url=f"{BASE}{GI_STABILITY_SCORING_PATH}", json={"ok": True}
    )
    with pytest.raises(LigandAIParameterNotForwarded):
        gi_client.gut_stability.score([GNRH])


def test_score_does_not_swallow_an_unrelated_error(
    httpx_mock: HTTPXMock, gi_client: LigandAI
) -> None:
    """A 403 is the scorer refusing, not the route being absent. It must NOT be
    rewritten into 'use score_local()'."""
    httpx_mock.add_response(
        url=f"{BASE}{GI_STABILITY_SCORING_PATH}",
        status_code=403,
        json={"message": "Forbidden"},
    )
    with pytest.raises(Exception) as exc:
        gi_client.gut_stability.score([GNRH])
    assert not isinstance(exc.value, LigandAIParameterNotForwarded)


def test_score_validates_locally_before_any_request(gi_client: LigandAI) -> None:
    with pytest.raises(LigandAIValidationError):
        gi_client.gut_stability.score([])
    with pytest.raises(LigandAIValidationError):
        gi_client.gut_stability.score(
            [GNRH], modifications={"d_residue_count": "three"}
        )


# ── the generation toggle ────────────────────────────────────────────────────


def test_generation_settings_refuses_to_manufacture_a_silent_no_op(
    gi_client: LigandAI,
) -> None:
    """⛔ The worker reads gi_stability_enabled; no API hop sends it. Returning
    the block by default would report the knob ON and generate with it OFF."""
    with pytest.raises(LigandAIParameterNotForwarded) as exc:
        gi_client.gut_stability.generation_settings()
    err = exc.value
    assert err.parameter == GI_STABILITY_CANONICAL_WIRE_KEY
    assert err.override == "allow_unforwarded=True"
    assert "ligandforge_parallel_v2" in (err.reached or "")
    assert "ptf-integration-routes" in (err.dropped_at or "")
    assert "score_local" in str(err)


def test_generation_settings_uses_the_canonical_spelling(
    gi_client: LigandAI,
) -> None:
    block = gi_client.gut_stability.generation_settings(allow_unforwarded=True)
    assert GI_STABILITY_CANONICAL_WIRE_KEY == "gi_stability_enabled"
    assert block[GI_STABILITY_CANONICAL_WIRE_KEY] is True
    # gut_stability_enabled is a worker-side alias only — never emitted.
    assert "gut_stability_enabled" not in block
    assert block["gi_stability_mode"] == "resist"
    assert block["gi_stability_strength"] == 2.0
    assert block["gi_stability_modules"] == {
        "sgf_pepsin": True,
        "sif_trypsin": True,
        "sif_chymotrypsin": True,
        "sif_dppiv": True,
        "permeability_size": True,
    }


def test_generation_settings_enabled_false_is_an_explicit_off(
    gi_client: LigandAI,
) -> None:
    block = gi_client.gut_stability.generation_settings(
        enabled=False, allow_unforwarded=True
    )
    assert block[GI_STABILITY_CANONICAL_WIRE_KEY] is False


def test_generation_settings_validates_before_the_forwarding_check(
    gi_client: LigandAI,
) -> None:
    """A bad argument must fail as a named validation error, not as the
    not-forwarded refusal — otherwise fixing the plumbing would surface a
    second, different error the caller never saw."""
    with pytest.raises(LigandAIValidationError):
        gi_client.gut_stability.generation_settings(mode="ignore")
    with pytest.raises(LigandAIValidationError):
        gi_client.gut_stability.generation_settings(strength=9.0)
    with pytest.raises(LigandAIValidationError):
        gi_client.gut_stability.generation_settings(modules={"sgf_pepsinn": True})
    with pytest.raises(LigandAIValidationError):
        gi_client.gut_stability.generation_settings(
            modules={k: False for k in ("sgf_pepsin", "sif_trypsin")}
        )


def test_generation_settings_target_mode_is_allowed(gi_client: LigandAI) -> None:
    """'target' is a deliberately gut-labile / lumen-released design, not a typo."""
    block = gi_client.gut_stability.generation_settings(
        mode="target", strength=0.0, allow_unforwarded=True
    )
    assert block["gi_stability_mode"] == "target"
    assert block["gi_stability_strength"] == 0.0


# ── entitlement ──────────────────────────────────────────────────────────────


def test_entitlement_allows_enterprise_and_superadmin() -> None:
    assert explain_entitlement("enterprise")["allowed"] is True
    assert explain_entitlement("ENTERPRISE")["state"] == "allowed"
    assert explain_entitlement("free", is_super_admin=True)["allowed"] is True
    assert explain_entitlement("free", is_super_admin=1)["state"] == "allowed"


@pytest.mark.parametrize("tier", ["pro", "academia", "basic", "free", "discovery_partner"])
def test_entitlement_is_indeterminate_not_denied_without_module_access(
    tier: str,
) -> None:
    """⛔ THREE STATES, NEVER TWO. An explicit module_access grant is exactly
    what would flip this answer, and GET /api/auth/user does not publish it. A
    confident 'denied' would be wrong for every granted account."""
    out = explain_entitlement(tier)
    assert out["allowed"] is False
    assert out["state"] == "indeterminate"
    assert "module_access" in out["reason"]
    assert out["default_enabled"] is False


def test_entitlement_honours_an_explicit_grant() -> None:
    for key in GI_STABILITY_MODULE_GRANT_KEYS:
        out = explain_entitlement("pro", module_access={key: True})
        assert out["allowed"] is True, key
        assert out["state"] == "allowed"
    # The server stores moduleAccess as JSON text on some rows.
    out = explain_entitlement("academia", module_access='{"gi_peptide_stability": true}')
    assert out["allowed"] is True


def test_entitlement_denies_only_when_module_access_was_actually_seen() -> None:
    out = explain_entitlement("pro", module_access={"some_other_module": True})
    assert out["allowed"] is False
    assert out["state"] == "denied"


def test_entitlement_reads_the_account(httpx_mock: HTTPXMock, gi_client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/auth/user",
        json={"id": "u_1", "subscriptionTier": "enterprise", "isSuperAdmin": False},
    )
    out = gi_client.gut_stability.entitlement()
    assert out["allowed"] is True
    assert out["tier"] == "enterprise"


# ── sidecar reading ──────────────────────────────────────────────────────────

_SIDECAR = {
    "feature_name": "gi_peptide_stability",
    "feature_version": GUT_STABILITY_RULESET_VERSION,
    "scores": {"sgf_risk": 0.86, "sif_risk": 0.51, "gi_balance_score": 0.402},
}


@pytest.mark.parametrize(
    "record",
    [
        {"gi_stability_proxy": _SIDECAR},
        {"gi_stability_scores": _SIDECAR},
        {"giStabilityScores": _SIDECAR},
        {"metadata": {"gi_stability_proxy": _SIDECAR}},
    ],
)
def test_read_sidecar_finds_every_spelling(gi_client: LigandAI, record: dict) -> None:
    out = gi_client.gut_stability.read_sidecar(record)
    assert out["feature_name"] == "gi_peptide_stability"
    assert out["scores"]["gi_balance_score"] == pytest.approx(0.402)


def test_read_sidecar_returns_absent_not_none(gi_client: LigandAI) -> None:
    """⛔ Absent has two causes that are not the same fact: the module was never
    run (default_enabled=False), or the account is not entitled. Returning None
    collapses them; the absent record names both."""
    out = gi_client.gut_stability.read_sidecar({"sequence": GNRH})
    assert out is not None
    assert out["status"] == "absent"
    assert "not run" in out["reason"] or "was not run" in out["reason"]
    assert "entitled" in out["reason"]
    assert "entitlement()" in out["reason"]


def test_read_sidecar_ignores_a_different_feature(gi_client: LigandAI) -> None:
    out = gi_client.gut_stability.read_sidecar(
        {"gi_stability_scores": {"feature_name": "ligandforge_thermostability_proxy"}}
    )
    assert out["status"] == "absent"


def test_read_sidecar_rejects_a_non_record(gi_client: LigandAI) -> None:
    with pytest.raises(LigandAIValidationError):
        gi_client.gut_stability.read_sidecar(["not", "a", "record"])


# ── anti-drift: the SDK ruleset vs the platform module ───────────────────────

_PLATFORM_MODULE = pathlib.Path(
    "/mnt/backup/LIGANDAI_ALPHA_V2/modal_workers/gut_stability_penalties.py"
)


#: Keys the platform module carries on GI_SOURCE that must NOT ship in a
#: published package. ``ported_from`` names an internal worktree path; a
#: customer pip-installing ligandai should never receive it.
SOURCE_KEYS_NOT_PUBLISHED = frozenset({"ported_from"})


def _load_platform_module():
    spec = importlib.util.spec_from_file_location(
        "_platform_gut_stability_penalties", _PLATFORM_MODULE
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.skipif(
    not _PLATFORM_MODULE.exists(),
    reason="platform module not on this machine (published SDK checkout)",
)
def test_sdk_ruleset_agrees_with_the_platform_module() -> None:
    """⛔ THE ONE TEST THAT MAKES A SECOND COPY OF THE RULE TABLE SAFE.

    The SDK carries its own pinned port so ``score_local`` works without a
    round trip. Two copies of one rule set is a divergence risk; this pins them
    together on every sequence shape the rules branch on. It skips on a machine
    that has only the published SDK, and fails loudly on this one.
    """
    platform = _load_platform_module()
    cases = [
        (GNRH, {}),
        (GNRH, {"n_terminal_protected": True, "c_terminal_protected": True, "cyclized": True}),
        ("ACDGFHIKLMNPQRSTVWY", {}),
        ("KKKLLLMMMAAADDD", {"d_residue_count": 3, "n_methyl_count": 2}),
        ("GPGPG", {}),
        ("AP", {}),
        ("M" * 45, {"cyclized": True}),
        (
            "RSSRASHFQSHSSERQRHGSSQVWKHGSYGPAEYDYGHTGYGPSGGSRKSISNSHLSWSTDSTANKQLSRH",
            {},
        ),
    ]
    for seq, mods in cases:
        theirs = platform.score_gut_design(seq, **mods)
        ours = score_gut_design(seq, **mods)
        for key in (
            "sgf_risk",
            "sif_risk",
            "gi_balance_score",
            "permeability_size_penalty",
            "modification_rescue",
            "terminal_unprotected_penalty",
            "gi_design_class",
            "length_bin",
            "length_bin_default_use",
            "oral_systemic_length_feasibility",
            "recommended_design_actions",
            "sgf_pepsin_aromatic_hydrophobic",
            "sif_trypsin_basic",
            "sif_chymotrypsin_aromatic_hydrophobic",
            "sif_dppiv_n_terminal_xp_or_xa",
        ):
            assert ours[key] == theirs[key], f"{seq!r} {mods} field={key}"


@pytest.mark.skipif(
    not _PLATFORM_MODULE.exists(),
    reason="platform module not on this machine (published SDK checkout)",
)
def test_sdk_declaration_matches_the_platform_declaration() -> None:
    """Identical field-for-field, except the internal path that must not ship."""
    ours = gut_stability_declaration()
    theirs = _load_platform_module().gut_stability_declaration()

    for key in (
        "quantity",
        "not_a_measured_rate",
        "not_a_survival_probability",
        "not_an_oral_bioavailability_prediction",
        "not_a_neural_network",
        "units",
        "statement",
        "descriptor_logistic_baseline_not_applied",
    ):
        assert ours[key] == theirs[key], key

    our_src, their_src = ours["source"], theirs["source"]
    assert set(their_src) - set(our_src) == SOURCE_KEYS_NOT_PUBLISHED
    for key in our_src:
        assert our_src[key] == their_src[key], key


def test_no_internal_worktree_path_ships_in_the_declaration() -> None:
    """⛔ A published SDK must not hand a customer an internal filesystem path."""
    blob = _json.dumps(gut_stability_declaration())
    for fragment in ("/home/dre", "/mnt/backup", "LIGANDAI_ALPHA_V2", "sandbox/"):
        assert fragment not in blob, fragment
