# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Peptide generation, folding, and scoring.

Public methods that submit GPU work return :class:`Job` (or :class:`AsyncJob`)
instances. Use ``.wait()`` to block until completion.

Endpoint mapping (server source-of-truth):

- :meth:`Peptides.generate`               → ``POST /api/ptf/parallel/generate``
- :meth:`Peptides.fold`                   → ``POST /api/folding/predict``
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
from typing import Any, Literal

from ligandai.errors import LigandAIError
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
_DeltaForgeScorer = Literal["auto", "current", "v10"]
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
    return DeltaForgeScore.model_validate(
        {
            "dg": scoring.get("dg") or scoring.get("delta_g"),
            "kd": scoring.get("kd") or scoring.get("kd_nm"),
            "kd_nm": scoring.get("kd_nm"),
            "contacts": scoring.get("contacts") or scoring.get("contact_count") or scoring.get("num_contacts"),
            "interfaceResidues": scoring.get("interface_residues"),
            "scorer": scoring.get("scorer"),
            "scorer_version": scoring.get("scorer_version"),
            "model_sha256": scoring.get("model_sha256"),
            "feature_schema_version": scoring.get("feature_schema_version"),
            "aggregate_method": scoring.get("aggregate_method"),
            "best_pair": scoring.get("best_pair"),
            "pair_scores": scoring.get("pair_scores"),
            "pair_errors": scoring.get("pair_errors"),
            "warnings": scoring.get("warnings"),
            "metadata": scoring.get("metadata") or scoring,
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

# Charge filtering mode applied by the filtered Modal worker.
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
    # Modal worker when any non-default constraint is present).
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
) -> dict[str, Any]:
    """Build the body for ``POST /api/folding/predict``.

    Single sequence → ``{model, sequence}``. Multiple → ``{model, entities}``.
    """
    normalized = [_norm_seq(s) for s in sequences]
    body: dict[str, Any] = {
        "model": "boltz2",
        "gpuCount": gpu_count,
        "diffusionSamples": num_trajectories if num_trajectories is not None else diffusion_samples,
        "templateMode": template_mode,
        "autoScore": auto_score,
    }
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
    return FoldResult.model_validate(
        {
            "jobId": payload.get("jobId") or payload.get("id") or "",
            "pdbUrl": payload.get("pdbUrl") or payload.get("pdb_url"),
            "pdbData": payload.get("pdbData") or payload.get("pdb_data") or payload.get("pdb"),
            "iptm": payload.get("iptm") or payload.get("ipTM"),
            "ipsae": payload.get("ipsae"),
            "plddt": payload.get("plddt"),
            "ptm": payload.get("ptm"),
            "chainPairIptm": payload.get("chainPairIptm") or payload.get("chain_pair_iptm"),
        }
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
    # Legacy production LigandIQ payloads normalize Modal's pred_iptm head into
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
        # filtered Modal worker when any non-default constraint is present).
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
    ) -> Job[FoldResult]:
        """Submit a Boltz-2 folding job (monomer or multimer)."""
        if self._client is not None:
            self._client._require_feature("predict_structure")
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
        )
        payload = self._transport.request("POST", "/api/folding/predict", json=body) or {}
        job_id = payload.get("jobId") or payload.get("id") or ""
        if not job_id:
            raise LigandAIError("Server did not return a jobId for fold", response=payload)
        return Job(
            self._transport,
            job_id,
            job_type="folding",
            parser=_parse_fold,
            status_path="/api/folding/jobs/{job_id}",
            cancel_path="/api/folding/jobs/{job_id}",
            sse_path="/api/jobs/{job_id}/sse",
            initial={"id": job_id, "type": "folding", "status": "queued", **payload},
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
    ) -> DeltaForgeScore:
        """Score a user-provided PDB with DeltaForge.

        Pass either ``pdb_content=`` or ``pdb_file=``. ``receptor_chains`` and
        ``peptide_chain`` are preferred; ``chain_a`` / ``chain_b`` are accepted
        as aliases for single-interface scoring.
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
        kd_max: float | None = None,
        program_id: int | None = None,
        min_ipsae: float | None = None,  # alias for ipsae_min (back-compat)
        limit: int = 20,
        offset: int = 0,
    ) -> list[Peptide]:
        """``GET /api/v1/peptides/search`` — cross-program peptide search.

        v0.5.0 backs this with the new `/v1/peptides/search` endpoint. You can
        now search across all your programs by score thresholds without
        specifying a gene first.

        Args:
            gene: Optional gene symbol filter.
            classification: Reserved (server-side classification filter; not
                yet wired to the new endpoint — passes through anyway).
            ipsae_min: Minimum iPSAE score (e.g. 0.8 for high-confidence).
            iptm_min: Minimum ipTM score.
            kd_max: Maximum predicted Kd in Molar (e.g. ``1e-8`` = 10nM).
            program_id: Optional program scope (omit to search across all).
            min_ipsae: Legacy alias for ``ipsae_min``.
            limit: Page size (max 200).
            offset: Pagination offset.
        """
        if min_ipsae is not None and ipsae_min is None:
            ipsae_min = min_ipsae
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if gene is not None:
            params["gene"] = gene.upper()
        if classification is not None:
            params["classification"] = classification
        if ipsae_min is not None:
            params["ipsae_min"] = ipsae_min
        if iptm_min is not None:
            params["iptm_min"] = iptm_min
        if kd_max is not None:
            params["kd_max"] = kd_max
        if program_id is not None:
            params["program_id"] = program_id

        payload = self._transport.request(
            "GET", "/api/v1/peptides/search", params=params
        ) or {}
        items = payload.get("peptides", []) if isinstance(payload, dict) else (payload or [])
        return [Peptide.model_validate(p) for p in items]

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
    # v0.2.0 surface — paid-only /api/v1/peptides/* (LIGANDAI_ALPHA_V2-afspr)
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

        **Auth:** Paid tiers only (basic/academia/pro/enterprise/superadmin).
        Free keys raise :class:`~ligandai.errors.LigandAIPaidTierRequired` from the
        server's 402 response.

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
        if self._client is not None:
            self._client._require_paid_tier()
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

        Andrew Keene's #1 bug: ``client.peptides.list(program_id)`` raised
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

        Convenience method for the common Andrew-Keene-style query: "give me
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
    ) -> AsyncJob[FoldResult]:
        if self._client is not None:
            self._client._require_feature("predict_structure")
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
        )
        payload = await self._transport.request("POST", "/api/folding/predict", json=body) or {}
        job_id = payload.get("jobId") or payload.get("id") or ""
        if not job_id:
            raise LigandAIError("Server did not return a jobId for fold", response=payload)
        return AsyncJob(
            self._transport,
            job_id,
            job_type="folding",
            parser=_parse_fold,
            status_path="/api/folding/jobs/{job_id}",
            cancel_path="/api/folding/jobs/{job_id}",
            sse_path="/api/jobs/{job_id}/sse",
            initial={"id": job_id, "type": "folding", "status": "queued", **payload},
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
        kd_max: float | None = None,
        program_id: int | None = None,
        min_ipsae: float | None = None,  # alias for ipsae_min
        limit: int = 20,
        offset: int = 0,
    ) -> list[Peptide]:
        """Async variant of :meth:`Peptides.search`."""
        if min_ipsae is not None and ipsae_min is None:
            ipsae_min = min_ipsae
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if gene is not None:
            params["gene"] = gene.upper()
        if classification is not None:
            params["classification"] = classification
        if ipsae_min is not None:
            params["ipsae_min"] = ipsae_min
        if iptm_min is not None:
            params["iptm_min"] = iptm_min
        if kd_max is not None:
            params["kd_max"] = kd_max
        if program_id is not None:
            params["program_id"] = program_id
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
        if self._client is not None:
            self._client._require_paid_tier()
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
