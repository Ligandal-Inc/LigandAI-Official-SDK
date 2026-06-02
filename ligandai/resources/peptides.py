# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Peptide generation, folding, and scoring.

Public methods that submit GPU work return :class:`Job` (or :class:`AsyncJob`)
instances. Use ``.wait()`` to block until completion.

Endpoint mapping (server source-of-truth):

- :meth:`Peptides.generate`               → ``POST /api/ptf/parallel/generate``
- :meth:`Peptides.fold`                   → ``POST /api/folding/predict``
- :meth:`Peptides.fold_batch`             → ``POST /api/v1/folding/predict-batch``
- :meth:`Peptides.fold_custom_mutation`   → ``POST /api/ptf/fold-custom-mutation`` (or boltz2/modified-fold)
- :meth:`Peptides.continue_folding`       → ``POST /api/ptf/parallel/{sid}/continue``
- :meth:`Peptides.score_complex`          → ``POST /api/binder-scoring/fold-and-score``
- :meth:`Peptides.score_pdb`              → ``POST /api/v1/deltaforge/score-pdb``
- :meth:`Peptides.score_with_ligandiq`    → ``POST /api/ptf/parallel/{sid}/ligandiq-score``
- :meth:`Peptides.analyze_solubility`     → ``POST /api/peptide-features/solubility``
- :meth:`Peptides.search`                 → ``GET  /api/ptf/genes/summary`` + filter
- :meth:`Peptides.search_by_pocket`       → ``GET  /api/ptf/peptides/by-pocket``
- :meth:`Peptides.get_elite`              → ``GET  /api/ptf/parallel/{sid}/elite``
- :meth:`Peptides.by_gene`                → ``GET  /api/v1/peptides/by-gene``  (paid-only, v0.2.0+)
- :meth:`Peptides.list`                   → ``GET  /api/ptf/generated-peptides/by-gene/:gene``  (v0.2.0+)
- :meth:`Peptides.get`                    → ``GET  /api/v1/peptides/:id``       (paid-only, v0.2.0+)
"""

from __future__ import annotations

import warnings
from datetime import datetime
from pathlib import Path
from collections.abc import AsyncIterator, Iterator
from typing import Any, Callable, Literal

from ligandai.errors import LigandAICreditError, LigandAIError
from ligandai.jobs import AsyncJob, Job
from ligandai.resources._base import AsyncResource, Resource
from ligandai.types import (
    CostEstimate,
    DeltaForgeScore,
    EcTrimmingConfig,
    FoldResult,
    GenerationResult,
    GeneSummary,
    LigandIQScore,
    PdcConfig,
    Peptide,
    PeptideDetail,
    PeptideInput,
    ResidueRange,
    SegmentConfig,
    Sequence,
    SolubilityResult,
)

# Allowed values for ``Peptides.get(include=[...])``. The server validates this
# against an allowlist and returns 400 for unknown entries — keep this in sync.
_IncludeField = Literal["pocket_features", "interface", "pdb"]
_ALLOWED_INCLUDE: frozenset[str] = frozenset({"pocket_features", "interface", "pdb"})
_DeltaForgeScorer = Literal["auto", "current", "v10", "v10_2", "unified"]
_DeltaForgeAggregateMethod = Literal["boltzmann_parallel", "best_pair", "mean_pair"]

# Cysteine-control keys that used to be passed via ``extra={...}`` and are now
# first-class typed kwargs on :meth:`Peptides.generate`. We continue to accept
# them via ``extra`` for backward compatibility but emit a DeprecationWarning;
# they will be hard-rejected in v0.3.0.
_DEPRECATED_EXTRA_CYS_KEYS: frozenset[str] = frozenset({
    "cys_mode",
    "cysteine_mode",
    "cysteineMode",
    "cys_gate",
    "cysteine_gate",
    "cyclic_mode",
    "cyclicMode",
    "cyclic_strength",
    "cyclicStrength",
    "strict_recombinant",
    "strictRecombinant",
    "dual_fold_viz",
    "dualFoldViz",
    "disulfide_constraints",
    "disulfideConstraints",
})

_LOGITS_EXTRA_KEYS: frozenset[str] = frozenset({
    "return_logits",
    "returnLogits",
    "output_logits",
    "outputLogits",
    "include_logits",
    "includeLogits",
    "logits",
})


def _warn_deprecated_cys_extra(extra: dict[str, Any] | None) -> None:
    """Emit DeprecationWarning when cys-related keys arrive via ``extra=``.

    The typed kwargs (``cysteine_mode``, ``cyclic_mode``, etc.) are the
    blessed surface as of v0.2.0. The ``extra`` path still works for
    backward compatibility, but will be hard-rejected in v0.3.0.
    """
    if not extra:
        return
    leaked = sorted(k for k in extra if k in _DEPRECATED_EXTRA_CYS_KEYS)
    if not leaked:
        return
    keys = ", ".join(leaked)
    warnings.warn(
        (
            f"Passing cysteine/cyclic controls via extra={{}} is deprecated as of "
            f"ligandai v0.2.0 (got: {keys}). Pass them as typed kwargs on "
            f"Peptides.generate() — e.g. cysteine_mode=, cyclic_mode=, "
            f"strict_recombinant=, ... — instead. The extra-dict path will be "
            f"removed in v0.3.0."
        ),
        DeprecationWarning,
        stacklevel=3,
    )


def _requests_advanced_guidance(
    *,
    immunogenicity: bool,
    immuno_modules: dict[str, bool] | None,
    serum_stability: bool,
    stability_modules: dict[str, bool] | None,
    halflife: _HalflifeTarget | None,
    cyclic_mode: _CyclicMode | None,
    extra: dict[str, Any] | None,
) -> bool:
    """Return True for guidance outputs gated to academia/pro/enterprise."""
    if (
        immunogenicity
        or immuno_modules is not None
        or serum_stability
        or stability_modules is not None
        or halflife is not None
        or (cyclic_mode is not None and cyclic_mode != "none")
    ):
        return True
    return bool(extra and any(bool(extra.get(key)) for key in _LOGITS_EXTRA_KEYS))


def _parse_deltaforge_score(data: dict[str, Any]) -> DeltaForgeScore:
    scoring = data.get("scoring") or data.get("deltaforge") or data

    def pick(*keys: str) -> Any:
        for key in keys:
            if key in scoring and scoring[key] is not None:
                return scoring[key]
        return None

    return DeltaForgeScore.model_validate(
        {
            "dg": pick("dg", "delta_g", "deltaG"),
            "kd": pick("kd", "kd_nm", "kdNm"),
            "kd_nm": pick("kd_nm", "kdNm"),
            "contacts": pick("contacts", "contact_count", "num_contacts"),
            "interfaceResidues": pick("interface_residues", "interfaceResidues"),
            "scorer": pick("scorer"),
            "scorer_version": pick("scorer_version", "scorerVersion"),
            "model_sha256": pick("model_sha256", "modelSha256"),
            "feature_schema_version": pick("feature_schema_version", "featureSchemaVersion"),
            "aggregate_method": pick("aggregate_method", "aggregateMethod"),
            "version_family": pick("version_family", "versionFamily"),
            "affinity_scorer": pick("affinity_scorer", "affinityScorer"),
            "affinity_scorer_version": pick("affinity_scorer_version", "affinityScorerVersion"),
            "calibration_head": pick("calibration_head", "calibrationHead"),
            "structure_source_detected": pick("structure_source_detected", "structureSourceDetected"),
            "calibration_router": pick("calibration_router", "calibrationRouter"),
            "peptide_length": pick("peptide_length", "peptideLength"),
            "platform_length_scope": pick("platform_length_scope", "platformLengthScope"),
            "predicted_affinity_tier": pick("predicted_affinity_tier", "predictedAffinityTier"),
            "predicted_binder": pick("predicted_binder", "predictedBinder"),
            "predicted_binder_call": pick("predicted_binder_call", "predictedBinderCall"),
            "predicted_binder_label": pick("predicted_binder_label", "predictedBinderLabel"),
            "predicted_binder_probability": pick(
                "predicted_binder_probability", "predictedBinderProbability"
            ),
            "binder_call_method": pick("binder_call_method", "binderCallMethod"),
            "predicted_non_binder_reasons": pick(
                "predicted_non_binder_reasons", "predictedNonBinderReasons"
            ),
            "missing_binder_gate_inputs": pick(
                "missing_binder_gate_inputs", "missingBinderGateInputs"
            ),
            "readout_note": pick("readout_note", "readoutNote"),
            "affinity_plus_structure_readout": pick(
                "affinity_plus_structure_readout", "affinityPlusStructureReadout"
            ),
            "dual_readout": pick("dual_readout", "dualReadout"),
            "structural_energy_gates": pick("structural_energy_gates", "structuralEnergyGates"),
            "best_pair": pick("best_pair", "bestPair"),
            "pair_scores": pick("pair_scores", "pairScores"),
            "pair_errors": pick("pair_errors", "pairErrors"),
            "warnings": pick("warnings"),
            "metadata": scoring.get("metadata") or scoring,
            # fold confidence + PAE passthrough
            "iptm": pick("iptm", "iPTM"),
            "ptm": pick("ptm", "pTM"),
            "ipsae": pick("ipsae", "iPSAE"),
            "plddt_mean": pick("plddt_mean", "plddtMean", "mean_plddt"),
            "foldJobId": pick("foldJobId", "fold_job_id"),
            "pae": pick("pae"),
            "paeStatus": pick("pae_status", "paeStatus"),
        }
    )

_TargetingStrategy = Literal["full_surface", "pocket_targeted"]

# Cyclic peptide mode. Controls which cyclization constraint is applied during
# generation (recombinant-only scope — Adaptyv synthesis path).
#
# - ``"none"`` — linear peptide, no cyclic constraint (default).
# - ``"lactam"`` — head-to-tail amide closure; PREDICTION/VIZ layer only.
#   The synthesis order goes out as the disulfide (Cys-Cys) construct when
#   the user accepts a lactam-designed peptide via Adaptyv.
# - ``"disulfide"`` — terminal Cys-Cys bridge. PRIMARY recombinant-shippable
#   mode. When ``strict_recombinant=True`` (default), no internal Cys allowed.
# - ``"head_tail_contact"`` — soft B-matrix bias toward terminal-pair-favorable
#   compositions; no synthesis constraint added.
#
# Tier-gated: advanced immunogenicity/stability/logits guidance remains
# academia/pro/enterprise only. Quality-guided base generation is available to
# all authenticated tiers, including free, subject to credits and limits.
_CyclicMode = Literal["none", "lactam", "disulfide", "head_tail_contact"]

# Charge filtering mode applied by the filtered design worker.
# - ``"off"`` — no charge filter (default behavior when chargeMode='off').
# - ``"lt"`` — keep peptides with net charge < chargeValue.
# - ``"gt"`` — keep peptides with net charge > chargeValue.
# - ``"between"`` — keep peptides with chargeMin ≤ net charge ≤ chargeMax.
_ChargeMode = Literal["off", "lt", "gt", "between"]

# Cysteine placement policy applied during peptide generation.
#
# - ``"allow_all"`` / ``"allow"`` — no filtering; the model is free to place
#   cysteines anywhere. Use this when you want unconstrained generation, e.g.
#   when targeting a covalent binder against a target Cys.
# - ``"disulfide_only"`` / ``"stability_only"`` (default) — only keep peptides
#   with 0 cysteines OR pairs whose positions form a plausible disulfide
#   geometry (|i-j| in {3,4} or >=6).
# - ``"exclude_all"`` / ``"exclude"`` — reject any peptide containing cysteine.
#
# Server-side this is enforced via rejection sampling with backpressure refill,
# so requesting ``num_peptides=N`` returns exactly N peptides regardless of mode.
_CysteineMode = Literal[
    "allow_all",
    "allow",
    "disulfide_only",
    "stability_only",
    "exclude_all",
    "exclude",
]

# Half-life guidance target. ``"extended"`` biases the model toward sequences
# with longer plasma half-life; ``"rapid"`` biases toward fast-clearing peptides
# (e.g. for dosing flexibility); ``"moderate"`` is the middle ground.
_HalflifeTarget = Literal["extended", "rapid", "moderate"]

# Proteolytic stability guidance mode. ``"resist"`` pushes the model away from
# protease-cleavable motifs; ``"target"`` does the inverse (deliberately
# cleavable, used for prodrugs / pro-peptides).
_StabilityMode = Literal["resist", "target"]

# Fold partner expansion mode. Controls which receptor chains are included in
# the peptide co-fold complex.
#
#   "target_only"     — fold peptide + ONLY the chain(s) listed in target_chains.
#                       Smallest, fastest fold; clearest single-target interface.
#   "native_complex"  — fold peptide + target chain(s) + their known native
#                       interaction partners from the input structure (e.g.,
#                       BMPR1A + RGMB). Lets the user compare the peptide's
#                       inhibitory effect against the native partner interface.
#   "all_conformations" — fold against every conformation/chain set the platform
#                       has on file for the gene (apo, bound, alternate states).
#                       Most expensive; useful for ensemble validation.
_FoldPartnerMode = Literal["target_only", "native_complex", "all_conformations"]


def _generation_target(
    gene: str,
    target_residues: list[ResidueRange] | None = None,
    targeting_strategy: _TargetingStrategy = "full_surface",
    variant_id: int | None = None,
) -> dict[str, Any]:
    """Build a single PTF target spec for the parallel generate endpoint."""
    target: dict[str, Any] = {"gene": gene, "targetingStrategy": targeting_strategy}
    if target_residues is not None:
        target["targetResidues"] = [
            r.model_dump(by_alias=True) if isinstance(r, ResidueRange) else r
            for r in target_residues
        ]
    if variant_id is not None:
        target["variantId"] = variant_id
    return target


def _generation_body(
    *,
    gene: str,
    num_peptides: int | None,
    length_range: tuple[int, int],
    target_residues: list[ResidueRange] | None,
    target_chains: list[str] | None,
    fold_partners: _FoldPartnerMode | list[str] | None,
    targeting_strategy: _TargetingStrategy | None,
    pocket_expansion_radius_a: float | None,
    auto_fold: bool,
    top_n_fold: int | None,
    ec_domain_trimming: bool,
    deimmunize_mode: bool,
    variant_id: int | None,
    gen_gpus: int,
    fold_gpus: int,
    program_id: int | None,
    cysteine_mode: _CysteineMode,
    quality_guided: bool,
    quality_guidance_scale: float,
    immunogenicity: bool,
    immuno_strength: float,
    immuno_modules: dict[str, bool] | None,
    serum_stability: bool,
    stability_strength: float,
    stability_mode: _StabilityMode,
    stability_modules: dict[str, bool] | None,
    halflife: _HalflifeTarget | None,
    halflife_strength: float,
    # Charge / solubility filtering (tier-gated; server activates filtered
    # design worker when any non-default constraint is present).
    charge_mode: _ChargeMode | None,
    charge_value: float | None,
    charge_min: float | None,
    charge_max: float | None,
    min_solubility: float | None,
    # Cyclization (tier-gated: academia/pro/enterprise only).
    cyclic_mode: _CyclicMode | None,
    cyclic_strength: float,
    strict_recombinant: bool,
    dual_fold_viz: bool,
    folding_mode: str | None,
    fold_strategy: str | None,
    folding_conformations: str | list[str] | None,
    max_folds_per_target: int | None,
    enable_expansion: bool | None,
    auto_conformation_expansion: bool | None,
    clash_resolution_enabled: bool | None,
    md_relaxation_enabled: bool | None,
    num_trajectories: int | None,
    sampling_steps: int | None,
    glycosylation_enabled: bool | None,
    segment_config: SegmentConfig | dict | None,
    pdc_config: PdcConfig | dict | None,
    ec_trimming_config: EcTrimmingConfig | dict | None,
    extra: dict[str, Any] | None,
) -> dict[str, Any]:
    # Auto-resolve targeting_strategy when not explicitly set:
    #   * target_residues present → "pocket_targeted" (treat residues as a real pocket)
    #   * otherwise → "full_surface"
    # This prevents the silent "I gave you a hotspot but you used full_surface
    # and ignored it" footgun that pre-2026-05-07 jobs hit.
    effective_strategy: _TargetingStrategy = (
        targeting_strategy
        if targeting_strategy is not None
        else ("pocket_targeted" if target_residues else "full_surface")
    )

    body: dict[str, Any] = {
        "targets": [_generation_target(gene, target_residues, effective_strategy, variant_id)],
        "lengthRange": list(length_range),
        "autoFoldEnabled": auto_fold,
        "ecDomainTrimming": ec_domain_trimming,
        "deimmunizeMode": deimmunize_mode,
        "genParallelCount": gen_gpus,
        "foldingGpus": fold_gpus,
        "cysteineMode": cysteine_mode,
        # Guidance modules. Quality-guided generation is free+; immunogenicity,
        # serum stability, and logits-style outputs are academia+.
        "qualityGuidedEnabled": quality_guided,
        "qualityGuidanceScale": quality_guidance_scale,
        "immunoEnabled": immunogenicity,
        "immunoStrength": immuno_strength,
        "stabilityEnabled": serum_stability,
        "stabilityStrength": stability_strength,
        "stabilityMode": stability_mode,
        "halflifeEnabled": halflife is not None,
    }
    if halflife is not None:
        body["halflifeTarget"] = halflife
        body["halflifeStrength"] = halflife_strength
    if num_peptides is not None:
        body["peptidesPerTarget"] = num_peptides
    if top_n_fold is not None:
        body["maxFoldsPerTarget"] = top_n_fold
    if max_folds_per_target is not None:
        body["maxFoldsPerTarget"] = max_folds_per_target
    if folding_mode is not None:
        body["foldingMode"] = folding_mode
    if fold_strategy is not None:
        body["foldStrategy"] = fold_strategy
    if folding_conformations is not None:
        body["foldingConformations"] = folding_conformations
    if enable_expansion is not None:
        body["enableExpansion"] = enable_expansion
    if auto_conformation_expansion is not None:
        body["autoConformationExpansion"] = auto_conformation_expansion
    if clash_resolution_enabled is not None:
        body["clashResolutionEnabled"] = clash_resolution_enabled
    if md_relaxation_enabled is not None:
        body["mdRelaxationEnabled"] = md_relaxation_enabled
    if num_trajectories is not None:
        body["numTrajectories"] = num_trajectories
        body["diffusionSamples"] = num_trajectories
    if sampling_steps is not None:
        body["samplingSteps"] = sampling_steps
    if glycosylation_enabled is not None:
        body["glycosylationEnabled"] = glycosylation_enabled
    if program_id is not None:
        body["programId"] = program_id
    # Optional immuno / stability sub-modules (dict of booleans per protease/epitope)
    if immuno_modules is not None:
        body["immunoModules"] = immuno_modules
    if stability_modules is not None:
        body["stabilityModules"] = stability_modules
    # Charge / solubility filtering — only send non-None values; server uses
    # its own defaults when these keys are absent.
    if charge_mode is not None:
        body["chargeMode"] = charge_mode
    if charge_value is not None:
        body["chargeValue"] = charge_value
    if charge_min is not None:
        body["chargeMin"] = charge_min
    if charge_max is not None:
        body["chargeMax"] = charge_max
    if min_solubility is not None:
        body["minSolubility"] = min_solubility
    # Cyclization — only send when explicitly requested (non-None + non-"none").
    # The server reads this via req.body.cyclicMode; tier gate is enforced
    # server-side (HTTP 403 for insufficient tiers), but we also document it here.
    if cyclic_mode is not None and cyclic_mode != "none":
        body["cyclicMode"] = cyclic_mode
        body["cyclicStrength"] = cyclic_strength
        body["strictRecombinant"] = strict_recombinant
        if dual_fold_viz:
            body["dualFoldViz"] = dual_fold_viz
    # Multi-segment scaffold config (binding/linker/stability/premade segments).
    if segment_config is not None:
        from ligandai.types import SegmentConfig
        body["segmentConfig"] = (
            segment_config.model_dump(by_alias=True)
            if isinstance(segment_config, SegmentConfig)
            else segment_config
        )
    # Peptide-Drug Conjugate configuration (Pro+ tier).
    if pdc_config is not None:
        from ligandai.types import PdcConfig
        body["pdcConfig"] = (
            pdc_config.model_dump(by_alias=True)
            if isinstance(pdc_config, PdcConfig)
            else pdc_config
        )
        body["pdcEnabled"] = True
    # Fine-grained EC trimming / structure preparation.
    if ec_trimming_config is not None:
        from ligandai.types import EcTrimmingConfig
        cfg = (
            ec_trimming_config.model_dump(by_alias=True)
            if isinstance(ec_trimming_config, EcTrimmingConfig)
            else ec_trimming_config
        )
        body["ecTrimming"] = cfg
    # Hotspot → pocket expansion. When the user passes a single residue
    # (or a small set), the server should include every residue within
    # ``pocket_expansion_radius_a`` Å of any hotspot atom so the design
    # pocket has enough surface area for a real binding interface.
    # Passed only when there are residues to expand; default 6.0 Å mirrors
    # the platform's "shell of contacts" pocket convention.
    if target_residues and pocket_expansion_radius_a and pocket_expansion_radius_a > 0:
        body["pocketExpansionRadiusA"] = float(pocket_expansion_radius_a)
        body["expandHotspotPocket"] = True

    # Restrict generation to specific receptor chains (multimer support).
    # Server reads ``config.targetChains`` and filters conformations / restricts
    # the binding surface to only the listed chain IDs.
    if target_chains is not None:
        normalized_chains = [str(c).upper() for c in target_chains if c]
        if normalized_chains:
            body["targetChains"] = normalized_chains

    # Fold partner expansion mode. Three explicit user intents map to the
    # underlying server flags:
    #
    #   "target_only"       -> foldingConformations="generation",
    #                          autoConformationExpansion=false
    #                          (peptide + selected target chain only)
    #   "native_complex"    -> foldingConformations="native",
    #                          autoConformationExpansion=false
    #                          (peptide + target + native interaction partners)
    #   "all_conformations" -> foldingConformations="all",
    #                          autoConformationExpansion=true
    #                          (full ensemble across receptor conformations)
    #   list[str]           -> foldingConformations=<list of conformation names>
    #                          (explicit conformation set)
    if fold_partners is not None:
        if isinstance(fold_partners, list):
            body["foldingConformations"] = fold_partners
        elif fold_partners == "target_only":
            body["foldingConformations"] = "generation"
            body["autoConformationExpansion"] = False
            body["enableExpansion"] = False
            body["foldPartnerMode"] = "target_only"
        elif fold_partners == "native_complex":
            body["foldingConformations"] = "native"
            body["autoConformationExpansion"] = False
            body["foldPartnerMode"] = "native_complex"
        elif fold_partners == "all_conformations":
            body["foldingConformations"] = "all"
            body["autoConformationExpansion"] = True
            body["enableExpansion"] = True
            body["foldPartnerMode"] = "all_conformations"

    if extra:
        body.update(extra)
    return body


_VALID_FOLD_APPROACHES = ("boltz2_affinity", "esmfold2", "esmfold2_fast")


def _resolve_fold_approach(fold_approach: str | None) -> str:
    """Normalize / validate ``fold_approach`` before HTTP submit.

    Default = ``"boltz2_affinity"`` — current production gold standard.
    Aliases: ``"boltz2"`` and ``"boltz"`` collapse to ``"boltz2_affinity"``.
    """
    if fold_approach is None or fold_approach == "":
        return "boltz2_affinity"
    fa = str(fold_approach).strip().lower().replace("-", "_")
    if fa in ("boltz2", "boltz", "boltz_2", "boltz_affinity"):
        return "boltz2_affinity"
    if fa == "esmfold":
        return "esmfold2"
    if fa not in _VALID_FOLD_APPROACHES:
        raise ValueError(
            f"fold_approach must be one of {_VALID_FOLD_APPROACHES}, got {fold_approach!r}"
        )
    return fa


def _fold_body(
    sequences: list[Sequence | str | dict[str, Any]],
    *,
    auto_score: bool = True,
    template_mode: bool = False,
    msa_enabled: bool | None = None,
    target_gene: str | None = None,
    glycosylation: bool | None = None,
    pegylation: bool | None = None,
    gpu_count: int = 1,
    diffusion_samples: int = 1,
    sampling_steps: int | None = None,
    recycling_steps: int | None = None,
    num_trajectories: int | None = None,
    step_scale: float | None = None,
    contribute_to_receptordb: bool | None = None,
    n_parallel_gpus: int | None = None,
    # / approach selection
    fold_approach: str | None = None,
    num_seeds: int | None = None,
    num_recycles: int | None = None,
    return_pdb: bool | None = None,
) -> dict[str, Any]:
    """Build the body for ``POST /api/folding/predict``.

    Single sequence → ``{model, sequence}``. Multiple → ``{model, entities}``.

    ``fold_approach`` selects the upstream design worker:
      - ``"boltz2_affinity"`` (default) — gold-standard 2-chain Boltz-2 + affinity head
      - ``"esmfold2"`` — single-sequence ESMFold2 on B200+ (~3-5 s/peptide)
      - ``"esmfold2_fast"`` — LF-Pose v3 (50 ms warm-pool variant)
    """
    approach = _resolve_fold_approach(fold_approach)
    normalized = [_norm_seq(s) for s in sequences]
    # Effective trajectory count: prefer num_seeds when explicitly set,
    # otherwise fall back to num_trajectories then diffusion_samples.
    effective_samples = (
        int(num_seeds) if num_seeds is not None
        else (num_trajectories if num_trajectories is not None else diffusion_samples)
    )
    body: dict[str, Any] = {
        "model": approach,
        "foldApproach": approach,
        "fold_approach": approach,
        "gpuCount": gpu_count,
        "diffusionSamples": effective_samples,
        "templateMode": template_mode,
        "autoScore": auto_score,
    }
    if num_seeds is not None:
        body["numSeeds"] = int(num_seeds)
        body["num_seeds"] = int(num_seeds)
    if num_recycles is not None:
        body["numRecycles"] = int(num_recycles)
        body["num_recycles"] = int(num_recycles)
    if return_pdb is not None:
        body["returnPdb"] = bool(return_pdb)
        body["return_pdb"] = bool(return_pdb)
    if sampling_steps is not None:
        body["samplingSteps"] = sampling_steps
    if recycling_steps is not None:
        body["recyclingSteps"] = recycling_steps
    if num_trajectories is not None:
        body["numTrajectories"] = num_trajectories
    if step_scale is not None:
        body["stepScale"] = step_scale
    if contribute_to_receptordb is not None:
        body["contributeToReceptordb"] = contribute_to_receptordb
        body["submitToCommunity"] = contribute_to_receptordb
    # explicit parallel-GPU cap (tier-validated server-side via tier GPU limits).
    # When None, the server picks a tier-appropriate default. When set above the
    # caller's tier cap the server returns 400 with the cap value in the error body.
    if n_parallel_gpus is not None:
        body["nParallelGpus"] = int(n_parallel_gpus)
        # Mirror snake_case for platform consumers
        body["n_parallel_gpus"] = int(n_parallel_gpus)
    if target_gene is not None:
        body["targetGeneName"] = target_gene
    if msa_enabled is not None:
        body["msaEnabled"] = msa_enabled
    if glycosylation:
        body["glycosylation"] = {"enabled": True}
    if pegylation:
        body["pegylation"] = {"enabled": True}

    if len(normalized) == 1 and not normalized[0].get("chainId"):
        body["sequence"] = normalized[0]["sequence"]
        if "name" in normalized[0]:
            body["name"] = normalized[0]["name"]
    else:
        body["entities"] = [
            {
                "type": s.get("type", "protein"),
                "chainId": s.get("chainId") or chr(ord("A") + i),
                "sequence": s["sequence"],
                **({"name": s["name"]} if "name" in s else {}),
                **({"geneName": s["geneName"]} if "geneName" in s else {}),
            }
            for i, s in enumerate(normalized)
        ]
    return body


def _norm_seq(s: Sequence | str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(s, str):
        return {"sequence": s}
    if isinstance(s, Sequence):
        out: dict[str, Any] = {"sequence": s.sequence}
        if s.name:
            out["name"] = s.name
        if s.target_gene:
            out["geneName"] = s.target_gene
        if s.target_chain:
            out["chainId"] = s.target_chain
        return out
    return dict(s)


def _parse_generation(payload: dict[str, Any]) -> GenerationResult:
    """Coerce a server result payload into :class:`GenerationResult`."""
    out: dict[str, Any] = {
        "jobId": payload.get("jobId") or payload.get("id") or payload.get("session_id") or "",
        "sessionId": payload.get("sessionId") or payload.get("session_id"),
        "gene": payload.get("gene") or _first_target_gene(payload) or "",
        "peptides": _extract_peptides(payload),
        "totalGenerated": payload.get("totalGenerated") or payload.get("total"),
        "parameters": payload.get("parameters") or payload.get("config"),
    }
    return GenerationResult.model_validate(out)


def _parse_fold(payload: dict[str, Any]) -> FoldResult:
    """Build a :class:`FoldResult` from a server status payload.

    the parser is now durable about which keys it
    inspects. The server may emit the PDB content at the top level, nested
    under ``result``, or under ``output_data`` (gpu_jobs row shape). We probe
    each one in priority order and surface :attr:`FoldResult.has_structure`
    set True only when actual content was found.
    """
    if not isinstance(payload, dict):
        payload = {}

    # Server emits the fold result in three possible places: top-level (raw
    # webhook), payload["result"] (folding_jobs endpoint), or
    # payload["output_data"] (gpu_jobs row). Probe in priority order.
    nested_candidates: list[dict[str, Any]] = []
    for key in ("result", "output_data", "outputData"):
        node = payload.get(key)
        if isinstance(node, dict):
            nested_candidates.append(node)

    def _first(*keys: str) -> Any:
        for src in (payload, *nested_candidates):
            if not isinstance(src, dict):
                continue
            for k in keys:
                v = src.get(k)
                if v is not None and v != "":
                    return v
        return None

    pdb_data = _first("pdbContent", "pdb_content", "pdbData", "pdb_data", "pdb")
    pdb_url = _first("pdbUrl", "pdb_url")
    cif_data = _first("cifContent", "cif_content", "cifData", "cif_data", "cif")
    iptm = _first("iptm", "ipTM")
    ipsae = _first("ipsae", "iPSAE")
    # server emits both a scalar mean_plddt AND an
    # array plddt (per-residue). FoldResult.plddt is a scalar — prefer the
    # mean if available, fall back to mean(plddt-array), else None.
    plddt_raw = _first("mean_plddt", "meanPlddt", "plddt")
    if isinstance(plddt_raw, (int, float)):
        plddt = float(plddt_raw)
    elif isinstance(plddt_raw, list) and plddt_raw:
        try:
            plddt = sum(float(v) for v in plddt_raw) / len(plddt_raw)
        except (TypeError, ValueError):
            plddt = None
    else:
        plddt = None
    ptm = _first("ptm")
    ipae = _first("ipae")
    chain_pair_iptm = _first("chainPairIptm", "chain_pair_iptm")
    per_chain = _first("perChain", "per_chain", "per_chain_metrics")
    pae_url = _first("paeUrl", "pae_url")
    confidence = _first("confidence", "confidence_metrics", "confidenceMetrics")
    metrics = _first("metrics") or {}
    scores = _first("scores", "deltaforge", "deltaforge_score") or {}

    def _to_float(v: Any) -> float | None:
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    iptm = _to_float(iptm)
    ipsae = _to_float(ipsae)
    ptm = _to_float(ptm)
    ipae = _to_float(ipae) if not isinstance(ipae, dict) else None

    if not isinstance(metrics, dict):
        metrics = {}
    # Make headline metrics easy to subscript even when the server didn't pre-
    # build the metrics dict.
    metrics = dict(metrics)
    for k, v in (
        ("iptm", iptm), ("ipsae", ipsae), ("ptm", ptm),
        ("mean_plddt", plddt), ("ipae", ipae),
    ):
        if v is not None and k not in metrics:
            try:
                metrics[k] = float(v)
            except (TypeError, ValueError):
                pass

    has_struct_flag = _first("hasStructure", "has_structure")
    has_structure = bool(has_struct_flag) or bool(pdb_data) or bool(cif_data)

    return FoldResult.model_validate(
        {
            "jobId": payload.get("jobId") or payload.get("id") or _first("jobId", "id") or "",
            "pdbUrl": pdb_url,
            "pdbData": pdb_data,
            "cifData": cif_data,
            "hasStructure": has_structure,
            "iptm": iptm,
            "ipsae": ipsae,
            "plddt": plddt,
            "ptm": ptm,
            "ipae": ipae,
            "chainPairIptm": chain_pair_iptm,
            "perChain": per_chain,
            "paeUrl": pae_url,
            "confidence": confidence if isinstance(confidence, dict) else None,
            "metrics": metrics or None,
            "scores": scores if isinstance(scores, dict) and scores else None,
        }
    )


# ─── Batch fold helpers (v0.5.5) ────────────────────────────────────────────

# The batch-fold endpoint takes either bare AA strings or FASTA blocks. The
# server parses FASTA already, so this list is forwarded verbatim — but we
# strip empty/whitespace-only entries client-side to surface argument errors
# closer to the caller.
def _normalize_batch_peptide_inputs(peptides: list[str]) -> list[str]:
    if not isinstance(peptides, list) or not peptides:
        raise ValueError("peptides must be a non-empty list of strings")
    cleaned: list[str] = []
    for i, p in enumerate(peptides):
        if not isinstance(p, str):
            raise TypeError(f"peptides[{i}] must be a string, got {type(p).__name__}")
        s = p.strip()
        if s:
            cleaned.append(s)
    if not cleaned:
        raise ValueError("peptides list contained no non-empty strings")
    return cleaned


def _resolve_receptor_pdb_arg(receptor_pdb: str | None) -> str | None:
    """Accept either raw PDB content or a filesystem path. Path -> read once."""
    if receptor_pdb is None:
        return None
    if not isinstance(receptor_pdb, str) or not receptor_pdb.strip():
        raise ValueError("receptor_pdb must be a non-empty string")
    # If the value looks like a real PDB record we forward as-is. Otherwise, if
    # it looks like a path that exists on disk, read it.
    has_pdb_header = any(
        receptor_pdb.lstrip().startswith(tok)
        for tok in ("HEADER", "TITLE", "COMPND", "REMARK", "ATOM", "HETATM", "MODEL", "CRYST1")
    )
    if has_pdb_header:
        return receptor_pdb
    # Heuristic: short single-line strings without ATOM records but with a
    # plausible file suffix are treated as paths.
    if len(receptor_pdb) < 4096 and ("/" in receptor_pdb or "\\" in receptor_pdb or receptor_pdb.endswith(".pdb")):
        try:
            path = Path(receptor_pdb).expanduser()
            if path.exists() and path.is_file():
                return path.read_text(encoding="utf-8")
        except OSError:
            # Fall through — caller probably meant a literal string
            pass
    return receptor_pdb


def _build_batch_fold_body(
    *,
    peptides: list[str],
    target_gene: str | None,
    receptor_pdb: str | None,
    receptor_sequence: str | None,
    receptor_name: str | None,
    diffusion_samples: int,
    sampling_steps: int,
    recycling_steps: int | None,
    step_scale: float | None,
    msa_enabled: bool | None,
    glycosylation: bool | None,
    template_mode: bool,
    n_parallel_gpus: int | None,
    session_id: str | None,
    contribute_to_receptordb: bool | None,
) -> dict[str, Any]:
    """Construct the POST /api/v1/folding/predict-batch JSON body."""
    receptor_specified = [v for v in (target_gene, receptor_pdb, receptor_sequence) if v]
    if len(receptor_specified) != 1:
        raise ValueError(
            "Pass exactly one of target_gene=, receptor_pdb=, or receptor_sequence="
        )

    body: dict[str, Any] = {
        "peptides": _normalize_batch_peptide_inputs(peptides),
        "diffusion_samples": int(diffusion_samples),
        "sampling_steps": int(sampling_steps),
        "template_mode": bool(template_mode),
    }
    if target_gene is not None:
        body["target_gene"] = target_gene
    if receptor_pdb is not None:
        body["receptor_pdb"] = _resolve_receptor_pdb_arg(receptor_pdb)
    if receptor_sequence is not None:
        body["receptor_sequence"] = receptor_sequence
    if receptor_name is not None:
        body["receptor_name"] = receptor_name
    if recycling_steps is not None:
        body["recycling_steps"] = int(recycling_steps)
    if step_scale is not None:
        body["step_scale"] = float(step_scale)
    if msa_enabled is not None:
        body["msa_enabled"] = bool(msa_enabled)
    if glycosylation is not None:
        body["glycosylation"] = bool(glycosylation)
    if n_parallel_gpus is not None:
        body["n_parallel_gpus"] = int(n_parallel_gpus)
    if session_id is not None:
        body["session_id"] = session_id
    if contribute_to_receptordb is not None:
        body["contribute_to_receptordb"] = bool(contribute_to_receptordb)
    return body


class BatchFoldJob:
    """A batch of N peptide-receptor fold jobs submitted by ``Peptides.fold_batch``.

    Internally wraps N :class:`~ligandai.jobs.Job` instances (one per peptide)
    sharing a single ``batch_id``. ``wait()`` blocks until every sub-job is
    terminal and returns the list of parsed :class:`FoldResult` objects in the
    same order as the input peptides.

    The credit charge is taken upfront on submission — sub-job failures do
    NOT automatically refund. Use ``.refunds_pending`` to see how many failed.
    """

    def __init__(
        self,
        transport: Any,
        *,
        batch_id: str,
        jobs: list[dict[str, Any]],
        total_cost_credits: int,
        peptide_count: int,
        trajectories_per_peptide: int,
        receptor: dict[str, Any] | None = None,
        sampling_steps: int | None = None,
    ) -> None:
        from ligandai.jobs import Job  # local to avoid circular import

        self._transport = transport
        self._batch_id = batch_id
        self._jobs_meta = jobs
        self._total_cost_credits = int(total_cost_credits)
        self._peptide_count = int(peptide_count)
        self._trajectories_per_peptide = int(trajectories_per_peptide)
        self._receptor = receptor or {}
        self._sampling_steps = sampling_steps
        self._sub_jobs: list[Job[FoldResult]] = []
        for entry in jobs:
            jid = entry.get("job_id") or entry.get("jobId") or ""
            if not jid:
                # Submission failed for this slot — placeholder kept so
                # peptide_index alignment is preserved in .results.
                self._sub_jobs.append(None)  # type: ignore[arg-type]
                continue
            self._sub_jobs.append(
                Job(
                    transport,
                    jid,
                    job_type="folding",
                    parser=_parse_fold,
                    status_path="/api/folding/jobs/{job_id}",
                    cancel_path="/api/folding/jobs/{job_id}",
                    sse_path="/api/folding/jobs/{job_id}/logs/stream",
                    initial={"id": jid, "type": "folding", "status": "queued"},
                )
            )
        self._results: list[FoldResult | None] | None = None

    # ─── Public properties ──────────────────────────────────────────────────

    @property
    def batch_id(self) -> str:
        return self._batch_id

    @property
    def jobs(self) -> list[dict[str, Any]]:
        """Raw per-peptide submission metadata: job_id, peptide_index, sequence."""
        return list(self._jobs_meta)

    @property
    def total_cost_credits(self) -> int:
        return self._total_cost_credits

    @property
    def peptide_count(self) -> int:
        return self._peptide_count

    @property
    def trajectories_per_peptide(self) -> int:
        return self._trajectories_per_peptide

    @property
    def receptor(self) -> dict[str, Any]:
        """Server-resolved receptor metadata: mode, gene, uniprot_id, length, source."""
        return dict(self._receptor)

    @property
    def sub_jobs(self) -> list[Any]:
        """List of :class:`Job` instances aligned with ``peptide_index``.

        Failed-to-submit slots are ``None`` (kept so indices line up with input).
        """
        return list(self._sub_jobs)

    @property
    def results(self) -> list[FoldResult | None]:
        """Parsed FoldResults aligned with ``peptide_index`` (None for failures).

        Triggers a per-job refresh + parse if not already cached. Call ``.wait()``
        first to ensure all sub-jobs are terminal.
        """
        if self._results is None:
            out: list[FoldResult | None] = []
            for sub in self._sub_jobs:
                if sub is None:
                    out.append(None)
                    continue
                try:
                    out.append(sub.results)
                except Exception:
                    out.append(None)
            self._results = out
        return list(self._results)

    @property
    def folds(self) -> list[FoldResult | None]:
        """Alias for :attr:`results` — matches the docstring example surface."""
        return self.results

    @property
    def refunds_pending(self) -> int:
        """Sub-jobs that failed-to-submit or ended non-successfully (refund tally)."""
        n = 0
        for sub in self._sub_jobs:
            if sub is None:
                n += 1
                continue
            try:
                if sub.is_terminal and not sub.succeeded:
                    n += 1
            except Exception:
                n += 1
        return n

    # ─── Polling & waiting ──────────────────────────────────────────────────

    def wait(
        self,
        timeout: float = 7200.0,
        poll_interval: float = 5.0,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[FoldResult | None]:
        """Block until every sub-job is terminal. Returns ordered results.

        Parameters
        ----------
        timeout
            Total wall-clock budget (seconds) for the whole batch. Raises
            :class:`LigandAITimeoutError` if exceeded.
        poll_interval
            Sleep between polls (seconds). Each tick refreshes every still-
            running sub-job before sleeping.
        on_progress
            Optional callback receiving a dict ``{"done": int, "total": int,
            "failed": int, "batch_id": str}`` on every poll tick.
        """
        import time
        from ligandai.errors import LigandAITimeoutError

        deadline = time.monotonic() + timeout
        total = len(self._sub_jobs)
        while True:
            done = 0
            failed = 0
            for sub in self._sub_jobs:
                if sub is None:
                    failed += 1
                    done += 1
                    continue
                if sub.is_terminal:
                    done += 1
                    if not sub.succeeded:
                        failed += 1
                else:
                    try:
                        sub.refresh()
                        if sub.is_terminal:
                            done += 1
                            if not sub.succeeded:
                                failed += 1
                    except Exception:
                        # Transient — keep retrying on next tick
                        pass
            if on_progress is not None:
                try:
                    on_progress({
                        "batch_id": self._batch_id,
                        "done": done,
                        "total": total,
                        "failed": failed,
                    })
                except Exception:
                    pass
            if done >= total:
                break
            if time.monotonic() > deadline:
                raise LigandAITimeoutError(
                    f"Batch {self._batch_id} did not complete within {timeout}s "
                    f"({done}/{total} sub-jobs terminal)"
                )
            time.sleep(poll_interval)
        # Force per-job result parse once
        self._results = None
        return self.results

    def stream(
        self,
        timeout: float = 7200.0,
        poll_interval: float = 5.0,
    ) -> "Iterator[BatchFoldEvent]":
        """Yield one :class:`~ligandai.types.BatchFoldEvent` per sub-job as it
        becomes terminal AND its structural payload has landed.

        canonical streaming surface for batch folds.
        Replaces the old pattern of ``[job.wait() for job in batch.sub_jobs]``
        which serialized waits and missed the structural-payload landing race.

        Each event carries the peptide index, peptide sequence, server job_id,
        full PDB content, and confidence metrics so callers can pipeline
        scoring/save-to-disk per-fold instead of waiting for the whole batch.

        Parameters
        ----------
        timeout
            Total wall-clock budget (seconds) for the whole batch. Raises
            :class:`~ligandai.errors.LigandAITimeoutError` when the deadline
            elapses with sub-jobs still pending.
        poll_interval
            Sleep between batch ticks (seconds).

        Yields
        ------
        BatchFoldEvent
            One per fully-resolved sub-job (or one per failed sub-job).

        Example
        -------
        .. code-block:: python

            batch = client.fold_batch(peptides=[P1, P2, P3], target=RECEPTOR)
            for event in batch.stream():
                if event.status == "succeeded":
                    Path(f"./{event.record_id}.pdb").write_text(event.pdb_content)
                    print(f"{event.peptide_sequence}: iptm={event.iptm:.2f}")
        """
        import time as _time
        from datetime import datetime as _datetime, timezone as _timezone
        from ligandai.errors import LigandAITimeoutError as _LigandAITimeoutError
        from ligandai.jobs import _fold_has_durable_payload as _has_durable
        from ligandai.types import BatchFoldEvent as _BatchFoldEvent

        deadline = _time.monotonic() + timeout
        emitted: set[int] = set()
        total = len(self._sub_jobs)

        while len(emitted) < total:
            for idx, sub in enumerate(self._sub_jobs):
                if idx in emitted:
                    continue
                meta = self._jobs_meta[idx] if idx < len(self._jobs_meta) else {}
                record_id = (
                    meta.get("record_id")
                    or meta.get("recordId")
                    or (sub.id if sub is not None else None)
                )
                peptide_sequence = (
                    meta.get("peptide")
                    or meta.get("peptide_sequence")
                    or meta.get("sequence")
                )
                if sub is None:
                    emitted.add(idx)
                    yield _BatchFoldEvent.model_validate({
                        "recordId": record_id,
                        "jobId": meta.get("job_id") or meta.get("jobId") or "",
                        "peptideIndex": idx,
                        "peptideSequence": peptide_sequence,
                        "status": "failed",
                        "phase": "submit_failed",
                        "timestamp": _datetime.now(_timezone.utc),
                    })
                    continue
                try:
                    sub.refresh()
                except Exception:
                    continue
                if not sub.is_terminal:
                    continue
                durable_ok, _missing, _call_id = _has_durable(
                    sub.info, sub.info.result if sub.info else None,
                )
                if sub.succeeded and not durable_ok:
                    # Skip this tick — payload hasn't landed yet.
                    continue
                emitted.add(idx)
                if sub.succeeded:
                    try:
                        fold = sub.results
                    except Exception:
                        fold = None
                    yield _BatchFoldEvent.model_validate({
                        "recordId": record_id,
                        "jobId": sub.id,
                        "peptideIndex": idx,
                        "peptideSequence": peptide_sequence,
                        "status": "succeeded",
                        "pdbContent": getattr(fold, "pdb_data", None),
                        "cifData": getattr(fold, "cif_data", None),
                        "iptm": getattr(fold, "iptm", None),
                        "ipsae": getattr(fold, "ipsae", None),
                        "ipae": getattr(fold, "ipae", None),
                        "ptm": getattr(fold, "ptm", None),
                        "meanPlddt": getattr(fold, "plddt", None),
                        "paeUrl": getattr(fold, "pae_url", None),
                        "confidence": getattr(fold, "confidence", None),
                        "perChain": getattr(fold, "per_chain", None),
                        "phase": "complete",
                        "timestamp": _datetime.now(_timezone.utc),
                    })
                else:
                    yield _BatchFoldEvent.model_validate({
                        "recordId": record_id,
                        "jobId": sub.id,
                        "peptideIndex": idx,
                        "peptideSequence": peptide_sequence,
                        "status": sub.status or "failed",
                        "phase": "failed",
                        "timestamp": _datetime.now(_timezone.utc),
                    })
            if len(emitted) >= total:
                break
            if _time.monotonic() > deadline:
                pending = [i for i in range(total) if i not in emitted]
                raise _LigandAITimeoutError(
                    f"Batch {self._batch_id} did not complete streaming within "
                    f"{timeout}s ({len(emitted)}/{total} folds emitted, "
                    f"pending indices: {pending[:10]}{'...' if len(pending) > 10 else ''})"
                )
            _time.sleep(poll_interval)

    def cancel(self) -> int:
        """Cancel every still-running sub-job. Returns the number canceled."""
        n = 0
        for sub in self._sub_jobs:
            if sub is None:
                continue
            try:
                if not sub.is_terminal and sub.cancel():
                    n += 1
            except Exception:
                pass
        return n

    def __len__(self) -> int:
        return len(self._sub_jobs)

    def __repr__(self) -> str:
        return (
            f"BatchFoldJob(batch_id={self._batch_id!r}, "
            f"peptide_count={self._peptide_count}, "
            f"trajectories_per_peptide={self._trajectories_per_peptide}, "
            f"total_cost_credits={self._total_cost_credits})"
        )


class AsyncBatchFoldJob:
    """Async sibling of :class:`BatchFoldJob`. See :class:`BatchFoldJob` for full API."""

    def __init__(
        self,
        transport: Any,
        *,
        batch_id: str,
        jobs: list[dict[str, Any]],
        total_cost_credits: int,
        peptide_count: int,
        trajectories_per_peptide: int,
        receptor: dict[str, Any] | None = None,
        sampling_steps: int | None = None,
    ) -> None:
        from ligandai.jobs import AsyncJob  # local to avoid circular import

        self._transport = transport
        self._batch_id = batch_id
        self._jobs_meta = jobs
        self._total_cost_credits = int(total_cost_credits)
        self._peptide_count = int(peptide_count)
        self._trajectories_per_peptide = int(trajectories_per_peptide)
        self._receptor = receptor or {}
        self._sampling_steps = sampling_steps
        self._sub_jobs: list[Any] = []
        for entry in jobs:
            jid = entry.get("job_id") or entry.get("jobId") or ""
            if not jid:
                self._sub_jobs.append(None)
                continue
            self._sub_jobs.append(
                AsyncJob(
                    transport,
                    jid,
                    job_type="folding",
                    parser=_parse_fold,
                    status_path="/api/folding/jobs/{job_id}",
                    cancel_path="/api/folding/jobs/{job_id}",
                    sse_path="/api/folding/jobs/{job_id}/logs/stream",
                    initial={"id": jid, "type": "folding", "status": "queued"},
                )
            )
        self._results: list[FoldResult | None] | None = None

    @property
    def batch_id(self) -> str:
        return self._batch_id

    @property
    def jobs(self) -> list[dict[str, Any]]:
        return list(self._jobs_meta)

    @property
    def total_cost_credits(self) -> int:
        return self._total_cost_credits

    @property
    def peptide_count(self) -> int:
        return self._peptide_count

    @property
    def trajectories_per_peptide(self) -> int:
        return self._trajectories_per_peptide

    @property
    def receptor(self) -> dict[str, Any]:
        return dict(self._receptor)

    @property
    def sub_jobs(self) -> list[Any]:
        return list(self._sub_jobs)

    async def wait(
        self,
        timeout: float = 7200.0,
        poll_interval: float = 5.0,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[FoldResult | None]:
        import asyncio
        import time
        from ligandai.errors import LigandAITimeoutError

        deadline = time.monotonic() + timeout
        total = len(self._sub_jobs)
        while True:
            done = 0
            failed = 0
            for sub in self._sub_jobs:
                if sub is None:
                    failed += 1
                    done += 1
                    continue
                if sub.is_terminal:
                    done += 1
                    if not sub.succeeded:
                        failed += 1
                else:
                    try:
                        await sub.refresh()
                        if sub.is_terminal:
                            done += 1
                            if not sub.succeeded:
                                failed += 1
                    except Exception:
                        pass
            if on_progress is not None:
                try:
                    on_progress({
                        "batch_id": self._batch_id,
                        "done": done,
                        "total": total,
                        "failed": failed,
                    })
                except Exception:
                    pass
            if done >= total:
                break
            if time.monotonic() > deadline:
                raise LigandAITimeoutError(
                    f"Batch {self._batch_id} did not complete within {timeout}s "
                    f"({done}/{total} sub-jobs terminal)"
                )
            await asyncio.sleep(poll_interval)
        # Force result parse
        out: list[FoldResult | None] = []
        for sub in self._sub_jobs:
            if sub is None:
                out.append(None)
                continue
            try:
                # AsyncJob.results may be a sync property that fetches lazily;
                # if it requires an await on a loader, fall back to a fresh
                # refresh + parse here.
                out.append(sub.results)
            except Exception:
                out.append(None)
        self._results = out
        return list(out)

    @property
    def results(self) -> list[FoldResult | None]:
        if self._results is None:
            return [None] * len(self._sub_jobs)
        return list(self._results)

    @property
    def folds(self) -> list[FoldResult | None]:
        return self.results

    async def cancel(self) -> int:
        n = 0
        for sub in self._sub_jobs:
            if sub is None:
                continue
            try:
                if not sub.is_terminal and await sub.cancel():
                    n += 1
            except Exception:
                pass
        return n

    def __len__(self) -> int:
        return len(self._sub_jobs)

    def __repr__(self) -> str:
        return (
            f"AsyncBatchFoldJob(batch_id={self._batch_id!r}, "
            f"peptide_count={self._peptide_count}, "
            f"trajectories_per_peptide={self._trajectories_per_peptide}, "
            f"total_cost_credits={self._total_cost_credits})"
        )


def _first_target_gene(payload: dict[str, Any]) -> str | None:
    targets = payload.get("targets")
    if isinstance(targets, list) and targets:
        first = targets[0]
        if isinstance(first, dict):
            return first.get("gene")
    return None


def _set_if_missing(out: dict[str, Any], key: str, value: Any) -> None:
    if value is not None and out.get(key) is None:
        out[key] = value


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _flatten_peptide(raw: dict[str, Any], gene: str | None = None) -> dict[str, Any]:
    """Promote quality_scores sub-fields to top level for Peptide mapping."""
    if not isinstance(raw, dict):
        return {"sequence": str(raw), **({"targetGene": gene} if gene else {})}
    qs = raw.get("quality_scores") or {}
    out = dict(raw)
    if gene and not out.get("targetGene") and not out.get("target_gene"):
        out["targetGene"] = gene

    _set_if_missing(out, "ligandiq", _first_present(raw.get("ligandiq_score"), qs.get("ligandiq_score")))
    _set_if_missing(out, "predictedIpsae", _first_present(raw.get("predicted_ipsae"), qs.get("predicted_ipsae")))
    predicted_iptm = _first_present(
        raw.get("predicted_iptm"),
        raw.get("pred_iptm"),
        raw.get("ligandiq_pred_iptm"),
        qs.get("predicted_iptm"),
        qs.get("pred_iptm"),
        qs.get("ligandiq_pred_iptm"),
    )
    legacy_predicted_ptm = _first_present(raw.get("predicted_ptm"), qs.get("predicted_ptm"))
    # Legacy production LigandIQ payloads normalize the platform's pred_iptm head into
    # quality_scores.predicted_ptm. Expose it only as predicted_iptm; current
    # LigandIQ does not emit a distinct predicted pTM head.
    if predicted_iptm is None and (
        raw.get("ligandiq_score") is not None
        or qs.get("ligandiq_score") is not None
        or raw.get("predicted_ipsae") is not None
        or qs.get("predicted_ipsae") is not None
    ):
        predicted_iptm = legacy_predicted_ptm
    out.pop("predicted_ptm", None)
    out.pop("predictedPtm", None)
    _set_if_missing(out, "predictedIptm", predicted_iptm)
    _set_if_missing(out, "predictedPlddt", _first_present(raw.get("predicted_plddt"), qs.get("predicted_plddt")))
    _set_if_missing(out, "binderProb", _first_present(raw.get("binder_prob"), qs.get("binder_prob")))

    # Stability / immuno (academia+ tier, may be None)
    if not out.get("stability_grade") and raw.get("stability_scores"):
        out["stabilityGrade"] = raw["stability_scores"].get("stability_grade")
    if not out.get("immunogenicity_score") and raw.get("immuno_scores"):
        out["immunogenicityScore"] = raw["immuno_scores"].get("immunogenicityScore")
    return out


def _extract_peptides(payload: dict[str, Any]) -> list[dict[str, Any]]:
    pep = payload.get("peptides")
    if isinstance(pep, list):
        return [_flatten_peptide(p) for p in pep]
    # Dict keyed by gene (session detail format) → flatten all genes
    if isinstance(pep, dict):
        flat: list[dict[str, Any]] = []
        for gene, gene_peps in pep.items():
            if isinstance(gene_peps, list):
                flat.extend(_flatten_peptide(p, gene=str(gene)) for p in gene_peps)
        return flat
    nested = payload.get("results")
    if isinstance(nested, dict) and isinstance(nested.get("peptides"), list):
        return [_flatten_peptide(p) for p in nested["peptides"]]
    if isinstance(nested, list):
        return [_flatten_peptide(p) for p in nested]
    return []


def _has_generation_peptides(payload: dict[str, Any]) -> bool:
    return bool(_extract_peptides(payload))


def _unwrap_session_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the session object from the common session endpoint envelopes."""
    if not isinstance(payload, dict):
        return {}
    session = payload.get("session")
    if isinstance(session, dict):
        return session
    data = payload.get("data")
    if isinstance(data, dict):
        nested = data.get("session")
        if isinstance(nested, dict):
            return nested
        if "peptides" in data:
            return data
    return payload


def _session_id_from_payload(payload: dict[str, Any]) -> str | None:
    sid = (
        payload.get("sessionId")
        or payload.get("session_id")
        or payload.get("sessionID")
        or payload.get("id")
    )
    return sid if isinstance(sid, str) else None


def _generation_result_from_session(
    payload: dict[str, Any],
    session_response: dict[str, Any],
    *,
    fallback_session_id: str | None,
    fallback_gene: str | None,
) -> dict[str, Any]:
    session = _unwrap_session_response(session_response)
    result = dict(payload)

    session_id = (
        _session_id_from_payload(result)
        or _session_id_from_payload(session)
        or fallback_session_id
    )
    if session_id:
        result.setdefault("sessionId", session_id)
        result.setdefault("jobId", session_id)

    gene = (
        result.get("gene")
        or session.get("gene")
        or _first_target_gene(result)
        or _first_target_gene(session)
        or fallback_gene
    )
    if gene:
        result.setdefault("gene", gene)

    if not _has_generation_peptides(result) and session.get("peptides") is not None:
        result["peptides"] = session["peptides"]

    if result.get("totalGenerated") is None:
        result["totalGenerated"] = (
            session.get("totalGenerated")
            or session.get("total_generated")
            or session.get("total")
            or len(_extract_peptides(result))
            or None
        )

    if result.get("parameters") is None:
        result["parameters"] = session.get("parameters") or session.get("config")

    return result


def _load_generation_result(
    transport: Any,
    info: Any,
    *,
    fallback_session_id: str | None,
    fallback_gene: str | None,
) -> dict[str, Any] | None:
    payload = dict(info.result or {})
    if _has_generation_peptides(payload):
        return payload
    session_id = _session_id_from_payload(payload) or fallback_session_id or getattr(info, "id", None)
    if not session_id:
        return payload
    session_response = transport.request("GET", f"/api/ptf/sessions/{session_id}") or {}
    return _generation_result_from_session(
        payload,
        session_response,
        fallback_session_id=session_id,
        fallback_gene=fallback_gene,
    )


async def _aload_generation_result(
    transport: Any,
    info: Any,
    *,
    fallback_session_id: str | None,
    fallback_gene: str | None,
) -> dict[str, Any] | None:
    payload = dict(info.result or {})
    if _has_generation_peptides(payload):
        return payload
    session_id = _session_id_from_payload(payload) or fallback_session_id or getattr(info, "id", None)
    if not session_id:
        return payload
    session_response = await transport.request("GET", f"/api/ptf/sessions/{session_id}") or {}
    return _generation_result_from_session(
        payload,
        session_response,
        fallback_session_id=session_id,
        fallback_gene=fallback_gene,
    )


# -- Sync resource ----------------------------------------------------------


class Peptides(Resource):
    """Generation, folding, scoring, and search."""

    def generate(
        self,
        gene: str,
        num_peptides: int | None = None,
        length_range: tuple[int, int] = (20, 70),
        target_residues: list[ResidueRange] | None = None,
        target_chains: list[str] | None = None,
        fold_partners: _FoldPartnerMode | list[str] | None = None,
        # When target_residues are supplied, the SDK auto-switches the strategy
        # to "pocket_targeted" unless you override here. Pass "full_surface"
        # explicitly to keep the residues as soft hints only.
        targeting_strategy: _TargetingStrategy | None = None,
        # Hotspot pocket expansion radius in Angstroms. When target_residues
        # are provided, the server includes every residue within this radius
        # of any listed hotspot atom in the design pocket. This is the right
        # default for "design against residue X" — the peptide needs the
        # surrounding shell to make contacts, not just X itself. Set to 0 to
        # use the literal residues only.
        pocket_expansion_radius_a: float | None = 6.0,
        auto_fold: bool = True,
        top_n_fold: int | None = None,
        ec_domain_trimming: bool = True,
        deimmunize_mode: bool = False,
        variant_id: int | None = None,
        gen_gpus: int = 1,
        fold_gpus: int = 1,
        program_id: int | None = None,
        cysteine_mode: _CysteineMode = "disulfide_only",
        quality_guided: bool = False,
        quality_guidance_scale: float = 1.0,
        immunogenicity: bool = False,
        immuno_strength: float = 2.0,
        immuno_modules: dict[str, bool] | None = None,
        serum_stability: bool = False,
        stability_strength: float = 2.0,
        stability_mode: _StabilityMode = "resist",
        stability_modules: dict[str, bool] | None = None,
        halflife: _HalflifeTarget | None = None,
        halflife_strength: float = 2.0,
        # Charge / solubility filtering (server-tier gated; server activates
        # filtered design worker when any non-default constraint is present).
        charge_mode: _ChargeMode | None = None,
        charge_value: float | None = None,
        charge_min: float | None = None,
        charge_max: float | None = None,
        min_solubility: float | None = None,
        # Cyclization (tier-gated: academia/pro/enterprise).
        # Pass ``cyclic_mode="disulfide"`` for terminal Cys-Cys bridge (primary
        # recombinant-shippable mode), ``"lactam"`` for head-to-tail amide
        # closure (prediction/viz layer only), or ``"head_tail_contact"`` for
        # soft B-matrix bias without a synthesis constraint.
        # insufficient-tier users receive HTTP 403 from the server.
        cyclic_mode: _CyclicMode | None = None,
        cyclic_strength: float = 2.0,
        strict_recombinant: bool = True,
        dual_fold_viz: bool = False,
        folding_mode: str | None = None,
        # Default fold strategy: rank candidates by composite (LigandIQ × predicted
        # iPTM) before folding. The top peptides by this score get fold GPUs first
        # so credits go to the most promising designs. Override to ``"distributed"``
        # for round-robin across targets, ``"consolidated"`` for sequential
        # single-target folding, or pass ``fold_strategy=None`` to defer to server.
        fold_strategy: str | None = "quality_ranked",
        folding_conformations: str | list[str] | None = None,
        max_folds_per_target: int | None = None,
        enable_expansion: bool | None = None,
        auto_conformation_expansion: bool | None = None,
        clash_resolution_enabled: bool | None = None,
        md_relaxation_enabled: bool | None = None,
        num_trajectories: int | None = None,
        sampling_steps: int | None = None,
        glycosylation_enabled: bool | None = None,
        segment_config: SegmentConfig | dict | None = None,
        pdc_config: PdcConfig | dict | None = None,
        ec_trimming_config: EcTrimmingConfig | dict | None = None,
        **extra: Any,
    ) -> Job[GenerationResult]:
        """Submit a peptide generation job. Returns a :class:`Job`.

        Args:
            gene: Target gene symbol (e.g. ``"EGFR"``).
            num_peptides: Peptides to generate per target. Tier caps are free=10,
                basic=100, academia/pro=300, enterprise=1000.
            length_range: ``(min_aa, max_aa)`` length bounds (default ``(20, 70)``).
            target_residues: Optional pocket residue ranges for guided targeting.
                Multiple ranges may target one or more receptor chains. Use
                ``ResidueRange.from_residues([32, 33, 34], chain="A")`` to
                compress selected residues into continuous chain-local ranges.
            target_chains: Optional list of chain IDs (e.g. ``["C"]``) to
                restrict design to. Use this when a multimer (e.g. PDB
                ``9MIR``) has chains A/B/C/D and you only want peptides
                designed against chain C. The server filters conformations
                and the binding surface to only those chains.
            targeting_strategy: ``"full_surface"`` or ``"pocket_targeted"``.
            auto_fold: Run Boltz-2 folding automatically after generation.
            top_n_fold: Cap on how many peptides to fold per target.
            ec_domain_trimming: Trim signal peptide / EC domain before generation.
            deimmunize_mode: Apply post-generation deimmunization.
            variant_id: Protein variant ID (from ``peptides.variants``).
            gen_gpus: GPU count for generation. Generation uses a one-GPU
                server path; higher values are ignored or clamped server-side.
            fold_gpus: GPU count for folding (default 1). Folding caps are
                free=1, basic=4, academia=16, pro=25, enterprise=50.
            program_id: Program/workstream ID to associate session with.
            cysteine_mode: Cysteine placement policy (``"disulfide_only"`` /
                ``"allow_all"`` / ``"exclude_all"``).
            quality_guided: Enable quality-guided generation. Available to all
                authenticated tiers, including free; server-side credits and
                retention/licensing terms still apply.
            quality_guidance_scale: Scale for quality guidance (default 1.0).
            immunogenicity: Enable immune guidance (academia+ tier). Steers
                generation away from MHC-binding motifs toward sequences with low
                predicted immunogenicity. Uses MHC-I/II anchor avoidance + scoring
                during diffusion.
            immuno_strength: Immune guidance strength 0.5-4.0 (default 2.0).
                Enterprise tier unlocks values above 3.0.
            immuno_modules: Optional per-module override, e.g.
                ``{"mhc_i": True, "mhc_ii": True}``.
            serum_stability: Enable stability guidance (academia+ tier). Suppresses
                protease-recognition motifs to resist serum/lysosomal cleavage.
                Use ``stability_mode="target"`` to flip this for prodrug designs.
            stability_strength: Stability guidance strength 1.0-3.0.
            stability_mode: ``"resist"`` (avoid cleavage) or ``"target"`` (prodrug).
            stability_modules: Optional dict enabling specific protease modules,
                e.g. ``{"trypsin": True, "dppiv": True}``.
            halflife: Half-life target (``"extended"`` / ``"rapid"`` / ``"moderate"``).
                ``None`` disables half-life guidance (default).
            halflife_strength: Half-life guidance strength 1.0-3.0.
            charge_mode: Charge filter mode (academia+ tier). ``"lt"`` keeps peptides
                with net charge < ``charge_value``; ``"gt"`` keeps those above;
                ``"between"`` uses ``charge_min``/``charge_max`` bounds;
                ``"off"`` disables filtering (server default).
            charge_value: Threshold for ``charge_mode="lt"`` or ``"gt"``.
            charge_min: Lower bound for ``charge_mode="between"``.
            charge_max: Upper bound for ``charge_mode="between"``.
            min_solubility: Minimum GRAVY-based solubility score filter.
            cyclic_mode: Cyclization constraint — ``"disulfide"`` (terminal
                Cys-Cys, primary recombinant-shippable), ``"lactam"``
                (head-to-tail amide, prediction only), or
                ``"head_tail_contact"`` (soft bias). Requires
                academia/pro/enterprise tier.
            cyclic_strength: Soft-constraint strength for cyclic guidance.
            strict_recombinant: For ``cyclic_mode="disulfide"``, forbid internal
                Cys residues (required for Adaptyv synthesis path).
            dual_fold_viz: For ``cyclic_mode="lactam"``, also fold the
                Cys-wrapped (disulfide) variant for side-by-side comparison.
            folding_mode: Server folding mode override, e.g. ``"serial"`` or
                ``"parallel"``.
            fold_strategy: Server fold strategy override.
            folding_conformations: Conformation set to fold, e.g.
                ``"generation"`` or ``["generation", "apo"]``.
            max_folds_per_target: Explicit fold cap. Overrides ``top_n_fold``
                when both are provided.
            enable_expansion: Enable server-side conformation/peptide expansion.
            auto_conformation_expansion: Let the server expand conformations
                automatically.
            clash_resolution_enabled: Enable server-side clash resolution before
                folding/scoring.
            md_relaxation_enabled: Enable MD relaxation when server-supported.
            num_trajectories: Diffusion samples / trajectories per folded
                peptide. ReceptorDB defaults to 4; set 1 to reduce runtime.
        """
        if self._client is not None:
            self._client._require_feature("generate_peptides")
            if _requests_advanced_guidance(
                immunogenicity=immunogenicity,
                immuno_modules=immuno_modules,
                serum_stability=serum_stability,
                stability_modules=stability_modules,
                halflife=halflife,
                cyclic_mode=cyclic_mode,
                extra=extra,
            ):
                self._client._require_feature("advanced_guidance")
        # v0.2.0: cys/cyclic controls passed via extra={...} are deprecated;
        # use the typed kwargs above. Hard-rejected in v0.3.0.
        _warn_deprecated_cys_extra(extra)
        body = _generation_body(
            gene=gene,
            num_peptides=num_peptides,
            length_range=length_range,
            target_residues=target_residues,
            target_chains=target_chains,
            fold_partners=fold_partners,
            targeting_strategy=targeting_strategy,
            pocket_expansion_radius_a=pocket_expansion_radius_a,
            auto_fold=auto_fold,
            top_n_fold=top_n_fold,
            ec_domain_trimming=ec_domain_trimming,
            deimmunize_mode=deimmunize_mode,
            variant_id=variant_id,
            gen_gpus=gen_gpus,
            fold_gpus=fold_gpus,
            program_id=program_id,
            cysteine_mode=cysteine_mode,
            quality_guided=quality_guided,
            quality_guidance_scale=quality_guidance_scale,
            immunogenicity=immunogenicity,
            immuno_strength=immuno_strength,
            immuno_modules=immuno_modules,
            serum_stability=serum_stability,
            stability_strength=stability_strength,
            stability_mode=stability_mode,
            stability_modules=stability_modules,
            halflife=halflife,
            halflife_strength=halflife_strength,
            charge_mode=charge_mode,
            charge_value=charge_value,
            charge_min=charge_min,
            charge_max=charge_max,
            min_solubility=min_solubility,
            cyclic_mode=cyclic_mode,
            cyclic_strength=cyclic_strength,
            strict_recombinant=strict_recombinant,
            dual_fold_viz=dual_fold_viz,
            folding_mode=folding_mode,
            fold_strategy=fold_strategy,
            folding_conformations=folding_conformations,
            max_folds_per_target=max_folds_per_target,
            enable_expansion=enable_expansion,
            auto_conformation_expansion=auto_conformation_expansion,
            clash_resolution_enabled=clash_resolution_enabled,
            md_relaxation_enabled=md_relaxation_enabled,
            num_trajectories=num_trajectories,
            sampling_steps=sampling_steps,
            glycosylation_enabled=glycosylation_enabled,
            segment_config=segment_config,
            pdc_config=pdc_config,
            ec_trimming_config=ec_trimming_config,
            extra=extra,
        )
        payload = self._transport.request("POST", "/api/ptf/parallel/generate", json=body) or {}
        job_id = payload.get("sessionId") or payload.get("jobId") or payload.get("session_id") or ""
        if not job_id:
            raise LigandAIError(
                "Server did not return a session_id/jobId for generation",
                response=payload,
            )
        return Job(
            self._transport,
            job_id,
            job_type="generation",
            parser=_parse_generation,
            status_path="/api/ptf/parallel/{job_id}/status",
            cancel_path="/api/ptf/parallel/{job_id}/cancel",
            sse_path="/api/ptf/parallel/{job_id}/stream",
            initial={"id": job_id, "type": "generation", "status": "queued", **payload},
            result_loader=lambda info: _load_generation_result(
                self._transport,
                info,
                fallback_session_id=job_id,
                fallback_gene=gene,
            ),
        )

    def fold(
        self,
        sequences: list[Sequence | str | dict[str, Any]],
        target_gene: str | None = None,
        auto_score: bool = True,
        template_mode: bool = False,
        msa_enabled: bool | None = None,
        glycosylation: bool | None = None,
        pegylation: bool | None = None,
        gpu_count: int = 1,
        diffusion_samples: int = 1,
        sampling_steps: int | None = None,
        recycling_steps: int | None = None,
        num_trajectories: int | None = None,
        step_scale: float | None = None,
        contribute_to_receptordb: bool | None = None,
        n_parallel_gpus: int | None = None,
        # Hardening kwargs
        gpu: str | None = None,
        force_resubmit: bool = False,
        # ESMFold2 + approach selection
        fold_approach: str | None = None,
        num_seeds: int | None = None,
        num_recycles: int | None = None,
        return_pdb: bool | None = None,
    ) -> Job[FoldResult]:
        """Submit a folding job (monomer or multimer).

        ``fold_approach`` (default ``"boltz2_affinity"``) selects the upstream
        folding method:

          - ``"boltz2_affinity"`` — 2-chain Boltz-2 with affinity
          - ``"esmfold2"`` — single-sequence ESMFold2 on B200+ (~3-5 s/peptide)
          - ``"esmfold2_fast"`` — fast warm-pool variant (~50 ms)

        ``num_seeds`` / ``num_recycles`` / ``return_pdb`` are forwarded to the
        folding method; ``num_recycles`` only affects ESMFold approaches.

        Args:
            n_parallel_gpus: Concurrent fold-worker GPUs. When None (default),
                the platform uses the tier-default (free=1, basic=4,
                academia=16, pro=25, enterprise=50). When set above the
                caller's tier cap, the platform returns HTTP 400 with the cap
                value in the response body. Use estimate_fold_time() to
                pre-compute the wall-clock impact.
            gpu: GPU type. Only ``"b200_plus"`` is accepted; other GPU strings
                raise :class:`~ligandai.errors.LigandAIInvalidConfig` BEFORE
                any HTTP call. Distinct from ``gpu_count`` (slot count, not type).
            force_resubmit: Bypass the local 24-hour dedupe and re-submit an
                identical fold call. Default ``False`` — identical fold calls
                return the cached :class:`~ligandai.jobs.Job` handle.
        """
        if self._client is not None:
            self._client._require_feature("predict_structure")
        # SDK hardening pre-flight
        from ligandai._hardening import (
            attach_job_id,
            build_fold_params_for_hash,
            dedupe_lookup_cached,
            enforce_concurrency,
            estimate_single_fold_credits,
            mark_failed,
            preflight_credits,
            receptor_seq_for_hash,
            record_submission,
            validate_gpu,
        )
        from ligandai._dedupe import compute_submission_hash

        canonical_gpu = validate_gpu(gpu)
        submitted_set = self._client.submitted_set if self._client is not None else None
        credit_ledger = self._client.credit_ledger if self._client is not None else None
        api_key_hash = self._client.api_key_hash if self._client is not None else ""

        # Identity = concatenation of normalized sequences. For a 2-chain
        # complex (receptor + peptide), this captures both. The first sequence
        # is the receptor when len(sequences) >= 2.
        normalized_seqs = [_norm_seq(s) for s in sequences]
        seq_strings = [str(s.get("sequence", "")) for s in normalized_seqs]
        if len(seq_strings) >= 2:
            rec_for_hash = receptor_seq_for_hash(
                target_gene=target_gene,
                receptor_sequence=seq_strings[0],
                receptor_pdb=None,
            )
            peptide_for_hash: str | list[str] = "|".join(seq_strings[1:])
        else:
            rec_for_hash = receptor_seq_for_hash(
                target_gene=target_gene,
                receptor_sequence=seq_strings[0] if seq_strings else None,
                receptor_pdb=None,
            )
            peptide_for_hash = ""

        sub_hash = compute_submission_hash(
            peptide_seq=peptide_for_hash,
            receptor_seq=rec_for_hash,
            gpu=canonical_gpu,
            params=build_fold_params_for_hash(
                target_gene=target_gene,
                diffusion_samples=(
                    num_trajectories if num_trajectories is not None
                    else diffusion_samples
                ),
                sampling_steps=sampling_steps,
                recycling_steps=recycling_steps,
                step_scale=step_scale,
                msa_enabled=msa_enabled,
                glycosylation=glycosylation,
                template_mode=template_mode,
                extra={"kind": "fold", "pegylation": bool(pegylation) if pegylation is not None else None},
            ),
        )

        enforce_concurrency(self._client, submitted_set)

        cached = dedupe_lookup_cached(
            submitted_set,
            submission_hash=sub_hash,
            api_key_hash=api_key_hash,
            force_resubmit=force_resubmit,
        )
        if cached and cached.get("job_id"):
            cached_job_id = str(cached["job_id"])
            return Job(
                self._transport,
                cached_job_id,
                job_type="folding",
                parser=_parse_fold,
                status_path="/api/folding/jobs/{job_id}",
                cancel_path="/api/folding/jobs/{job_id}",
                sse_path="/api/folding/jobs/{job_id}/logs/stream",
                initial={"id": cached_job_id, "type": "folding", "status": "cached"},
            )

        # Single-fold cost: trajectories × 100 × max(1.0, steps/50)
        effective_trajectories = int(
            num_trajectories if num_trajectories is not None else diffusion_samples
        )
        estimated = estimate_single_fold_credits(
            trajectories=effective_trajectories,
            sampling_steps=sampling_steps,
        )
        balance_before, _ = preflight_credits(
            self._client, estimated=estimated, kind="fold",
        )

        body = _fold_body(
            sequences,
            auto_score=auto_score,
            template_mode=template_mode,
            msa_enabled=msa_enabled,
            target_gene=target_gene,
            glycosylation=glycosylation,
            pegylation=pegylation,
            gpu_count=gpu_count,
            diffusion_samples=diffusion_samples,
            sampling_steps=sampling_steps,
            recycling_steps=recycling_steps,
            num_trajectories=num_trajectories,
            step_scale=step_scale,
            contribute_to_receptordb=contribute_to_receptordb,
            n_parallel_gpus=n_parallel_gpus,
            fold_approach=fold_approach,
            num_seeds=num_seeds,
            num_recycles=num_recycles,
            return_pdb=return_pdb,
        )

        record_submission(
            submitted_set,
            credit_ledger,
            submission_hash=sub_hash,
            api_key_hash=api_key_hash,
            kind="fold",
            gpu=canonical_gpu,
            estimated_credits=estimated,
            balance_before=balance_before,
            meta={"target_gene": target_gene, "chains": len(seq_strings)},
        )

        try:
            payload = self._transport.request("POST", "/api/folding/predict", json=body) or {}
        except Exception as exc:
            mark_failed(
                submitted_set, submission_hash=sub_hash,
                api_key_hash=api_key_hash, reason=type(exc).__name__,
            )
            raise

        job_id = payload.get("jobId") or payload.get("id") or ""
        if not job_id:
            mark_failed(
                submitted_set, submission_hash=sub_hash,
                api_key_hash=api_key_hash, reason="server_no_job_id",
            )
            raise LigandAIError("Server did not return a jobId for fold", response=payload)
        attach_job_id(
            submitted_set, submission_hash=sub_hash,
            api_key_hash=api_key_hash, job_id=job_id,
        )
        return Job(
            self._transport,
            job_id,
            job_type="folding",
            parser=_parse_fold,
            status_path="/api/folding/jobs/{job_id}",
            cancel_path="/api/folding/jobs/{job_id}",
            sse_path="/api/folding/jobs/{job_id}/logs/stream",
            initial={"id": job_id, "type": "folding", "status": "queued", **payload},
        )

    def fold_batch(
        self,
        peptides: list[str],
        *,
        target_gene: str | None = None,
        receptor_pdb: str | None = None,
        receptor_sequence: str | None = None,
        receptor_name: str | None = None,
        diffusion_samples: int = 1,
        sampling_steps: int = 50,
        recycling_steps: int | None = None,
        step_scale: float | None = None,
        msa_enabled: bool | None = None,
        glycosylation: bool | None = None,
        template_mode: bool = False,
        n_parallel_gpus: int | None = None,
        session_id: str | None = None,
        contribute_to_receptordb: bool | None = None,
        on_credit_exhausted: Callable[[LigandAICreditError, dict[str, Any]], bool] | None = None,
        # ─── hardening kwargs ────────────────────
        # gpu: only "b200_plus" is accepted by the SDK; anything else raises
        # LigandAIInvalidConfig BEFORE any HTTP call.
        gpu: str | None = None,
        # force_resubmit: bypass the local 24h dedupe and POST anyway.
        force_resubmit: bool = False,
    ) -> BatchFoldJob:
        """Submit N peptides against a single fixed receptor for batch Boltz-2 folding.

        ``POST /api/v1/folding/predict-batch``

        Each peptide is folded as a 2-chain complex (chain A = receptor,
        chain B = peptide). The full peptide list is folded in parallel
        subject to server-side concurrency limits (tier + GPU quota).

        Billing
        -------
        100 credits per fold per trajectory. Sampling steps >50 apply a
        multiplier of ``max(1.0, sampling_steps / 50)``. Total cost is
        charged upfront before any GPU work begins; HTTP 402 is raised when
        balance is insufficient.

        Graceful credit-exhaustion (``on_credit_exhausted``)
        ----------------------------------------------------
        When the server rejects the batch with ``HTTP 402 INSUFFICIENT_CREDITS``,
        the SDK raises :class:`~ligandai.errors.LigandAICreditError`. The error
        instance carries the structured server response (``shortfall``,
        ``recovery_url``, ``top_up_usd``, ``upgrade_url``) so callers can
        prompt the user to top up.

        Pass ``on_credit_exhausted=my_callback`` to handle the error inline
        instead of having it bubble up. The callback receives ``(err, payload)``
        where ``err`` is the :class:`LigandAICreditError` and ``payload`` is
        the JSON body the SDK was about to POST. Return ``True`` to retry the
        submission (after the user tops up); return ``False`` (or anything
        falsy) to re-raise the original error. Example::

            def topup_prompt(err, payload):
                print(f"Need {err.shortfall:,} more credits (${err.top_up_usd}).")
                print(f"Top up at: {err.recovery_url}")
                input("Press Enter once topped up to resume...")
                return True

            job = client.peptides.fold_batch(
                peptides=peps, target_gene="EGFR",
                on_credit_exhausted=topup_prompt,
            )

        Because ``fold_batch`` is a single POST (the server dispatches all
        peptide sub-jobs atomically), there is no "in-flight batch" to pause
        — the entire request is rejected before any GPU work begins. The
        callback simply gives the caller a clean hook to top up and retry
        the same batch without restructuring their loop.

        Receptor (pass exactly one)
        ---------------------------
        ``target_gene``
            Gene symbol (e.g. ``"EGFR"``). Resolved server-side via the
            canonical predicted PDB if available, otherwise via the human
            proteome UniProt SQLite.
        ``receptor_pdb``
            Raw PDB content, base64-encoded PDB, or a local path to a .pdb
            file (the SDK reads the file once and forwards the content).
        ``receptor_sequence``
            Raw amino-acid sequence. The server attempts a UniProt match for
            attribution and falls back to the literal sequence labelled with
            ``receptor_name`` (or ``"custom_sequence"``).

        Peptide input
        -------------
        Each entry in ``peptides`` may be a bare AA string OR a FASTA block
        (string starting with ``>``). Multi-record FASTA is supported: every
        record produces one fold job. Duplicates are de-duplicated server-side.

        Examples
        --------
        Gene-based receptor::

            job = client.peptides.fold_batch(
                peptides=["ACDEFGHIK", "WYLKPRSTV", "MNPQRSTAV"],
                target_gene="EGFR",
                diffusion_samples=4,
            )
            result = job.wait()
            for fold in result:
                if fold is not None:
                    print(fold.iptm, fold.ipsae)

        FASTA input::

            with open("candidates.fasta") as fh:
                job = client.peptides.fold_batch(
                    peptides=[fh.read()],
                    target_gene="CD47",
                    diffusion_samples=4,
                )

        Custom PDB receptor (e.g. an Adaptyv-expressed mutant)::

            job = client.peptides.fold_batch(
                peptides=peptide_library,
                receptor_pdb="receptors/my_target.pdb",
                receptor_name="MY_TARGET_v2",
                sampling_steps=100,        # 2x billing multiplier
            )

        Returns
        -------
        :class:`BatchFoldJob`
            ``.batch_id``, ``.jobs`` (per-peptide job metadata),
            ``.total_cost_credits``, and ``.wait()`` for results.
        """
        if self._client is not None:
            self._client._require_feature("predict_structure")
        # ─── SDK hardening pre-flight ──────────────
        # 1) GPU type allowlist (raises LigandAIInvalidConfig if bad)
        # 2) Local concurrency cap (raises LigandAIConcurrencyLimit)
        # 3) Dedupe lookup (returns cached BatchFoldJob if recent match)
        # 4) Credit pre-flight (raises LigandAIInsufficientCredits)
        # 5) Record submission in submitted.db + credit_ledger.db
        # Network POST only happens AFTER all five guards pass.
        from ligandai._hardening import (
            attach_job_id,
            dedupe_lookup_cached,
            enforce_concurrency,
            estimate_fold_batch_credits,
            mark_failed,
            preflight_credits,
            receptor_seq_for_hash,
            record_submission,
            validate_gpu,
            build_fold_params_for_hash,
        )
        from ligandai._dedupe import compute_submission_hash

        canonical_gpu = validate_gpu(gpu)
        submitted_set = self._client.submitted_set if self._client is not None else None
        credit_ledger = self._client.credit_ledger if self._client is not None else None
        api_key_hash = self._client.api_key_hash if self._client is not None else ""

        # Compute submission hash from the SAME inputs that determine result
        # identity. Receptor identity uses sequence > pdb > gene.
        rec_for_hash = receptor_seq_for_hash(
            target_gene=target_gene,
            receptor_sequence=receptor_sequence,
            receptor_pdb=receptor_pdb,
        )
        sub_hash = compute_submission_hash(
            peptide_seq=peptides,
            receptor_seq=rec_for_hash,
            gpu=canonical_gpu,
            params=build_fold_params_for_hash(
                target_gene=target_gene,
                diffusion_samples=diffusion_samples,
                sampling_steps=sampling_steps,
                recycling_steps=recycling_steps,
                step_scale=step_scale,
                msa_enabled=msa_enabled,
                glycosylation=glycosylation,
                template_mode=template_mode,
                extra={"kind": "fold_batch", "receptor_name": receptor_name},
            ),
        )

        # 1) Concurrency cap (only after passing GPU validation; raises if full)
        enforce_concurrency(self._client, submitted_set)

        # 2) Dedupe — return cached batch handle if a recent identical
        #    submission exists and force_resubmit was not requested.
        cached = dedupe_lookup_cached(
            submitted_set,
            submission_hash=sub_hash,
            api_key_hash=api_key_hash,
            force_resubmit=force_resubmit,
        )
        if cached and cached.get("job_id"):
            # Reconstruct a thin BatchFoldJob from the cached batch_id. We
            # cannot recover per-sub-job metadata, but the user can call
            # .wait() / .results via the server status endpoint.
            cached_batch_id = str(cached["job_id"])
            return BatchFoldJob(
                self._transport,
                batch_id=cached_batch_id,
                jobs=[],
                total_cost_credits=int(cached.get("estimated_credits") or 0),
                peptide_count=len(peptides),
                trajectories_per_peptide=int(diffusion_samples),
                receptor={"source": "cached"},
                sampling_steps=int(sampling_steps),
            )

        # 3) Credit pre-flight — estimate locally, compare to balance, raise
        #    LigandAIInsufficientCredits if short (skipped for unlimited tiers).
        estimated = estimate_fold_batch_credits(
            peptide_count=len(peptides),
            trajectories=int(diffusion_samples),
            sampling_steps=int(sampling_steps),
        )
        balance_before, _ = preflight_credits(
            self._client, estimated=estimated, kind="fold_batch",
        )

        body = _build_batch_fold_body(
            peptides=peptides,
            target_gene=target_gene,
            receptor_pdb=receptor_pdb,
            receptor_sequence=receptor_sequence,
            receptor_name=receptor_name,
            diffusion_samples=diffusion_samples,
            sampling_steps=sampling_steps,
            recycling_steps=recycling_steps,
            step_scale=step_scale,
            msa_enabled=msa_enabled,
            glycosylation=glycosylation,
            template_mode=template_mode,
            n_parallel_gpus=n_parallel_gpus,
            session_id=session_id,
            contribute_to_receptordb=contribute_to_receptordb,
        )

        # 4) Record submission BEFORE the POST. If the POST fails we'll mark
        #    the row 'failed' so the next attempt isn't blocked by dedupe.
        record_submission(
            submitted_set,
            credit_ledger,
            submission_hash=sub_hash,
            api_key_hash=api_key_hash,
            kind="fold_batch",
            gpu=canonical_gpu,
            estimated_credits=estimated,
            balance_before=balance_before,
            meta={
                "peptide_count": len(peptides),
                "target_gene": target_gene,
                "receptor_name": receptor_name,
                "trajectories": int(diffusion_samples),
                "sampling_steps": int(sampling_steps),
            },
        )

        # retry-after-topup hook. The submission is a
        # single POST; on HTTP 402 INSUFFICIENT_CREDITS, the SDK invokes the
        # caller-supplied callback and, if it returns truthy, re-POSTs the
        # same body once. One retry is intentional — repeated retries on a
        # still-empty balance would just hammer the server. Callers wanting
        # bounded retry loops should drive that from outside fold_batch.
        try:
            payload = self._transport.request("POST", "/api/v1/folding/predict-batch", json=body) or {}
        except LigandAICreditError as cred_err:
            if on_credit_exhausted is None:
                mark_failed(
                    submitted_set, submission_hash=sub_hash,
                    api_key_hash=api_key_hash, reason="credit_exhausted",
                )
                raise
            try:
                should_retry = bool(on_credit_exhausted(cred_err, body))
            except Exception:
                # Never let a callback exception swallow the original credit
                # error — re-raise the credit error so the caller sees the
                # actionable shortfall/recovery_url metadata.
                mark_failed(
                    submitted_set, submission_hash=sub_hash,
                    api_key_hash=api_key_hash, reason="credit_exhausted_callback_error",
                )
                raise cred_err from None
            if not should_retry:
                mark_failed(
                    submitted_set, submission_hash=sub_hash,
                    api_key_hash=api_key_hash, reason="credit_exhausted_no_retry",
                )
                raise
            payload = self._transport.request("POST", "/api/v1/folding/predict-batch", json=body) or {}
        except Exception as exc:
            mark_failed(
                submitted_set, submission_hash=sub_hash,
                api_key_hash=api_key_hash, reason=type(exc).__name__,
            )
            raise
        batch_id = payload.get("batch_id") or payload.get("batchId") or ""
        if not batch_id:
            mark_failed(
                submitted_set, submission_hash=sub_hash,
                api_key_hash=api_key_hash, reason="server_no_batch_id",
            )
            raise LigandAIError("Server did not return a batch_id for fold_batch", response=payload)
        # 5) Persist the batch_id onto the dedupe row so a repeat call returns
        #    the cached handle.
        attach_job_id(
            submitted_set, submission_hash=sub_hash,
            api_key_hash=api_key_hash, job_id=batch_id,
        )
        return BatchFoldJob(
            self._transport,
            batch_id=batch_id,
            jobs=payload.get("jobs") or [],
            total_cost_credits=int(payload.get("total_cost_credits") or 0),
            peptide_count=int(payload.get("peptide_count") or 0),
            trajectories_per_peptide=int(payload.get("trajectories_per_peptide") or diffusion_samples),
            receptor=payload.get("receptor"),
            sampling_steps=int(payload.get("sampling_steps") or sampling_steps),
        )

    def fold_custom_mutation(
        self,
        gene: str,
        mutations: list[str],
        alias: str | None = None,
    ) -> Job[FoldResult]:
        """``POST /api/ptf/fold-custom-mutation`` — fold a mutated variant."""
        if self._client is not None:
            self._client._require_feature("predict_structure")
        body: dict[str, Any] = {"gene": gene, "mutations": mutations}
        if alias is not None:
            body["alias"] = alias
        payload = self._transport.request("POST", "/api/ptf/fold-custom-mutation", json=body) or {}
        job_id = payload.get("jobId") or payload.get("id") or ""
        if not job_id:
            raise LigandAIError("Server did not return a jobId for custom mutation fold", response=payload)
        return Job(
            self._transport,
            job_id,
            job_type="folding",
            parser=_parse_fold,
            status_path="/api/folding/jobs/{job_id}",
            cancel_path="/api/folding/jobs/{job_id}",
            initial={"id": job_id, "type": "folding", "status": "queued", **payload},
        )

    def continue_folding(
        self,
        session_id: str | None = None,
        gene: str | None = None,
        top_n: int = 25,
        gpu_count: int = 5,
        template_mode: bool = False,
    ) -> Job[GenerationResult]:
        """``POST /api/ptf/parallel/{sid}/continue`` — fold more peptides from an existing session."""
        if not session_id and not gene:
            raise ValueError("Pass session_id= or gene=")
        if not session_id:
            assert gene is not None
            from_session = self._transport.request("GET", f"/api/ptf/sessions/by-gene/{gene}") or {}
            session_id = from_session.get("id")
            if not session_id:
                raise LigandAIError(f"No active session found for gene {gene!r}")
        body = {
            "topN": top_n,
            "gpuCount": gpu_count,
            "templateMode": template_mode,
        }
        payload = (
            self._transport.request("POST", f"/api/ptf/parallel/{session_id}/continue", json=body) or {}
        )
        job_id = payload.get("jobId") or session_id
        return Job(
            self._transport,
            job_id,
            job_type="generation",
            parser=_parse_generation,
            status_path="/api/ptf/parallel/{job_id}/status",
            cancel_path="/api/ptf/parallel/{job_id}/cancel",
            sse_path="/api/ptf/parallel/{job_id}/stream",
            initial={"id": job_id, "type": "generation", "status": "running", **payload},
            result_loader=lambda info: _load_generation_result(
                self._transport,
                info,
                fallback_session_id=session_id,
                fallback_gene=gene,
            ),
        )

    def score_complex(
        self,
        binder_sequence: str,
        target_sequence: str,
        binder_name: str = "binder",
        target_name: str = "target",
        scorer: _DeltaForgeScorer = "auto",
    ) -> Job[DeltaForgeScore]:
        """``POST /api/binder-scoring/fold-and-score`` — submit a fold + DeltaForge scoring job.

        Returns a :class:`Job[DeltaForgeScore]`. Poll with ``.wait()`` and read
        the parsed ``DeltaForgeScore`` from ``.results``.
        """
        body = {
            "binderSequence": binder_sequence,
            "targetSequence": target_sequence,
            "binderName": binder_name,
            "targetName": target_name,
            "scorer": scorer,
        }
        payload = self._transport.request("POST", "/api/binder-scoring/fold-and-score", json=body) or {}
        job_id = payload.get("jobId") or payload.get("id") or ""
        if not job_id:
            raise LigandAIError("Server did not return a jobId", response=payload)

        def parse(data: dict[str, Any]) -> DeltaForgeScore:
            return _parse_deltaforge_score(data)

        return Job(
            self._transport,
            job_id,
            job_type="scoring",
            parser=parse,
            status_path=f"/api/binder-scoring/job/{{job_id}}?scorer={scorer}",
            initial={"id": job_id, "type": "scoring", "status": "submitted"},
        )

    def score_pdb(
        self,
        *,
        pdb_content: str | None = None,
        pdb_file: str | Path | None = None,
        receptor_chains: list[str] | None = None,
        peptide_chain: str | None = None,
        chain_a: str | None = None,
        chain_b: str | None = None,
        scorer: _DeltaForgeScorer = "auto",
        aggregate_method: _DeltaForgeAggregateMethod = "boltzmann_parallel",
        include_features: bool = False,
        include_pae: bool = False,
        fold_ipsae: float | None = None,
        fold_iptm: float | None = None,
        fold_ptm: float | None = None,
        fold_plddt_mean: float | None = None,
        fold_complex_plddt: float | None = None,
        fold_complex_iplddt: float | None = None,
    ) -> DeltaForgeScore:
        """Score a user-provided PDB with DeltaForge.

        Pass either ``pdb_content=`` or ``pdb_file=``. ``receptor_chains`` and
        ``peptide_chain`` are preferred; ``chain_a`` / ``chain_b`` are accepted
        as aliases for single-interface scoring. Optional ``fold_*`` metrics
        are forwarded to the production binder/non-binder gate; affinity
        ``dg``/``kd_nm`` still return independently when the gate calls
        ``not_binder``.

        ``include_pae=True`` asks the server to attach the NxN PAE matrix
        (Angstroms) on :attr:`DeltaForgeScore.pae` when a matching artifact is
        available; otherwise ``pae`` is ``None`` and ``pae_status`` explains why
        (``'pending'`` while a backend pull completes, ``'unavailable'`` when no
        artifact exists for the uploaded structure). Credits are charged only on
        a successful score.
        """
        if not pdb_content and not pdb_file:
            raise ValueError("Pass pdb_content= or pdb_file=")
        if pdb_content and pdb_file:
            raise ValueError("Pass only one of pdb_content= or pdb_file=")
        content = pdb_content if pdb_content is not None else Path(pdb_file).read_text()
        receptors = receptor_chains or ([chain_a] if chain_a else None)
        peptide = peptide_chain or chain_b
        if not receptors or not peptide:
            raise ValueError("Pass receptor_chains= and peptide_chain=, or chain_a= and chain_b=")

        payload = self._transport.request(
            "POST",
            "/api/v1/deltaforge/score-pdb",
            json={
                "pdbContent": content,
                "receptorChains": receptors,
                "peptideChain": peptide,
                "scorer": scorer,
                "aggregateMethod": aggregate_method,
                "includeFeatures": include_features,
                "includePae": include_pae,
                "foldIpsae": fold_ipsae,
                "foldIptm": fold_iptm,
                "foldPtm": fold_ptm,
                "foldPlddtMean": fold_plddt_mean,
                "foldComplexPlddt": fold_complex_plddt,
                "foldComplexIplddt": fold_complex_iplddt,
            },
        ) or {}
        return _parse_deltaforge_score(payload)

    def score_with_ligandiq(
        self,
        session_id: str | None = None,
        gene: str | None = None,
        top_n: int = 20,
    ) -> list[LigandIQScore]:
        """LigandIQ scoring on a session's peptides — synchronous (CPU-only)."""
        if not session_id and not gene:
            raise ValueError("Pass session_id= or gene=")
        if not session_id:
            from_session = self._transport.request("GET", f"/api/ptf/sessions/by-gene/{gene}") or {}
            session_id = from_session.get("id")
            if not session_id:
                raise LigandAIError(f"No active session found for gene {gene!r}")
        body = {"topN": top_n}
        payload = (
            self._transport.request(
                "POST", f"/api/ptf/parallel/{session_id}/ligandiq-score", json=body
            )
            or {}
        )
        items = payload.get("scores") or payload.get("results") or []
        return [LigandIQScore.model_validate(s) for s in items]

    def analyze_solubility(
        self,
        peptides: list[PeptideInput | dict[str, Any] | str],
        gravy_threshold: float = 0.0,
        flag_multi_cys: bool = True,
    ) -> list[SolubilityResult]:
        """``POST /api/peptide-features/solubility`` — GRAVY + cysteine + disulfide check."""
        normalized = [
            (p.model_dump(by_alias=True) if isinstance(p, PeptideInput) else
             {"sequence": p} if isinstance(p, str) else p)
            for p in peptides
        ]
        body = {
            "peptides": normalized,
            "gravyThreshold": gravy_threshold,
            "flagMultiCys": flag_multi_cys,
        }
        payload = (
            self._transport.request("POST", "/api/peptide-features/solubility", json=body)
            or {}
        )
        items = payload.get("results") or payload.get("solubility") or []
        return [SolubilityResult.model_validate(s) for s in items]

    def search(
        self,
        gene: str | None = None,
        classification: str | None = None,
        ipsae_min: float | None = None,
        iptm_min: float | None = None,
        plddt_min: float | None = None,
        kd_max: float | None = None,
        dg_max: float | None = None,
        binder_pct_min: float | None = None,
        length_min: int | None = None,
        length_max: int | None = None,
        is_elite: bool | None = None,
        super_elite: bool | None = None,
        super_elite_affinity: bool | None = None,
        super_elite_thermo: bool | None = None,
        hotspot_residues: list[str] | None = None,
        pocket_residues: list[str] | None = None,
        hotspot_hit: bool | None = None,
        pocket_hit: bool | None = None,
        contact_distance_a: float | None = None,
        stability_grade: list[str] | None = None,
        immuno_grade: list[str] | None = None,
        conformation: str | None = None,
        program_id: int | None = None,
        session_id: str | None = None,
        pdb_id: str | None = None,
        sort: str = "ipsae",
        order: str = "desc",
        min_ipsae: float | None = None,  # legacy alias
        limit: int = 20,
        offset: int = 0,
    ) -> list[Peptide]:
        """``GET /api/v1/peptides/search`` — rich cross-program peptide search.

        Supports every criterion the workspace UI exposes plus a few extras
        for SDK-driven workflows. All filters AND-combine.

        Args:
            gene: Gene symbol filter (uppercased server-side).
            ipsae_min / iptm_min / plddt_min: Minimum Boltz-2 quality scores.
            kd_max: Maximum predicted Kd in Molar (e.g. ``1e-8`` = 10 nM).
            dg_max: Maximum predicted ΔG in kcal/mol (negative is better; pass
                ``-8.0`` for "ΔG ≤ -8 kcal/mol").
            binder_pct_min: Minimum DeltaForge binder probability (0..1).
            length_min / length_max: Peptide length range in residues.
            is_elite: Server-flagged elite (iPSAE ≥ 0.80 by default).
            super_elite: Proteina-Complexa structural-confidence gate
                (bioRxiv v27): iPSAE ≥ 0.67 AND iPTM ≥ 0.80 AND
                pLDDT ≥ 88 (0–100 scale; null passes). The 3-metric
                structural gate. Use this for the headline
                "super-elite" count.
            super_elite_affinity: Affinity super-elite — structural gate
                above PLUS predicted Kd < 100 nM (DeltaForge). The
                synthesis-priority subset. Reported as a SEPARATE
                bucket from the structural gate.
            super_elite_thermo: Deprecated alias for ``super_elite_affinity``.
            hotspot_residues: List of ``"chain:resi"`` strings (PDB
                numbering) for the residues you wanted the peptide to
                contact, e.g. ``["A:60", "A:62"]``.
            pocket_residues: Same shape, for the surrounding pocket.
            hotspot_hit: Require the peptide's fold to contact AT LEAST one
                listed hotspot residue within ``contact_distance_a`` Å.
            pocket_hit: Require contact with the hotspot OR pocket set.
            contact_distance_a: Heavy-atom cutoff for "hit" (default 5.0 Å).
            stability_grade / immuno_grade: List of acceptable grades
                (e.g. ``["A", "B"]``).
            conformation: Exact conformation string filter.
            program_id / session_id / pdb_id: Scope filters.
            sort: Sort key — one of ``ipsae`` | ``iptm`` | ``plddt`` |
                ``kd`` | ``dg`` | ``length`` | ``created_at``.
            order: ``"asc"`` or ``"desc"`` (default ``"desc"``).
            limit: Page size (max 200).
            offset: Pagination offset.

        Returns:
            List of peptides matching all criteria, sorted by ``sort``/``order``.
            Each peptide includes ``hotspot_contacts`` / ``pocket_contacts``
            arrays when residue criteria were specified, with per-residue
            heavy-atom distances.
        """
        if min_ipsae is not None and ipsae_min is None:
            ipsae_min = min_ipsae
        if super_elite_thermo is not None:
            warnings.warn(
                "super_elite_thermo is deprecated; use super_elite_affinity instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            if super_elite_affinity is None:
                super_elite_affinity = super_elite_thermo
        params: dict[str, Any] = {"limit": limit, "offset": offset, "sort": sort, "order": order}
        if gene is not None: params["gene"] = gene.upper()
        if classification is not None: params["classification"] = classification
        if ipsae_min is not None: params["ipsae_min"] = ipsae_min
        if iptm_min is not None: params["iptm_min"] = iptm_min
        if plddt_min is not None: params["plddt_min"] = plddt_min
        if kd_max is not None: params["kd_max"] = kd_max
        if dg_max is not None: params["dg_max"] = dg_max
        if binder_pct_min is not None: params["binder_pct_min"] = binder_pct_min
        if length_min is not None: params["length_min"] = length_min
        if length_max is not None: params["length_max"] = length_max
        if is_elite is not None: params["is_elite"] = "true" if is_elite else "false"
        if super_elite is not None: params["super_elite"] = "true" if super_elite else "false"
        if super_elite_affinity is not None: params["super_elite_affinity"] = "true" if super_elite_affinity else "false"
        if hotspot_hit is not None: params["hotspot_hit"] = "true" if hotspot_hit else "false"
        if pocket_hit is not None: params["pocket_hit"] = "true" if pocket_hit else "false"
        if hotspot_residues: params["hotspot_residues"] = ",".join(hotspot_residues)
        if pocket_residues: params["pocket_residues"] = ",".join(pocket_residues)
        if contact_distance_a is not None: params["contact_distance_a"] = contact_distance_a
        if stability_grade: params["stability_grade"] = ",".join(stability_grade)
        if immuno_grade: params["immuno_grade"] = ",".join(immuno_grade)
        if conformation is not None: params["conformation"] = conformation
        if program_id is not None: params["program_id"] = program_id
        if session_id is not None: params["session_id"] = session_id
        if pdb_id is not None: params["pdb_id"] = pdb_id.upper()

        payload = self._transport.request(
            "GET", "/api/v1/peptides/search", params=params
        ) or {}
        items = payload.get("peptides", []) if isinstance(payload, dict) else (payload or [])
        return [Peptide.model_validate(p) for p in items]

    def fill_until(
        self,
        gene: str,
        target_count: int = 10,
        criteria: dict[str, Any] | None = None,
        batch_size: int = 100,
        max_iterations: int = 3,
        budget_credits_max: int = 50000,
        mode: str = "plan",
    ) -> dict[str, Any]:
        """``POST /api/v1/peptides/auto-generate-until`` — generate-and-fold loop.

        Plan or kick off a generate-and-fold loop until ``target_count``
        peptides match ``criteria``. ``criteria`` accepts the same keys as
        :meth:`search` (e.g. ``ipsae_min``, ``kd_max``, ``hotspot_residues``,
        ``hotspot_hit``, ``super_elite``).

        With ``mode="plan"`` (default) the server returns:
            * ``current_passing_count`` — peptides already in the DB that match
            * ``remaining`` — how many more you need
            * ``plan.batches_recommended`` / ``est_credits`` / ``est_minutes``

        With ``mode="start"`` the server validates the plan against your
        ``budget_credits_max`` and returns a ``next_action`` describing the
        exact /api/ptf/parallel/generate calls to make. The SDK does NOT
        currently spawn the loop server-side — you wrap it in your own
        ``while`` so you can checkpoint progress and inspect intermediate
        peptides.

        Example (loop client-side until 25 super-elite peptides found):

            crit = {"super_elite": True, "hotspot_residues": ["A:60","A:62"], "hotspot_hit": True}
            for _ in range(5):
                plan = client.peptides.fill_until("BMPR1A", target_count=25, criteria=crit, mode="plan")
                if plan["remaining"] == 0:
                    break
                client.peptides.generate(gene="BMPR1A", num_peptides=plan["plan"]["batch_size"])
                client.peptides.wait_for_session(...)  # poll generation status
            results = client.peptides.search(gene="BMPR1A", **crit, limit=25)
        """
        body = {
            "gene": gene.upper() if gene else None,
            "target_count": target_count,
            "criteria": criteria or {},
            "batch_size": batch_size,
            "max_iterations": max_iterations,
            "budget_credits_max": budget_credits_max,
            "mode": mode,
        }
        return self._transport.request(
            "POST", "/api/v1/peptides/auto-generate-until", json=body
        ) or {}

    def pocket_for_hotspots(
        self,
        pdb_id: str | None = None,
        hotspots: list[str] | None = None,
        radius_a: float = 8.0,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """``GET /api/v1/structures/{pdb_id}/pocket`` — pocket residues
        within ``radius_a`` Å of one or more hotspots.

        Args:
            pdb_id: RCSB PDB code (e.g. ``"9MIR"``). Required unless
                ``session_id`` is provided.
            hotspots: ``["A:60", "A:62"]`` — chain:residue PDB numbering.
            radius_a: Heavy-atom radius for pocket inclusion (default 8.0).
            session_id: Compute the pocket from a fold session's structure
                instead of the canonical PDB.

        Returns:
            ``{ pdb_id, hotspots, radius_a, pocket_residues, n_pocket_residues }``.
            Each pocket residue is ``{ chain, residue (PDB), resname, distance_a }``.
        """
        if not pdb_id and not session_id:
            raise ValueError("pocket_for_hotspots requires either pdb_id or session_id")
        if not hotspots:
            raise ValueError("pocket_for_hotspots requires hotspots, e.g. ['A:60','A:62']")
        params: dict[str, Any] = {"hotspots": ",".join(hotspots), "radius_a": radius_a}
        if session_id:
            params["session_id"] = session_id
        path = f"/api/v1/structures/{(pdb_id or '').upper()}/pocket"
        return self._transport.request("GET", path, params=params) or {}

    def search_by_pocket(
        self,
        gene: str,
        chain: str | None = None,
        start_residue: int | None = None,
        end_residue: int | None = None,
        targeted_only: bool = True,
    ) -> list[Peptide]:
        """``GET /api/ptf/peptides/by-pocket`` — find prior peptides targeting a pocket."""
        params: dict[str, Any] = {"gene": gene, "targeted_only": targeted_only}
        if chain is not None:
            params["chain"] = chain
        if start_residue is not None:
            params["start_residue"] = start_residue
        if end_residue is not None:
            params["end_residue"] = end_residue
        payload = self._transport.request("GET", "/api/ptf/peptides/by-pocket", params=params) or []
        items = payload if isinstance(payload, list) else payload.get("peptides", [])
        return [Peptide.model_validate(p) for p in items]

    def get_elite(
        self,
        session_id: str | None = None,
        gene: str | None = None,
    ) -> list[Peptide]:
        """``GET /api/ptf/parallel/{sid}/elite`` — elite peptides for a session."""
        if not session_id and not gene:
            raise ValueError("Pass session_id= or gene=")
        if not session_id:
            from_session = self._transport.request("GET", f"/api/ptf/sessions/by-gene/{gene}") or {}
            session_id = from_session.get("id")
            if not session_id:
                raise LigandAIError(f"No active session for gene {gene!r}")
        payload = self._transport.request("GET", f"/api/ptf/parallel/{session_id}/elite") or []
        items = payload if isinstance(payload, list) else payload.get("peptides", [])
        return [Peptide.model_validate(p) for p in items]

    # ------------------------------------------------------------------
    # v0.2.0 surface — paid-only /api/v1/peptides/*
    # ------------------------------------------------------------------

    def by_gene(
        self,
        genes: list[str] | None = None,
        min_ipsae: float | None = None,
        program_id: int | None = None,
        project_id: int | None = None,
        since: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[GeneSummary]:
        """``GET /api/v1/peptides/by-gene`` — gene-level peptide aggregation.

        Aggregates peptide stats per gene across **all of the caller's
        sessions and programs**. Use this to answer "what binders do I have
        for gene X?" — one row per gene with folded counts, best scores,
        program/session coverage. Follow up with :meth:`list` for the
        actual peptide rows.

        **Auth (changed v0.5.3):** Open to ALL tiers including free. Free
        users see aggregate counts only — sequences and structures from
        downstream :meth:`list` / :meth:`get` are masked (first 4 AA +
        ``********``) and PDBs are returned as polyalanine.

        Args:
            genes: Optional whitelist (case-insensitive). When omitted, all
                genes the caller has folds for are returned.
            min_ipsae: Filter aggregation to folds with iPSAE ≥ this threshold.
                ``foldedCount`` reflects only folds meeting the bar.
            program_id: Restrict to one program (Layer-4 program DB id).
            project_id: Restrict to one project.
            since: Only count folds at or after this timestamp.
            limit: Page size (max 200; server caps).
            offset: Pagination offset.

        Returns:
            List of :class:`~ligandai.types.GeneSummary` rows, sorted by
            ``last_activity_at`` descending. To compute total pages, divide
            the server's ``total`` (in the raw response, not exposed here)
            by your ``limit``.
        """
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if genes:
            params["genes"] = ",".join(g.upper() for g in genes if g)
        if min_ipsae is not None:
            params["minIpsae"] = min_ipsae
        if program_id is not None:
            params["programId"] = program_id
        if project_id is not None:
            params["projectId"] = project_id
        if since is not None:
            params["since"] = since.isoformat()
        payload = self._transport.request("GET", "/api/v1/peptides/by-gene", params=params) or {}
        rows = payload.get("rows", []) if isinstance(payload, dict) else []
        return [GeneSummary.model_validate(r) for r in rows]

    def by_pdb(
        self,
        pdb: str | list[str],
        min_ipsae: float | None = None,
        since: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """``GET /api/v1/peptides/by-pdb`` — peptide aggregation pivoted by target PDB code.

        Mirror of :meth:`by_gene` for users whose generation requests
        targeted a specific PDB ID instead of a gene symbol (common for
        custom-uploaded structures or multi-chain complexes — e.g.
        ``"9MIR"`` for the BMPR1A–RGMB heteromer). Returns one row per
        ``(pdb_code, gene)`` combination with session count + total
        peptides generated + activity timestamps.

        **Auth:** Open to ALL tiers (free included). Aggregate counts only;
        sequences and structures still tier-gated through
        :meth:`list` / :meth:`get` / :meth:`download_pdb`.

        Args:
            pdb: One PDB code or a list of codes (case-insensitive).
            min_ipsae: Server-side iPSAE threshold (currently informational
                only — the per-row endpoints carry the real iPSAE filter).
            since: Only sessions newer than this timestamp.
            limit: Page size (max 200).
            offset: Pagination offset.

        Returns:
            List of dicts with ``pdbCode``, ``gene``, ``sessions``,
            ``peptidesGenerated``, ``firstActivityAt``, ``lastActivityAt``.

        Example:
            >>> rows = client.peptides.by_pdb("9MIR")
            >>> for r in rows:
            ...     print(r["pdbCode"], r["gene"], r["sessions"], r["peptidesGenerated"])
            9MIR BMPR1A 3 150
        """
        codes = [pdb] if isinstance(pdb, str) else list(pdb)
        params: dict[str, Any] = {
            "pdb": ",".join(c.upper() for c in codes if c),
            "limit": limit,
            "offset": offset,
        }
        if min_ipsae is not None:
            params["min_ipsae"] = min_ipsae
        if since is not None:
            params["since"] = since.isoformat()
        payload = self._transport.request("GET", "/api/v1/peptides/by-pdb", params=params) or {}
        return payload.get("rows", []) if isinstance(payload, dict) else []

    def list(
        self,
        gene_or_program_id: str | int | None = None,
        *,
        gene: str | None = None,
        program_id: int | None = None,
        min_ipsae: float | None = None,
        min_iptm: float | None = None,
        max_kd: float | None = None,
        include_unfolded: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Peptide]:
        """List peptide rows. v0.5.0: now supports both ``gene`` and ``program_id``.

        Backwards-compatible:
          * ``client.peptides.list("EGFR")`` — gene mode (legacy)
          * ``client.peptides.list(gene="EGFR")`` — explicit gene (legacy)
          * ``client.peptides.list(42)`` — program_id mode (NEW)
          * ``client.peptides.list(program_id=42, min_ipsae=0.8)`` — explicit (NEW)
          * ``client.peptides.list(program_id=42, gene="EGFR")`` — both (NEW)

        a user's #1 bug: ``client.peptides.list(program_id)`` raised
        ``TypeError`` because the signature only took ``gene``. v0.5.0 accepts
        either by detecting the positional arg's type (str → gene, int →
        program_id) and exposes both as keyword args.

        Args:
            gene_or_program_id: Positional shortcut. ``str`` = gene symbol;
                ``int`` = program DB id. Pass ``None`` (the default) to scope
                across all programs (paid tier only — free keys must scope to
                a program or gene to limit response size).
            gene: Gene symbol (case-insensitive; upper-cased server-side).
            program_id: Restrict to one program (Layer-4 program DB id).
            min_ipsae: Filter to folds with iPSAE ≥ this threshold.
            min_iptm: Filter to folds with ipTM ≥ this threshold.
            max_kd: Filter to folds with predicted Kd ≤ this value (M).
            include_unfolded: Reserved for legacy compatibility. Has no effect
                on the new ``/v1/peptides/list`` endpoint (folded only).
            limit: Page size (max 200).
            offset: Pagination offset.

        Returns:
            List of :class:`~ligandai.types.Peptide` rows. When the caller is
            on the free tier, ``sequence`` is truncated to the first 10 amino
            acids + ``********``; check the response's ``_tier_redacted``
            metadata (or :class:`~ligandai.errors.LigandAIUpgradeRequired`).
        """
        # Reconcile positional arg with explicit keywords
        if gene_or_program_id is not None:
            if isinstance(gene_or_program_id, str):
                if gene is not None and gene != gene_or_program_id:
                    raise ValueError("conflicting gene values: pass either positionally or by keyword, not both")
                gene = gene_or_program_id
            elif isinstance(gene_or_program_id, int):
                if program_id is not None and program_id != gene_or_program_id:
                    raise ValueError("conflicting program_id values: pass either positionally or by keyword, not both")
                program_id = gene_or_program_id
            else:
                raise TypeError(
                    f"list() positional arg must be a gene symbol (str) or program_id (int); "
                    f"got {type(gene_or_program_id).__name__}"
                )

        # New /v1/peptides/list endpoint when program_id, min_iptm, or max_kd is supplied
        # OR when gene is supplied (always preferred — returns the richer schema)
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if program_id is not None:
            params["program_id"] = program_id
        if gene is not None:
            if not gene.strip():
                raise ValueError("gene must be a non-empty string")
            params["gene"] = gene.upper()
        if min_ipsae is not None:
            params["min_ipsae"] = min_ipsae
        if min_iptm is not None:
            params["min_iptm"] = min_iptm
        if max_kd is not None:
            params["max_kd"] = max_kd

        payload = self._transport.request(
            "GET", "/api/v1/peptides/list", params=params
        ) or {}
        items = payload.get("peptides", []) if isinstance(payload, dict) else (payload or [])
        return [Peptide.model_validate(p) for p in items]

    def list_by_program(
        self,
        program_id: int,
        *,
        min_ipsae: float | None = None,
        min_iptm: float | None = None,
        max_kd: float | None = None,
        gene: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Peptide]:
        """``GET /api/v1/peptides/list?program_id=X`` — list peptides in a program.

        Convenience method for the common a common query: "give me
        all peptides in program 42, optionally filtered by score thresholds."

        Args:
            program_id: Program DB id.
            min_ipsae: Filter to folds with iPSAE ≥ this threshold.
            min_iptm: Filter to folds with ipTM ≥ this threshold.
            max_kd: Filter to folds with predicted Kd ≤ this value (M).
            gene: Optional gene filter within the program.
            limit: Page size (max 200).
            offset: Pagination offset.
        """
        return self.list(
            program_id=program_id,
            gene=gene,
            min_ipsae=min_ipsae,
            min_iptm=min_iptm,
            max_kd=max_kd,
            limit=limit,
            offset=offset,
        )

    def get(
        self,
        peptide_id: int | str,
        include: list[_IncludeField] | None = None,
    ) -> PeptideDetail:
        """``GET /api/v1/peptides/:id`` — single-peptide detail.

        Default response is "thin" — sequence + scores + metadata, no heavy
        fields. Heavy fields are gated behind ``include=`` to keep typical
        reads fast.

        **Auth:** Paid tiers only. Free keys raise
        :class:`~ligandai.errors.LigandAIPaidTierRequired`.

        Args:
            peptide_id: ``ptf_fold_results.id`` (positive integer). Strings
                are accepted and parsed.
            include: Optional list of heavy fields to fetch:

                - ``"pocket_features"`` adds ``pocket_features_48_dim`` and
                  ``pocket_features_metadata``.
                - ``"interface"`` adds ``peptide_per_receptor`` and
                  ``disulfide_analysis``.
                - ``"pdb"`` adds ``pdb_content`` (5-50KB).

                Unknown values raise ``ValueError`` client-side; the server
                also rejects them with HTTP 400.
        """
        if self._client is not None:
            self._client._require_paid_tier()
        try:
            id_int = int(peptide_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"peptide_id must be a positive integer (got {peptide_id!r})"
            ) from exc
        if id_int <= 0:
            raise ValueError(f"peptide_id must be > 0 (got {id_int})")

        params: dict[str, Any] = {}
        if include:
            unknown = [v for v in include if v not in _ALLOWED_INCLUDE]
            if unknown:
                raise ValueError(
                    f"Unknown include value(s): {unknown}. "
                    f"Allowed: {sorted(_ALLOWED_INCLUDE)}"
                )
            params["include"] = ",".join(include)

        payload = self._transport.request(
            "GET", f"/api/v1/peptides/{id_int}", params=params
        ) or {}
        return PeptideDetail.model_validate(payload)


    def estimate_cost(
        self,
        *,
        num_peptides: int,
        auto_fold: bool = True,
        fold_top_n: int | None = None,
        fold_trajectories: int = 4,
    ) -> CostEstimate:
        """``GET /api/billing/estimate`` — estimate the credit cost of a generation + folding job.

        Args:
            num_peptides: Number of peptides to generate.
            auto_fold: Whether folding will run automatically (default True).
            fold_top_n: Cap on peptides folded; when None the server uses its default.
            fold_trajectories: Diffusion samples per fold (default 4, matches Boltz-2 default).

        Returns:
            :class:`~ligandai.types.CostEstimate` with ``credits`` (int),
            ``cost_usd`` (float), and ``breakdown`` dict by phase
            (generation, folding, scoring).
        """
        params: dict[str, Any] = {
            "num_peptides": num_peptides,
            "auto_fold": auto_fold,
            "fold_trajectories": fold_trajectories,
        }
        if fold_top_n is not None:
            params["top_n"] = fold_top_n
        payload = (
            self._transport.request("GET", "/api/billing/estimate", params=params) or {}
        )
        return CostEstimate.model_validate(payload)

    def download_pdb(
        self,
        peptide_id: int | str,
        save_to: str | None = None,
    ) -> bytes:
        """``GET /api/v1/structures/{id}/pdb`` — download raw PDB content.

        Convenience helper that uses the ``pdb_url`` returned on every peptide
        in :meth:`list`, :meth:`search`, and :meth:`get`. Resolves to the same
        endpoint as ``client.structures.get_pdb(peptide_id)``.

        Args:
            peptide_id: ``ptf_fold_results.id`` (positive integer). Strings
                are accepted and parsed.
            save_to: Optional file path; when provided, writes the PDB bytes
                to disk and returns them. Parent directory must already
                exist.

        Returns:
            Raw PDB content as bytes.

        Tier behavior: free-tier keys receive a side-chain-scrambled PDB
        with a ``LIGANDAI FREE TIER — SIDE CHAIN IDENTITY REDACTED`` REMARK
        header. Paid tiers receive the original. The peptide row's
        ``_pdb_masked: True`` flag indicates when the response will be
        scrambled.
        """
        try:
            id_int = int(peptide_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"peptide_id must be a positive integer (got {peptide_id!r})"
            ) from exc
        path = f"/api/v1/structures/{id_int}/pdb"
        # Transport returns the raw bytes when the response is non-JSON.
        body = self._transport.request("GET", path)
        if isinstance(body, dict) and "pdb_content" in body:
            content = body.get("pdb_content") or ""
            data = content.encode("utf-8") if isinstance(content, str) else content
        elif isinstance(body, (bytes, bytearray)):
            data = bytes(body)
        elif isinstance(body, str):
            data = body.encode("utf-8")
        else:
            raise RuntimeError(f"Unexpected /v1/structures/{id_int}/pdb response shape: {type(body).__name__}")
        if save_to:
            import os
            os.makedirs(os.path.dirname(os.path.abspath(save_to)) or ".", exist_ok=True)
            with open(save_to, "wb") as f:
                f.write(data)
        return data


# -- Async resource ---------------------------------------------------------


class AsyncPeptides(AsyncResource):
    async def generate(
        self,
        gene: str,
        num_peptides: int | None = None,
        length_range: tuple[int, int] = (20, 70),
        target_residues: list[ResidueRange] | None = None,
        target_chains: list[str] | None = None,
        fold_partners: _FoldPartnerMode | list[str] | None = None,
        # When target_residues are supplied, the SDK auto-switches the strategy
        # to "pocket_targeted" unless you override here. Pass "full_surface"
        # explicitly to keep the residues as soft hints only.
        targeting_strategy: _TargetingStrategy | None = None,
        # Hotspot pocket expansion radius in Angstroms. When target_residues
        # are provided, the server includes every residue within this radius
        # of any listed hotspot atom in the design pocket. This is the right
        # default for "design against residue X" — the peptide needs the
        # surrounding shell to make contacts, not just X itself. Set to 0 to
        # use the literal residues only.
        pocket_expansion_radius_a: float | None = 6.0,
        auto_fold: bool = True,
        top_n_fold: int | None = None,
        ec_domain_trimming: bool = True,
        deimmunize_mode: bool = False,
        variant_id: int | None = None,
        gen_gpus: int = 1,
        fold_gpus: int = 1,
        program_id: int | None = None,
        cysteine_mode: _CysteineMode = "disulfide_only",
        quality_guided: bool = False,
        quality_guidance_scale: float = 1.0,
        immunogenicity: bool = False,
        immuno_strength: float = 2.0,
        immuno_modules: dict[str, bool] | None = None,
        serum_stability: bool = False,
        stability_strength: float = 2.0,
        stability_mode: _StabilityMode = "resist",
        stability_modules: dict[str, bool] | None = None,
        halflife: _HalflifeTarget | None = None,
        halflife_strength: float = 2.0,
        charge_mode: _ChargeMode | None = None,
        charge_value: float | None = None,
        charge_min: float | None = None,
        charge_max: float | None = None,
        min_solubility: float | None = None,
        cyclic_mode: _CyclicMode | None = None,
        cyclic_strength: float = 2.0,
        strict_recombinant: bool = True,
        dual_fold_viz: bool = False,
        folding_mode: str | None = None,
        # Default fold strategy: rank candidates by composite (LigandIQ × predicted
        # iPTM) before folding. The top peptides by this score get fold GPUs first
        # so credits go to the most promising designs. Override to ``"distributed"``
        # for round-robin across targets, ``"consolidated"`` for sequential
        # single-target folding, or pass ``fold_strategy=None`` to defer to server.
        fold_strategy: str | None = "quality_ranked",
        folding_conformations: str | list[str] | None = None,
        max_folds_per_target: int | None = None,
        enable_expansion: bool | None = None,
        auto_conformation_expansion: bool | None = None,
        clash_resolution_enabled: bool | None = None,
        md_relaxation_enabled: bool | None = None,
        num_trajectories: int | None = None,
        sampling_steps: int | None = None,
        glycosylation_enabled: bool | None = None,
        segment_config: SegmentConfig | dict | None = None,
        pdc_config: PdcConfig | dict | None = None,
        ec_trimming_config: EcTrimmingConfig | dict | None = None,
        **extra: Any,
    ) -> AsyncJob[GenerationResult]:
        """Async variant of :meth:`Peptides.generate`. See that method for full docs."""
        if self._client is not None:
            self._client._require_feature("generate_peptides")
            if _requests_advanced_guidance(
                immunogenicity=immunogenicity,
                immuno_modules=immuno_modules,
                serum_stability=serum_stability,
                stability_modules=stability_modules,
                halflife=halflife,
                cyclic_mode=cyclic_mode,
                extra=extra,
            ):
                self._client._require_feature("advanced_guidance")
        # v0.2.0: cys/cyclic controls passed via extra={...} are deprecated;
        # use the typed kwargs above. Hard-rejected in v0.3.0.
        _warn_deprecated_cys_extra(extra)
        body = _generation_body(
            gene=gene,
            num_peptides=num_peptides,
            length_range=length_range,
            target_residues=target_residues,
            target_chains=target_chains,
            fold_partners=fold_partners,
            targeting_strategy=targeting_strategy,
            pocket_expansion_radius_a=pocket_expansion_radius_a,
            auto_fold=auto_fold,
            top_n_fold=top_n_fold,
            ec_domain_trimming=ec_domain_trimming,
            deimmunize_mode=deimmunize_mode,
            variant_id=variant_id,
            gen_gpus=gen_gpus,
            fold_gpus=fold_gpus,
            program_id=program_id,
            cysteine_mode=cysteine_mode,
            quality_guided=quality_guided,
            quality_guidance_scale=quality_guidance_scale,
            immunogenicity=immunogenicity,
            immuno_strength=immuno_strength,
            immuno_modules=immuno_modules,
            serum_stability=serum_stability,
            stability_strength=stability_strength,
            stability_mode=stability_mode,
            stability_modules=stability_modules,
            halflife=halflife,
            halflife_strength=halflife_strength,
            charge_mode=charge_mode,
            charge_value=charge_value,
            charge_min=charge_min,
            charge_max=charge_max,
            min_solubility=min_solubility,
            cyclic_mode=cyclic_mode,
            cyclic_strength=cyclic_strength,
            strict_recombinant=strict_recombinant,
            dual_fold_viz=dual_fold_viz,
            folding_mode=folding_mode,
            fold_strategy=fold_strategy,
            folding_conformations=folding_conformations,
            max_folds_per_target=max_folds_per_target,
            enable_expansion=enable_expansion,
            auto_conformation_expansion=auto_conformation_expansion,
            clash_resolution_enabled=clash_resolution_enabled,
            md_relaxation_enabled=md_relaxation_enabled,
            num_trajectories=num_trajectories,
            sampling_steps=sampling_steps,
            glycosylation_enabled=glycosylation_enabled,
            segment_config=segment_config,
            pdc_config=pdc_config,
            ec_trimming_config=ec_trimming_config,
            extra=extra,
        )
        payload = await self._transport.request("POST", "/api/ptf/parallel/generate", json=body) or {}
        job_id = payload.get("sessionId") or payload.get("jobId") or payload.get("session_id") or ""
        if not job_id:
            raise LigandAIError("Server did not return a session_id/jobId", response=payload)
        return AsyncJob(
            self._transport,
            job_id,
            job_type="generation",
            parser=_parse_generation,
            status_path="/api/ptf/parallel/{job_id}/status",
            cancel_path="/api/ptf/parallel/{job_id}/cancel",
            sse_path="/api/ptf/parallel/{job_id}/stream",
            initial={"id": job_id, "type": "generation", "status": "queued", **payload},
            result_loader=lambda info: _aload_generation_result(
                self._transport,
                info,
                fallback_session_id=job_id,
                fallback_gene=gene,
            ),
        )

    async def fold(
        self,
        sequences: list[Sequence | str | dict[str, Any]],
        target_gene: str | None = None,
        auto_score: bool = True,
        template_mode: bool = False,
        msa_enabled: bool | None = None,
        glycosylation: bool | None = None,
        pegylation: bool | None = None,
        gpu_count: int = 1,
        diffusion_samples: int = 1,
        sampling_steps: int | None = None,
        recycling_steps: int | None = None,
        num_trajectories: int | None = None,
        step_scale: float | None = None,
        contribute_to_receptordb: bool | None = None,
        n_parallel_gpus: int | None = None,
        # Hardening kwargs
        gpu: str | None = None,
        force_resubmit: bool = False,
        # ESMFold2 + approach selection
        fold_approach: str | None = None,
        num_seeds: int | None = None,
        num_recycles: int | None = None,
        return_pdb: bool | None = None,
    ) -> AsyncJob[FoldResult]:
        """Async sibling of :meth:`Peptides.fold`. See :meth:`Peptides.fold` for
        ``n_parallel_gpus`` semantics and the GPU / dedupe / credit pre-flight
        hardening (gpu='b200_plus' only, 24h local dedupe).
        """
        if self._client is not None:
            self._client._require_feature("predict_structure")
        # Hardening pre-flight
        from ligandai._hardening import (
            attach_job_id,
            build_fold_params_for_hash,
            dedupe_lookup_cached,
            enforce_concurrency,
            estimate_single_fold_credits,
            mark_failed,
            preflight_credits,
            receptor_seq_for_hash,
            record_submission,
            validate_gpu,
        )
        from ligandai._dedupe import compute_submission_hash

        canonical_gpu = validate_gpu(gpu)
        submitted_set = self._client.submitted_set if self._client is not None else None
        credit_ledger = self._client.credit_ledger if self._client is not None else None
        api_key_hash = self._client.api_key_hash if self._client is not None else ""

        normalized_seqs = [_norm_seq(s) for s in sequences]
        seq_strings = [str(s.get("sequence", "")) for s in normalized_seqs]
        if len(seq_strings) >= 2:
            rec_for_hash = receptor_seq_for_hash(
                target_gene=target_gene,
                receptor_sequence=seq_strings[0],
                receptor_pdb=None,
            )
            peptide_for_hash: str | list[str] = "|".join(seq_strings[1:])
        else:
            rec_for_hash = receptor_seq_for_hash(
                target_gene=target_gene,
                receptor_sequence=seq_strings[0] if seq_strings else None,
                receptor_pdb=None,
            )
            peptide_for_hash = ""

        sub_hash = compute_submission_hash(
            peptide_seq=peptide_for_hash,
            receptor_seq=rec_for_hash,
            gpu=canonical_gpu,
            params=build_fold_params_for_hash(
                target_gene=target_gene,
                diffusion_samples=(
                    num_trajectories if num_trajectories is not None
                    else diffusion_samples
                ),
                sampling_steps=sampling_steps,
                recycling_steps=recycling_steps,
                step_scale=step_scale,
                msa_enabled=msa_enabled,
                glycosylation=glycosylation,
                template_mode=template_mode,
                extra={"kind": "fold", "pegylation": bool(pegylation) if pegylation is not None else None},
            ),
        )

        enforce_concurrency(self._client, submitted_set)

        cached = dedupe_lookup_cached(
            submitted_set,
            submission_hash=sub_hash,
            api_key_hash=api_key_hash,
            force_resubmit=force_resubmit,
        )
        if cached and cached.get("job_id"):
            cached_job_id = str(cached["job_id"])
            return AsyncJob(
                self._transport,
                cached_job_id,
                job_type="folding",
                parser=_parse_fold,
                status_path="/api/folding/jobs/{job_id}",
                cancel_path="/api/folding/jobs/{job_id}",
                sse_path="/api/folding/jobs/{job_id}/logs/stream",
                initial={"id": cached_job_id, "type": "folding", "status": "cached"},
            )

        effective_trajectories = int(
            num_trajectories if num_trajectories is not None else diffusion_samples
        )
        estimated = estimate_single_fold_credits(
            trajectories=effective_trajectories,
            sampling_steps=sampling_steps,
        )
        balance_before, _ = preflight_credits(
            self._client, estimated=estimated, kind="fold",
        )

        body = _fold_body(
            sequences,
            auto_score=auto_score,
            template_mode=template_mode,
            msa_enabled=msa_enabled,
            target_gene=target_gene,
            glycosylation=glycosylation,
            pegylation=pegylation,
            gpu_count=gpu_count,
            diffusion_samples=diffusion_samples,
            sampling_steps=sampling_steps,
            recycling_steps=recycling_steps,
            num_trajectories=num_trajectories,
            step_scale=step_scale,
            contribute_to_receptordb=contribute_to_receptordb,
            n_parallel_gpus=n_parallel_gpus,
            fold_approach=fold_approach,
            num_seeds=num_seeds,
            num_recycles=num_recycles,
            return_pdb=return_pdb,
        )

        record_submission(
            submitted_set,
            credit_ledger,
            submission_hash=sub_hash,
            api_key_hash=api_key_hash,
            kind="fold",
            gpu=canonical_gpu,
            estimated_credits=estimated,
            balance_before=balance_before,
            meta={"target_gene": target_gene, "chains": len(seq_strings)},
        )

        try:
            payload = await self._transport.request("POST", "/api/folding/predict", json=body) or {}
        except Exception as exc:
            mark_failed(
                submitted_set, submission_hash=sub_hash,
                api_key_hash=api_key_hash, reason=type(exc).__name__,
            )
            raise

        job_id = payload.get("jobId") or payload.get("id") or ""
        if not job_id:
            mark_failed(
                submitted_set, submission_hash=sub_hash,
                api_key_hash=api_key_hash, reason="server_no_job_id",
            )
            raise LigandAIError("Server did not return a jobId for fold", response=payload)
        attach_job_id(
            submitted_set, submission_hash=sub_hash,
            api_key_hash=api_key_hash, job_id=job_id,
        )
        return AsyncJob(
            self._transport,
            job_id,
            job_type="folding",
            parser=_parse_fold,
            status_path="/api/folding/jobs/{job_id}",
            cancel_path="/api/folding/jobs/{job_id}",
            sse_path="/api/folding/jobs/{job_id}/logs/stream",
            initial={"id": job_id, "type": "folding", "status": "queued", **payload},
        )

    async def fold_batch(
        self,
        peptides: list[str],
        *,
        target_gene: str | None = None,
        receptor_pdb: str | None = None,
        receptor_sequence: str | None = None,
        receptor_name: str | None = None,
        diffusion_samples: int = 1,
        sampling_steps: int = 50,
        recycling_steps: int | None = None,
        step_scale: float | None = None,
        msa_enabled: bool | None = None,
        glycosylation: bool | None = None,
        template_mode: bool = False,
        n_parallel_gpus: int | None = None,
        session_id: str | None = None,
        contribute_to_receptordb: bool | None = None,
        on_credit_exhausted: Callable[[LigandAICreditError, dict[str, Any]], bool] | None = None,
        # ─── hardening kwargs ────────────────────
        gpu: str | None = None,
        force_resubmit: bool = False,
    ) -> AsyncBatchFoldJob:
        """Async variant of :meth:`Peptides.fold_batch`.

        See :meth:`Peptides.fold_batch` for receptor modes, FASTA support,
        billing, the ``on_credit_exhausted`` callback contract, and the
        submission hardening (gpu='b200_plus' only, 24h dedupe, credit
        pre-flight, tier concurrency cap).
        """
        if self._client is not None:
            self._client._require_feature("predict_structure")
        # Hardening pre-flight
        from ligandai._hardening import (
            attach_job_id,
            build_fold_params_for_hash,
            dedupe_lookup_cached,
            enforce_concurrency,
            estimate_fold_batch_credits,
            mark_failed,
            preflight_credits,
            receptor_seq_for_hash,
            record_submission,
            validate_gpu,
        )
        from ligandai._dedupe import compute_submission_hash

        canonical_gpu = validate_gpu(gpu)
        submitted_set = self._client.submitted_set if self._client is not None else None
        credit_ledger = self._client.credit_ledger if self._client is not None else None
        api_key_hash = self._client.api_key_hash if self._client is not None else ""

        rec_for_hash = receptor_seq_for_hash(
            target_gene=target_gene,
            receptor_sequence=receptor_sequence,
            receptor_pdb=receptor_pdb,
        )
        sub_hash = compute_submission_hash(
            peptide_seq=peptides,
            receptor_seq=rec_for_hash,
            gpu=canonical_gpu,
            params=build_fold_params_for_hash(
                target_gene=target_gene,
                diffusion_samples=diffusion_samples,
                sampling_steps=sampling_steps,
                recycling_steps=recycling_steps,
                step_scale=step_scale,
                msa_enabled=msa_enabled,
                glycosylation=glycosylation,
                template_mode=template_mode,
                extra={"kind": "fold_batch", "receptor_name": receptor_name},
            ),
        )

        enforce_concurrency(self._client, submitted_set)

        cached = dedupe_lookup_cached(
            submitted_set,
            submission_hash=sub_hash,
            api_key_hash=api_key_hash,
            force_resubmit=force_resubmit,
        )
        if cached and cached.get("job_id"):
            cached_batch_id = str(cached["job_id"])
            return AsyncBatchFoldJob(
                self._transport,
                batch_id=cached_batch_id,
                jobs=[],
                total_cost_credits=int(cached.get("estimated_credits") or 0),
                peptide_count=len(peptides),
                trajectories_per_peptide=int(diffusion_samples),
                receptor={"source": "cached"},
                sampling_steps=int(sampling_steps),
            )

        estimated = estimate_fold_batch_credits(
            peptide_count=len(peptides),
            trajectories=int(diffusion_samples),
            sampling_steps=int(sampling_steps),
        )
        balance_before, _ = preflight_credits(
            self._client, estimated=estimated, kind="fold_batch",
        )

        body = _build_batch_fold_body(
            peptides=peptides,
            target_gene=target_gene,
            receptor_pdb=receptor_pdb,
            receptor_sequence=receptor_sequence,
            receptor_name=receptor_name,
            diffusion_samples=diffusion_samples,
            sampling_steps=sampling_steps,
            recycling_steps=recycling_steps,
            step_scale=step_scale,
            msa_enabled=msa_enabled,
            glycosylation=glycosylation,
            template_mode=template_mode,
            n_parallel_gpus=n_parallel_gpus,
            session_id=session_id,
            contribute_to_receptordb=contribute_to_receptordb,
        )

        record_submission(
            submitted_set,
            credit_ledger,
            submission_hash=sub_hash,
            api_key_hash=api_key_hash,
            kind="fold_batch",
            gpu=canonical_gpu,
            estimated_credits=estimated,
            balance_before=balance_before,
            meta={
                "peptide_count": len(peptides),
                "target_gene": target_gene,
                "receptor_name": receptor_name,
                "trajectories": int(diffusion_samples),
                "sampling_steps": int(sampling_steps),
            },
        )

        # mirror the sync graceful-credit retry.
        try:
            payload = await self._transport.request("POST", "/api/v1/folding/predict-batch", json=body) or {}
        except LigandAICreditError as cred_err:
            if on_credit_exhausted is None:
                mark_failed(
                    submitted_set, submission_hash=sub_hash,
                    api_key_hash=api_key_hash, reason="credit_exhausted",
                )
                raise
            try:
                should_retry = bool(on_credit_exhausted(cred_err, body))
            except Exception:
                mark_failed(
                    submitted_set, submission_hash=sub_hash,
                    api_key_hash=api_key_hash, reason="credit_exhausted_callback_error",
                )
                raise cred_err from None
            if not should_retry:
                mark_failed(
                    submitted_set, submission_hash=sub_hash,
                    api_key_hash=api_key_hash, reason="credit_exhausted_no_retry",
                )
                raise
            payload = await self._transport.request("POST", "/api/v1/folding/predict-batch", json=body) or {}
        except Exception as exc:
            mark_failed(
                submitted_set, submission_hash=sub_hash,
                api_key_hash=api_key_hash, reason=type(exc).__name__,
            )
            raise
        batch_id = payload.get("batch_id") or payload.get("batchId") or ""
        if not batch_id:
            mark_failed(
                submitted_set, submission_hash=sub_hash,
                api_key_hash=api_key_hash, reason="server_no_batch_id",
            )
            raise LigandAIError("Server did not return a batch_id for fold_batch", response=payload)
        attach_job_id(
            submitted_set, submission_hash=sub_hash,
            api_key_hash=api_key_hash, job_id=batch_id,
        )
        return AsyncBatchFoldJob(
            self._transport,
            batch_id=batch_id,
            jobs=payload.get("jobs") or [],
            total_cost_credits=int(payload.get("total_cost_credits") or 0),
            peptide_count=int(payload.get("peptide_count") or 0),
            trajectories_per_peptide=int(payload.get("trajectories_per_peptide") or diffusion_samples),
            receptor=payload.get("receptor"),
            sampling_steps=int(payload.get("sampling_steps") or sampling_steps),
        )

    async def fold_custom_mutation(
        self,
        gene: str,
        mutations: list[str],
        alias: str | None = None,
    ) -> AsyncJob[FoldResult]:
        if self._client is not None:
            self._client._require_feature("predict_structure")
        body: dict[str, Any] = {"gene": gene, "mutations": mutations}
        if alias is not None:
            body["alias"] = alias
        payload = await self._transport.request("POST", "/api/ptf/fold-custom-mutation", json=body) or {}
        job_id = payload.get("jobId") or payload.get("id") or ""
        if not job_id:
            raise LigandAIError("Server did not return a jobId", response=payload)
        return AsyncJob(
            self._transport,
            job_id,
            job_type="folding",
            parser=_parse_fold,
            status_path="/api/folding/jobs/{job_id}",
            cancel_path="/api/folding/jobs/{job_id}",
            initial={"id": job_id, "type": "folding", "status": "queued", **payload},
        )

    async def continue_folding(
        self,
        session_id: str | None = None,
        gene: str | None = None,
        top_n: int = 25,
        gpu_count: int = 5,
        template_mode: bool = False,
    ) -> AsyncJob[GenerationResult]:
        if not session_id and not gene:
            raise ValueError("Pass session_id= or gene=")
        if not session_id:
            assert gene is not None
            from_session = await self._transport.request("GET", f"/api/ptf/sessions/by-gene/{gene}") or {}
            session_id = from_session.get("id")
            if not session_id:
                raise LigandAIError(f"No active session for gene {gene!r}")
        body = {
            "topN": top_n,
            "gpuCount": gpu_count,
            "templateMode": template_mode,
        }
        payload = (
            await self._transport.request("POST", f"/api/ptf/parallel/{session_id}/continue", json=body) or {}
        )
        job_id = payload.get("jobId") or session_id
        return AsyncJob(
            self._transport,
            job_id,
            job_type="generation",
            parser=_parse_generation,
            status_path="/api/ptf/parallel/{job_id}/status",
            cancel_path="/api/ptf/parallel/{job_id}/cancel",
            sse_path="/api/ptf/parallel/{job_id}/stream",
            initial={"id": job_id, "type": "generation", "status": "running", **payload},
            result_loader=lambda info: _aload_generation_result(
                self._transport,
                info,
                fallback_session_id=session_id,
                fallback_gene=gene,
            ),
        )

    async def score_complex(
        self,
        binder_sequence: str,
        target_sequence: str,
        binder_name: str = "binder",
        target_name: str = "target",
        scorer: _DeltaForgeScorer = "auto",
    ) -> AsyncJob[DeltaForgeScore]:
        body = {
            "binderSequence": binder_sequence,
            "targetSequence": target_sequence,
            "binderName": binder_name,
            "targetName": target_name,
            "scorer": scorer,
        }
        payload = await self._transport.request("POST", "/api/binder-scoring/fold-and-score", json=body) or {}
        job_id = payload.get("jobId") or payload.get("id") or ""
        if not job_id:
            raise LigandAIError("Server did not return a jobId", response=payload)

        def parse(data: dict[str, Any]) -> DeltaForgeScore:
            return _parse_deltaforge_score(data)

        return AsyncJob(
            self._transport,
            job_id,
            job_type="scoring",
            parser=parse,
            status_path=f"/api/binder-scoring/job/{{job_id}}?scorer={scorer}",
            initial={"id": job_id, "type": "scoring", "status": "submitted"},
        )

    async def score_pdb(
        self,
        *,
        pdb_content: str | None = None,
        pdb_file: str | Path | None = None,
        receptor_chains: list[str] | None = None,
        peptide_chain: str | None = None,
        chain_a: str | None = None,
        chain_b: str | None = None,
        scorer: _DeltaForgeScorer = "auto",
        aggregate_method: _DeltaForgeAggregateMethod = "boltzmann_parallel",
        include_features: bool = False,
        include_pae: bool = False,
        fold_ipsae: float | None = None,
        fold_iptm: float | None = None,
        fold_ptm: float | None = None,
        fold_plddt_mean: float | None = None,
        fold_complex_plddt: float | None = None,
        fold_complex_iplddt: float | None = None,
    ) -> DeltaForgeScore:
        if not pdb_content and not pdb_file:
            raise ValueError("Pass pdb_content= or pdb_file=")
        if pdb_content and pdb_file:
            raise ValueError("Pass only one of pdb_content= or pdb_file=")
        content = pdb_content if pdb_content is not None else Path(pdb_file).read_text()
        receptors = receptor_chains or ([chain_a] if chain_a else None)
        peptide = peptide_chain or chain_b
        if not receptors or not peptide:
            raise ValueError("Pass receptor_chains= and peptide_chain=, or chain_a= and chain_b=")

        payload = await self._transport.request(
            "POST",
            "/api/v1/deltaforge/score-pdb",
            json={
                "pdbContent": content,
                "receptorChains": receptors,
                "peptideChain": peptide,
                "scorer": scorer,
                "aggregateMethod": aggregate_method,
                "includeFeatures": include_features,
                "includePae": include_pae,
                "foldIpsae": fold_ipsae,
                "foldIptm": fold_iptm,
                "foldPtm": fold_ptm,
                "foldPlddtMean": fold_plddt_mean,
                "foldComplexPlddt": fold_complex_plddt,
                "foldComplexIplddt": fold_complex_iplddt,
            },
        ) or {}
        return _parse_deltaforge_score(payload)

    async def score_with_ligandiq(
        self,
        session_id: str | None = None,
        gene: str | None = None,
        top_n: int = 20,
    ) -> list[LigandIQScore]:
        if not session_id and not gene:
            raise ValueError("Pass session_id= or gene=")
        if not session_id:
            from_session = await self._transport.request("GET", f"/api/ptf/sessions/by-gene/{gene}") or {}
            session_id = from_session.get("id")
            if not session_id:
                raise LigandAIError(f"No active session for gene {gene!r}")
        body = {"topN": top_n}
        payload = (
            await self._transport.request(
                "POST", f"/api/ptf/parallel/{session_id}/ligandiq-score", json=body
            )
            or {}
        )
        items = payload.get("scores") or payload.get("results") or []
        return [LigandIQScore.model_validate(s) for s in items]

    async def analyze_solubility(
        self,
        peptides: list[PeptideInput | dict[str, Any] | str],
        gravy_threshold: float = 0.0,
        flag_multi_cys: bool = True,
    ) -> list[SolubilityResult]:
        normalized = [
            (p.model_dump(by_alias=True) if isinstance(p, PeptideInput) else
             {"sequence": p} if isinstance(p, str) else p)
            for p in peptides
        ]
        body = {
            "peptides": normalized,
            "gravyThreshold": gravy_threshold,
            "flagMultiCys": flag_multi_cys,
        }
        payload = (
            await self._transport.request("POST", "/api/peptide-features/solubility", json=body)
            or {}
        )
        items = payload.get("results") or payload.get("solubility") or []
        return [SolubilityResult.model_validate(s) for s in items]

    async def search(
        self,
        gene: str | None = None,
        classification: str | None = None,
        ipsae_min: float | None = None,
        iptm_min: float | None = None,
        plddt_min: float | None = None,
        kd_max: float | None = None,
        dg_max: float | None = None,
        binder_pct_min: float | None = None,
        length_min: int | None = None,
        length_max: int | None = None,
        is_elite: bool | None = None,
        super_elite: bool | None = None,
        super_elite_affinity: bool | None = None,
        super_elite_thermo: bool | None = None,
        hotspot_residues: list[str] | None = None,
        pocket_residues: list[str] | None = None,
        hotspot_hit: bool | None = None,
        pocket_hit: bool | None = None,
        contact_distance_a: float | None = None,
        stability_grade: list[str] | None = None,
        immuno_grade: list[str] | None = None,
        conformation: str | None = None,
        program_id: int | None = None,
        session_id: str | None = None,
        pdb_id: str | None = None,
        sort: str = "ipsae",
        order: str = "desc",
        min_ipsae: float | None = None,  # legacy alias
        limit: int = 20,
        offset: int = 0,
    ) -> list[Peptide]:
        """Async variant of :meth:`Peptides.search`.

        Mirrors the full sync signature — every score/coverage/scope filter
        the workspace UI exposes. See :meth:`Peptides.search` for argument
        documentation. All criteria AND-combine.
        """
        if min_ipsae is not None and ipsae_min is None:
            ipsae_min = min_ipsae
        if super_elite_thermo is not None:
            warnings.warn(
                "super_elite_thermo is deprecated; use super_elite_affinity instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            if super_elite_affinity is None:
                super_elite_affinity = super_elite_thermo
        params: dict[str, Any] = {"limit": limit, "offset": offset, "sort": sort, "order": order}
        if gene is not None: params["gene"] = gene.upper()
        if classification is not None: params["classification"] = classification
        if ipsae_min is not None: params["ipsae_min"] = ipsae_min
        if iptm_min is not None: params["iptm_min"] = iptm_min
        if plddt_min is not None: params["plddt_min"] = plddt_min
        if kd_max is not None: params["kd_max"] = kd_max
        if dg_max is not None: params["dg_max"] = dg_max
        if binder_pct_min is not None: params["binder_pct_min"] = binder_pct_min
        if length_min is not None: params["length_min"] = length_min
        if length_max is not None: params["length_max"] = length_max
        if is_elite is not None: params["is_elite"] = "true" if is_elite else "false"
        if super_elite is not None: params["super_elite"] = "true" if super_elite else "false"
        if super_elite_affinity is not None: params["super_elite_affinity"] = "true" if super_elite_affinity else "false"
        if hotspot_hit is not None: params["hotspot_hit"] = "true" if hotspot_hit else "false"
        if pocket_hit is not None: params["pocket_hit"] = "true" if pocket_hit else "false"
        if hotspot_residues: params["hotspot_residues"] = ",".join(hotspot_residues)
        if pocket_residues: params["pocket_residues"] = ",".join(pocket_residues)
        if contact_distance_a is not None: params["contact_distance_a"] = contact_distance_a
        if stability_grade: params["stability_grade"] = ",".join(stability_grade)
        if immuno_grade: params["immuno_grade"] = ",".join(immuno_grade)
        if conformation is not None: params["conformation"] = conformation
        if program_id is not None: params["program_id"] = program_id
        if session_id is not None: params["session_id"] = session_id
        if pdb_id is not None: params["pdb_id"] = pdb_id.upper()

        payload = await self._transport.request(
            "GET", "/api/v1/peptides/search", params=params
        ) or {}
        items = payload.get("peptides", []) if isinstance(payload, dict) else (payload or [])
        return [Peptide.model_validate(p) for p in items]

    async def search_by_pocket(
        self,
        gene: str,
        chain: str | None = None,
        start_residue: int | None = None,
        end_residue: int | None = None,
        targeted_only: bool = True,
    ) -> list[Peptide]:
        params: dict[str, Any] = {"gene": gene, "targeted_only": targeted_only}
        if chain is not None:
            params["chain"] = chain
        if start_residue is not None:
            params["start_residue"] = start_residue
        if end_residue is not None:
            params["end_residue"] = end_residue
        payload = await self._transport.request("GET", "/api/ptf/peptides/by-pocket", params=params) or []
        items = payload if isinstance(payload, list) else payload.get("peptides", [])
        return [Peptide.model_validate(p) for p in items]

    async def get_elite(
        self,
        session_id: str | None = None,
        gene: str | None = None,
    ) -> list[Peptide]:
        if not session_id and not gene:
            raise ValueError("Pass session_id= or gene=")
        if not session_id:
            from_session = await self._transport.request("GET", f"/api/ptf/sessions/by-gene/{gene}") or {}
            session_id = from_session.get("id")
            if not session_id:
                raise LigandAIError(f"No active session for gene {gene!r}")
        payload = await self._transport.request("GET", f"/api/ptf/parallel/{session_id}/elite") or []
        items = payload if isinstance(payload, list) else payload.get("peptides", [])
        return [Peptide.model_validate(p) for p in items]

    # --- v0.2.0 paid-only surface (async) ---

    async def by_gene(
        self,
        genes: list[str] | None = None,
        min_ipsae: float | None = None,
        program_id: int | None = None,
        project_id: int | None = None,
        since: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[GeneSummary]:
        """Async variant of :meth:`Peptides.by_gene`."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if genes:
            params["genes"] = ",".join(g.upper() for g in genes if g)
        if min_ipsae is not None:
            params["minIpsae"] = min_ipsae
        if program_id is not None:
            params["programId"] = program_id
        if project_id is not None:
            params["projectId"] = project_id
        if since is not None:
            params["since"] = since.isoformat()
        payload = await self._transport.request(
            "GET", "/api/v1/peptides/by-gene", params=params
        ) or {}
        rows = payload.get("rows", []) if isinstance(payload, dict) else []
        return [GeneSummary.model_validate(r) for r in rows]

    async def by_pdb(
        self,
        pdb: str | list[str],
        min_ipsae: float | None = None,
        since: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Async variant of :meth:`Peptides.by_pdb`."""
        codes = [pdb] if isinstance(pdb, str) else list(pdb)
        params: dict[str, Any] = {
            "pdb": ",".join(c.upper() for c in codes if c),
            "limit": limit, "offset": offset,
        }
        if min_ipsae is not None:
            params["min_ipsae"] = min_ipsae
        if since is not None:
            params["since"] = since.isoformat()
        payload = await self._transport.request(
            "GET", "/api/v1/peptides/by-pdb", params=params
        ) or {}
        return payload.get("rows", []) if isinstance(payload, dict) else []

    async def list(
        self,
        gene_or_program_id: str | int | None = None,
        *,
        gene: str | None = None,
        program_id: int | None = None,
        min_ipsae: float | None = None,
        min_iptm: float | None = None,
        max_kd: float | None = None,
        include_unfolded: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Peptide]:
        """Async variant of :meth:`Peptides.list`."""
        if gene_or_program_id is not None:
            if isinstance(gene_or_program_id, str):
                if gene is not None and gene != gene_or_program_id:
                    raise ValueError("conflicting gene values: pass either positionally or by keyword, not both")
                gene = gene_or_program_id
            elif isinstance(gene_or_program_id, int):
                if program_id is not None and program_id != gene_or_program_id:
                    raise ValueError("conflicting program_id values: pass either positionally or by keyword, not both")
                program_id = gene_or_program_id
            else:
                raise TypeError(
                    f"list() positional arg must be a gene symbol (str) or program_id (int); "
                    f"got {type(gene_or_program_id).__name__}"
                )

        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if program_id is not None:
            params["program_id"] = program_id
        if gene is not None:
            if not gene.strip():
                raise ValueError("gene must be a non-empty string")
            params["gene"] = gene.upper()
        if min_ipsae is not None:
            params["min_ipsae"] = min_ipsae
        if min_iptm is not None:
            params["min_iptm"] = min_iptm
        if max_kd is not None:
            params["max_kd"] = max_kd

        payload = await self._transport.request(
            "GET", "/api/v1/peptides/list", params=params
        ) or {}
        items = payload.get("peptides", []) if isinstance(payload, dict) else (payload or [])
        return [Peptide.model_validate(p) for p in items]

    async def list_by_program(
        self,
        program_id: int,
        *,
        min_ipsae: float | None = None,
        min_iptm: float | None = None,
        max_kd: float | None = None,
        gene: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Peptide]:
        """Async variant of :meth:`Peptides.list_by_program`."""
        return await self.list(
            program_id=program_id,
            gene=gene,
            min_ipsae=min_ipsae,
            min_iptm=min_iptm,
            max_kd=max_kd,
            limit=limit,
            offset=offset,
        )

    async def get(
        self,
        peptide_id: int | str,
        include: list[_IncludeField] | None = None,
    ) -> PeptideDetail:
        """Async variant of :meth:`Peptides.get`."""
        if self._client is not None:
            self._client._require_paid_tier()
        try:
            id_int = int(peptide_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"peptide_id must be a positive integer (got {peptide_id!r})"
            ) from exc
        if id_int <= 0:
            raise ValueError(f"peptide_id must be > 0 (got {id_int})")
        params: dict[str, Any] = {}
        if include:
            unknown = [v for v in include if v not in _ALLOWED_INCLUDE]
            if unknown:
                raise ValueError(
                    f"Unknown include value(s): {unknown}. "
                    f"Allowed: {sorted(_ALLOWED_INCLUDE)}"
                )
            params["include"] = ",".join(include)
        payload = await self._transport.request(
            "GET", f"/api/v1/peptides/{id_int}", params=params
        ) or {}
        return PeptideDetail.model_validate(payload)

    async def estimate_cost(
        self,
        *,
        num_peptides: int,
        auto_fold: bool = True,
        fold_top_n: int | None = None,
        fold_trajectories: int = 4,
    ) -> CostEstimate:
        """Async variant of :meth:`Peptides.estimate_cost`."""
        params: dict[str, Any] = {
            "num_peptides": num_peptides,
            "auto_fold": auto_fold,
            "fold_trajectories": fold_trajectories,
        }
        if fold_top_n is not None:
            params["top_n"] = fold_top_n
        payload = (
            await self._transport.request("GET", "/api/billing/estimate", params=params)
            or {}
        )
        return CostEstimate.model_validate(payload)
