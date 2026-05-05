# Copyright © 2025 Ligandal, Inc. All rights reserved.
"""LIGANDAI® Python SDK.

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
    job = client.peptides.generate(gene="EGFR", num_peptides=300, auto_fold=True)
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
    LigandAINotFoundError,
    LigandAIPaidTierRequired,
    LigandAIRateLimitError,
    LigandAIServerError,
    LigandAITierError,
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
    AutoTopupConfig,
    BindingOrientationResult,
    BiotinLinker,
    BivalentTarget,
    CostEstimate,
    DeltaForgeBestPair,
    DeltaForgePairScore,
    DeltaForgeScore,
    EcTrimmingConfig,
    GeneSummary,
    GenerationMaskGuidance,
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

__all__ = [
    "PROTEINVIEW_ATTRIBUTION",
    "AccountBalance",
    "AdaptyvExperiment",
    "AdaptyvSequence",
    "AdaptyvTarget",
    "AsyncJob",
    "AsyncLigandAI",
    "AsyncReceptorDBClient",
    "AutoTopupConfig",
    "BindingOrientationResult",
    "BiotinLinker",
    "BivalentTarget",
    "CostEstimate",
    "DashboardHandle",
    "DeltaForgeBestPair",
    "DeltaForgePairScore",
    "DeltaForgeScore",
    "EcTrimmingConfig",
    "GeneSummary",
    "GenerationMaskGuidance",
    "Job",
    "LigandAI",
    "LigandAIAuthError",
    "LigandAICreditError",
    "LigandAIError",
    "LigandAINotFoundError",
    "LigandAIPaidTierRequired",
    "LigandAIRateLimitError",
    "LigandAIServerError",
    "LigandAITierError",
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
    "launch_proteinview",
    "load_peptide_results",
    "rank_peptides",
    "serve_dashboard",
    "write_dashboard",
]
