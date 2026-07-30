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
from ligandai.errors import (
    LigandAIAuthError,
    LigandAIConcurrencyLimit,
    LigandAICreditError,
    LigandAIDuplicateSubmission,
    LigandAIError,
    LigandAIForbidden,
    LigandAIIncompleteResult,
    LigandAIInsufficientCredits,
    LigandAIInvalidConfig,
    LigandAIJobError,
    LigandAINotFoundError,
    LigandAIPaidTierRequired,
    LigandAIRateLimitError,
    LigandAIServerError,
    LigandAITierError,
    LigandAITimeoutError,
    LigandAIUpgradeRequired,
    LigandAIValidationError,
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
    LigandScore,
    LinkerConfig,
    LinkerRecommendation,
    MSAChain,
    MSAResult,
    PdcConfig,
    Peptide,
    PeptideDetail,
    PeptideInput,
    PeptideSegment,
    ReceptorAtlas,
    ResidueRange,
    SegmentConfig,
    SpeciesEntitlement,
    SynthesisPeptide,
    TargetGroup,
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
    "ENGINES",
    "METRICS",
    "METRIC_META",
    "MIN_DISTRIBUTION_SAMPLES",
    "PERCENTILE_AGREEMENT_TOLERANCE",
    "PERCENTILE_TIERS",
    "PROTEINVIEW_ATTRIBUTION",
    "AccountBalance",
    "AdaptyvExperiment",
    "AdaptyvSequence",
    "AdaptyvTarget",
    "ApiCallLogEntry",
    "AsyncBatchFoldJob",
    "AsyncJob",
    "AsyncLigandAI",
    "AsyncLinkerModifications",
    "AsyncReceptorDBClient",
    "AutoTopupConfig",
    "BatchFoldEvent",
    "BatchFoldJob",
    "BindingOrientationResult",
    "BiotinLinker",
    "BivalentTarget",
    "ClientSessionUsage",
    "ClientSessionUsageSummary",
    "CostEstimate",
    "CovalentAttachment",
    "DashboardHandle",
    "DeltaForgeBestPair",
    "DeltaForgeGateReadout",
    "DeltaForgePairScore",
    "DeltaForgeScore",
    "DevelopabilityResult",
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
    "Job",
    "LigandAI",
    "LigandAIAuthError",
    "LigandAIConcurrencyLimit",
    "LigandAICreditError",
    "LigandAIDuplicateSubmission",
    "LigandAIError",
    "LigandAIForbidden",
    "LigandAIIncompleteResult",
    "LigandAIInsufficientCredits",
    "LigandAIInvalidConfig",
    "LigandAIJobError",
    "LigandAINotFoundError",
    "LigandAIPaidTierRequired",
    "LigandAIRateLimitError",
    "LigandAIServerError",
    "LigandAITierError",
    "LigandAITimeoutError",
    "LigandAIUpgradeRequired",
    "LigandAIValidationError",
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
    "PayloadFilter",
    "PayloadOptimizationRun",
    "PdcConfig",
    "Peptide",
    "PeptideCandidate",
    "PeptideDetail",
    "PeptideInput",
    "PeptideSegment",
    "DEFAULT_SPECIES",
    "SPECIES_TARGETING_CAPABILITY",
    "ReceptorAtlas",
    "ReceptorChain",
    "ReceptorDBClient",
    "ResidueRange",
    "SegmentConfig",
    "Species",
    "SpeciesEntitlement",
    "SynthesisPeptide",
    "TargetGroup",
    "TopUpResult",
    "UnlimitedCredits",
    "__version__",
    "is_mouse",
    "normalize_species",
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
    "is_outdated",
    "launch_proteinview",
    "linked_line_figure",
    "load_peptide_results",
    "metric_higher_is_better",
    "normalize_engine",
    "normalize_metric",
    "percentile_label",
    "rank_peptides",
    "serve_comparison_dashboard",
    "serve_dashboard",
    "standing",
    "update_fold_time_model",
    "write_comparison_dashboard",
    "write_dashboard",
]
