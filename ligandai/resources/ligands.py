# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Small-molecule Kd scoring (LF-SM v3) — FREE-TIER accessible.

Refs: bd-dre-0meky

``client.ligands.score_ligand(...)`` scores a (holo structure, ligand SMILES)
pair with the LF-SM v3 KdHead and returns a predicted pKd + binder probability.

Unlike most of the v0.2.0 surface, THIS method is free-tier accessible: it does
NOT call ``_require_paid_tier`` / ``_require_feature``, so a ``lgai_free_`` key
works (subject to a per-key daily quota enforced server-side). This is an
intentional growth carve-out scoped to this single endpoint.

Scoring requires a HOLO complex — the ligand must be present as a HETATM block
in the uploaded PDB (or in a prior holo co-fold). A bare SMILES against an apo
structure is not directly scorable on the free CPU path; run a credit-gated
co-fold first to obtain a docked pose. When no scorable ligand is found the
server returns HTTP 422, surfaced here as a :class:`LigandAIError` carrying the
server hint.
"""

from __future__ import annotations

from pathlib import Path

from ligandai.resources._base import AsyncResource, Resource
from ligandai.types import LigandScore

_ENDPOINT = "/api/v1/score/ligand"


def _build_payload(
    *,
    pdb_content: str | None,
    pdb_file: str | Path | None,
    ligand_smiles: str,
    fold_id: str | None,
    session_id: str | None,
    receptor_chains: list[str] | None,
    het_code: str | None,
    pocket_residues: list[int] | None,
) -> dict:
    """Validate inputs and assemble the camelCase request body.

    Exactly one structure source is required when no fold reference is given:
    ``pdb_content=`` or ``pdb_file=``. A ``fold_id``/``session_id`` reference is
    accepted for forward-compat (resolved server-side) but the free CPU path
    expects PDB text today.
    """
    if not ligand_smiles or not str(ligand_smiles).strip():
        raise ValueError("ligand_smiles= is required")

    if pdb_content and pdb_file:
        raise ValueError("Pass only one of pdb_content= or pdb_file=")

    content: str | None = None
    if pdb_content is not None:
        content = pdb_content
    elif pdb_file is not None:
        content = Path(pdb_file).read_text()

    fold_ref: dict | None = None
    if session_id or fold_id:
        fold_ref = {}
        if session_id:
            fold_ref["sessionId"] = session_id
        if fold_id:
            fold_ref["foldId"] = fold_id

    if content is None and fold_ref is None:
        raise ValueError(
            "Pass pdb_content= or pdb_file= (a holo complex), "
            "or a fold reference (session_id=/fold_id=)"
        )

    body: dict = {"ligandSmiles": str(ligand_smiles).strip()}
    if content is not None:
        body["pdbContent"] = content
    if fold_ref is not None:
        body["foldRef"] = fold_ref
    if receptor_chains is not None:
        body["receptorChains"] = receptor_chains
    if het_code is not None:
        body["hetCode"] = het_code
    if pocket_residues is not None:
        body["pocketResidues"] = pocket_residues
    return body


class Ligands(Resource):
    """``/api/v1/score/ligand`` — small-molecule Kd scoring (free-tier)."""

    def score_ligand(
        self,
        *,
        pdb_content: str | None = None,
        pdb_file: str | Path | None = None,
        ligand_smiles: str,
        fold_id: str | None = None,
        session_id: str | None = None,
        receptor_chains: list[str] | None = None,
        het_code: str | None = None,
        pocket_residues: list[int] | None = None,
    ) -> LigandScore:
        """Score one (holo structure, ligand SMILES) pair -> pKd + binder prob.

        Free-tier accessible. Pass ``pdb_content=`` or ``pdb_file=`` (a holo
        complex: protein + bound ligand HETATM) plus ``ligand_smiles=``. When
        the structure carries several ligands, ``het_code=`` forces a specific
        CCD code; otherwise the matching HET is selected by InChIKey skeleton.

        Returns a :class:`LigandScore`. Raises :class:`ValueError` for bad
        inputs and :class:`ligandai.errors.LigandAIError` when the server
        cannot score (e.g. no scorable HET ligand -> HTTP 422, or research
        backend unavailable -> HTTP 501).
        """
        body = _build_payload(
            pdb_content=pdb_content,
            pdb_file=pdb_file,
            ligand_smiles=ligand_smiles,
            fold_id=fold_id,
            session_id=session_id,
            receptor_chains=receptor_chains,
            het_code=het_code,
            pocket_residues=pocket_residues,
        )
        payload = self._transport.request("POST", _ENDPOINT, json=body) or {}
        return LigandScore.model_validate(payload)

    def health(self) -> dict:
        """Cheap availability probe for the scorer backend (no model load)."""
        return self._transport.request("GET", f"{_ENDPOINT}/health") or {}


class AsyncLigands(AsyncResource):
    """Async ``/api/v1/score/ligand`` — small-molecule Kd scoring (free-tier)."""

    async def score_ligand(
        self,
        *,
        pdb_content: str | None = None,
        pdb_file: str | Path | None = None,
        ligand_smiles: str,
        fold_id: str | None = None,
        session_id: str | None = None,
        receptor_chains: list[str] | None = None,
        het_code: str | None = None,
        pocket_residues: list[int] | None = None,
    ) -> LigandScore:
        """Async variant of :meth:`Ligands.score_ligand`."""
        body = _build_payload(
            pdb_content=pdb_content,
            pdb_file=pdb_file,
            ligand_smiles=ligand_smiles,
            fold_id=fold_id,
            session_id=session_id,
            receptor_chains=receptor_chains,
            het_code=het_code,
            pocket_residues=pocket_residues,
        )
        payload = await self._transport.request("POST", _ENDPOINT, json=body) or {}
        return LigandScore.model_validate(payload)

    async def health(self) -> dict:
        """Async cheap availability probe for the scorer backend."""
        return await self._transport.request("GET", f"{_ENDPOINT}/health") or {}
