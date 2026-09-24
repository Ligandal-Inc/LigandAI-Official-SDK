# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""GI / gut-stability developability triage — SGF / SIF protease-motif burden.

Wraps the platform's GI peptide-stability module (``gi_peptide_stability``,
bead ``bd-dre-c80f4.1``) and exposes it as ``client.gut_stability``:

- :meth:`GutStability.score`        → ``POST /api/scoring/gut-stability``
- :meth:`GutStability.score_local`  → the same rule set, evaluated in-process
- :meth:`GutStability.generation_settings` → the ``gi_stability_enabled`` block
- :meth:`GutStability.entitlement`  → why a value is absent for your account
- :meth:`GutStability.read_sidecar` → pull the sidecar off a platform record

⛔ THE DECLARATION CONTRACT — READ THIS BEFORE USING ANY NUMBER HERE
--------------------------------------------------------------------
``sgf_risk``, ``sif_risk`` and ``gi_balance_score`` are **RULE COUNTS
NORMALIZED BY LENGTH**. Copied verbatim from the module's own ``declaration``
block (``modal_workers/gut_stability_penalties.py::gut_stability_declaration``):

    quantity                            rule_count_normalized
    not_a_measured_rate                 True
    not_a_survival_probability          True
    not_an_oral_bioavailability_prediction   True
    not_a_neural_network                True

They are **not** measured degradation rates, **not** a probability that a
peptide survives the gastrointestinal tract, **not** an oral bioavailability
prediction, and **not** a neural-network output. There is no gut-stability
neural net. Any sentence that reads "this peptide has an X% chance of
surviving the gut" is a misreading of this module.

Units: dimensionless rule-count burden per residue, clamped to ``[0, 1]``.
Higher ``sgf_risk`` / ``sif_risk`` is worse; higher ``gi_balance_score`` is
better. A "risk" of 0.86 means the counted motif burden saturated the rule's
own normalisation scale — it does not mean 86% of anything.

Data source
-----------
Dataset: *Peptide Stability in the Gastrointestinal Tract*, figshare DOI
``10.6084/m9.figshare.25941580.v1`` (CC BY 4.0), paper DOI
``10.1016/j.ijpharm.2023.122643``. 109 incubation rows — 46 gastric, 63
intestinal — with 200 RDKit whole-molecule descriptor columns,
``has_amino_acid_sequence_column = False`` and no SMILES column.

The 200-descriptor logistic baseline measured on that dataset (ROC-AUC 0.8112
gastric / 0.7435 intestinal) **requires whole-molecule descriptors and cannot
be evaluated on a bare designed sequence** — the source rows carry no sequence
to build them from. It is therefore carried, never applied, as
``declaration["descriptor_logistic_baseline_not_applied"]``. Nothing in this
module turns a sequence into those descriptors, and nothing here should.

``scores.sgf_stable_probability`` / ``scores.sif_stable_probability`` are
DECLARED by ``shared/schema.ts`` and are deliberately **ABSENT** from every
record this module emits. They are probabilities, and the only thing in this
feature family that could produce one is the descriptor logistic above. Writing
a rule count into a field named ``..._probability`` is exactly the mislabelling
the declaration contract forbids, so the field stays absent rather than filled.

Absent is absent
----------------
An unusable input yields an explicit ``{"status": "absent", "reason": ...}``
record, never a zero-filled score. A sequence with no standard amino acids has
an *undefined* motif count, not a count of zero.

Entitlement
-----------
The GI sidecar is an enterprise module, ``default_enabled = False``. Per
``server/gi-stability-sidecar.ts::canUseGiStabilityModule`` the gate is:

    superadmin  OR  subscription tier == "enterprise"  OR  an explicit
    ``module_access`` grant naming one of ``gi_stability`` /
    ``gi_peptide_stability`` / ``gi_stability_proxy`` /
    ``enterprise_gi_stability``.

A Pro or Academia account without a grant gets an **absent** sidecar on
platform peptide records. That absence is the entitlement gate doing its job —
it is not a failed call. :meth:`GutStability.entitlement` says which of the
three it is, and answers ``"indeterminate"`` rather than ``"denied"`` when the
account endpoint does not publish ``module_access`` (it does not today).
"""

from __future__ import annotations

import json as _json
from collections.abc import Mapping, Sequence
from typing import Any

from ligandai.errors import (
    LigandAIError,
    LigandAIParameterNotForwarded,
    LigandAIValidationError,
)
from ligandai.resources._base import AsyncResource, Resource

__all__ = [
    "AsyncGutStability",
    "DESCRIPTOR_LOGISTIC_BASELINE",
    "GI_SOURCE",
    "GI_STABILITY_CANONICAL_WIRE_KEY",
    "GI_STABILITY_MODULE_GRANT_KEYS",
    "GI_STABILITY_SCORING_PATH",
    "GI_STABILITY_TIER_ALLOWLIST",
    "GUIDANCE_MODULES",
    "GUT_STABILITY_RULESET_VERSION",
    "LENGTH_BINS",
    "GutStability",
    "absent",
    "build_gi_stability_record",
    "clean_sequence",
    "explain_entitlement",
    "gut_stability_declaration",
    "length_bin_for_length",
    "length_bin_for_sequence",
    "protease_motif_counts",
    "score_gut_design",
]

# ---------------------------------------------------------------------------
# Provenance / version pins
# ---------------------------------------------------------------------------

#: Ruleset identity. Matches ``feature_version`` in
#: ``modal_workers/gut_stability_penalties.py::build_gi_stability_record``.
#: Bump this ONLY together with the platform module — a local result that
#: silently disagreed with the platform's would be worse than no local result.
GUT_STABILITY_RULESET_VERSION = "gut_stability_v1_c80f4"

#: Route the platform scorer is designed to answer on. See
#: :meth:`GutStability.score` for what happens when a deployment does not
#: mount it.
GI_STABILITY_SCORING_PATH = "/api/scoring/gut-stability"

#: Canonical generation wire key. ``server/gi-stability-sidecar.ts`` accepts six
#: spellings; the worker reads this one first and ``gut_stability_enabled`` as
#: an alias. New surfaces use the canonical spelling only.
GI_STABILITY_CANONICAL_WIRE_KEY = "gi_stability_enabled"

#: Tiers that reach the module without an explicit grant.
GI_STABILITY_TIER_ALLOWLIST = frozenset({"enterprise"})

#: ``module_access`` keys that grant the module to a non-enterprise account.
#: Verbatim from ``server/gi-stability-sidecar.ts::GI_STABILITY_MODULE_KEYS``.
GI_STABILITY_MODULE_GRANT_KEYS = (
    "gi_stability",
    "gi_peptide_stability",
    "gi_stability_proxy",
    "enterprise_gi_stability",
)

#: Dataset provenance. Field-identical to the platform module's ``GI_SOURCE``
#: EXCEPT for its ``ported_from`` key, which names an internal worktree path and
#: deliberately does not ship in a published package. See
#: ``tests/unit/test_gut_stability_c80f4.py::SOURCE_KEYS_NOT_PUBLISHED``.
GI_SOURCE: dict[str, Any] = {
    "name": "Dataset: Peptide Stability in the Gastrointestinal Tract",
    "figshare_doi": "10.6084/m9.figshare.25941580.v1",
    "figshare_article_url": (
        "https://figshare.com/articles/dataset/"
        "Dataset_Peptide_Stability_in_the_Gastrointestinal_Tract/25941580"
    ),
    "paper_doi": "10.1016/j.ijpharm.2023.122643",
    "github_url": "https://github.com/FrankWanger/ML_Peptide",
    "license": "CC BY 4.0",
    "rows": 109,
    "environment_counts": {"Gastric": 46, "Intestinal": 63},
    "descriptor_columns": 200,
    "has_amino_acid_sequence_column": False,
    "has_smiles_column": False,
}

#: Measured on the source dataset at review time. Recorded ONLY so the
#: declaration can state what it is refusing to apply to a bare sequence.
#: Nothing in the sequence path evaluates it.
DESCRIPTOR_LOGISTIC_BASELINE: dict[str, Any] = {
    "Gastric": {
        "n": 46,
        "stable_rows": 33,
        "not_stable_or_partly_rows": 13,
        "feature_count": 200,
        "roc_auc": 0.8111888111888111,
        "balanced_accuracy": 0.8158508158508159,
    },
    "Intestinal": {
        "n": 63,
        "stable_rows": 23,
        "not_stable_or_partly_rows": 40,
        "feature_count": 200,
        "roc_auc": 0.7434782608695651,
        "balanced_accuracy": 0.7228260869565217,
    },
    "evaluation": "5fold_descriptor_logistic_stable_vs_other",
    "input_requirement": "200 RDKit whole-molecule descriptors",
    "cannot_be_evaluated_on": "a bare designed amino-acid sequence",
}

_DECLARATION_TEXT = (
    "sgf_risk / sif_risk / gi_balance_score are RULE COUNTS NORMALIZED BY LENGTH. "
    "They are not measured degradation rates, not a probability of surviving the "
    "gastrointestinal tract, and not an oral bioavailability prediction. No neural "
    "network produces them."
)


def gut_stability_declaration() -> dict[str, Any]:
    """The declaration block that rides on every emitted gut-stability record.

    Field-for-field identical to the platform module's
    ``gut_stability_declaration()``. Never strip it from a record you forward —
    it is the only thing travelling with the number that says what the number
    is not.
    """
    return {
        "quantity": "rule_count_normalized",
        "not_a_measured_rate": True,
        "not_a_survival_probability": True,
        "not_an_oral_bioavailability_prediction": True,
        "not_a_neural_network": True,
        "units": "dimensionless rule-count burden per residue, clamped to [0, 1]",
        "statement": _DECLARATION_TEXT,
        "source": dict(GI_SOURCE),
        "descriptor_logistic_baseline_not_applied": dict(DESCRIPTOR_LOGISTIC_BASELINE),
    }


def absent(reason: str, **extra: Any) -> dict[str, Any]:
    """Explicit ABSENT record. Never a zero-fill.

    An undefined measurement and a measurement of zero are different facts and
    must not share a representation.
    """
    out: dict[str, Any] = {"status": "absent", "reason": reason}
    out.update(extra)
    return out


# ---------------------------------------------------------------------------
# Rule tables — a pinned port of
# modal_workers/gut_stability_penalties.py (which is itself a verbatim port of
# sandbox/thermostability/gi_peptide_design_rules.py).
#
# ⛔ The coefficients below are NOT tuned here and must not be "improved"
# locally. tests/unit/test_gut_stability_c80f4.py pins them against the two
# measured worked examples; a local change that moved a number would make the
# SDK and the platform disagree about the same peptide, which is worse than
# having no local path at all.
# ---------------------------------------------------------------------------

_AA_SET = frozenset("ACDEFGHIKLMNPQRSTVWY")

#: Pepsin-like P1 alphabet in simulated gastric fluid.
SGF_PEPSIN_P1 = frozenset("FWYL")
#: Trypsin P1 alphabet in simulated intestinal fluid.
SIF_TRYPSIN_P1 = frozenset("KR")
#: Chymotrypsin P1 alphabet in simulated intestinal fluid.
SIF_CHYMOTRYPSIN_P1 = frozenset("FWYL")
#: DPP-IV is strictly positional — ``seq[1] in {"P", "A"}``.
SIF_DPPIV_POS2 = frozenset("PA")
#: A proline at P1' removes the preceding site from every count.
P1_PRIME_SHIELD = "P"

LENGTH_BINS: list[dict[str, Any]] = [
    {
        "name": "di_tripeptide_transporter_like",
        "min_len": 2,
        "max_len": 3,
        "default_use": "transporter_substrate_or_fragment",
        "permeability_size_penalty": 0.05,
    },
    {
        "name": "short_linear_oral_exploration",
        "min_len": 4,
        "max_len": 12,
        "default_use": "oral_or_local_candidate_with_protease_design",
        "permeability_size_penalty": 0.25,
    },
    {
        "name": "modified_or_lumen_local",
        "min_len": 13,
        "max_len": 30,
        "default_use": "modified_peptide_or_local_gi_exposure",
        "permeability_size_penalty": 0.65,
    },
    {
        "name": "formulation_or_gi_exposure",
        "min_len": 31,
        "max_len": 10_000,
        "default_use": "formulation_local_delivery_or_nonoral",
        "permeability_size_penalty": 0.9,
    },
]

_LENGTH_POLICY: dict[str, dict[str, str]] = {
    "di_tripeptide_transporter_like": {
        "oral_systemic_length_feasibility": "transporter_constrained",
        "length_policy_note": (
            "2-3 aa may use peptide transport biology, but target specificity is "
            "often the limiting issue."
        ),
    },
    "short_linear_oral_exploration": {
        "oral_systemic_length_feasibility": "best_unmodified_starting_range",
        "length_policy_note": (
            "4-12 aa is the best default range for unmodified or lightly modified "
            "oral/permeability exploration."
        ),
    },
    "modified_or_lumen_local": {
        "oral_systemic_length_feasibility": "modification_or_local_lumen_required",
        "length_policy_note": (
            "13-30 aa should be designed with explicit protease/permeability rescue "
            "or local GI-lumen intent."
        ),
    },
    "formulation_or_gi_exposure": {
        "oral_systemic_length_feasibility": "formulation_or_nonoral_required",
        "length_policy_note": (
            "30+ aa should be routed to formulation/local-exposure or nonoral "
            "assumptions unless direct permeability data exists."
        ),
    },
}

#: Generation-guidance modules and their default on/off state.
GUIDANCE_MODULES: dict[str, bool] = {
    "sgf_pepsin": True,
    "sif_trypsin": True,
    "sif_chymotrypsin": True,
    "sif_dppiv": True,
    "permeability_size": True,
}

#: Guidance strength bounds. ``0`` is "module listed but contributing nothing",
#: which is a different request from omitting the module, and is allowed.
_MIN_STRENGTH = 0.0
_MAX_STRENGTH = 5.0

#: ``resist`` steers generation AWAY from gut-labile motifs. ``target`` steers
#: TOWARD them — a deliberately lumen-released design, not a mistake.
_GUIDANCE_MODES = ("resist", "target")

_MODIFICATION_KEYS = (
    "n_terminal_protected",
    "c_terminal_protected",
    "cyclized",
    "d_residue_count",
    "n_methyl_count",
)

_EMPTY_MODS: dict[str, Any] = {
    "n_terminal_protected": False,
    "c_terminal_protected": False,
    "cyclized": False,
    "d_residue_count": 0,
    "n_methyl_count": 0,
}


def clean_sequence(seq: str | None) -> str:
    """Upper-case and keep only the canonical 20 amino acids.

    Non-standard characters are DROPPED rather than rejected, matching the
    platform module. The dropped characters are reported back on the record as
    ``non_standard_residues_dropped`` so the drop is never silent.
    """
    return "".join(ch for ch in (seq or "").upper() if ch in _AA_SET)


def length_bin_for_length(length: int) -> dict[str, Any]:
    """The length bin covering ``length`` residues.

    Bins are a developability-route policy, not a degradation endpoint:
    ``<= 12`` aa is the oral-exploration range, ``13-30`` aa is
    modification / lumen-local territory, ``> 30`` aa is formulation or
    non-oral territory.
    """
    for item in LENGTH_BINS:
        if item["min_len"] <= length <= item["max_len"]:
            return dict(item)
    return dict(LENGTH_BINS[-1])


def length_bin_for_sequence(sequence: str) -> dict[str, Any]:
    """The length bin for ``sequence`` after :func:`clean_sequence`."""
    return length_bin_for_length(len(clean_sequence(sequence)))


def protease_motif_counts(sequence: str) -> dict[str, int]:
    """Count SGF / SIF cleavage motifs. Integer counts, not rates.

    A site is counted at position ``i`` only when the residue at ``i + 1`` is
    not proline — the P1' shield. DPP-IV is strictly positional (``seq[1]``).

    Returns:
        ``{sgf_pepsin_aromatic_hydrophobic, sif_trypsin_basic,
        sif_chymotrypsin_aromatic_hydrophobic, sif_dppiv_n_terminal_xp_or_xa}``.
    """
    seq = clean_sequence(sequence)
    sgf_pepsin = 0
    sif_trypsin = 0
    sif_chymotrypsin = 0
    sif_dppiv = 0
    for i, aa in enumerate(seq[:-1]):
        nxt = seq[i + 1]
        if aa in SGF_PEPSIN_P1 and nxt != P1_PRIME_SHIELD:
            sgf_pepsin += 1
        if aa in SIF_TRYPSIN_P1 and nxt != P1_PRIME_SHIELD:
            sif_trypsin += 1
        if aa in SIF_CHYMOTRYPSIN_P1 and nxt != P1_PRIME_SHIELD:
            sif_chymotrypsin += 1
    if len(seq) >= 2 and seq[1] in SIF_DPPIV_POS2:
        sif_dppiv = 1
    return {
        "sgf_pepsin_aromatic_hydrophobic": sgf_pepsin,
        "sif_trypsin_basic": sif_trypsin,
        "sif_chymotrypsin_aromatic_hydrophobic": sif_chymotrypsin,
        "sif_dppiv_n_terminal_xp_or_xa": sif_dppiv,
    }


def _bounded_ratio(count: int, length: int, scale: float) -> float:
    """``(count / length) / scale``, clamped to 1.0. This is the normalisation
    that makes the output a *rule-count burden per residue* rather than a rate."""
    if length <= 0:
        return 1.0
    return min(1.0, (count / length) / scale)


def score_gut_design(
    sequence: str,
    *,
    n_terminal_protected: bool = False,
    c_terminal_protected: bool = False,
    cyclized: bool = False,
    d_residue_count: int = 0,
    n_methyl_count: int = 0,
) -> dict[str, Any]:
    """Score one sequence for SGF / SIF motif burden and oral length feasibility.

    ⛔ Every number returned is a RULE COUNT NORMALIZED BY LENGTH. See the
    module docstring and :func:`gut_stability_declaration`. Nothing here
    predicts whether the peptide survives the gut.

    Args:
        sequence: Peptide sequence, single-letter. Non-canonical characters are
            dropped by :func:`clean_sequence`.
        n_terminal_protected: N-terminus is chemically capped (e.g. acetylated).
            Post-design chemistry, scored as a flag — never assumed.
        c_terminal_protected: C-terminus is chemically capped (e.g. amidated).
        cyclized: The peptide is cyclic (head-to-tail or side-chain).
        d_residue_count: Number of D-amino acids. ⛔ D-residues are NOT
            expressible in the canonical-20 generator alphabet — this is a flag
            describing post-design chemistry you intend to apply, not something
            the generator can produce or the scorer can detect.
        n_methyl_count: Number of N-methylated backbone amides. Same caveat as
            ``d_residue_count``.

    Returns:
        A dict carrying ``sgf_risk``, ``sif_risk``, ``gi_balance_score``,
        ``permeability_size_penalty``, ``gi_design_class``, ``length_bin``, the
        four raw motif counts, and the ``declaration`` block. Dimensionless,
        clamped to ``[0, 1]``; higher risk is worse, higher balance is better.

    Raises:
        LigandAIValidationError: if the cleaned sequence is shorter than two
            residues. A one-residue input has no P1/P1' pair at all, so the
            motif counts are UNDEFINED — returning zeros would be a fabricated
            clean bill of health. Batch callers get an ``absent`` record for the
            same input instead of an exception; see :meth:`GutStability.score_local`.
    """
    seq = clean_sequence(sequence)
    length = len(seq)
    if length < 2:
        raise LigandAIValidationError(
            f"gut-stability scoring needs at least 2 standard amino acids "
            f"(got {length} after cleaning {sequence!r}). A 1-residue sequence "
            f"has no P1/P1' pair, so the SGF/SIF motif counts are undefined — "
            f"not zero. Use client.gut_stability.score_local([...]) for a batch, "
            f"which returns an explicit absent record instead of raising."
        )

    bin_info = length_bin_for_length(length)
    policy = _LENGTH_POLICY[str(bin_info["name"])]
    counts = protease_motif_counts(seq)

    modification_rescue = min(
        0.45,
        (0.10 if n_terminal_protected else 0.0)
        + (0.08 if c_terminal_protected else 0.0)
        + (0.18 if cyclized else 0.0)
        + min(0.12, 0.03 * max(0, int(d_residue_count)))
        + min(0.10, 0.025 * max(0, int(n_methyl_count))),
    )
    terminal_penalty = (0.10 if not n_terminal_protected else 0.0) + (
        0.06 if not c_terminal_protected else 0.0
    )

    sgf_raw = (
        0.70 * _bounded_ratio(counts["sgf_pepsin_aromatic_hydrophobic"], length, 0.18)
        + terminal_penalty
        + 0.15 * float(length > 30)
    )
    sif_raw = (
        0.45 * _bounded_ratio(counts["sif_trypsin_basic"], length, 0.12)
        + 0.35
        * _bounded_ratio(counts["sif_chymotrypsin_aromatic_hydrophobic"], length, 0.18)
        + 0.20 * counts["sif_dppiv_n_terminal_xp_or_xa"]
        + terminal_penalty
        + 0.10 * float(length > 30)
    )
    sgf_risk = max(0.0, min(1.0, sgf_raw - modification_rescue))
    sif_risk = max(0.0, min(1.0, sif_raw - modification_rescue))

    permeability_size_penalty = float(bin_info["permeability_size_penalty"])
    balance_score = max(
        0.0,
        1.0 - (0.40 * sgf_risk + 0.40 * sif_risk + 0.20 * permeability_size_penalty),
    )
    if balance_score >= 0.72:
        design_class = "balanced_gi_candidate"
    elif balance_score >= 0.50:
        design_class = "needs_targeted_gi_rescue"
    else:
        design_class = "high_gi_developability_risk"

    actions: list[str] = []
    if sgf_risk >= 0.55:
        actions.append(
            "reduce pepsin-like aromatic/hydrophobic cleavage burden or add gastric protection"
        )
    if sif_risk >= 0.55:
        actions.append(
            "reduce trypsin/chymotrypsin/DPP-IV liabilities or add intestinal protease rescue"
        )
    if permeability_size_penalty >= 0.65:
        actions.append(
            "treat oral-systemic exposure as restricted unless modification, formulation, "
            "or permeability evidence is present"
        )
    if not actions:
        actions.append(
            "advance to paired SGF/SIF assay plus potency and permeability checks"
        )

    return {
        "sequence": seq,
        "seq_len": length,
        "length_bin": bin_info["name"],
        "length_bin_default_use": bin_info["default_use"],
        **policy,
        "sgf_risk": round(float(sgf_risk), 4),
        "sif_risk": round(float(sif_risk), 4),
        "permeability_size_penalty": round(permeability_size_penalty, 4),
        "modification_rescue": round(float(modification_rescue), 4),
        "terminal_unprotected_penalty": round(float(terminal_penalty), 4),
        "gi_balance_score": round(float(balance_score), 4),
        "gi_design_class": design_class,
        "recommended_design_actions": "; ".join(actions),
        **counts,
        "declaration": gut_stability_declaration(),
    }


def build_gi_stability_record(
    profile: Mapping[str, Any],
    *,
    candidate_id: str | None = None,
    sequence_hash: str | None = None,
    target: str | None = None,
    design_class: str | None = None,
    include_sequence: bool = False,
    feature_version: str = GUT_STABILITY_RULESET_VERSION,
) -> dict[str, Any]:
    """Build the ``gi_stability_scores`` sidecar payload from a scored profile.

    Same shape the platform persists on a generated peptide, so a locally
    scored design and a platform-scored one can be read by one code path.

    ⛔ ``scores.sgf_stable_probability`` / ``scores.sif_stable_probability`` and
    ``classes.sgf_stability_class`` / ``classes.sif_stability_class`` are
    declared by the schema and are deliberately ABSENT. The first pair would
    need the descriptor logistic (unreachable from a sequence); the second pair
    would need a real SGF/SIF assay. Absent, not filled.
    """
    candidate_ref: dict[str, Any] = {
        "seq_len": profile["seq_len"],
        "target": target,
        "design_class": design_class,
    }
    if candidate_id is not None:
        candidate_ref["candidate_id"] = candidate_id
    if sequence_hash is not None:
        candidate_ref["sequence_hash"] = sequence_hash
    if include_sequence:
        candidate_ref["sequence"] = profile["sequence"]

    return {
        "feature_name": "gi_peptide_stability",
        "feature_version": feature_version,
        "endpoint_family": "degradation_or_gi_fluid_stability",
        "candidate_ref": candidate_ref,
        "scores": {
            # sgf_stable_probability / sif_stable_probability intentionally absent.
            "sgf_risk": profile["sgf_risk"],
            "sif_risk": profile["sif_risk"],
            "gi_balance_score": profile["gi_balance_score"],
            "permeability_size_penalty": profile["permeability_size_penalty"],
        },
        "classes": {
            # sgf_stability_class / sif_stability_class intentionally absent.
            "gi_design_class": profile["gi_design_class"],
            "length_bin": profile["length_bin"],
            "oral_systemic_length_feasibility": profile[
                "oral_systemic_length_feasibility"
            ],
        },
        "protease_motif_counts": {
            key: profile[key]
            for key in (
                "sgf_pepsin_aromatic_hydrophobic",
                "sif_trypsin_basic",
                "sif_chymotrypsin_aromatic_hydrophobic",
                "sif_dppiv_n_terminal_xp_or_xa",
            )
        },
        "design_guidance": {
            "recommended_design_actions": [
                action.strip()
                for action in str(profile["recommended_design_actions"]).split(";")
                if action.strip()
            ],
            "length_policy_note": profile["length_policy_note"],
            "route_mode": profile["length_bin_default_use"],
            "paired_assay_requirement": (
                "Any production claim requires paired SGF/SIF measurements under the "
                "intended formulation/exposure conditions. These are rule counts, not "
                "measured degradation rates."
            ),
        },
        "feature_gate": {
            "default_enabled": False,
            "enterprise_feature": True,
            "enterprise_visible": True,
            "primary_rank_eligible": False,
            "thermostability_rank_eligible": False,
            "serum_half_life_rank_eligible": False,
            "ranking_contribution": 0,
        },
        "messages": {
            "endpoint_warning": _DECLARATION_TEXT,
            "ui_badge": "GI rule-count triage (not a measured rate)",
            "recommended_use": profile["recommended_design_actions"],
        },
        "evidence": {
            "evidence_level": "rule_based_motif_counts_not_measured",
            "source_rows": GI_SOURCE["rows"],
            "source_environments": dict(GI_SOURCE["environment_counts"]),
            "relationship_to_current_heads": {
                "declaration": gut_stability_declaration(),
                "not_thermostability_tm_c": True,
                "not_serum_half_life": True,
                "length_policy_note": profile["length_policy_note"],
                "length_bin_default_use": profile["length_bin_default_use"],
            },
            "production_blockers": [
                "No paired SGF/SIF measurement exists for LigandForge designs; these "
                "are rule counts normalized by length, not measured degradation rates.",
                "The 200-descriptor logistic baseline (n=46 gastric ROC-AUC 0.811 / "
                "n=63 intestinal ROC-AUC 0.743) cannot be evaluated on a bare designed "
                "sequence — the source dataset has no amino-acid-sequence column.",
                "gi_balance_score is a Pareto-style triage score, not an oral "
                "bioavailability prediction, and carries ranking_contribution 0.",
            ],
        },
    }


# ---------------------------------------------------------------------------
# Input normalisation
# ---------------------------------------------------------------------------


def _coerce_mods(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize one modification record.

    An unsupplied flag defaults to "not modified", which is the CONSERVATIVE
    case — it raises risk through ``terminal_unprotected_penalty``. That is a
    deliberate default, not a zero-fill of a measurement.
    """
    out = dict(_EMPTY_MODS)
    if not raw:
        return out
    for key in ("n_terminal_protected", "c_terminal_protected", "cyclized"):
        if key in raw:
            out[key] = bool(raw[key])
    for key in ("d_residue_count", "n_methyl_count"):
        if key in raw and raw[key] is not None:
            try:
                out[key] = max(0, int(raw[key]))
            except (TypeError, ValueError) as exc:
                raise LigandAIValidationError(
                    f"modifications[{key!r}] must be an integer count "
                    f"(got {raw[key]!r})"
                ) from exc
    return out


def _mods_for(
    index: int,
    sequence: str,
    modifications: Any,
) -> dict[str, Any]:
    """Resolve the modification record for one sequence.

    Accepts a single global dict, a list aligned to ``sequences``, or a dict
    keyed by the raw sequence string. The three are told apart by shape: a
    global record carries the known flag names at its top level.
    """
    if modifications is None:
        return _coerce_mods(None)
    if isinstance(modifications, (list, tuple)):
        if index < len(modifications):
            return _coerce_mods(modifications[index])
        return _coerce_mods(None)
    if isinstance(modifications, Mapping):
        if any(k in modifications for k in _MODIFICATION_KEYS):
            return _coerce_mods(modifications)
        if sequence in modifications:
            return _coerce_mods(modifications[sequence])
        return _coerce_mods(None)
    raise LigandAIValidationError(
        "modifications must be a dict, a list aligned to sequences, or a dict "
        f"keyed by sequence (got {type(modifications).__name__})"
    )


def _normalize_sequences(sequences: str | Sequence[str]) -> list[str]:
    if isinstance(sequences, str):
        return [sequences]
    if not isinstance(sequences, Sequence):
        raise LigandAIValidationError(
            "sequences must be a string or a sequence of strings "
            f"(got {type(sequences).__name__})"
        )
    seqs = list(sequences)
    if not seqs:
        raise LigandAIValidationError("sequences (non-empty) is required")
    return ["" if s is None else str(s) for s in seqs]


def _proline_report(cleaned: list[str], max_fraction: float = 0.30) -> dict[str, Any]:
    """Batch proline-fraction summary, same keys as the platform tracker.

    An empty batch reports ABSENT — a proline fraction over zero residues is
    undefined, not 0.0.
    """
    kept = [s for s in cleaned if s]
    n_residues = sum(len(s) for s in kept)
    if not kept or n_residues == 0:
        return absent(
            "no sequences observed; proline fraction is undefined, not zero",
            declared_max_proline_fraction=max_fraction,
        )
    n_proline = sum(s.count(P1_PRIME_SHIELD) for s in kept)
    fractions = sorted(s.count(P1_PRIME_SHIELD) / len(s) for s in kept)
    mid = len(fractions) // 2
    median = (
        fractions[mid]
        if len(fractions) % 2
        else 0.5 * (fractions[mid - 1] + fractions[mid])
    )
    batch_fraction = n_proline / n_residues
    return {
        "status": "present",
        "n_sequences": len(kept),
        "n_residues": n_residues,
        "n_proline": n_proline,
        "batch_proline_fraction": batch_fraction,
        "median_sequence_proline_fraction": median,
        "max_sequence_proline_fraction": fractions[-1],
        "declared_max_proline_fraction": max_fraction,
        "n_sequences_over_ceiling": sum(1 for f in fractions if f > max_fraction),
        "exceeded": batch_fraction > max_fraction or fractions[-1] > max_fraction,
    }


def _score_batch_local(
    sequences: str | Sequence[str],
    modifications: Any = None,
    *,
    include_sequence_in_record: bool = False,
) -> dict[str, Any]:
    """The in-process batch scorer. Shared by the sync and async resources."""
    raw_seqs = _normalize_sequences(sequences)
    declaration = gut_stability_declaration()
    results: list[dict[str, Any]] = []
    cleaned_all: list[str] = []
    n_absent = 0

    for idx, raw in enumerate(raw_seqs):
        stripped = raw.strip().upper()
        cleaned = clean_sequence(raw)
        cleaned_all.append(cleaned)

        if not cleaned:
            n_absent += 1
            results.append(
                {
                    "sequence": stripped,
                    **absent(
                        "sequence contains no standard amino acids; SGF/SIF motif "
                        "counts are undefined, not zero",
                        input_index=idx,
                    ),
                    "declaration": declaration,
                }
            )
            continue

        if len(cleaned) < 2:
            n_absent += 1
            results.append(
                {
                    "sequence": cleaned,
                    **absent(
                        "sequence shorter than 2 residues has no P1/P1' pair; SGF/SIF "
                        "motif counts are undefined, not zero",
                        input_index=idx,
                        seq_len=len(cleaned),
                    ),
                    "declaration": declaration,
                }
            )
            continue

        dropped = sorted({ch for ch in stripped if ch.isalpha() and ch not in _AA_SET})
        mods = _mods_for(idx, raw, modifications)
        profile = score_gut_design(
            cleaned,
            n_terminal_protected=bool(mods["n_terminal_protected"]),
            c_terminal_protected=bool(mods["c_terminal_protected"]),
            cyclized=bool(mods["cyclized"]),
            d_residue_count=int(mods["d_residue_count"]),
            n_methyl_count=int(mods["n_methyl_count"]),
        )

        record = dict(profile)
        record["status"] = "present"
        record["input_index"] = idx
        record["modifications"] = mods
        # ⛔ Provenance is not decoration. A local result and a platform result
        # must never be indistinguishable in a customer's records.
        record["computed_by"] = "ligandai_sdk_local_ruleset"
        record["ruleset_version"] = GUT_STABILITY_RULESET_VERSION
        if dropped:
            record["non_standard_residues_dropped"] = dropped
        record["gi_stability_scores"] = build_gi_stability_record(
            profile, include_sequence=include_sequence_in_record
        )
        results.append(record)

    return {
        "results": results,
        "declaration": declaration,
        "computed_by": "ligandai_sdk_local_ruleset",
        "ruleset_version": GUT_STABILITY_RULESET_VERSION,
        "batch": {
            "n_input": len(raw_seqs),
            "n_scored": len(results) - n_absent,
            "n_absent": n_absent,
            "proline_guard": _proline_report(cleaned_all),
        },
    }


# ---------------------------------------------------------------------------
# Entitlement
# ---------------------------------------------------------------------------


def explain_entitlement(
    tier: str | None,
    *,
    is_super_admin: bool | int | None = None,
    module_access: Mapping[str, Any] | str | None = None,
) -> dict[str, Any]:
    """Explain whether an account reaches the GI module, and why.

    Mirrors ``server/gi-stability-sidecar.ts::canUseGiStabilityModule``:
    superadmin OR tier ``enterprise`` OR an explicit ``module_access`` grant.
    ``default_enabled`` is ``False`` for everyone.

    ⛔ THREE STATES, NEVER TWO. When ``module_access`` is not supplied — which
    is the normal case, because ``GET /api/auth/user`` does not publish it —
    a non-enterprise account is ``"indeterminate"``, not ``"denied"``. An
    explicit grant is exactly the thing that would flip it, and this function
    cannot see one. Reporting "denied" would be a confident wrong answer to a
    customer who does in fact hold a grant.

    Returns:
        ``{allowed, state, reason, tier, default_enabled, grant_keys_checked}``
        where ``state`` is ``"allowed"``, ``"denied"`` or ``"indeterminate"``.
    """
    normalized_tier = (tier or "").strip().lower()

    if bool(is_super_admin):
        return {
            "allowed": True,
            "state": "allowed",
            "reason": "superadmin accounts bypass the module gate",
            "tier": normalized_tier or None,
            "default_enabled": False,
            "grant_keys_checked": list(GI_STABILITY_MODULE_GRANT_KEYS),
        }

    if normalized_tier in GI_STABILITY_TIER_ALLOWLIST:
        return {
            "allowed": True,
            "state": "allowed",
            "reason": f"subscription tier {normalized_tier!r} is on the module allowlist",
            "tier": normalized_tier,
            "default_enabled": False,
            "grant_keys_checked": list(GI_STABILITY_MODULE_GRANT_KEYS),
        }

    parsed: Mapping[str, Any] | None = None
    if isinstance(module_access, str):
        try:
            candidate = _json.loads(module_access)
        except ValueError:
            candidate = None
        parsed = candidate if isinstance(candidate, Mapping) else None
    elif isinstance(module_access, Mapping):
        parsed = module_access

    if parsed is not None:
        granted = [k for k in GI_STABILITY_MODULE_GRANT_KEYS if parsed.get(k) is True]
        if granted:
            return {
                "allowed": True,
                "state": "allowed",
                "reason": f"explicit module_access grant: {', '.join(granted)}",
                "tier": normalized_tier or None,
                "default_enabled": False,
                "grant_keys_checked": list(GI_STABILITY_MODULE_GRANT_KEYS),
            }
        shown = repr(normalized_tier) if normalized_tier else "<unknown>"
        return {
            "allowed": False,
            "state": "denied",
            "reason": (
                f"tier {shown} is not on the allowlist and the supplied "
                f"module_access carries none of "
                f"{', '.join(GI_STABILITY_MODULE_GRANT_KEYS)}"
            ),
            "tier": normalized_tier or None,
            "default_enabled": False,
            "grant_keys_checked": list(GI_STABILITY_MODULE_GRANT_KEYS),
        }

    shown = repr(normalized_tier) if normalized_tier else "<unknown>"
    return {
        "allowed": False,
        "state": "indeterminate",
        "reason": (
            f"tier {shown} is not on the enterprise allowlist, "
            f"and this account endpoint does not publish module_access — so an explicit "
            f"grant cannot be ruled out from here. If GI values come back absent on your "
            f"peptide records, that is the entitlement gate, not a failed call. Ask your "
            f"Ligandal contact whether your account carries one of "
            f"{', '.join(GI_STABILITY_MODULE_GRANT_KEYS)}."
        ),
        "tier": normalized_tier or None,
        "default_enabled": False,
        "grant_keys_checked": list(GI_STABILITY_MODULE_GRANT_KEYS),
    }


# ---------------------------------------------------------------------------
# Generation toggle
# ---------------------------------------------------------------------------


def _validate_guidance(
    modules: Mapping[str, bool] | None,
    strength: float,
    mode: str,
) -> dict[str, Any]:
    """Validate and normalize a guidance request. Raises rather than clamping."""
    if mode not in _GUIDANCE_MODES:
        raise LigandAIValidationError(
            f"gi_stability_mode must be one of {_GUIDANCE_MODES} (got {mode!r}). "
            f"'resist' steers away from gut-labile motifs; 'target' steers toward "
            f"them for a deliberately lumen-released design."
        )
    try:
        strength_f = float(strength)
    except (TypeError, ValueError) as exc:
        raise LigandAIValidationError(
            f"gi_stability_strength must be a number (got {strength!r})"
        ) from exc
    if not (_MIN_STRENGTH <= strength_f <= _MAX_STRENGTH):
        raise LigandAIValidationError(
            f"gi_stability_strength must be in [{_MIN_STRENGTH}, {_MAX_STRENGTH}] "
            f"(got {strength_f})"
        )

    if modules is None:
        resolved = dict(GUIDANCE_MODULES)
    else:
        unknown = sorted(set(modules) - set(GUIDANCE_MODULES))
        if unknown:
            raise LigandAIValidationError(
                f"unknown gi_stability module(s): {unknown}. "
                f"Available: {sorted(GUIDANCE_MODULES)}"
            )
        resolved = {name: bool(modules.get(name, False)) for name in GUIDANCE_MODULES}
        if not any(resolved.values()):
            raise LigandAIValidationError(
                "at least one gi_stability module must be enabled; pass "
                "enabled=False to turn the whole head off rather than sending "
                "an all-off module map, which reads as a request that does nothing"
            )
    return {"modules": resolved, "strength": strength_f, "mode": mode}


def _toggle_not_forwarded() -> LigandAIParameterNotForwarded:
    """The generation toggle reaches the worker but not through the API hop.

    ⛔ VERIFIED 2026-09-24, not assumed. ``modal_workers/ligandforge_parallel_v2.py``
    reads ``gi_stability_enabled`` (with ``gut_stability_enabled`` as an alias)
    off the per-target dict and threads it into the denoising loop. But no
    Express route builds that key: ``server/ptf-integration-routes.ts`` imports
    the gate helpers from ``server/gi-stability-sidecar.ts`` and never calls
    them, and every generate payload builder in that file emits
    ``immuno_enabled`` / ``proteolytic_enabled`` / ``halflife_enabled`` /
    ``cellfree_enabled`` and no ``gi_stability_*`` key at all. The FastAPI
    backend has no occurrence of the key either.

    So a request that sets it today reports the knob ON and runs with it OFF —
    the exact defect :class:`LigandAIParameterNotForwarded` exists to refuse to
    manufacture. Neither the request schema nor the response body rejects
    unknown keys, so this cannot be detected at the wire level; the check has
    to live here.
    """
    return LigandAIParameterNotForwarded(
        "gi_stability_enabled is not forwarded by this platform's generation "
        "path. The generation worker reads it (ligandforge_parallel_v2 accepts "
        "gi_stability_enabled, and gut_stability_enabled as an alias), but no "
        "API hop in front of the worker puts it on the wire — the generate "
        "payload builders emit immuno / proteolytic / halflife / cellfree keys "
        "and no gi_stability key. Sending it anyway would report the knob ON "
        "and generate with it OFF, with nothing in the response saying so.\n"
        "\n"
        "What works today: score sequences you already have with "
        "client.gut_stability.score_local(...) (or .score(...) against a "
        "deployment that mounts the scorer) and filter on gi_balance_score. "
        "Pass allow_unforwarded=True to get the payload block anyway if you are "
        "running against a build where the plumbing has landed.",
        parameter=GI_STABILITY_CANONICAL_WIRE_KEY,
        reached="modal_workers/ligandforge_parallel_v2.py (generation worker)",
        dropped_at="server/ptf-integration-routes.ts (generate payload builders)",
        tracking="bd-dre-c80f4",
        override="allow_unforwarded=True",
    )


#: Statuses that mean "nothing is mounted here", as opposed to "the scorer ran
#: and said no". 404 no such route; 405 the path exists for another method;
#: 501 the hop answered but implements nothing.
_ROUTE_ABSENT_STATUSES = frozenset({404, 405, 501})


def _is_route_absent(exc: LigandAIError) -> bool:
    return getattr(exc, "status_code", None) in _ROUTE_ABSENT_STATUSES


def _scorer_not_mounted(path: str) -> LigandAIParameterNotForwarded:
    """The scoring route is not mounted on this deployment.

    An unmounted API path on a single-page-app server commonly answers 200 with
    the app shell rather than a 404, so BOTH signatures are treated as "the
    route is not there": a 404/405/501, and a 2xx body that carries no
    ``results`` list and no ``declaration`` block. Neither is "the scorer ran
    and found nothing" — that is a populated body, and it is returned.
    """
    return LigandAIParameterNotForwarded(
        f"the GI/gut-stability scoring route {path} is not mounted on this "
        f"deployment. The scorer exists as a Modal function "
        f"(modal_workers/gut_stability_scorer.py) but no Express or FastAPI "
        f"route forwards to it yet, so there is nothing to answer this call.\n"
        f"\n"
        f"Use client.gut_stability.score_local(...) — the same rule set, "
        f"evaluated in-process, no credits, no round trip. Results carry "
        f"computed_by='ligandai_sdk_local_ruleset' so they can never be "
        f"mistaken for a platform-computed record.",
        parameter="gut_stability.score",
        reached="modal_workers/gut_stability_scorer.py (Modal function)",
        dropped_at=f"public API surface ({path} not mounted)",
        tracking="bd-dre-c80f4",
        override="use score_local()",
    )


def _looks_like_scorer_payload(payload: Any) -> bool:
    """True only when the body actually came from the gut-stability scorer."""
    return (
        isinstance(payload, Mapping)
        and isinstance(payload.get("results"), list)
        and isinstance(payload.get("declaration"), Mapping)
    )


def _score_body(
    sequences: str | Sequence[str],
    modifications: Any,
) -> dict[str, Any]:
    seqs = _normalize_sequences(sequences)
    # Validate the modification shape locally so a malformed record fails here
    # with a named error rather than as an opaque server rejection.
    for idx, seq in enumerate(seqs):
        _mods_for(idx, seq, modifications)
    body: dict[str, Any] = {"sequences": seqs}
    if modifications is not None:
        body["modifications"] = modifications
    return body


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------

_SIDECAR_KEYS = (
    "gi_stability_proxy",
    "gi_stability_scores",
    "giStabilityScores",
)


class GutStability(Resource):
    """``client.gut_stability`` — GI / gut-stability developability triage.

    ⛔ Every score in this namespace is a rule count normalized by length: not
    a measured degradation rate, not a survival probability, not an oral
    bioavailability prediction, not a neural network. See the module docstring
    and :func:`gut_stability_declaration`.

    Enterprise module, ``default_enabled = False``. See :meth:`entitlement`.
    """

    # -- declaration ------------------------------------------------------

    @staticmethod
    def declaration() -> dict[str, Any]:
        """The machine-readable declaration block. Carry it with every number."""
        return gut_stability_declaration()

    # -- scoring ----------------------------------------------------------

    def score(
        self,
        sequences: str | Sequence[str],
        *,
        modifications: Mapping[str, Any] | Sequence[Mapping[str, Any] | None] | None = None,
    ) -> dict[str, Any]:
        """``POST /api/scoring/gut-stability`` — score a batch on the platform.

        Args:
            sequences: One peptide sequence or a list of them (single-letter).
            modifications: Post-design chemistry flags. Either one dict applied
                to every sequence, a list aligned to ``sequences``, or a dict
                keyed by sequence string. Keys:
                ``n_terminal_protected`` (bool), ``c_terminal_protected``
                (bool), ``cyclized`` (bool), ``d_residue_count`` (int),
                ``n_methyl_count`` (int). ⛔ D-residues and N-methylation are
                NOT expressible in the canonical-20 generator alphabet — they
                describe chemistry you intend to apply after design, are scored
                as flags, and are never inferred from the sequence.

        Returns:
            ``{results: [...], declaration: {...}, batch: {...}}``. Each result
            is either a scored record (``status="present"``) or an explicit
            ``{"status": "absent", "reason": ...}`` — never a zero-filled score.

        Raises:
            LigandAIParameterNotForwarded: when this deployment does not mount
                the scoring route. The message names :meth:`score_local` as the
                reachable path. It is NOT raised for a scorer that ran and
                found nothing — that is a populated body and is returned.
            LigandAIValidationError: on a malformed ``sequences`` /
                ``modifications`` argument, before any request is sent.
        """
        body = _score_body(sequences, modifications)
        try:
            payload = self._transport.request(
                "POST", GI_STABILITY_SCORING_PATH, json=body
            )
        except LigandAIError as exc:
            if _is_route_absent(exc):
                raise _scorer_not_mounted(GI_STABILITY_SCORING_PATH) from exc
            raise
        if not _looks_like_scorer_payload(payload):
            raise _scorer_not_mounted(GI_STABILITY_SCORING_PATH)
        return dict(payload)

    def score_local(
        self,
        sequences: str | Sequence[str],
        *,
        modifications: Mapping[str, Any] | Sequence[Mapping[str, Any] | None] | None = None,
        include_sequence_in_record: bool = False,
    ) -> dict[str, Any]:
        """Score in-process with the pinned rule set — no HTTP, no credits.

        The GI module is pure rule arithmetic over the sequence: motif counts
        normalized by length, plus a length-bin permeability term and the
        modification-rescue terms. There are no model weights and no data
        lookup, so the whole computation runs locally and reproduces the
        platform's numbers exactly.

        ⛔ PROVENANCE IS EXPLICIT. Every record carries
        ``computed_by="ligandai_sdk_local_ruleset"`` and ``ruleset_version``,
        so a locally scored design can never be mistaken for a
        platform-computed one in a customer's records.

        Args:
            sequences: One peptide sequence or a list of them.
            modifications: As in :meth:`score`.
            include_sequence_in_record: Put the raw sequence into the sidecar's
                ``candidate_ref``. Off by default — the sidecar is normally
                joined on ``sequence_hash``.

        Returns:
            The same shape :meth:`score` returns, plus the ``computed_by`` /
            ``ruleset_version`` provenance keys.

        Raises:
            LigandAIValidationError: on a malformed argument. A sequence that
                is merely unusable (empty, or under 2 residues) is reported as
                an ``absent`` record in ``results``, not raised.
        """
        return _score_batch_local(
            sequences,
            modifications,
            include_sequence_in_record=include_sequence_in_record,
        )

    # -- generation toggle ------------------------------------------------

    def generation_settings(
        self,
        *,
        enabled: bool = True,
        modules: Mapping[str, bool] | None = None,
        strength: float = 2.0,
        mode: str = "resist",
        allow_unforwarded: bool = False,
    ) -> dict[str, Any]:
        """Build the ``gi_stability_*`` generation-guidance block.

        Steers LigandForge away from (``mode="resist"``) or toward
        (``mode="target"``) the SGF/SIF cleavage motifs during discrete
        diffusion denoising. The returned dict is meant for
        ``client.peptides.generate(..., extra=<this block>)``.

        Args:
            enabled: Turn the guidance head on. ``False`` returns the block with
                the canonical key set false — an explicit "off", not an omission.
            modules: Per-module toggles. Keys: ``sgf_pepsin``, ``sif_trypsin``,
                ``sif_chymotrypsin``, ``sif_dppiv``, ``permeability_size``.
                Defaults to all on. An all-off map is refused.
            strength: Guidance multiplier, ``0.0``–``5.0``. Out-of-range raises
                rather than clamping.
            mode: ``"resist"`` (avoid gut cleavage, the default) or ``"target"``
                (deliberately gut-labile / lumen-released design).
            allow_unforwarded: Return the block even though this platform's
                generation path drops it. See below.

        Returns:
            ``{"gi_stability_enabled": bool, "gi_stability_modules": {...},
            "gi_stability_strength": float, "gi_stability_mode": str}`` — the
            canonical spelling. ``gut_stability_enabled`` is a worker-side alias
            and is deliberately not emitted here.

        Raises:
            LigandAIParameterNotForwarded: by default, because no API hop
                forwards ``gi_stability_enabled`` today. The worker reads it;
                the route in front of the worker never sends it. Passing it
                anyway would report the knob ON and generate with it OFF. Set
                ``allow_unforwarded=True`` to get the block regardless — for a
                build where the plumbing has landed.
            LigandAIValidationError: on an unknown module, an out-of-range
                strength, or an invalid mode.
        """
        resolved = _validate_guidance(modules, strength, mode)
        if not allow_unforwarded:
            raise _toggle_not_forwarded()
        return {
            GI_STABILITY_CANONICAL_WIRE_KEY: bool(enabled),
            "gi_stability_modules": resolved["modules"],
            "gi_stability_strength": resolved["strength"],
            "gi_stability_mode": resolved["mode"],
        }

    # -- entitlement ------------------------------------------------------

    def entitlement(self) -> dict[str, Any]:
        """Why GI values are (or are not) visible on your peptide records.

        Reads the authenticated account and applies the same gate the server
        does: superadmin OR tier ``enterprise`` OR an explicit ``module_access``
        grant, with ``default_enabled = False`` for everyone.

        ⛔ Answers ``state="indeterminate"``, never ``"denied"``, for a
        non-enterprise account — ``GET /api/auth/user`` does not publish
        ``module_access``, and an explicit grant is exactly what would flip the
        answer. See :func:`explain_entitlement`.

        Returns:
            ``{allowed, state, reason, tier, default_enabled, grant_keys_checked}``.
        """
        user = self._transport.request("GET", "/api/auth/user") or {}
        tier = user.get("subscriptionTier") or user.get("subscription_tier")
        is_super = user.get("isSuperAdmin", user.get("is_super_admin"))
        module_access = user.get("moduleAccess", user.get("module_access"))
        return explain_entitlement(
            tier, is_super_admin=is_super, module_access=module_access
        )

    # -- sidecar ----------------------------------------------------------

    @staticmethod
    def read_sidecar(peptide: Mapping[str, Any] | None) -> dict[str, Any]:
        """Pull the GI sidecar off a platform peptide record.

        Mirrors ``server/gi-stability-sidecar.ts::pickGiStabilitySidecar``: the
        payload may arrive as ``gi_stability_proxy``, ``gi_stability_scores``,
        ``giStabilityScores``, or nested under ``metadata.gi_stability_proxy``.

        Returns the sidecar dict when present, and an explicit ``absent``
        record otherwise — never ``None``. An absent sidecar has two very
        different causes that must not be collapsed: the module was never run
        for this design, or your account is not entitled to see it. The absent
        record names both and points at :meth:`entitlement`.
        """
        if not isinstance(peptide, Mapping):
            raise LigandAIValidationError(
                f"read_sidecar expects a peptide record (mapping), got "
                f"{type(peptide).__name__}"
            )
        for key in _SIDECAR_KEYS:
            value = peptide.get(key)
            if isinstance(value, Mapping) and value.get("feature_name") == "gi_peptide_stability":
                return dict(value)
        metadata = peptide.get("metadata")
        if isinstance(metadata, Mapping):
            value = metadata.get("gi_stability_proxy")
            if isinstance(value, Mapping) and value.get("feature_name") == "gi_peptide_stability":
                return dict(value)
        return absent(
            "no gi_peptide_stability sidecar on this record. Two different causes "
            "produce this and they are not the same fact: (1) the GI module was "
            "not run for this design — it is default_enabled=False, or (2) your "
            "account is not entitled to see it. Call "
            "client.gut_stability.entitlement() to tell them apart, and "
            "client.gut_stability.score_local([sequence]) to score the design "
            "yourself either way.",
            keys_checked=[*_SIDECAR_KEYS, "metadata.gi_stability_proxy"],
        )


class AsyncGutStability(AsyncResource):
    """Async variant of :class:`GutStability` — identical method surface.

    The local scorer, the guidance block and the sidecar reader do no I/O, so
    they are the SAME functions here rather than coroutine copies. Sharing the
    object (instead of re-typing the body) is what stops the sync and async
    surfaces from drifting into two rule sets.
    """

    declaration = staticmethod(gut_stability_declaration)
    score_local = GutStability.score_local
    generation_settings = GutStability.generation_settings
    read_sidecar = staticmethod(GutStability.read_sidecar)

    async def score(
        self,
        sequences: str | Sequence[str],
        *,
        modifications: Mapping[str, Any] | Sequence[Mapping[str, Any] | None] | None = None,
    ) -> dict[str, Any]:
        """Async variant of :meth:`GutStability.score`."""
        body = _score_body(sequences, modifications)
        try:
            payload = await self._transport.request(
                "POST", GI_STABILITY_SCORING_PATH, json=body
            )
        except LigandAIError as exc:
            if _is_route_absent(exc):
                raise _scorer_not_mounted(GI_STABILITY_SCORING_PATH) from exc
            raise
        if not _looks_like_scorer_payload(payload):
            raise _scorer_not_mounted(GI_STABILITY_SCORING_PATH)
        return dict(payload)

    async def entitlement(self) -> dict[str, Any]:
        """Async variant of :meth:`GutStability.entitlement`."""
        user = await self._transport.request("GET", "/api/auth/user") or {}
        tier = user.get("subscriptionTier") or user.get("subscription_tier")
        is_super = user.get("isSuperAdmin", user.get("is_super_admin"))
        module_access = user.get("moduleAccess", user.get("module_access"))
        return explain_entitlement(
            tier, is_super_admin=is_super, module_access=module_access
        )
