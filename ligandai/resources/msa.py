# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""MSA (Multiple Sequence Alignment) generation.

Endpoint:
- :meth:`MSA.generate` → ``POST /api/msa/generate``

The platform runs a self-hosted mmseqs2-based MSA service backed by the
UniRef30 + ColabFoldDB databases. Cache hits return in under 100 ms; fresh
searches take ~60 s (first time a novel sequence is seen).

Available to all tiers including free.

Example::

    from ligandai import LigandAI

    client = LigandAI()

    result = client.msa.generate(
        sequences={"A": "MGHHHHHHSSGVDLGTENLYFQSMGHHHHHHSSGVDLGT..."},
        gene="IL31RA",
    )
    print(f"Chain A: {result.chains['A'].hits} homologs, cached={result.cached}")

    # Write Boltz-2-compatible CSV for downstream folding:
    for chain_id, chain in result.chains.items():
        with open(f"msa_{chain_id}.csv", "w") as f:
            f.write(chain.csv)
"""

from __future__ import annotations

from ligandai.resources._base import AsyncResource, Resource
from ligandai.resources.msa_contract import PreparedMSAResult, validate_prepared_msa


class MSA(Resource):
    """MSA generation resource."""

    def generate(
        self,
        sequences: dict[str, str],
        gene: str | None = None,
    ) -> PreparedMSAResult:
        """``POST /api/msa/generate`` — generate MSA for one or more protein chains.

        Args:
            sequences: Mapping of chain ID to amino acid sequence,
                e.g. ``{"A": "MVLSPADKTNVK..."}``. Up to 20 chains; each
                sequence must be 10-5,000 residues.
            gene: Optional HGNC gene symbol (e.g. ``"IL31RA"``). Used for
                caching and search-intelligence logging; pass it whenever
                you know the target gene.

        Returns:
            A compatible MSAResult subclass with per-chain CSV data, hit
            counts and a ``cached`` flag. ``chain.prepared`` binds the exact
            query and CSV bytes after alignment validation; it does not claim
            native featurization or model consumption.

        Raises:
            :class:`~ligandai.errors.LigandAIError`: on validation errors or
                service unavailability.
        """
        payload: dict = {"sequences": sequences}
        if gene is not None:
            payload["gene"] = gene
        raw = self._transport.request("POST", "/api/msa/generate", json=payload) or {}
        return validate_prepared_msa(raw, sequences)


class AsyncMSA(AsyncResource):
    async def generate(
        self,
        sequences: dict[str, str],
        gene: str | None = None,
    ) -> PreparedMSAResult:
        payload: dict = {"sequences": sequences}
        if gene is not None:
            payload["gene"] = gene
        raw = await self._transport.request("POST", "/api/msa/generate", json=payload) or {}
        return validate_prepared_msa(raw, sequences)
