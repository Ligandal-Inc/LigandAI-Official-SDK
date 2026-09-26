# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""LIGANDAI (TM) Python SDK.

Official Python client for the LIGANDAI platform.

New here? Call ``ligandai.guide()`` (or ``LigandAI.guide()``) for a concise map
of the canonical workflows — including the built-in target-discovery
(transcriptomics) funnel — and a pointer to the agent docs shipped in the
package (``AGENTS.md`` and ``.claude/skills/ligandai/``). Don't hand-stitch
external data sources: ``client.discovery`` already runs the funnel natively.

Example
-------
.. code-block:: python

    import ligandai
    from ligandai import LigandAI

    ligandai.guide()                        # prints the canonical workflow map

    client = LigandAI(api_key="lgai_pro_...")
    print(f"Tier: {client.tier}, Credits: {client.credits}")

    # Discover targets: SI-ranked surface receptors enriched in a tissue
    markers = client.discovery.tissue_markers(
        target_tissues=["Liver"], receptor_only=True, top_n=200,
    )
    gene = markers.top[0].gene

    # Generate peptides against the top target
    job = client.peptides.generate(gene=gene, num_peptides=50, auto_fold=True)
    result = job.wait()

See https://docs.ligandai.com for full documentation.
"""

from __future__ import annotations

from ligandai._fold_time_model import (
    estimate_fold_time,
    format_eta,
    get_fold_time_model,
    update_fold_time_model,
)
from ligandai._guide import guide
from ligandai._version import __version__
from ligandai.client import AsyncLigandAI, LigandAI
from ligandai.diagnostics import (
    DIAGNOSTIC_CODES,
    DIAGNOSTIC_EVENT,
    DIAGNOSTIC_SCHEMA_VERSION,
    Diagnostic,
    known_codes,
)
from ligandai.errors import (
    LigandAIAmbiguousTarget,
    LigandAIAuthError,
    LigandAIConcurrencyLimit,
    LigandAIConnectionError,
    LigandAICreditError,
    LigandAIDiagnosticWarning,
    LigandAIDuplicateSubmission,
    LigandAIReusedSessionError,
    LigandAIError,
    LigandAIForbidden,
    LigandAIIncompleteResult,
    LigandAIInsufficientCredits,
    LigandAIInvalidConfig,
    LigandAIParameterNotForwarded,
    LigandAIAnalysisNotComputed,
    LigandAIEdgeBlocked,
    LigandAIMutationUnconfirmed,
    LigandAISurfaceNotInThisPackage,
    LigandAIUnreadableResponse,
    LigandAIJobError,
    LigandAIReceptorMsaUnavailable,
    LigandAILegalAcceptanceRequired,
    LigandAINotFoundError,
    LigandAIPaidTierRequired,
    LigandAIRateLimitError,
    LigandAIServerError,
    LigandAITargetingWarning,
    LigandAITierError,
    LigandAITimeoutError,
    LigandAIUpgradeRequired,
    LigandAIValidationError,
    LigandAIVersionWarning,
    LigandAIWaitTimeout,
    NotSupportedOnReceptorDB,
)
from ligandai.fold_calibration import (
    ENGINES,
    METRIC_META,
    METRICS,
    MIN_DISTRIBUTION_SAMPLES,
    PERCENTILE_AGREEMENT_TOLERANCE,
    PERCENTILE_TIERS,
    EngineAgreement,
    EngineDistributions,
    EngineStanding,
    MetricMeta,
    build_distributions,
    engine_agreement,
    metric_higher_is_better,
    normalize_engine,
    normalize_metric,
    percentile_label,
    standing,
)
from ligandai.fold_charts import (
    FoldComparison,
    FoldPoint,
    build_fold_comparison,
    distribution_figure,
    linked_line_figure,
)
from ligandai.jobs import AsyncJob, Job
from ligandai.peptide_viewer import (
    PROTEINVIEW_ATTRIBUTION,
    DashboardHandle,
    PeptideCandidate,
    align_candidates_to_receptor,
    align_pdb_to_receptor,
    build_comparison_summary,
    launch_proteinview,
    load_peptide_results,
    rank_peptides,
    serve_comparison_dashboard,
    serve_dashboard,
    write_comparison_dashboard,
    write_dashboard,
)
from ligandai.receptordb import AsyncReceptorDBClient, ReceptorDBClient

# Programmatic ToS/EULA review + acceptance — reachable as `client.legal`,
# and as a top-level module/classes: `from ligandai import legal` /
# `from ligandai import Legal, AsyncLegal`.
from ligandai.resources import legal
from ligandai.resources.legal import AsyncLegal, Legal

# GI / gut-stability developability triage — reachable as `client.gut_stability`,
# and as a top-level module for the pure functions:
# `from ligandai import gut_stability` / `from ligandai import score_gut_design`.
#
# ⛔ Everything this module emits is a RULE COUNT NORMALIZED BY LENGTH: not a
# measured degradation rate, not a probability of surviving the gut, not an
# oral bioavailability prediction, not a neural-network output. The declaration
# block travels on every record — do not strip it. [bd-dre-c80f4.1]
# ⛔ The two sibling scorers on the same namespace [bd-dre-c80f4.26.2]:
# chameleonicity applies ONLY to macrocycles (a linear construct yields
# applicable=False / index=None, never 0.0), and full-protein gut stability
# returns INDETERMINATE_NO_STRUCTURE with accessible_* ABSENT when no structure
# is supplied. Absence is never a zero in either.
from ligandai.resources import gut_stability
from ligandai.resources.gut_stability import (
    AsyncGutStability,
    GutStability,
    LigandAIPlatformModuleUnavailable,
    gut_stability_declaration,
    protease_motif_counts,
    score_gut_design,
)

# bd-dre-if562 — the recursive selectivity campaign. Every OTHER object a user
# touches to drive `client.campaigns` was importable as `from ligandai import X`;
# these were not, so the one workflow whose docstrings say "build this object and
# pass it in" was also the one whose objects had no discoverable import path. The
# canonical module paths still work and are unchanged.
from ligandai.contrastive import (
    AvoidancePolicy,
    ContrastiveGuidance,
    ContrastiveReference,
    PocketFeatureMatrix,
)
from ligandai.multitarget import (
    CROSS_TARGET_AGGREGATES,
    CoOptimization,
    CrossTargetAggregate,
    TargetSpec,
    aggregate_across_targets,
    dual_objective_pass,
)
# The dual objective's shared primitives: ipSAE gates, DeltaForge Kd scores.
# [bd-LIGANDAI_ALPHA_V2-jb73b]
from ligandai.thermo import (
    CONFIRM_MIN_DRAWS,
    VALID_IPSAE,
    MetricDirection,
    SelectivityRatio,
    geometric_mean_kd,
    kd_bootstrap_ci,
    kd_provenance_ok,
    multiplicity_note,
    selectivity_ratio,
)
from ligandai.resources.campaigns import (
    CampaignRound,
    Lineage,
    MultiTargetDelta,
    MultiTargetPartial,
    PanelLeg,
    PanelResult,
    ParentComparison,
    PartialDelta,
    RecursiveCampaign,
    ReplicationReport,
    SelectivityDelta,
)

# 3dalk — linker modifications + payload optimization (pro+ tier).
from ligandai.resources.linker_modifications import (
    AsyncLinkerModifications,
    CovalentAttachment,
    LinkerModification,
    LinkerModifications,
    PayloadFilter,
    PayloadOptimizationRun,
    ReceptorChain,
)
from ligandai.resources.peptides import AsyncBatchFoldJob, BatchFoldJob
from ligandai.species import (
    DEFAULT_SPECIES,
    SPECIES_TARGETING_CAPABILITY,
    Species,
    is_mouse,
    normalize_species,
)
from ligandai.types import (
    AccountBalance,
    AdaptyvExperiment,
    AdaptyvSequence,
    AdaptyvTarget,
    ApiCallLogEntry,
    AutoTopupConfig,
    BatchFoldEvent,
    BindingOrientationResult,
    BiotinLinker,
    BivalentTarget,
    ClientSessionUsage,
    ClientSessionUsageSummary,
    CostEstimate,
    DeltaForgeBestPair,
    DeltaForgeGateReadout,
    DeltaForgePairScore,
    DeltaForgeScore,
    DevelopabilityResult,
    EcTrimmingConfig,
    GenerationMaskGuidance,
    GeneSummary,
    GoalAcceptanceCriterion,
    GoalBudgetState,
    GoalChecklistItem,
    GoalCompletionAudit,
    GoalEvaluation,
    GoalPlanStep,
    GoalProgress,
    GoalProjectState,
    GoalRun,
    GoalRunEvent,
    GoalRunStart,
    GoalStepRecord,
    GoalTaskDependency,
    HotspotResidue,
    LigandScore,
    LinkerConfig,
    LinkerRecommendation,
    MSAChain,
    MSAResult,
    PdcConfig,
    PdeScores,
    Peptide,
    PeptideDetail,
    PeptideInput,
    PeptideSegment,
    ReceptorAtlas,
    ReceptorProfile,
    ResidueRange,
    SegmentConfig,
    SpeciesEntitlement,
    SynthesisPeptide,
    TargetGroup,
    TargetRegion,
    TopUpResult,
    UnlimitedCredits,
)
from ligandai.version_check import (
    emit_update_notice,
    get_latest_pypi_version,
    get_update_notice,
    is_outdated,
)

__all__ = [
    "DEFAULT_SPECIES",
    "DIAGNOSTIC_CODES",
    "DIAGNOSTIC_EVENT",
    "DIAGNOSTIC_SCHEMA_VERSION",
    "ENGINES",
    "METRICS",
    "METRIC_META",
    "MIN_DISTRIBUTION_SAMPLES",
    "PERCENTILE_AGREEMENT_TOLERANCE",
    "PERCENTILE_TIERS",
    "PROTEINVIEW_ATTRIBUTION",
    "SPECIES_TARGETING_CAPABILITY",
    "AccountBalance",
    "AdaptyvExperiment",
    "AdaptyvSequence",
    "AdaptyvTarget",
    "ApiCallLogEntry",
    "AsyncBatchFoldJob",
    "AsyncGutStability",
    "GutStability",
    "AsyncJob",
    "AsyncLegal",
    "AsyncLigandAI",
    "AsyncLinkerModifications",
    "AsyncReceptorDBClient",
    "AutoTopupConfig",
    "AvoidancePolicy",
    "BatchFoldEvent",
    "BatchFoldJob",
    "BindingOrientationResult",
    "BiotinLinker",
    "BivalentTarget",
    "CampaignRound",
    # Lineage: where a round-N design came from, and whether the parent was
    # measured beside it. bd-dre-umd3l.
    "Lineage",
    "ParentComparison",
    "ClientSessionUsage",
    "ClientSessionUsageSummary",
    "ContrastiveGuidance",
    "ContrastiveReference",
    # Multi-target co-optimization (N positive targets). bd-dre-if562.
    "CoOptimization",
    "TargetSpec",
    "CrossTargetAggregate",
    "CROSS_TARGET_AGGREGATES",
    "MultiTargetDelta",
    "MultiTargetPartial",
    "aggregate_across_targets",
    "dual_objective_pass",
    # thermo primitives [bd-LIGANDAI_ALPHA_V2-jb73b]
    "CONFIRM_MIN_DRAWS",
    "VALID_IPSAE",
    "MetricDirection",
    "SelectivityRatio",
    "geometric_mean_kd",
    "kd_bootstrap_ci",
    "kd_provenance_ok",
    "multiplicity_note",
    "selectivity_ratio",
    "CostEstimate",
    "CovalentAttachment",
    "DashboardHandle",
    "DeltaForgeBestPair",
    "DeltaForgeGateReadout",
    "DeltaForgePairScore",
    "DeltaForgeScore",
    "DevelopabilityResult",
    "Diagnostic",
    "EcTrimmingConfig",
    "EngineAgreement",
    "EngineDistributions",
    "EngineStanding",
    "FoldComparison",
    "FoldPoint",
    "GeneSummary",
    "GenerationMaskGuidance",
    "GoalAcceptanceCriterion",
    "GoalBudgetState",
    "GoalChecklistItem",
    "GoalCompletionAudit",
    "GoalEvaluation",
    "GoalPlanStep",
    "GoalProgress",
    "GoalProjectState",
    "GoalRun",
    "GoalRunEvent",
    "GoalRunStart",
    "GoalStepRecord",
    "GoalTaskDependency",
    "HotspotResidue",
    "Job",
    "Legal",
    "LigandAI",
    "LigandAIAmbiguousTarget",
    "LigandAIAnalysisNotComputed",
    "LigandAIAuthError",
    "LigandAIConcurrencyLimit",
    "LigandAIConnectionError",
    "LigandAICreditError",
    "LigandAIDiagnosticWarning",
    "LigandAIDuplicateSubmission",
    "LigandAIEdgeBlocked",
    "LigandAIError",
    "LigandAIForbidden",
    "LigandAIIncompleteResult",
    "LigandAIInsufficientCredits",
    "LigandAIInvalidConfig",
    "LigandAIJobError",
    "LigandAILegalAcceptanceRequired",
    "LigandAIMutationUnconfirmed",
    "LigandAINotFoundError",
    "LigandAIPaidTierRequired",
    "LigandAIParameterNotForwarded",
    # A LOCAL scorer's platform module is absent — an explicit ImportError-shaped
    # absence, never a degraded score. [bd-dre-c80f4.26.2]
    "LigandAIPlatformModuleUnavailable",
    "LigandAIRateLimitError",
    "LigandAIReceptorMsaUnavailable",
    "LigandAIReusedSessionError",
    "LigandAIServerError",
    "LigandAISurfaceNotInThisPackage",
    "LigandAITargetingWarning",
    "LigandAITierError",
    "LigandAITimeoutError",
    "LigandAIUnreadableResponse",
    "LigandAIUpgradeRequired",
    "LigandAIValidationError",
    "LigandAIVersionWarning",
    "LigandAIWaitTimeout",
    "LigandScore",
    "LinkerConfig",
    "LinkerModification",
    "LinkerModifications",
    "LinkerRecommendation",
    "MSAChain",
    "MSAResult",
    "MetricMeta",
    "NotSupportedOnReceptorDB",
    "PanelLeg",
    "PanelResult",
    "PartialDelta",
    "PayloadFilter",
    "PayloadOptimizationRun",
    "PdcConfig",
    "PdeScores",
    "Peptide",
    "PeptideCandidate",
    "PeptideDetail",
    "PeptideInput",
    "PeptideSegment",
    "PocketFeatureMatrix",
    "ReceptorAtlas",
    "ReceptorChain",
    "ReceptorDBClient",
    "ReceptorProfile",
    "RecursiveCampaign",
    "ReplicationReport",
    "ResidueRange",
    "SegmentConfig",
    "SelectivityDelta",
    "Species",
    "SpeciesEntitlement",
    "SynthesisPeptide",
    "TargetGroup",
    "TargetRegion",
    "TopUpResult",
    "UnlimitedCredits",
    "__version__",
    "align_candidates_to_receptor",
    "align_pdb_to_receptor",
    "build_comparison_summary",
    "build_distributions",
    "build_fold_comparison",
    "distribution_figure",
    "emit_update_notice",
    "engine_agreement",
    "estimate_fold_time",
    "format_eta",
    "get_fold_time_model",
    "get_latest_pypi_version",
    "get_update_notice",
    "guide",
    # GI / gut-stability developability triage [bd-dre-c80f4.1] — rule counts
    # normalized by length, never a measured rate or a survival probability.
    "gut_stability",
    "gut_stability_declaration",
    "protease_motif_counts",
    "score_gut_design",
    "is_mouse",
    "is_outdated",
    "known_codes",
    "launch_proteinview",
    "legal",
    "linked_line_figure",
    "load_peptide_results",
    "metric_higher_is_better",
    "normalize_engine",
    "normalize_metric",
    "normalize_species",
    "percentile_label",
    "rank_peptides",
    "serve_comparison_dashboard",
    "serve_dashboard",
    "standing",
    "update_fold_time_model",
    "write_comparison_dashboard",
    "write_dashboard",
]
