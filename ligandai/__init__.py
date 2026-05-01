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
from ligandai.receptordb import AsyncReceptorDBClient, ReceptorDBClient
from ligandai.types import (
    AccountBalance,
    AutoTopupConfig,
    BivalentTarget,
    CostEstimate,
    DeltaForgeBestPair,
    DeltaForgePairScore,
    DeltaForgeScore,
    GeneSummary,
    LinkerConfig,
    MSAChain,
    MSAResult,
    Peptide,
    PeptideDetail,
    PeptideInput,
    ResidueRange,
    SynthesisPeptide,
    TargetGroup,
    TopUpResult,
)

__all__ = [
    "AccountBalance",
    "AsyncJob",
    "AsyncLigandAI",
    "AsyncReceptorDBClient",
    "AutoTopupConfig",
    # Types
    "BivalentTarget",
    "CostEstimate",
    "DeltaForgeBestPair",
    "DeltaForgePairScore",
    "DeltaForgeScore",
    "GeneSummary",
    # Jobs
    "Job",
    # Clients
    "LigandAI",
    "LigandAIAuthError",
    "LigandAICreditError",
    # Errors
    "LigandAIError",
    "LigandAINotFoundError",
    "LigandAIPaidTierRequired",
    "LigandAIRateLimitError",
    "LigandAIServerError",
    "LigandAITierError",
    "LigandAIValidationError",
    "LinkerConfig",
    "MSAChain",
    "MSAResult",
    "NotSupportedOnReceptorDB",
    "Peptide",
    "PeptideDetail",
    "PeptideInput",
    "ReceptorDBClient",
    "ResidueRange",
    "SynthesisPeptide",
    "TargetGroup",
    "TopUpResult",
    "__version__",
]
