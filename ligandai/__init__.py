# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""LIGANDAI (TM) Python SDK.

Official Python client for the LIGANDAI platform.

Example
-------
.. code-block:: python

    from ligandai import LigandAI

    client = LigandAI(api_key="lgai_pro_...")
    print(f"Tier: {client.tier}, Credits: {client.credits}")

    # Find tissue-specific markers
    markers = client.discovery.tissue_markers(target_tissues=["Liver"])

    # Generate peptides
    job = client.peptides.generate(gene="EGFR", num_peptides=50, auto_fold=True)
    result = job.wait()

See https://docs.ligandai.com for full documentation.
"""

from __future__ import annotations

from ligandai._version import __version__
from ligandai.client import AsyncLigandAI, LigandAI
from ligandai.errors import (
    LigandAIAuthError,
    LigandAICreditError,
    LigandAIError,
    LigandAIForbidden,
    LigandAIJobError,
    LigandAINotFoundError,
    LigandAIPaidTierRequired,
    LigandAIRateLimitError,
    LigandAIServerError,
    LigandAITierError,
    LigandAITimeoutError,
    LigandAIUpgradeRequired,
    LigandAIValidationError,
    NotSupportedOnReceptorDB,
)
from ligandai.jobs import AsyncJob, Job
from ligandai.peptide_viewer import (
    PROTEINVIEW_ATTRIBUTION,
    DashboardHandle,
    PeptideCandidate,
    align_candidates_to_receptor,
    align_pdb_to_receptor,
    launch_proteinview,
    load_peptide_results,
    rank_peptides,
    serve_dashboard,
    write_dashboard,
)
from ligandai.receptordb import AsyncReceptorDBClient, ReceptorDBClient
from ligandai.types import (
    AccountBalance,
    AdaptyvExperiment,
    AdaptyvSequence,
    AdaptyvTarget,
    ApiCallLogEntry,
    AutoTopupConfig,
    BindingOrientationResult,
    BiotinLinker,
    BivalentTarget,
    ClientSessionUsage,
    ClientSessionUsageSummary,
    CostEstimate,
    DeltaForgeBestPair,
    DeltaForgePairScore,
    DeltaForgeScore,
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
    LinkerConfig,
    LinkerRecommendation,
    MSAChain,
    MSAResult,
    PdcConfig,
    Peptide,
    PeptideDetail,
    PeptideInput,
    PeptideSegment,
    ResidueRange,
    SegmentConfig,
    SynthesisPeptide,
    TargetGroup,
    TopUpResult,
)
from ligandai.version_check import (
    emit_update_notice,
    get_latest_pypi_version,
    get_update_notice,
    is_outdated,
)

__all__ = [
    "PROTEINVIEW_ATTRIBUTION",
    "AccountBalance",
    "AdaptyvExperiment",
    "AdaptyvSequence",
    "AdaptyvTarget",
    "ApiCallLogEntry",
    "AsyncJob",
    "AsyncLigandAI",
    "AsyncReceptorDBClient",
    "AutoTopupConfig",
    "BindingOrientationResult",
    "BiotinLinker",
    "BivalentTarget",
    "ClientSessionUsage",
    "ClientSessionUsageSummary",
    "CostEstimate",
    "DashboardHandle",
    "DeltaForgeBestPair",
    "DeltaForgePairScore",
    "DeltaForgeScore",
    "EcTrimmingConfig",
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
    "LigandAICreditError",
    "LigandAIError",
    "LigandAIForbidden",
    "LigandAIJobError",
    "LigandAINotFoundError",
    "LigandAIPaidTierRequired",
    "LigandAIRateLimitError",
    "LigandAIServerError",
    "LigandAITierError",
    "LigandAITimeoutError",
    "LigandAIUpgradeRequired",
    "LigandAIValidationError",
    "LinkerConfig",
    "LinkerRecommendation",
    "MSAChain",
    "MSAResult",
    "NotSupportedOnReceptorDB",
    "PdcConfig",
    "Peptide",
    "PeptideCandidate",
    "PeptideDetail",
    "PeptideInput",
    "PeptideSegment",
    "ReceptorDBClient",
    "ResidueRange",
    "SegmentConfig",
    "SynthesisPeptide",
    "TargetGroup",
    "TopUpResult",
    "__version__",
    "align_candidates_to_receptor",
    "align_pdb_to_receptor",
    "emit_update_notice",
    "get_latest_pypi_version",
    "get_update_notice",
    "is_outdated",
    "launch_proteinview",
    "load_peptide_results",
    "rank_peptides",
    "serve_dashboard",
    "write_dashboard",
]
