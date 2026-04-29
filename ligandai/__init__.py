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
    LigandAIRateLimitError,
    LigandAIServerError,
    LigandAITierError,
    LigandAIValidationError,
    NotSupportedOnReceptorDB,
)
from ligandai.jobs import AsyncJob, Job
from ligandai.receptordb import AsyncReceptorDBClient, ReceptorDBClient
from ligandai.types import (
    BivalentTarget,
    LinkerConfig,
    PeptideInput,
    ResidueRange,
    SynthesisPeptide,
    TargetGroup,
)

__all__ = [
    "AsyncJob",
    "AsyncLigandAI",
    "AsyncReceptorDBClient",
    # Types
    "BivalentTarget",
    # Jobs
    "Job",
    # Clients
    "LigandAI",
    "LigandAIAuthError",
    "LigandAICreditError",
    # Errors
    "LigandAIError",
    "LigandAINotFoundError",
    "LigandAIRateLimitError",
    "LigandAIServerError",
    "LigandAITierError",
    "LigandAIValidationError",
    "LinkerConfig",
    "NotSupportedOnReceptorDB",
    "PeptideInput",
    "ReceptorDBClient",
    "ResidueRange",
    "SynthesisPeptide",
    "TargetGroup",
    "__version__",
]
