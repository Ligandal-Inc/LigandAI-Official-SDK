# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""DeltaForge thermodynamic scoring — PDB, existing fold, and batch.

This namespace groups the three user-facing DeltaForge scoring surfaces so AI
coding assistants and humans find a single, unambiguous entry point. The
canonical HTTP endpoints are:

  * ``client.deltaforge.score_pdb(...)``        -> ``POST /api/v1/deltaforge/score-pdb``
  * ``client.deltaforge.score_fold(...)``       -> ``POST /api/v1/deltaforge/score-fold``
  * ``client.deltaforge.batch_score_fold(...)`` -> ``POST /api/v1/deltaforge/batch-score-fold``

Auth is ``Authorization: Bearer <api_key>`` (handled by the client). The online
production scorer is selected server-side (``scorer="auto"``); the returned
:attr:`DeltaForgeScore.scorer_version` records exactly which model ran.

There is NO ``/api/binder-scoring/deltaforge`` endpoint — that path does not
exist and returns 404. Use the paths above.

Credits are charged ON SUCCESS only. ``score_fold`` scores a fold the user has
already run (no re-fold) by passing ``fold_job_id=``; chain mapping is inferred
from the fold's stored chain metadata (receptor chains first, peptide/binder
last) unless ``receptor_chains=``/``peptide_chain=`` are passed explicitly.

``include_pae=True`` attaches the NxN PAE matrix (Angstroms) on
:attr:`DeltaForgeScore.pae` when the artifact is resolvable; otherwise ``pae``
is ``None`` and ``pae_status`` is ``'pending'`` or ``'unavailable'``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ligandai.resources._base import AsyncResource, Resource
from ligandai.resources.peptides import (
    _DeltaForgeAggregateMethod,
    _DeltaForgeScorer,
    _parse_deltaforge_score,
)
from ligandai.types import DeltaForgeScore

_SCORE_PDB = "/api/v1/deltaforge/score-pdb"
_SCORE_FOLD = "/api/v1/deltaforge/score-fold"
_BATCH_SCORE_FOLD = "/api/v1/deltaforge/batch-score-fold"


def _score_pdb_body(
    *,
    pdb_content: str | None,
    pdb_file: str | Path | None,
    receptor_chains: list[str] | None,
    peptide_chain: str | None,
    chain_a: str | None,
    chain_b: str | None,
    scorer: _DeltaForgeScorer,
    aggregate_method: _DeltaForgeAggregateMethod,
    include_features: bool,
    include_pae: bool,
) -> dict:
    if not pdb_content and not pdb_file:
        raise ValueError("Pass pdb_content= or pdb_file=")
    if pdb_content and pdb_file:
        raise ValueError("Pass only one of pdb_content= or pdb_file=")
    content = pdb_content if pdb_content is not None else Path(pdb_file).read_text()
    receptors = receptor_chains or ([chain_a] if chain_a else None)
    peptide = peptide_chain or chain_b
    if not receptors or not peptide:
        raise ValueError("Pass receptor_chains= and peptide_chain=, or chain_a= and chain_b=")
    return {
        "pdbContent": content,
        "receptorChains": receptors,
        "peptideChain": peptide,
        "scorer": scorer,
        "aggregateMethod": aggregate_method,
        "includeFeatures": include_features,
        "includePae": include_pae,
    }


def _score_fold_body(
    *,
    fold_job_id: str,
    receptor_chains: list[str] | None,
    peptide_chain: str | None,
    scorer: _DeltaForgeScorer,
    aggregate_method: _DeltaForgeAggregateMethod,
    include_features: bool,
    include_pae: bool,
) -> dict:
    if not fold_job_id or not str(fold_job_id).strip():
        raise ValueError("fold_job_id= is required")
    body: dict[str, Any] = {
        "foldJobId": str(fold_job_id).strip(),
        "scorer": scorer,
        "aggregateMethod": aggregate_method,
        "includeFeatures": include_features,
        "includePae": include_pae,
    }
    if receptor_chains is not None:
        body["receptorChains"] = receptor_chains
    if peptide_chain is not None:
        body["peptideChain"] = peptide_chain
    return body


def _batch_body(
    *,
    fold_job_ids: list[str],
    scorer: _DeltaForgeScorer,
    aggregate_method: _DeltaForgeAggregateMethod,
    include_features: bool,
    include_pae: bool,
) -> dict:
    ids = [str(x).strip() for x in (fold_job_ids or []) if str(x).strip()]
    if not ids:
        raise ValueError("fold_job_ids= (non-empty list) is required")
    return {
        "foldJobIds": ids,
        "scorer": scorer,
        "aggregateMethod": aggregate_method,
        "includeFeatures": include_features,
        "includePae": include_pae,
    }


class DeltaForge(Resource):
    """``client.deltaforge`` — thermodynamic scoring (PDB / fold / batch)."""

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
    ) -> DeltaForgeScore:
        """Score a user-provided PDB. ``POST /api/v1/deltaforge/score-pdb``."""
        body = _score_pdb_body(
            pdb_content=pdb_content, pdb_file=pdb_file,
            receptor_chains=receptor_chains, peptide_chain=peptide_chain,
            chain_a=chain_a, chain_b=chain_b, scorer=scorer,
            aggregate_method=aggregate_method, include_features=include_features,
            include_pae=include_pae,
        )
        payload = self._transport.request("POST", _SCORE_PDB, json=body) or {}
        return _parse_deltaforge_score(payload)

    def score_fold(
        self,
        fold_job_id: str,
        *,
        receptor_chains: list[str] | None = None,
        peptide_chain: str | None = None,
        scorer: _DeltaForgeScorer = "auto",
        aggregate_method: _DeltaForgeAggregateMethod = "boltzmann_parallel",
        include_features: bool = False,
        include_pae: bool = False,
    ) -> DeltaForgeScore:
        """Score a fold the user already ran (no re-fold).

        ``POST /api/v1/deltaforge/score-fold``. The stored PDB + fold confidence
        metrics (iptm/ptm/ipsae/plddt) are pulled server-side and forwarded into
        the binder/non-binder gate; those metrics are echoed back on the result.
        Chain mapping is inferred from the fold's chain metadata unless you pass
        ``receptor_chains=``/``peptide_chain=``.
        """
        body = _score_fold_body(
            fold_job_id=fold_job_id, receptor_chains=receptor_chains,
            peptide_chain=peptide_chain, scorer=scorer,
            aggregate_method=aggregate_method, include_features=include_features,
            include_pae=include_pae,
        )
        payload = self._transport.request("POST", _SCORE_FOLD, json=body) or {}
        return _parse_deltaforge_score(payload)

    def batch_score_fold(
        self,
        fold_job_ids: list[str],
        *,
        scorer: _DeltaForgeScorer = "auto",
        aggregate_method: _DeltaForgeAggregateMethod = "boltzmann_parallel",
        include_features: bool = False,
        include_pae: bool = False,
    ) -> dict[str, Any]:
        """DeltaForge-score many existing folds in one call.

        ``POST /api/v1/deltaforge/batch-score-fold``. Returns the raw envelope
        ``{success, scored, failed, results: [...], errors: [...]}`` where each
        ``results[i]`` carries sequence, iptm/ptm/ipsae/plddt, delta_g, kd_nm,
        classification (and ``pae`` when ``include_pae=True``). Credits are
        charged only for folds that scored successfully.
        """
        body = _batch_body(
            fold_job_ids=fold_job_ids, scorer=scorer,
            aggregate_method=aggregate_method, include_features=include_features,
            include_pae=include_pae,
        )
        return self._transport.request("POST", _BATCH_SCORE_FOLD, json=body) or {}

    def batch_score_fold_csv(
        self,
        fold_job_ids: list[str],
        *,
        scorer: _DeltaForgeScorer = "auto",
        aggregate_method: _DeltaForgeAggregateMethod = "boltzmann_parallel",
        include_features: bool = False,
        include_pae: bool = False,
    ) -> str:
        """Same as :meth:`batch_score_fold` but returns a CSV string.

        ``POST /api/v1/deltaforge/batch-score-fold?format=csv``. Columns include
        the fold metrics and, when ``include_pae=True``, ``pae_shapeN`` /
        ``pae_status`` so large NxN matrices are summarized rather than inlined.
        """
        body = _batch_body(
            fold_job_ids=fold_job_ids, scorer=scorer,
            aggregate_method=aggregate_method, include_features=include_features,
            include_pae=include_pae,
        )
        resp = self._transport.request(
            "POST", _BATCH_SCORE_FOLD, json=body, params={"format": "csv"},
            headers={"Accept": "text/csv"}, expect_json=False,
        )
        return getattr(resp, "text", "") or ""


class AsyncDeltaForge(AsyncResource):
    """Async ``client.deltaforge`` — thermodynamic scoring (PDB / fold / batch)."""

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
    ) -> DeltaForgeScore:
        """Async variant of :meth:`DeltaForge.score_pdb`."""
        body = _score_pdb_body(
            pdb_content=pdb_content, pdb_file=pdb_file,
            receptor_chains=receptor_chains, peptide_chain=peptide_chain,
            chain_a=chain_a, chain_b=chain_b, scorer=scorer,
            aggregate_method=aggregate_method, include_features=include_features,
            include_pae=include_pae,
        )
        payload = await self._transport.request("POST", _SCORE_PDB, json=body) or {}
        return _parse_deltaforge_score(payload)

    async def score_fold(
        self,
        fold_job_id: str,
        *,
        receptor_chains: list[str] | None = None,
        peptide_chain: str | None = None,
        scorer: _DeltaForgeScorer = "auto",
        aggregate_method: _DeltaForgeAggregateMethod = "boltzmann_parallel",
        include_features: bool = False,
        include_pae: bool = False,
    ) -> DeltaForgeScore:
        """Async variant of :meth:`DeltaForge.score_fold`."""
        body = _score_fold_body(
            fold_job_id=fold_job_id, receptor_chains=receptor_chains,
            peptide_chain=peptide_chain, scorer=scorer,
            aggregate_method=aggregate_method, include_features=include_features,
            include_pae=include_pae,
        )
        payload = await self._transport.request("POST", _SCORE_FOLD, json=body) or {}
        return _parse_deltaforge_score(payload)

    async def batch_score_fold(
        self,
        fold_job_ids: list[str],
        *,
        scorer: _DeltaForgeScorer = "auto",
        aggregate_method: _DeltaForgeAggregateMethod = "boltzmann_parallel",
        include_features: bool = False,
        include_pae: bool = False,
    ) -> dict[str, Any]:
        """Async variant of :meth:`DeltaForge.batch_score_fold`."""
        body = _batch_body(
            fold_job_ids=fold_job_ids, scorer=scorer,
            aggregate_method=aggregate_method, include_features=include_features,
            include_pae=include_pae,
        )
        return await self._transport.request("POST", _BATCH_SCORE_FOLD, json=body) or {}
