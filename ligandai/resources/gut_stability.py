# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""GI / gut-stability developability triage — SGF / SIF protease-motif burden.

Wraps the platform's GI peptide-stability module (``gi_peptide_stability``,
bead ``bd-dre-c80f4.1``) and exposes it as ``client.gut_stability``:

- :meth:`GutStability.score`        → ``POST /api/scoring/gut-stability``
- :meth:`GutStability.score_local`  → the same rule set, evaluated in-process
- :meth:`GutStability.generation_settings` → the ``gi_stability_enabled`` block
- :meth:`GutStability.entitlement`  → why a value is absent for your account
- :meth:`GutStability.read_sidecar` → pull the sidecar off a platform record

Two sibling developability scorers ride on the same namespace, because a
customer asking "is this orally viable" needs all three answers and they share
one declaration discipline [bd-dre-c80f4.26.2]:

- :meth:`GutStability.chameleonicity` → ``POST /api/scoring/chameleonicity``
  and :meth:`GutStability.chameleonicity_local`. Conformational-amphipathicity
  index for a MACROCYCLE. ⛔ On a linear construct the answer is
  ``applicable=False, index=None`` — never ``0.0``, which would read as "a bad
  chameleon" instead of "the question does not apply".
- :meth:`GutStability.protein_gut_stability` →
  ``POST /api/scoring/protein-gut-stability`` and
  :meth:`GutStability.protein_gut_stability_local`. Structure-weighted gastric
  and intestinal protease exposure for a FULL PROTEIN or a
  biosimilar/biobetter variant. ⛔ There are THREE verdicts that mean
  "accessibility is ABSENT, never zero" and they are not interchangeable:
  ``INDETERMINATE_NO_STRUCTURE`` (none supplied),
  ``INDETERMINATE_FRAME_MISMATCH`` (supplied, but no motif site landed on a
  modelled residue — a wrong ``residue_offset``) and
  ``INDETERMINATE_NO_ACCESSIBILITY``. ⛔ ``residue_offset`` is the STRUCTURE
  RESIDUE NUMBER OF SEQUENCE POSITION 1: first modelled residue 29 ⇒
  ``residue_offset=29``, not 28. Getting it wrong does not raise.

Each of the three carries its OWN declaration block, owned by the module that
computes it and forwarded verbatim. Nothing here restates, summarises or
softens them.

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

import importlib
import json as _json
import os
import pathlib
import sys
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from ligandai.errors import (
    LigandAIError,
    LigandAIParameterNotForwarded,
    LigandAIValidationError,
)
from ligandai.resources._base import AsyncResource, Resource

__all__ = [
    "AsyncGutStability",
    "CHAMELEONICITY_MODULE",
    "CHAMELEONICITY_SCORING_PATH",
    "CONSTRUCT_RESOLVER_MODULE",
    "DESCRIPTOR_LOGISTIC_BASELINE",
    "GI_SOURCE",
    "GI_STABILITY_CANONICAL_WIRE_KEY",
    "GI_STABILITY_MODULE_GRANT_KEYS",
    "GI_STABILITY_SCORING_PATH",
    "GI_STABILITY_TIER_ALLOWLIST",
    "GUIDANCE_MODULES",
    "GUT_STABILITY_RULESET_VERSION",
    "INDETERMINATE_VERDICTS",
    "LENGTH_BINS",
    "MAX_PROTEIN_RECORDS",
    "MAX_SEQUENCE_BATCH",
    "MAX_STRUCTURE_BYTES",
    "PLATFORM_MODULE_PATH_ENV",
    "PROTEIN_GUT_STABILITY_MODULE",
    "PROTEIN_GUT_STABILITY_SCORING_PATH",
    "STRUCTURE_FORMATS",
    "VALID_CYCLIC_MODES",
    "GutStability",
    "LigandAIPlatformModuleUnavailable",
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

#: Keys that identify a mapping as a MODIFICATION RECORD rather than a
#: ``{sequence: record}`` map. ``declared`` is in here because the same chemistry
#: is accepted nested as well as flat — see :func:`_merge_declared`. Leaving it
#: out made ``{"declared": {...}}`` read as a sequence-keyed map and resolve to
#: no modifications at all, silently.
_MODIFICATION_CONTAINER_KEYS = (*_MODIFICATION_KEYS, "declared")

#: The structural fields of a construct — how it was MADE, as opposed to the
#: declared chemistry applied to it. Defined here rather than beside the
#: chameleonicity helpers because the PEPTIDE paths need them too: see
#: :data:`_MODIFICATION_RECORD_KEYS`.
_CONSTRUCT_STRUCTURAL_KEYS = (
    "modality",
    "cyclic_mode",
    "strict_recombinant",
    "modality_transform_applied",
)

#: Keys that identify a mapping as a modification/construct RECORD rather than a
#: ``{sequence: record}`` map.
#:
#: ⛔ THE STRUCTURAL KEYS BELONG IN HERE. Leaving them out is the same defect the
#: comment above records for ``declared``, and it bit twice:
#:
#:   * ``score_local({"cyclic_mode": "lactam"})`` fell through to the
#:     sequence-keyed-map branch, matched no sequence, and resolved to NO
#:     modifications at all — silently. Measured: rescue 0.0 where the platform
#:     resolver gives 0.36, which straddles the 0.72 ``balanced_gi_candidate``
#:     threshold.
#:   * on the wire, a construct-only record survived only BY ACCIDENT, through
#:     that same wrong branch, because its values happened not to be mappings.
#:     Add one chemistry key and the structural keys vanished.
_MODIFICATION_RECORD_KEYS = (
    *_MODIFICATION_CONTAINER_KEYS,
    *_CONSTRUCT_STRUCTURAL_KEYS,
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


def _merge_declared(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """The declared construct chemistry, accepted FLAT **or** nested.

    ⛔ ONE NORMALIZER, NOT TWO CALL-SITE SPELLINGS. The platform's two scoring
    ops used to want opposite shapes for the same input and each ignored the
    other SILENTLY: the peptide op read ``n_methyl_count`` flat, the
    chameleonicity op read it under ``declared``, and the same payload produced
    two different numbers with no error. Measured on the chameleonicity side, the
    ignored spelling left backbone NH donors at 9 / index 0.6947 where the
    honoured one gave 6 / 0.8487 — N-methylation is the most load-bearing input
    that module has, so this was never a rounding difference.

    So every path in this module funnels through here. Both spellings are
    accepted, a NESTED value wins a conflict (it is the more specific
    statement), and only keys the caller actually supplied come back — an
    unsupplied flag must stay unsupplied so the scorer applies its own
    conservative default rather than a value invented here.
    """
    if not raw:
        return {}
    out: dict[str, Any] = {k: raw[k] for k in _MODIFICATION_KEYS if k in raw}
    nested = raw.get("declared")
    if nested is not None:
        if not isinstance(nested, Mapping):
            raise LigandAIValidationError(
                f"declared must be an object of construct chemistry flags (got "
                f"{type(nested).__name__})"
            )
        # Nested wins: it is the more specific statement of the two.
        out.update({k: nested[k] for k in _MODIFICATION_KEYS if k in nested})
    return out


def _coerce_mods(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize one modification record to the five flat scorer flags.

    Accepts the chemistry flat or nested under ``declared`` — see
    :func:`_merge_declared`.

    An unsupplied flag defaults to "not modified", which is the CONSERVATIVE
    case — it raises risk through ``terminal_unprotected_penalty``. That is a
    deliberate default, not a zero-fill of a measurement.
    """
    out = dict(_EMPTY_MODS)
    supplied = _merge_declared(raw)
    if not supplied:
        return out
    for key in ("n_terminal_protected", "c_terminal_protected", "cyclized"):
        if key in supplied:
            out[key] = bool(supplied[key])
    for key in ("d_residue_count", "n_methyl_count"):
        if key in supplied and supplied[key] is not None:
            try:
                out[key] = max(0, int(supplied[key]))
            except (TypeError, ValueError) as exc:
                raise LigandAIValidationError(
                    f"modifications[{key!r}] must be an integer count "
                    f"(got {supplied[key]!r})"
                ) from exc
    return out


def _chemistry_both_spellings(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """The supplied chemistry emitted BOTH flat and nested, for the wire.

    A deployment whose scoring op reads only one of the two spellings still sees
    the chemistry. Sending one spelling and hoping is how the silent-ignore
    defect above survived; a scorer that ignores a declared N-methyl count does
    not error, it just answers a different question.
    """
    if not raw:
        return {}
    supplied = _merge_declared(raw)
    # ⛔ Everything that is NOT chemistry is carried through untouched — above
    # all the STRUCTURAL construct keys (cyclic_mode, modality, ...). Rebuilding
    # the record from the chemistry alone dropped them: a caller sending
    # {"cyclic_mode": "lactam", "declared": {"n_methyl_count": 3}} went out as
    # {"n_methyl_count": 3, "declared": {...}} with the cyclization GONE, losing
    # the +0.18 cyclized term and the terminal protection a head-to-tail amide
    # implies. That is the most ordinary call shape there is — a macrocycle that
    # is also N-methylated — and it silently understated the score.
    rest = {
        key: value
        for key, value in raw.items()
        if key != "declared" and key not in _MODIFICATION_KEYS
    }
    if not supplied:
        return rest
    return {**rest, **supplied, "declared": dict(supplied)}


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
        if any(k in modifications for k in _MODIFICATION_RECORD_KEYS):
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


def _raw_record_for(index: int, sequence: str, modifications: Any) -> Mapping[str, Any] | None:
    """The caller's record for one sequence, UNCOERCED.

    Same container discrimination as :func:`_mods_for`, but it hands back what the
    caller actually wrote, so the structural construct keys are still visible.
    """
    if modifications is None:
        return None
    if isinstance(modifications, (list, tuple)):
        rec = modifications[index] if index < len(modifications) else None
        return rec if isinstance(rec, Mapping) else None
    if isinstance(modifications, Mapping):
        if any(k in modifications for k in _MODIFICATION_RECORD_KEYS):
            return modifications
        rec = modifications.get(sequence)
        return rec if isinstance(rec, Mapping) else None
    return None


def _resolve_structural_constructs(
    raw_seqs: Sequence[str],
    modifications: Any,
    *,
    platform_module_path: str | Sequence[str] | None = None,
) -> tuple[Any, list[dict[str, Any] | None]]:
    """Turn structural construct keys into the flat flags the in-SDK port reads.

    The pinned in-SDK rule table takes RESOLVED chemistry (``cyclized``,
    ``n_terminal_protected``, ...). It has, and should have, no opinion about
    ``cyclic_mode`` or ``modality``: resolving those means deciding whether the
    REALISED peptide actually carries the bond — the terminal-Cys pair, the
    interior-Cys rule under recombinant expression, the terminal-Pro check that
    blocks a head-to-tail closure — and that logic lives with the generator, in
    ``gut_stability_penalties.resolve_construct_modifications``.

    ⛔ SO STRUCTURAL KEYS ARE EITHER RESOLVED THROUGH THAT MODULE OR REFUSED.
    They are never ignored. ``cyclized`` is worth +0.18, the largest single
    rescue term, and before this the local path dropped it silently: measured
    rescue 0.0 against the resolver's 0.36 for one ``{"cyclic_mode": "lactam"}``,
    which straddles the 0.72 ``balanced_gi_candidate`` threshold. A second copy
    of the resolver here would be a second opinion about the same molecule, which
    is the defect one layer along, so importing it is the only honest option.

    Returns the modifications to score with, plus the per-sequence resolution
    records (``None`` where nothing structural was supplied).
    """
    empty: list[dict[str, Any] | None] = [None] * len(raw_seqs)
    if modifications is None:
        return None, empty

    records = [_raw_record_for(i, s, modifications) for i, s in enumerate(raw_seqs)]

    def _is_structural(rec: Mapping[str, Any] | None) -> bool:
        return bool(rec) and any(k in rec for k in _CONSTRUCT_STRUCTURAL_KEYS)

    if not any(_is_structural(r) for r in records):
        return modifications, empty          # pure offline path, untouched

    penalties = _import_platform_module(
        "gut_stability_penalties",
        needed_by=(
            "score_local with a structural construct key "
            f"({', '.join(_CONSTRUCT_STRUCTURAL_KEYS)}). Resolving one means "
            "deciding whether the realised peptide carries the bond, which the "
            "SDK deliberately does not reimplement. Pass the already-resolved "
            f"chemistry instead ({', '.join(_MODIFICATION_KEYS)}) to score "
            "fully offline"
        ),
        platform_module_path=platform_module_path,
    )

    resolved: list[Any] = []
    resolutions: list[dict[str, Any] | None] = []
    for seq, rec in zip(raw_seqs, records, strict=True):
        if not _is_structural(rec):
            resolved.append(dict(rec) if rec else None)
            resolutions.append(None)
            continue
        assert rec is not None
        res = penalties.resolve_construct_modifications(
            seq,
            modality=rec.get("modality"),
            cyclic_mode=rec.get("cyclic_mode"),
            strict_recombinant=bool(rec.get("strict_recombinant", True)),
            declared=_merge_declared(rec) or None,
            modality_transform_applied=bool(rec.get("modality_transform_applied", True)),
        )
        resolved.append(dict(res["scorer_kwargs"]))
        # The three-valued truth and the omitted terms, so "cyclization unknown"
        # stays distinguishable from "declared linear" — both score the term at
        # 0.0, and only this record says which happened.
        resolutions.append(
            {
                key: res.get(key)
                for key in (
                    "cyclized_state",
                    "cyclized_evidence",
                    "cyclized_reason",
                    "cyclization_route",
                    "cyclization_route_source",
                    "modality",
                    "cyclic_mode",
                    "provenance",
                    "rescue_terms_applied",
                    "rescue_terms_omitted",
                )
            }
        )
    return resolved, resolutions


def _score_batch_local(
    sequences: str | Sequence[str],
    modifications: Any = None,
    *,
    include_sequence_in_record: bool = False,
    platform_module_path: str | Sequence[str] | None = None,
) -> dict[str, Any]:
    """The in-process batch scorer. Shared by the sync and async resources."""
    raw_seqs = _normalize_sequences(sequences)
    modifications, resolutions = _resolve_structural_constructs(
        raw_seqs, modifications, platform_module_path=platform_module_path
    )
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
        if resolutions[idx] is not None:
            record["modification_resolution"] = resolutions[idx]
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


def _scorer_not_mounted(
    path: str,
    *,
    scorer: str = "GI/gut-stability",
    parameter: str = "gut_stability.score",
    local_method: str = "score_local",
    module: str = "modal_workers/gut_stability_scorer.py",
) -> LigandAIParameterNotForwarded:
    """The scoring route is not mounted on THIS deployment.

    ⛔ This is raised ONLY for a route that is genuinely absent. A mounted route
    that answers with the frozen envelope returns its results; there is no
    hardcoded refusal in the success path. Two signatures count as absent,
    because an unmounted API path on a single-page-app server commonly answers
    200 with the app shell rather than a 404: a 404/405/501, and a 2xx body
    that carries no ``results`` list and no ``declaration`` block. Neither is
    "the scorer ran and found nothing" — that is a populated body, and it is
    returned. A 403 is the ENTITLEMENT GATE refusing, which is a different fact
    again and is re-raised untouched.
    """
    return LigandAIParameterNotForwarded(
        f"the {scorer} scoring route {path} is not mounted on this deployment. "
        f"The scorer itself exists ({module}); this build's public API surface "
        f"does not expose it, so there is nothing to answer this call.\n"
        f"\n"
        f"Use client.gut_stability.{local_method}(...) — the same rule set, "
        f"evaluated in-process, no credits, no round trip. Local results carry "
        f"an explicit computed_by provenance key so they can never be mistaken "
        f"for a platform-computed record.",
        parameter=parameter,
        reached=f"{module} (platform scorer)",
        dropped_at=f"public API surface ({path} not mounted)",
        tracking="bd-dre-c80f4",
        override=f"use {local_method}()",
    )


def _looks_like_scorer_payload(payload: Any) -> bool:
    """True only when the body actually came from the gut-stability scorer."""
    return (
        isinstance(payload, Mapping)
        and isinstance(payload.get("results"), list)
        and isinstance(payload.get("declaration"), Mapping)
    )


def _modifications_for_wire(modifications: Any) -> Any:
    """Re-emit a modification side-channel with BOTH chemistry spellings.

    The CONTAINER shape the caller chose is preserved (one shared record, a list
    aligned to the batch, or a map keyed by sequence) — only the chemistry inside
    each record is normalized, so a caller's alignment is never silently
    re-interpreted. See :func:`_chemistry_both_spellings`.
    """
    if modifications is None:
        return None
    if isinstance(modifications, (list, tuple)):
        return [
            _chemistry_both_spellings(r) if isinstance(r, Mapping) else r
            for r in modifications
        ]
    if isinstance(modifications, Mapping):
        if any(k in modifications for k in _MODIFICATION_RECORD_KEYS):
            return _chemistry_both_spellings(modifications)
        return {
            key: _chemistry_both_spellings(value) if isinstance(value, Mapping) else value
            for key, value in modifications.items()
        }
    return modifications


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
        body["modifications"] = _modifications_for_wire(modifications)
    return body


# ---------------------------------------------------------------------------
# Chameleonicity (macrocycles) and full-protein / biosimilar gut stability
#
# ⛔ NEITHER OF THESE IS AN IN-SDK PORT. :meth:`GutStability.score_local` carries
# a pinned copy of the GI rule table because that rule set is pure arithmetic
# over a sequence — no data, no coordinates, nothing to drift against but the
# coefficients, which one test pins. These two are different:
#
#   * chameleonicity resolves cyclicity through the platform's construct
#     resolver, whose verdict depends on modality/cyclic-mode vocabulary that
#     lives with the generator, and
#   * the protein scorer reads real coordinates.
#
# A second copy of either would be a second opinion about the same molecule. So
# the local paths IMPORT the platform modules and degrade with an explicit,
# named error when they are absent — which is the normal case for a
# pip-installed customer. An absent module is never a zero.
# ---------------------------------------------------------------------------

#: ``POST /api/scoring/chameleonicity``. Body ``{sequences, constructs?}``.
CHAMELEONICITY_SCORING_PATH = "/api/scoring/chameleonicity"

#: ``POST /api/scoring/protein-gut-stability``. Body ``{records, top_n?}``.
PROTEIN_GUT_STABILITY_SCORING_PATH = "/api/scoring/protein-gut-stability"


def _chameleonicity_not_mounted() -> LigandAIParameterNotForwarded:
    return _scorer_not_mounted(
        CHAMELEONICITY_SCORING_PATH,
        scorer="chameleonicity",
        parameter="gut_stability.chameleonicity",
        local_method="chameleonicity_local",
        module="modal_workers/chameleonicity.py",
    )


def _protein_gut_not_mounted() -> LigandAIParameterNotForwarded:
    return _scorer_not_mounted(
        PROTEIN_GUT_STABILITY_SCORING_PATH,
        scorer="full-protein / biosimilar gut-stability",
        parameter="gut_stability.protein_gut_stability",
        local_method="protein_gut_stability_local",
        module="modal_workers/protein_gut_stability.py",
    )


#: Platform modules the LOCAL paths import. NOT SDK dependencies — they ship
#: with the platform, not with the ``ligandai`` wheel.
CHAMELEONICITY_MODULE = "chameleonicity"
PROTEIN_GUT_STABILITY_MODULE = "protein_gut_stability"
CONSTRUCT_RESOLVER_MODULE = "gut_stability_penalties"

#: ``os.pathsep``-separated directories to add to ``sys.path`` while importing a
#: platform module. ⛔ There is deliberately NO baked-in default: a published
#: wheel must never hand a customer an internal filesystem path (the same reason
#: ``GI_SOURCE`` drops the platform module's ``ported_from`` key).
PLATFORM_MODULE_PATH_ENV = "LIGANDAI_PLATFORM_MODULE_PATH"

#: Structure formats the platform's reader dispatches on. Verbatim from
#: ``modal_workers/gut_scoring_dispatch_c80f4.py::_STRUCTURE_SUFFIX`` — the
#: suffix matters, because gemmi picks its parser from it.
_STRUCTURE_SUFFIX: dict[str, str] = {
    "pdb": ".pdb",
    "ent": ".pdb",
    "cif": ".cif",
    "mmcif": ".cif",
    "mcif": ".cif",
    "pdbx": ".cif",
}
STRUCTURE_FORMATS = frozenset(_STRUCTURE_SUFFIX)

#: ``cyclic_guidance.VALID_CYCLIC_MODES``.
VALID_CYCLIC_MODES = frozenset({"none", "disulfide", "head_tail_contact", "lactam"})

#: Batch bounds, matching the platform bridge so a local run and a remote run
#: refuse the same oversized request.
MAX_SEQUENCE_BATCH = 500
MAX_PROTEIN_RECORDS = 25
MAX_STRUCTURE_BYTES = 40 * 1024 * 1024

_PDB_ID_LENGTH = 4

#: The THREE distinct verdicts that mean "accessible_* is absent", each for a
#: different reason. ⛔ Never collapse them: "you supplied no structure", "your
#: residue_offset is wrong" and "the panel produced no per-residue figure" call
#: for different fixes, and a prefix match on ``INDETERMINATE_`` would hide
#: which one happened. Verbatim from
#: ``modal_workers/protein_gut_stability.py``.
INDETERMINATE_VERDICTS = (
    "INDETERMINATE_NO_STRUCTURE",
    "INDETERMINATE_FRAME_MISMATCH",
    "INDETERMINATE_NO_ACCESSIBILITY",
)

#: Keys that make a mapping a CONSTRUCT rather than a ``{sequence: construct}``
#: map. Used to refuse an ambiguous side-channel instead of guessing. Includes
#: the flat chemistry spellings, because ``{"cyclized": True}`` is a perfectly
#: good construct and must not read as a mis-keyed sequence map.
_CONSTRUCT_KEYS = (*_CONSTRUCT_STRUCTURAL_KEYS, *_MODIFICATION_CONTAINER_KEYS)

_LOCAL_PLATFORM_PROVENANCE = "ligandai_sdk_local_platform_module"


class LigandAIPlatformModuleUnavailable(LigandAIError, ImportError):
    """A LOCAL scorer needs a platform module this installation does not carry.

    ⛔ WHY THIS IS AN ERROR AND NOT A DEGRADED NUMBER. The two local paths that
    raise it (:meth:`GutStability.chameleonicity_local` and
    :meth:`GutStability.protein_gut_stability_local`) compute a developability
    verdict. "The module that computes it is not installed" and "the molecule
    scored badly" are different facts, and a package that answered the first
    with a low score would be manufacturing the second. Same reason a linear
    construct gets ``index=None`` rather than ``0.0``.

    Subclasses :class:`ImportError` as well as :class:`LigandAIError`, matching
    the SDK's convention for a missing local dependency (see
    ``ligandai.fold_charts._require_matplotlib`` and the numpy path in
    ``ligandai.resources.folds``), so ``except ImportError`` keeps working while
    anything that surfaces the message gets the full explanation.

    Attributes
    ----------
    module : str
        The import name that could not be resolved.
    needed_by : str
        The SDK method that needed it.
    env_var : str
        The environment variable that points at the platform module directory.
    searched : tuple[str, ...]
        The directories that were actually put on ``sys.path`` and retried.
    """

    def __init__(
        self,
        message: str,
        *,
        module: str,
        needed_by: str,
        env_var: str = PLATFORM_MODULE_PATH_ENV,
        searched: tuple[str, ...] = (),
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.module = module
        self.needed_by = needed_by
        self.env_var = env_var
        self.searched = searched


def _platform_search_dirs(extra: str | Sequence[str] | None = None) -> list[str]:
    """Candidate directories holding the platform modules, most specific first."""
    out: list[str] = []
    if isinstance(extra, str):
        out.append(extra)
    elif extra is not None:
        out.extend(str(p) for p in extra)
    env = os.environ.get(PLATFORM_MODULE_PATH_ENV, "")
    out.extend(part for part in env.split(os.pathsep) if part.strip())
    return [p for p in (s.strip() for s in out) if p]


@contextmanager
def _sys_path_prefixed(dirs: Sequence[str]) -> Iterator[tuple[str, ...]]:
    """Temporarily prepend existing ``dirs`` to ``sys.path``.

    The entries are REMOVED again on exit. A permanent mutation would make the
    absence of a platform module depend on which SDK call ran first in the
    process, which is exactly the kind of order-dependent "it worked on my
    machine" that an explicit-absence contract exists to prevent.
    """
    added = [d for d in dirs if os.path.isdir(d) and d not in sys.path]
    for d in reversed(added):
        sys.path.insert(0, d)
    try:
        yield tuple(added)
    finally:
        for d in added:
            try:
                sys.path.remove(d)
            except ValueError:  # pragma: no cover - another caller removed it
                pass


def _import_platform_module(
    name: str,
    *,
    needed_by: str,
    platform_module_path: str | Sequence[str] | None = None,
) -> Any:
    """Import a platform module, or raise :class:`LigandAIPlatformModuleUnavailable`.

    Tried in order: the ambient import path (the platform's own processes, and
    anything the caller has already arranged), then the directories named by
    ``platform_module_path`` and :data:`PLATFORM_MODULE_PATH_ENV`.
    """
    try:
        return importlib.import_module(name)
    except ImportError as first_exc:
        cause: ImportError = first_exc

    dirs = _platform_search_dirs(platform_module_path)
    with _sys_path_prefixed(dirs) as added:
        if added:
            try:
                return importlib.import_module(name)
            except ImportError as exc:
                cause = exc

    raise LigandAIPlatformModuleUnavailable(
        f"{needed_by} needs the platform module {name!r}, which is not "
        f"importable here ({type(cause).__name__}: {cause}).\n"
        f"\n"
        f"⛔ This module ships with the LigandAI platform, NOT with the "
        f"ligandai package, so a pip-installed client does not have it. That is "
        f"why this is an error and not a number: 'the scorer is not installed' "
        f"and 'this molecule scored badly' are different facts, and returning a "
        f"score here would turn the first into the second.\n"
        f"\n"
        f"What works:\n"
        f"  - call the REMOTE method instead (it runs on the platform), or\n"
        f"  - point the SDK at a platform checkout: export "
        f"{PLATFORM_MODULE_PATH_ENV}=/path/to/modal_workers (or pass "
        f"platform_module_path=... to this call).\n"
        f"Directories searched: {list(added) or 'none'}.",
        module=name,
        needed_by=needed_by,
        searched=added,
    ) from cause


def _require_gemmi(needed_by: str) -> Any:
    """gemmi, or an explicit absence.

    ⛔ Called BEFORE a structure is handed to the protein scorer. The platform
    module catches its own import failure and falls through to
    ``verdict=INDETERMINATE_NO_STRUCTURE`` — correct for "no structure was
    supplied", but a caller who DID supply one would read it as their file being
    ignored. A missing structure reader is a missing instrument, and it says so.
    """
    try:
        return importlib.import_module("gemmi")
    except ImportError as exc:
        raise LigandAIPlatformModuleUnavailable(
            f"{needed_by} was given a structure, and structure parsing needs "
            f"gemmi, which is not installed ({exc}).\n"
            f"\n"
            f"⛔ Not returning a record instead: without a structure reader the "
            f"accessibility terms are ABSENT, and a record saying "
            f"'INDETERMINATE_NO_STRUCTURE' would read as 'you gave me no "
            f"structure' when you did. Install it with `pip install gemmi`, or "
            f"call the remote protein_gut_stability(...) method.",
            module="gemmi",
            needed_by=needed_by,
        ) from exc


# --- shared input normalisation for the two new scorers --------------------


def _normalize_wire_sequences(sequences: str | Sequence[str]) -> list[str]:
    """Strip + upper-case a batch, exactly as the platform bridge does.

    ⛔ Mirrors ``gut_scoring_dispatch_c80f4._normalize_sequences``, including the
    500-sequence bound, so the local and remote paths refuse the same inputs.
    """
    if isinstance(sequences, str):
        raw: Sequence[Any] = [sequences]
    elif isinstance(sequences, Sequence):
        raw = list(sequences)
    else:
        raise LigandAIValidationError(
            "sequences must be a string or a sequence of strings "
            f"(got {type(sequences).__name__})"
        )
    if not raw:
        raise LigandAIValidationError("sequences (non-empty) is required")
    if len(raw) > MAX_SEQUENCE_BATCH:
        raise LigandAIValidationError(
            f"maximum {MAX_SEQUENCE_BATCH} sequences per request (got {len(raw)})"
        )
    out: list[str] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, str):
            raise LigandAIValidationError(
                f"sequences[{idx}] is not a string (got {type(item).__name__})"
            )
        out.append(item.strip().upper())
    return out


def _normalize_construct(
    construct: Mapping[str, Any] | None,
    index: int,
) -> dict[str, Any] | None:
    """Validate one construct and normalize its chemistry to BOTH spellings.

    ⛔ THE SAME ONE NORMALIZER the peptide path uses. Declared chemistry is
    accepted flat (``{"cyclic_mode": "lactam", "n_methyl_count": 3}``) or nested
    (``{"cyclic_mode": "lactam", "declared": {"n_methyl_count": 3}}``); a nested
    value wins a conflict; the result carries both so neither the local resolver
    nor a route that reads only one spelling can silently drop it.
    """
    if construct is None:
        return None
    if not isinstance(construct, Mapping):
        raise LigandAIValidationError(
            f"constructs[{index}] must be an object (got "
            f"{type(construct).__name__})"
        )
    mode = construct.get("cyclic_mode")
    if mode is not None:
        if not isinstance(mode, str) or mode.strip().lower() not in VALID_CYCLIC_MODES:
            raise LigandAIValidationError(
                f"constructs[{index}].cyclic_mode must be one of "
                f"{sorted(VALID_CYCLIC_MODES)} (got {mode!r})"
            )
    modality = construct.get("modality")
    if modality is not None and not isinstance(modality, str):
        raise LigandAIValidationError(
            f"constructs[{index}].modality must be a string (got "
            f"{type(modality).__name__})"
        )

    try:
        chemistry = _chemistry_both_spellings(construct)
        # Raises on a non-integer count, with the same message the peptide path
        # gives — one validator, one error text.
        _coerce_mods(construct)
    except LigandAIValidationError as exc:
        raise LigandAIValidationError(f"constructs[{index}]: {exc.message}") from exc

    out: dict[str, Any] = {
        key: construct[key]
        for key in _CONSTRUCT_STRUCTURAL_KEYS
        if key in construct
    }
    out.update(chemistry)
    return out


def _constructs_for_batch(raw: Any, sequences: Sequence[str]) -> list[dict[str, Any] | None]:
    """Resolve the ``constructs`` side-channel onto the batch.

    One object means "the same construct for every sequence". A list is aligned
    by index. A mapping keyed by sequence is looked up per sequence.

    ⛔ ABSENT IS NOT LINEAR. A missing construct leaves cyclization UNKNOWN
    (``cyclized_state=None``), which the resolver reports as an OMITTED rescue
    term rather than an assertion that the peptide is linear.

    ⛔ AN AMBIGUOUS MAPPING IS REFUSED, NOT GUESSED. A ``{sequence: construct}``
    map whose keys do not match the batch (a lower-case key, a typo, a trimmed
    tag) would otherwise fall through to "one shared construct" and silently
    apply the wrong cyclicity to every sequence — and ``cyclized`` is worth
    +0.18, the largest rescue term. So a mapping that carries none of the
    construct field names AND is not keyed by the batch raises.
    """
    n = len(sequences)
    if raw is None:
        return [None] * n
    if isinstance(raw, (list, tuple)):
        if len(raw) != n:
            raise LigandAIValidationError(
                f"constructs has length {len(raw)} but there are {n} sequences; "
                f"align it by index, or send one object to share it"
            )
        return [dict(r) if isinstance(r, Mapping) else None for r in raw]
    if isinstance(raw, Mapping):
        keys = set(raw.keys())
        if keys and keys.issubset(set(sequences)):
            return [
                dict(raw[s]) if isinstance(raw.get(s), Mapping) else None
                for s in sequences
            ]
        if not any(k in raw for k in _CONSTRUCT_KEYS):
            raise LigandAIValidationError(
                f"constructs is neither a construct object (none of "
                f"{list(_CONSTRUCT_KEYS)} is present) nor keyed by the "
                f"sequences in this batch. Sequences are matched after "
                f"strip()+upper(), so a sequence-keyed map must use those exact "
                f"strings. Refusing rather than treating it as one shared "
                f"construct, which would apply the wrong cyclicity — worth "
                f"+0.18 — to every sequence. Unmatched keys: "
                f"{sorted(keys - set(sequences))[:5]}"
            )
        return [dict(raw)] * n
    raise LigandAIValidationError(
        "constructs must be an object, a list aligned to sequences, or a dict "
        f"keyed by sequence (got {type(raw).__name__})"
    )


def _resolved_constructs(
    constructs: Any,
    sequences: Sequence[str],
) -> list[dict[str, Any] | None]:
    """Per-sequence constructs, validated and chemistry-normalized."""
    return [
        _normalize_construct(con, idx)
        for idx, con in enumerate(_constructs_for_batch(constructs, sequences))
    ]


def _chameleonicity_body(sequences: str | Sequence[str], constructs: Any) -> dict[str, Any]:
    """The wire body.

    ⛔ ``constructs`` is sent as a LIST ALIGNED TO ``sequences``, whatever shape
    the caller used. The SDK has already resolved a shared object or a
    sequence-keyed map onto the batch and validated it; forwarding the ambiguous
    original would let the route resolve the alignment a second time, and two
    resolutions of one side-channel is how a construct ends up applied to the
    wrong sequence.
    """
    seqs = _normalize_wire_sequences(sequences)
    body: dict[str, Any] = {"sequences": seqs}
    if constructs is not None:
        # ⛔ An absent construct stays null, not {}. "No construct was supplied"
        # leaves cyclization UNKNOWN; an empty object is a supplied record that
        # happens to declare nothing. The resolver treats them alike today, but
        # collapsing them here would make the wire unable to say which it was.
        body["constructs"] = _resolved_constructs(constructs, seqs)
    return body


# --- protein records ------------------------------------------------------


def _coerce_top_n(top_n: Any) -> int:
    try:
        value = int(top_n)
    except (TypeError, ValueError) as exc:
        raise LigandAIValidationError(
            f"top_n must be an integer (got {top_n!r})"
        ) from exc
    return max(1, min(100, value))


def _format_for_suffix(path: pathlib.Path) -> str:
    fmt = path.suffix.lstrip(".").lower()
    if fmt not in STRUCTURE_FORMATS:
        raise LigandAIValidationError(
            f"cannot tell the structure format of {path.name!r}; pass "
            f"structure_format= explicitly, one of {sorted(STRUCTURE_FORMATS)}. "
            f"A compressed structure (.gz) must be decompressed first."
        )
    return fmt


def _read_structure_file(path_str: str, *, field: str) -> str:
    path = pathlib.Path(path_str).expanduser()
    if not path.is_file():
        raise LigandAIValidationError(f"{field} does not exist: {path}")
    raw = path.read_bytes()
    if len(raw) > MAX_STRUCTURE_BYTES:
        raise LigandAIValidationError(
            f"{field} is {len(raw)} bytes, over the {MAX_STRUCTURE_BYTES}-byte "
            f"limit"
        )
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LigandAIValidationError(
            f"{field} is not UTF-8 text ({exc}). A gzipped or binary structure "
            f"must be decompressed / converted to PDB or mmCIF first — the SDK "
            f"will not guess at the bytes."
        ) from exc


def _validated_structure_format(raw: Any, *, field: str) -> str:
    fmt = str(raw or "pdb").strip().lower().lstrip(".")
    if fmt not in STRUCTURE_FORMATS:
        raise LigandAIValidationError(
            f"unsupported {field} {fmt!r}; expected one of "
            f"{sorted(STRUCTURE_FORMATS)}"
        )
    return fmt


def _normalize_protein_records(records: Any) -> list[dict[str, Any]]:
    """Shape-check the ``records`` batch. Shared by the local and remote paths."""
    if isinstance(records, Mapping):
        items: list[Any] = [records]
    elif isinstance(records, (list, tuple)):
        items = list(records)
    else:
        raise LigandAIValidationError(
            "records must be an object or a non-empty list of objects "
            f"(got {type(records).__name__})"
        )
    if not items:
        raise LigandAIValidationError("records (non-empty) is required")
    if len(items) > MAX_PROTEIN_RECORDS:
        raise LigandAIValidationError(
            f"maximum {MAX_PROTEIN_RECORDS} records per request (got {len(items)})"
        )
    out: list[dict[str, Any]] = []
    for idx, rec in enumerate(items):
        if not isinstance(rec, Mapping):
            raise LigandAIValidationError(
                f"records[{idx}] is not an object (got {type(rec).__name__})"
            )
        seq = rec.get("sequence")
        if not isinstance(seq, str) or not seq.strip():
            raise LigandAIValidationError(
                f"records[{idx}].sequence is required and must be a non-empty "
                f"string"
            )
        out.append(dict(rec))
    return out


def _structure_frame(
    rec: Mapping[str, Any],
    idx: int,
    *,
    has_structure: bool,
) -> tuple[str | None, int | None]:
    """``chain_id`` and ``residue_offset``, both REQUIRED once a structure exists.

    ⛔ ``residue_offset`` maps sequence position 1 onto the structure's
    numbering. It is not defaulted when a structure is present: a construct
    expressed as UniProt 19-129 scored against a PDB numbered in UniProt
    coordinates with an assumed offset of 1 does not fail — it silently scores
    the wrong residues and returns a clean, plausible, wrong answer.
    """
    chain_id = rec.get("chain_id")
    raw_offset = rec.get("residue_offset")
    resolved_chain = (
        chain_id.strip() if isinstance(chain_id, str) and chain_id.strip() else None
    )
    if not has_structure and raw_offset is None:
        # Nothing to map onto, so nothing is required. The platform module's own
        # default of 1 is used for the glycosylation sequon numbering.
        return resolved_chain, None
    if has_structure and (not isinstance(chain_id, str) or not chain_id.strip()):
        raise LigandAIValidationError(
            f"records[{idx}].chain_id is required when a structure is supplied "
            f"— accessibility is computed on one chain"
        )
    if has_structure and raw_offset is None:
        raise LigandAIValidationError(
            f"records[{idx}].residue_offset is required when a structure is "
            f"supplied. It is THE STRUCTURE RESIDUE NUMBER OF SEQUENCE POSITION "
            f"1 — so a chain whose first modelled residue is 29, scored against "
            f"a sequence starting at that residue, takes residue_offset=29, NOT "
            f"28. An assumed offset does not raise; it silently scores the wrong "
            f"residues, and every site landing outside the modelled numbering "
            f"yields verdict=INDETERMINATE_FRAME_MISMATCH rather than a number."
        )
    try:
        offset = int(raw_offset) if raw_offset is not None else None
    except (TypeError, ValueError) as exc:
        raise LigandAIValidationError(
            f"records[{idx}].residue_offset must be an integer (got "
            f"{raw_offset!r})"
        ) from exc
    return resolved_chain, offset


def _protein_record_for_wire(rec: Mapping[str, Any], idx: int) -> dict[str, Any]:
    """One record, normalized for the HTTP route.

    ⛔ ``structure_path`` IS NEVER SENT. The route rejects it — a server-side
    path in a request body is an arbitrary-file-read primitive. A caller who
    passes a local path gets the FILE read here, client-side, and forwarded as
    ``structure_content``; the path itself does not leave the machine.
    """
    path = rec.get("structure_path")
    content = rec.get("structure_content")
    pdb_id = rec.get("pdb_id")

    supplied = [
        name
        for name, value in (
            ("structure_path", path),
            ("structure_content", content),
            ("pdb_id", pdb_id),
        )
        if isinstance(value, str) and value.strip()
    ]
    if len(supplied) > 1:
        raise LigandAIValidationError(
            f"records[{idx}] names more than one structure source ({supplied}); "
            f"pass exactly one of structure_path, structure_content or pdb_id"
        )

    out: dict[str, Any] = {"sequence": str(rec["sequence"]).strip()}
    label = rec.get("label")
    if label is not None:
        out["label"] = str(label)

    if supplied == ["structure_path"]:
        text = _read_structure_file(str(path), field=f"records[{idx}].structure_path")
        out["structure_content"] = text
        out["structure_format"] = (
            _validated_structure_format(
                rec["structure_format"], field=f"records[{idx}].structure_format"
            )
            if rec.get("structure_format")
            else _format_for_suffix(pathlib.Path(str(path)))
        )
        # ⛔ No extra key is added to say where the content came from: the wire
        # contract is frozen, and a field the route does not declare is at best
        # ignored and at worst a 400. The caller already knows what they passed.
    elif supplied == ["structure_content"]:
        text = str(content)
        if len(text.encode("utf-8", "ignore")) > MAX_STRUCTURE_BYTES:
            raise LigandAIValidationError(
                f"records[{idx}].structure_content exceeds the "
                f"{MAX_STRUCTURE_BYTES}-byte limit"
            )
        out["structure_content"] = text
        out["structure_format"] = _validated_structure_format(
            rec.get("structure_format"), field=f"records[{idx}].structure_format"
        )
    elif supplied == ["pdb_id"]:
        code = str(pdb_id).strip()
        if len(code) != _PDB_ID_LENGTH or not code.isalnum():
            raise LigandAIValidationError(
                f"records[{idx}].pdb_id must be a {_PDB_ID_LENGTH}-character "
                f"alphanumeric PDB code (got {pdb_id!r})"
            )
        out["pdb_id"] = code

    chain_id, offset = _structure_frame(rec, idx, has_structure=bool(supplied))
    if chain_id is not None:
        out["chain_id"] = chain_id
    if offset is not None:
        out["residue_offset"] = offset
    return out


def _protein_gut_body(records: Any, top_n: Any) -> dict[str, Any]:
    normalized = _normalize_protein_records(records)
    return {
        "records": [_protein_record_for_wire(r, i) for i, r in enumerate(normalized)],
        "top_n": _coerce_top_n(top_n),
    }


# --- local scorers --------------------------------------------------------


def _chameleonicity_batch_local(
    sequences: str | Sequence[str],
    constructs: Any = None,
    *,
    platform_module_path: str | Sequence[str] | None = None,
) -> dict[str, Any]:
    """In-process chameleonicity batch. Mirrors the verified platform bridge.

    ⛔ THE CYCLICITY CONTRACT. ``cyclic=`` is fed the THREE-VALUED
    ``cyclized_state`` from ``resolve_construct_modifications`` — True / False /
    None — and never a coerced bool. The scorer requires ``cyclic is True``, so
    False ("declared linear") and None ("never supplied") both land in
    ``applicable=False``, and the resolution record rides along so the caller can
    tell those two apart. ``n_methyl_count`` / ``d_residue_count`` come from the
    resolver's ``scorer_kwargs`` rather than being re-derived here, because the
    resolver is where the realised construct is known.
    """
    seqs = _normalize_wire_sequences(sequences)
    resolved_constructs = _resolved_constructs(constructs, seqs)

    cham = _import_platform_module(
        CHAMELEONICITY_MODULE,
        needed_by="gut_stability.chameleonicity_local()",
        platform_module_path=platform_module_path,
    )
    penalties = _import_platform_module(
        CONSTRUCT_RESOLVER_MODULE,
        needed_by="gut_stability.chameleonicity_local()",
        platform_module_path=platform_module_path,
    )

    resolutions: list[dict[str, Any]] = []
    # strict=: a length mismatch here would misalign a construct onto the
    # wrong sequence, which is a silent wrong answer rather than a crash.
    for seq, con in zip(seqs, resolved_constructs, strict=True):
        record = con or {}
        # ⛔ The resolver takes the NESTED spelling only. `_normalize_construct`
        # has already merged a flat spelling into it, so a caller who wrote
        # `n_methyl_count=` at the top level of the construct reaches the donor
        # arithmetic here instead of being silently dropped.
        declared = record.get("declared")
        resolutions.append(
            penalties.resolve_construct_modifications(
                seq,
                modality=record.get("modality"),
                cyclic_mode=record.get("cyclic_mode"),
                strict_recombinant=bool(record.get("strict_recombinant", True)),
                declared=dict(declared) if isinstance(declared, Mapping) else None,
                modality_transform_applied=bool(
                    record.get("modality_transform_applied", True)
                ),
            )
        )

    def _kwargs_for(res: Mapping[str, Any]) -> dict[str, Any]:
        scorer_kwargs = res.get("scorer_kwargs") or {}
        return {
            # ⛔ three-valued, NOT bool(...)
            "cyclic": res.get("cyclized_state"),
            "cyclization_route": res.get("cyclization_route"),
            "n_methyl_count": int(scorer_kwargs.get("n_methyl_count") or 0),
            "d_residue_count": int(scorer_kwargs.get("d_residue_count") or 0),
        }

    kwargs = [_kwargs_for(r) for r in resolutions]
    if all(k == kwargs[0] for k in kwargs):
        # score_batch IS this loop; driven here when one construct covers the
        # whole batch so the module's own batch entry point stays exercised.
        records = cham.score_batch(seqs, **kwargs[0])
    else:
        records = [
            cham.score_chameleonicity(s, **k)
            for s, k in zip(seqs, kwargs, strict=True)
        ]

    results: list[dict[str, Any]] = []
    for seq, rec, res in zip(seqs, records, resolutions, strict=True):
        out = dict(rec)
        out.setdefault("sequence", seq)
        # Provenance for the cyclicity that decided applicability.
        out["construct"] = {
            "cyclized_state": res.get("cyclized_state"),
            "cyclized_evidence": res.get("cyclized_evidence"),
            "cyclized_reason": res.get("cyclized_reason"),
            "cyclization_route": res.get("cyclization_route"),
            "cyclization_route_source": res.get("cyclization_route_source"),
            "modality": res.get("modality"),
            "cyclic_mode": res.get("cyclic_mode"),
            "provenance": res.get("provenance"),
            "rescue_terms_applied": res.get("rescue_terms_applied"),
            "rescue_terms_omitted": res.get("rescue_terms_omitted"),
        }
        out["computed_by"] = _LOCAL_PLATFORM_PROVENANCE
        results.append(out)

    applicable = sum(1 for r in results if r.get("applicable") is True)
    return {
        "results": results,
        # Read off the module, never retyped here.
        "declaration": dict(cham.DECLARATION),
        "computed_by": _LOCAL_PLATFORM_PROVENANCE,
        "platform_module": {
            "name": CHAMELEONICITY_MODULE,
            "file": getattr(cham, "__file__", None),
            "construct_resolver": CONSTRUCT_RESOLVER_MODULE,
        },
        "batch": {
            "n": len(results),
            "applicable": applicable,
            "not_applicable": len(results) - applicable,
            "not_applicable_note": (
                "a not-applicable record carries index=None, NOT 0.0. "
                "Chameleonicity is undefined on a linear construct, which is a "
                "different statement from a macrocycle that would permeate badly"
            ),
        },
    }


def _materialize_structure_local(
    rec: Mapping[str, Any],
    tmpdir: str,
    idx: int,
) -> tuple[str | None, str]:
    """A readable structure path for the LOCAL path, and where it came from.

    ⛔ ``structure_path`` is valid ONLY here. The HTTP route rejects it (a
    server-side path is an arbitrary-file-read primitive); on the local path the
    file is on the caller's own machine, so reading it is exactly what they
    asked for.

    ⛔ ``pdb_id`` is refused. Fetching one is network egress, and the local path
    is offline by contract.
    """
    if isinstance(rec.get("pdb_id"), str) and str(rec["pdb_id"]).strip():
        raise LigandAIValidationError(
            f"records[{idx}].pdb_id is not supported on the local path, which "
            f"does no network I/O by contract. Fetch the coordinates yourself "
            f"and pass structure_path / structure_content, or call the remote "
            f"protein_gut_stability(...) which lets the platform fetch it."
        )

    path = rec.get("structure_path")
    if isinstance(path, str) and path.strip():
        resolved = pathlib.Path(path.strip()).expanduser()
        if not resolved.is_file():
            raise LigandAIValidationError(
                f"records[{idx}].structure_path does not exist: {resolved}"
            )
        return str(resolved), "structure_path"

    content = rec.get("structure_content")
    if not isinstance(content, str) or not content.strip():
        return None, "none"
    if len(content.encode("utf-8", "ignore")) > MAX_STRUCTURE_BYTES:
        raise LigandAIValidationError(
            f"records[{idx}].structure_content exceeds the "
            f"{MAX_STRUCTURE_BYTES}-byte limit"
        )
    fmt = _validated_structure_format(
        rec.get("structure_format"), field=f"records[{idx}].structure_format"
    )
    out = os.path.join(tmpdir, f"structure_{idx}{_STRUCTURE_SUFFIX[fmt]}")
    with open(out, "w", encoding="utf-8") as handle:
        handle.write(content)
    return out, "structure_content"


def _protein_gut_batch_local(
    records: Any,
    *,
    top_n: int = 10,
    platform_module_path: str | Sequence[str] | None = None,
) -> dict[str, Any]:
    """In-process full-protein / biosimilar gut stability. Mirrors the bridge.

    ⛔ THREE DISTINCT INDETERMINATE VERDICTS, AND THEY ARE NOT INTERCHANGEABLE:

    - ``INDETERMINATE_NO_STRUCTURE`` — no structure was supplied, or it was
      unreadable. The sequence count is returned as the UNFOLDED bound.
    - ``INDETERMINATE_FRAME_MISMATCH`` — a structure WAS supplied but every
      motif site mapped outside the modelled numbering, i.e. ``residue_offset``
      is wrong or the sequence belongs to a different construct than the chain.
    - ``INDETERMINATE_NO_ACCESSIBILITY`` — a structure mapped but the intestinal
      panel still has no accessible-per-residue figure.

    In all three, ``accessible_*`` is ABSENT. This function does not fill any of
    them in, default them, or coerce them to zero, and it does not collapse them
    to one code — sequence motif counts alone cannot distinguish a stable folded
    protein from a labile one (mature FSH-beta reads 1.75 motif sites per residue
    from sequence and is a clinical protein), and "fully shielded from having
    measured nothing" is the most flattering wrong answer the arithmetic can
    produce.
    """
    normalized = _normalize_protein_records(records)
    resolved_top_n = _coerce_top_n(top_n)

    module = _import_platform_module(
        PROTEIN_GUT_STABILITY_MODULE,
        needed_by="gut_stability.protein_gut_stability_local()",
        platform_module_path=platform_module_path,
    )

    out: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="ligandai_proteingut_") as tmpdir:
        for idx, rec in enumerate(normalized):
            structure_path, source = _materialize_structure_local(rec, tmpdir, idx)
            chain_id, offset = _structure_frame(
                rec, idx, has_structure=bool(structure_path)
            )
            if structure_path is not None:
                # Explicit absence BEFORE scoring, never a silent zero.
                _require_gemmi("gut_stability.protein_gut_stability_local()")

            scored = module.score_protein_gut_stability(
                str(rec["sequence"]),
                structure_path=structure_path,
                chain_id=chain_id,
                residue_offset=offset if offset is not None else 1,
                label=str(rec.get("label") or ""),
            )
            if "error" not in scored:
                scored["biobetter_targets"] = module.biobetter_targets(
                    scored, top_n=resolved_top_n
                )
                # Echoed so a caller can never be unsure which frame was scored.
                scored["residue_offset_echo"] = offset if offset is not None else 1
                scored["structure_supplied"] = bool(structure_path)
                scored["structure_source"] = source
                scored["computed_by"] = _LOCAL_PLATFORM_PROVENANCE
            out.append(scored)

    # ⛔ Counted SEPARATELY, never summed into one "indeterminate" number. "You
    # gave me no structure" and "your residue_offset is wrong" need different
    # actions from the caller, and one counter would hide which it was.
    counts = {
        verdict: sum(1 for r in out if r.get("verdict") == verdict)
        for verdict in INDETERMINATE_VERDICTS
    }
    return {
        "results": out,
        "declaration": dict(module.DECLARATION),
        "computed_by": _LOCAL_PLATFORM_PROVENANCE,
        "platform_module": {
            "name": PROTEIN_GUT_STABILITY_MODULE,
            "file": getattr(module, "__file__", None),
        },
        "batch": {
            "n": len(out),
            "indeterminate_no_structure": counts["INDETERMINATE_NO_STRUCTURE"],
            "indeterminate_frame_mismatch": counts["INDETERMINATE_FRAME_MISMATCH"],
            "indeterminate_no_accessibility": counts[
                "INDETERMINATE_NO_ACCESSIBILITY"
            ],
            "indeterminate_total": sum(counts.values()),
            "indeterminate_note": (
                "accessible_* is ABSENT on these records, not zero. A "
                "folded-state claim requires folded-state evidence; supply a "
                "structure plus chain_id and the right residue_offset to obtain "
                "one. FRAME_MISMATCH specifically means the structure was "
                "readable but no motif site landed on a modelled residue — the "
                "signature of a wrong residue_offset, and the case that would "
                "otherwise read as 'fully shielded'"
            ),
        },
    }


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

        ⛔ THERE IS NO REFUSAL IN THE SUCCESS PATH. A deployment that mounts the
        route answers with the frozen envelope and this method returns its
        results. :class:`LigandAIParameterNotForwarded` is raised only for a
        route that is genuinely absent (404/405/501, or a 2xx body that is
        plainly not the scorer's) — and a 403 is the entitlement gate refusing,
        which is re-raised as itself rather than being rewritten into "use
        score_local()".

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
                ⛔ The same five keys are also accepted nested under
                ``declared``, identically to ``constructs`` on
                :meth:`chameleonicity` — one normalizer, so the SDK cannot
                develop two spellings for one input.

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
        platform_module_path: str | Sequence[str] | None = None,
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

        ⛔ A STRUCTURAL CONSTRUCT KEY NEEDS THE PLATFORM RESOLVER.
        Pre-resolved chemistry (``cyclized``, ``n_terminal_protected``,
        ``c_terminal_protected``, ``d_residue_count``, ``n_methyl_count``) scores
        fully offline. A STRUCTURAL key — ``cyclic_mode``, ``modality``,
        ``strict_recombinant``, ``modality_transform_applied`` — describes how the
        construct was MADE, and turning that into ``cyclized`` means deciding
        whether the realised peptide carries the bond (the terminal-Cys pair, the
        interior-Cys rule under recombinant expression, the terminal-Pro check).
        That logic lives with the generator, so it is imported from
        ``gut_stability_penalties`` and never reimplemented here. Supply one
        without the platform modules and this raises
        :class:`LigandAIPlatformModuleUnavailable` rather than scoring the peptide
        as unmodified — ``cyclized`` is worth +0.18, the largest rescue term, and
        ignoring it silently understates every macrocycle.

        When a structural key IS resolved, the record carries
        ``modification_resolution`` with the three-valued ``cyclized_state``
        (True / False / None) and ``rescue_terms_omitted``, so "cyclization
        unknown" stays distinguishable from "declared linear" — both score the
        term at 0.0 and only that record says which happened.

        Args:
            sequences: One peptide sequence or a list of them.
            modifications: As in :meth:`score`.
            include_sequence_in_record: Put the raw sequence into the sidecar's
                ``candidate_ref``. Off by default — the sidecar is normally
                joined on ``sequence_hash``.
            platform_module_path: Where to find the platform modules, needed only
                when a structural construct key is supplied. Defaults to
                ``LIGANDAI_PLATFORM_MODULE_PATH``.

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
            platform_module_path=platform_module_path,
        )

    # -- chameleonicity (macrocycles only) --------------------------------

    def chameleonicity(
        self,
        sequences: str | Sequence[str],
        *,
        constructs: Mapping[str, Any] | Sequence[Mapping[str, Any] | None] | None = None,
    ) -> dict[str, Any]:
        """``POST /api/scoring/chameleonicity`` — conformational amphipathicity.

        A cyclic peptide that is orally absorbed is not simply small and greasy:
        it is conformationally amphipathic, burying its backbone amide NH donors
        in transannular hydrogen bonds inside a membrane and re-exposing them in
        water. This index counts those donors, credits the chemistry that removes
        or shields them, and scores ring geometry against the sizes where
        transannular networks are possible.

        ⛔ IT APPLIES ONLY TO MACROCYCLES. On a linear construct the result is
        ``{"applicable": False, "index": None, "reason":
        "not_a_cyclic_construct"}``. ``index`` is **None, never 0.0** — a linear
        peptide has no ring to close a network across and no collapsed state to
        switch into, so the quantity is UNDEFINED rather than low. Code that
        reads ``rec["index"] or 0`` turns "the question does not apply" into "a
        bad chameleon"; read ``applicable`` first.

        ⛔ IT IS NOT A MEASURED PERMEABILITY. No PAMPA / Caco-2 / RRCK / MDCK
        value is involved, nothing was fitted to assay data, and it is not a
        3D calculation — true chameleonicity is the 3D-PSA difference across a
        conformer ensemble in two solvents. The declaration block on every
        response says all of this; forward it with the number.

        Args:
            sequences: One peptide sequence or a list of them (single-letter).
            constructs: The REALISED construct, which is where cyclicity comes
                from — not a request flag. Either one object shared by the whole
                batch, a list aligned to ``sequences``, or a dict keyed by
                sequence (matched after ``strip()`` + ``upper()``). Fields:
                ``modality``, ``cyclic_mode`` (one of
                ``none|disulfide|head_tail_contact|lactam``),
                ``strict_recombinant`` (bool, default ``True``),
                ``modality_transform_applied`` (bool, default ``True``), and the
                declared construct chemistry — ``cyclized``,
                ``n_terminal_protected``, ``c_terminal_protected``,
                ``d_residue_count``, ``n_methyl_count``.

                ⛔ The chemistry is accepted FLAT (the documented SDK spelling,
                matching ``modifications`` on :meth:`score`) or nested under
                ``declared``; a nested value wins a conflict and both are sent.
                One normalizer handles both, because the two platform ops once
                wanted opposite shapes and each ignored the other SILENTLY — same
                payload, different index, no error.

                Omitting ``constructs`` entirely leaves cyclization UNKNOWN,
                which is not the same as declaring it linear.

        Returns:
            ``{results: [...], declaration: {...}, batch: {...}}``, plus
            ``success`` / ``count`` from the route.

        Raises:
            LigandAIParameterNotForwarded: when this deployment does not mount
                the route. Names :meth:`chameleonicity_local`.
            LigandAIValidationError: on a malformed argument, before any request.
        """
        body = _chameleonicity_body(sequences, constructs)
        try:
            payload = self._transport.request(
                "POST", CHAMELEONICITY_SCORING_PATH, json=body
            )
        except LigandAIError as exc:
            if _is_route_absent(exc):
                raise _chameleonicity_not_mounted() from exc
            raise
        if not _looks_like_scorer_payload(payload):
            raise _chameleonicity_not_mounted()
        return dict(payload)

    def chameleonicity_local(
        self,
        sequences: str | Sequence[str],
        *,
        constructs: Mapping[str, Any] | Sequence[Mapping[str, Any] | None] | None = None,
        platform_module_path: str | Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """:meth:`chameleonicity` in-process — no HTTP, no API key, no credits.

        Same arguments and the same record shape, plus ``computed_by`` and a
        ``platform_module`` provenance block so a local record can never be
        mistaken for a platform-computed one.

        ⛔ NEEDS THE PLATFORM MODULES. Unlike :meth:`score_local` — which carries
        a pinned in-SDK port of the GI rule table — this path imports the
        platform's ``chameleonicity`` and ``gut_stability_penalties`` modules.
        They ship with the platform, NOT with the ``ligandai`` wheel, so a
        pip-installed client does not have them and this call raises
        :class:`LigandAIPlatformModuleUnavailable` (an ``ImportError`` subclass)
        rather than returning a degraded number. Point the SDK at a platform
        checkout with ``platform_module_path=`` or the
        ``LIGANDAI_PLATFORM_MODULE_PATH`` environment variable, or use
        :meth:`chameleonicity`.

        Args:
            sequences: As in :meth:`chameleonicity`.
            constructs: As in :meth:`chameleonicity`.
            platform_module_path: Directory (or directories) holding the platform
                modules, prepended to ``sys.path`` for the duration of the
                import and removed again afterwards.

        Raises:
            LigandAIPlatformModuleUnavailable: the platform modules are absent.
            LigandAIValidationError: on a malformed argument.
        """
        return _chameleonicity_batch_local(
            sequences,
            constructs,
            platform_module_path=platform_module_path,
        )

    # -- full protein / biosimilar-biobetter ------------------------------

    def protein_gut_stability(
        self,
        records: Mapping[str, Any] | Sequence[Mapping[str, Any]],
        *,
        top_n: int = 10,
    ) -> dict[str, Any]:
        """``POST /api/scoring/protein-gut-stability`` — folded-state exposure.

        The peptide module bins by length and puts everything over 30 aa in
        ``formulation_or_gi_exposure``, which for a 92-145 aa glycoprotein
        hormone is a dead end rather than an assessment. This scorer weights
        every cleavage motif by its STRUCTURAL ACCESSIBILITY — a K or R buried in
        a folded core cannot be presented to a protease — credits real SG-SG
        disulfide bridges and N-glycosylation sequons, and reports gastric and
        intestinal panels separately because the pH and the protease panels
        differ.

        ⛔ WITHOUT A STRUCTURE THE VERDICT IS ``"INDETERMINATE_NO_STRUCTURE"``
        AND THE ``accessible_*`` FIELDS ARE ABSENT, NOT ZERO. Sequence motif
        counts alone cannot tell a stable folded protein from a labile one:
        mature FSH-beta reads 1.75 motif sites per residue from sequence and is
        a clinically administered protein. The sequence count is returned as the
        UNFOLDED bound, which is the right number for a protein that denatures
        at gastric pH.

        ⛔ ``residue_offset`` IS THE STRUCTURE RESIDUE NUMBER OF SEQUENCE
        POSITION 1, and is REQUIRED whenever a structure is supplied. A chain
        whose first modelled residue is 29, scored against a sequence starting
        at that residue, takes ``residue_offset=29`` — **not 28**. Site positions
        are 0-based internally and map as ``pos_struct = position +
        residue_offset``. This is the single easiest thing to get wrong and it
        FAILS SILENTLY: a wrong offset does not raise, every site simply lands on
        an unmodelled residue number. It is echoed back on every record as
        ``residue_offset_echo`` so the scored frame is never in doubt.

        ⛔ THREE DISTINCT INDETERMINATE VERDICTS — match the exact string, never
        an ``INDETERMINATE_`` prefix:

        - ``INDETERMINATE_NO_STRUCTURE`` — none supplied, or unreadable.
        - ``INDETERMINATE_FRAME_MISMATCH`` — a structure was read, but every
          motif site mapped outside the modelled numbering. Wrong
          ``residue_offset``, or a sequence from a different construct than the
          chain. ⛔ This is the case that used to read as ``accessible_sites=0``,
          ``shielded_fraction=1.0`` and ``low_accessible_proteolysis`` — the most
          FLATTERING verdict the module emits — from having measured nothing.
        - ``INDETERMINATE_NO_ACCESSIBILITY`` — mapped, but the intestinal panel
          produced no per-residue figure.

        Each panel also reports ``sites_mapped_to_structure``,
        ``sites_unmapped``, ``mapped_fraction``, ``shielded_fraction_basis`` and,
        under 50% coverage, a ``frame_warning``. ``shielded_fraction`` is computed
        over MAPPED sites only — dividing by the sequence count would credit
        unmapped sites as shielded and drift optimistic as coverage falls. All of
        these are forwarded untouched; none is coalesced to 0.

        ⛔ ORAL DELIVERY OF A FULL GLYCOPROTEIN IS A FORMULATION PROBLEM. Nothing
        here implies a disulfide-rich glycoprotein can be dosed orally on
        sequence merit. The supported uses are ranking biosimilar/biobetter
        variants against each other and picking which exposed sites to engineer
        out — and ``biobetter_targets`` deliberately does NOT propose the
        substitution.

        Args:
            records: One record or a list of up to 25. Each needs ``sequence``;
                optional ``label``. A structure is supplied as EXACTLY ONE of:

                - ``structure_content`` (the file's text) plus
                  ``structure_format`` (``pdb|ent|cif|mmcif|mcif|pdbx``),
                - ``structure_path`` — a path on YOUR machine. ⛔ The path is
                  never sent: the file is read here, client-side, and forwarded
                  as ``structure_content``, because a server-side path in a
                  request body is an arbitrary-file-read primitive and the route
                  rejects it.
                - ``pdb_id`` — a 4-character code the platform fetches.

                With a structure, ``chain_id`` and ``residue_offset`` are both
                required — see the offset convention above.
            top_n: How many biobetter engineering targets to return per record,
                clamped to ``1``–``100``.

        Returns:
            ``{results: [...], declaration: {...}, batch: {...}}``, plus
            ``success`` / ``count`` from the route.

        Raises:
            LigandAIParameterNotForwarded: when this deployment does not mount
                the route. Names :meth:`protein_gut_stability_local`.
            LigandAIValidationError: on a malformed record, a missing
                ``chain_id`` / ``residue_offset``, an unreadable
                ``structure_path``, or more than one structure source — all
                before any request is sent.
        """
        body = _protein_gut_body(records, top_n)
        try:
            payload = self._transport.request(
                "POST", PROTEIN_GUT_STABILITY_SCORING_PATH, json=body
            )
        except LigandAIError as exc:
            if _is_route_absent(exc):
                raise _protein_gut_not_mounted() from exc
            raise
        if not _looks_like_scorer_payload(payload):
            raise _protein_gut_not_mounted()
        return dict(payload)

    def protein_gut_stability_local(
        self,
        records: Mapping[str, Any] | Sequence[Mapping[str, Any]],
        *,
        top_n: int = 10,
        platform_module_path: str | Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """:meth:`protein_gut_stability` in-process — no HTTP, no API key.

        ⛔ ``structure_path`` IS VALID HERE and only here: the file is on your own
        machine, so reading it is exactly what you asked for. ``pdb_id`` is
        REFUSED — fetching one is network egress and this path is offline by
        contract; fetch the coordinates yourself, or use
        :meth:`protein_gut_stability`.

        ⛔ NEEDS THE PLATFORM MODULE AND gemmi. ``protein_gut_stability`` ships
        with the platform, not with the ``ligandai`` wheel, and reading
        coordinates needs ``gemmi``. Either being absent raises
        :class:`LigandAIPlatformModuleUnavailable`. gemmi is checked BEFORE
        scoring when a structure was supplied, because the platform module
        catches its own import failure and falls through to
        ``INDETERMINATE_NO_STRUCTURE`` — correct for "no structure was given",
        but a caller who DID give one would read it as their file being ignored.

        ⛔ Same ``residue_offset`` convention and the same three indeterminate
        verdicts as :meth:`protein_gut_stability` — the offset is the STRUCTURE
        RESIDUE NUMBER OF SEQUENCE POSITION 1 (first modelled residue 29 ⇒ 29,
        not 28), and ``INDETERMINATE_FRAME_MISMATCH`` is what a wrong one
        produces instead of a flattering "fully shielded" number. The batch block
        counts the three verdicts separately.

        Args:
            records: As in :meth:`protein_gut_stability`, except that
                ``structure_path`` is accepted and ``pdb_id`` is not.
            top_n: As in :meth:`protein_gut_stability`.
            platform_module_path: Directory (or directories) holding the platform
                modules.

        Raises:
            LigandAIPlatformModuleUnavailable: the platform module or gemmi is
                absent.
            LigandAIValidationError: on a malformed record, or a ``pdb_id``.
        """
        return _protein_gut_batch_local(
            records,
            top_n=top_n,
            platform_module_path=platform_module_path,
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
    chameleonicity_local = GutStability.chameleonicity_local
    protein_gut_stability_local = GutStability.protein_gut_stability_local
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

    async def chameleonicity(
        self,
        sequences: str | Sequence[str],
        *,
        constructs: Mapping[str, Any] | Sequence[Mapping[str, Any] | None] | None = None,
    ) -> dict[str, Any]:
        """Async variant of :meth:`GutStability.chameleonicity`.

        ⛔ Same applicability contract: a linear construct returns
        ``applicable=False`` with ``index=None``, never ``0.0``.
        """
        body = _chameleonicity_body(sequences, constructs)
        try:
            payload = await self._transport.request(
                "POST", CHAMELEONICITY_SCORING_PATH, json=body
            )
        except LigandAIError as exc:
            if _is_route_absent(exc):
                raise _chameleonicity_not_mounted() from exc
            raise
        if not _looks_like_scorer_payload(payload):
            raise _chameleonicity_not_mounted()
        return dict(payload)

    async def protein_gut_stability(
        self,
        records: Mapping[str, Any] | Sequence[Mapping[str, Any]],
        *,
        top_n: int = 10,
    ) -> dict[str, Any]:
        """Async variant of :meth:`GutStability.protein_gut_stability`.

        ⛔ Same frame contract: ``residue_offset`` is required whenever a
        structure is supplied, ``structure_path`` is read client-side and never
        sent, and no structure means ``INDETERMINATE_NO_STRUCTURE`` with
        ``accessible_*`` absent.
        """
        body = _protein_gut_body(records, top_n)
        try:
            payload = await self._transport.request(
                "POST", PROTEIN_GUT_STABILITY_SCORING_PATH, json=body
            )
        except LigandAIError as exc:
            if _is_route_absent(exc):
                raise _protein_gut_not_mounted() from exc
            raise
        if not _looks_like_scorer_payload(payload):
            raise _protein_gut_not_mounted()
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
