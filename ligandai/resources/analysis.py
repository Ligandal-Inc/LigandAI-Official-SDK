# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Structure / sequence analysis endpoints — interface residues, immunogenicity,
and E. coli cell-free expression risk.

These wrap the platform's SDK-capability routes:

- :meth:`Analysis.interface_residues` → ``POST /api/analysis/interface``
- :meth:`Analysis.immunogenicity`      → ``POST /api/analysis/immunogenicity``
- :meth:`Analysis.batch_immunogenicity`→ ``POST /api/analysis/immunogenicity/batch``
- :meth:`Analysis.score_expression`    → ``POST /api/synthesis/score-expression``

The immunogenicity routes inherit the upstream multi-organism MHC scanner's
pro-feature gate, rate limiter, and quota (they forward in-process with the
caller's credentials), so a free-tier key will surface the upstream 402/403
verbatim rather than a faked result.
"""

from __future__ import annotations

from typing import Any

from ligandai.resources._base import AsyncResource, Resource

# Heavy-atom amino-acid alphabet for sequence validation (immunogenicity /
# expression scoring operate on canonical single-letter peptide sequences).
_VALID_AA = frozenset("ACDEFGHIKLMNPQRSTVWY")


def _validate_sequence(sequence: str) -> str:
    """Normalize + validate a single-letter peptide sequence."""
    seq = (sequence or "").upper().strip()
    if not seq:
        raise ValueError("sequence is required")
    bad = sorted(set(seq) - _VALID_AA)
    if bad:
        raise ValueError(
            f"Invalid amino-acid character(s) in sequence: {''.join(bad)}"
        )
    return seq


def _interface_body(
    pdb_content: str,
    chain_a: str,
    chain_b: str,
    distance_cutoff: float,
) -> dict[str, Any]:
    if not pdb_content or not isinstance(pdb_content, str):
        raise ValueError("pdb_content (PDB text) is required")
    if not chain_a or not chain_b:
        raise ValueError("chain_a and chain_b are required")
    return {
        "pdb_content": pdb_content,
        "chain_a": str(chain_a),
        "chain_b": str(chain_b),
        "distance_cutoff": distance_cutoff,
    }


def _immunogenicity_body(
    sequence: str,
    species: list[str] | None,
    threshold: float,
    include_positions: bool,
) -> dict[str, Any]:
    return {
        "sequence": _validate_sequence(sequence),
        "species": species or ["human"],
        "threshold": threshold,
        "include_positions": include_positions,
    }


def _batch_immunogenicity_body(
    sequences: list[str],
    species: list[str] | None,
    threshold: float,
) -> dict[str, Any]:
    if not isinstance(sequences, list) or not sequences:
        raise ValueError("sequences (non-empty list) is required")
    return {
        "sequences": [_validate_sequence(s) for s in sequences],
        "species": species or ["human"],
        "threshold": threshold,
    }


def _expression_body(
    sequences: str | list[str],
    include_bli: bool,
) -> dict[str, Any]:
    seqs = [sequences] if isinstance(sequences, str) else list(sequences)
    if not seqs:
        raise ValueError("sequences (non-empty) is required")
    return {
        "sequences": [_validate_sequence(s) for s in seqs],
        "include_bli": include_bli,
    }


class Analysis(Resource):
    """``/api/analysis/*`` + ``/api/synthesis/score-expression``.

    Real server endpoints (not stubs): interface residues come from the
    ``binder_design`` interface analyzer; immunogenicity forwards to the
    multi-organism MHC scanner; expression risk comes from the synthesis
    expression screener.
    """

    def interface_residues(
        self,
        pdb_content: str,
        chain_a: str,
        chain_b: str,
        *,
        distance_cutoff: float = 5.0,
    ) -> dict[str, Any]:
        """``POST /api/analysis/interface`` — interface residues between two chains.

        Real interface residues come from the ``binder_design`` interface
        analyzer, which finds heavy-atom interface contacts. The analyzer's
        contact definition is heavy-atom 8.0 Å (echoed back as
        ``distance_cutoff_used``); the ``distance_cutoff`` arg is forwarded but
        currently reserved for a future cutoff-configurable analyzer pass.

        Args:
            pdb_content: PDB text of the complex.
            chain_a: First chain ID (e.g. the receptor).
            chain_b: Second chain ID (e.g. the binder/peptide).
            distance_cutoff: Requested contact distance in Å (advisory).

        Returns:
            ``{chain_a, chain_b, chain_a_residues, chain_b_residues,
            distance_cutoff_used, contact_definition}``.
        """
        body = _interface_body(pdb_content, chain_a, chain_b, distance_cutoff)
        return self._transport.request("POST", "/api/analysis/interface", json=body) or {
            "chain_a": str(chain_a),
            "chain_b": str(chain_b),
            "chain_a_residues": [],
            "chain_b_residues": [],
        }

    def immunogenicity(
        self,
        sequence: str,
        *,
        species: list[str] | None = None,
        threshold: float = 0.3,
        include_positions: bool = False,
    ) -> dict[str, Any]:
        """``POST /api/analysis/immunogenicity`` — MHC-binding (immunogenicity) risk.

        Forwards to the multi-organism MHC scanner, so the pro-feature gate +
        quota apply. Lower ``score`` is better; ``passes`` is ``score <=
        threshold``.

        Args:
            sequence: Peptide sequence (single-letter).
            species: Organisms to scan (default ``["human"]``).
            threshold: Pass/fail cutoff on the cross-species score.
            include_positions: Include per-position high-risk data + MHC-I motifs.

        Returns:
            ``{sequence, species, threshold, score, passes, per_species,
            recommendations, [high_risk_positions, mhc_i_motifs]}``.
        """
        body = _immunogenicity_body(sequence, species, threshold, include_positions)
        return self._transport.request(
            "POST", "/api/analysis/immunogenicity", json=body
        ) or {}

    def batch_immunogenicity(
        self,
        sequences: list[str],
        *,
        species: list[str] | None = None,
        threshold: float = 0.3,
    ) -> list[dict[str, Any]]:
        """``POST /api/analysis/immunogenicity/batch`` — score many sequences.

        Each sequence is metered through the single-sequence path (no batch
        bypass of the pro-gate/quota). A hard auth/quota failure (401/402/403)
        stops the batch and raises the upstream error; per-sequence soft errors
        are returned inline as ``{sequence, error}`` rows.

        Args:
            sequences: Peptide sequences (max 100 server-side).
            species: Organisms to scan (default ``["human"]``).
            threshold: Pass/fail cutoff.

        Returns:
            List of ``{sequence, score, passes, species, per_species}`` (or
            ``{sequence, error}``) rows.
        """
        body = _batch_immunogenicity_body(sequences, species, threshold)
        payload = self._transport.request(
            "POST", "/api/analysis/immunogenicity/batch", json=body
        ) or {}
        return payload.get("scores", []) if isinstance(payload, dict) else (payload or [])

    def score_expression(
        self,
        sequences: str | list[str],
        *,
        include_bli: bool = True,
    ) -> dict[str, Any]:
        """``POST /api/synthesis/score-expression`` — E. coli cell-free expression risk.

        Assesses sequence-intrinsic expression risk (hydrophobicity/GRAVY,
        charge clustering, terminal basic residues, …), optionally with BLI
        experiment suitability.

        Args:
            sequences: A single sequence or list of peptide sequences.
            include_bli: Include BLI suitability scoring.

        Returns:
            ``{results: [...], ranking: [...]}`` per-sequence expression risk.
        """
        body = _expression_body(sequences, include_bli)
        return self._transport.request(
            "POST", "/api/synthesis/score-expression", json=body
        ) or {}


class AsyncAnalysis(AsyncResource):
    """Async variant of :class:`Analysis` — identical method surface."""

    async def interface_residues(
        self,
        pdb_content: str,
        chain_a: str,
        chain_b: str,
        *,
        distance_cutoff: float = 5.0,
    ) -> dict[str, Any]:
        """Async variant of :meth:`Analysis.interface_residues`."""
        body = _interface_body(pdb_content, chain_a, chain_b, distance_cutoff)
        return await self._transport.request(
            "POST", "/api/analysis/interface", json=body
        ) or {
            "chain_a": str(chain_a),
            "chain_b": str(chain_b),
            "chain_a_residues": [],
            "chain_b_residues": [],
        }

    async def immunogenicity(
        self,
        sequence: str,
        *,
        species: list[str] | None = None,
        threshold: float = 0.3,
        include_positions: bool = False,
    ) -> dict[str, Any]:
        """Async variant of :meth:`Analysis.immunogenicity`."""
        body = _immunogenicity_body(sequence, species, threshold, include_positions)
        return await self._transport.request(
            "POST", "/api/analysis/immunogenicity", json=body
        ) or {}

    async def batch_immunogenicity(
        self,
        sequences: list[str],
        *,
        species: list[str] | None = None,
        threshold: float = 0.3,
    ) -> list[dict[str, Any]]:
        """Async variant of :meth:`Analysis.batch_immunogenicity`."""
        body = _batch_immunogenicity_body(sequences, species, threshold)
        payload = await self._transport.request(
            "POST", "/api/analysis/immunogenicity/batch", json=body
        ) or {}
        return payload.get("scores", []) if isinstance(payload, dict) else (payload or [])

    async def score_expression(
        self,
        sequences: str | list[str],
        *,
        include_bli: bool = True,
    ) -> dict[str, Any]:
        """Async variant of :meth:`Analysis.score_expression`."""
        body = _expression_body(sequences, include_bli)
        return await self._transport.request(
            "POST", "/api/synthesis/score-expression", json=body
        ) or {}
