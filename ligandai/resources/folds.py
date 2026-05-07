# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Fold-result filtering and pocket-expansion endpoints.

Stream D / W4 (dre-twv8o). Wraps:

- :meth:`Folds.partition_by_hotspot` → ``POST /api/folds/partition-by-hotspot``
- :meth:`Folds.expand_hotspot`       → ``GET  /api/folds/expand-hotspot``

Example workflow::

    from ligandai import LigandAI

    client = LigandAI()

    # 1. Auto-expand a single hotspot to surrounding pocket residues (8 Å).
    pocket = client.folds.expand_hotspot(
        session_id="ptf_abc123",
        chain="C",
        residue=148,
        radius_a=8.0,
    )

    # 2. Partition the session's folds by direct contact with CYS148 (5 Å)
    #    and proximity to the auto-expanded pocket.
    result = client.folds.partition_by_hotspot(
        session_id="ptf_abc123",
        hotspots=[{"chain": "C", "residue": 148, "numbering": "pdb"}],
        pocket_residues=[
            {"chain": pr["chain"], "residue": pr["residue"], "numbering": "pdb"}
            for pr in pocket["pocket_residues"]
        ],
        distance_threshold_a=5.0,
    )

    print(f"{result['stats']['passes_hotspot']} of {result['stats']['total']} "
          f"peptides hit CYS148; "
          f"{result['stats']['passes_pocket']} more landed in the pocket.")
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from ligandai.resources._base import AsyncResource, Resource


class HotspotSpec(TypedDict, total=False):
    """One hotspot or pocket residue.

    ``numbering`` defaults to ``"pdb"`` when omitted — i.e. the residue number
    matches the user-uploaded PDB. Use ``"boltz"`` only if you know you're
    passing a Boltz-2 internal index.
    """

    chain: str
    residue: int
    numbering: Literal["pdb", "boltz"]


class Folds(Resource):
    """``/api/folds/*`` — hotspot/pocket fold-result filtering."""

    def partition_by_hotspot(
        self,
        session_id: str,
        hotspots: list[HotspotSpec] | None = None,
        pocket_residues: list[HotspotSpec] | None = None,
        distance_threshold_a: float = 5.0,
    ) -> dict[str, Any]:
        """Partition a session's fold results by hotspot/pocket contact.

        :param session_id:           PTF session ID.
        :param hotspots:             Residues the peptide MUST directly contact
                                     (≤ ``distance_threshold_a``) to land in
                                     ``passes_hotspot``.
        :param pocket_residues:      Residues that count for ``passes_pocket``.
                                     Folds that pass ``hotspots`` are NOT also
                                     placed here — hotspot match takes priority.
        :param distance_threshold_a: Heavy-atom min-distance cutoff in Å.

        :returns: Dict with ``passes_hotspot``, ``passes_pocket``,
                  ``wrong_interface``, and ``stats`` keys. See docstring for
                  the example workflow.
        """
        body: dict[str, Any] = {
            "session_id": session_id,
            "hotspots": list(hotspots or []),
            "pocket_residues": list(pocket_residues or []),
            "distance_threshold_a": float(distance_threshold_a),
        }
        return self._transport.request(
            "POST", "/api/folds/partition-by-hotspot", json=body,
        ) or {}

    def expand_hotspot(
        self,
        session_id: str,
        chain: str,
        residue: int,
        radius_a: float = 8.0,
    ) -> dict[str, Any]:
        """Expand a single hotspot to surrounding residues within ``radius_a``.

        Picks the most recent fold result in the session that has a PDB and
        runs a heavy-atom distance scan around the hotspot. Returns a list of
        residues across all chains (pockets often span chain boundaries).

        :returns: Dict with ``pocket_residues`` (sorted by distance ascending)
                  plus ``hotspot_residue`` and ``radius_a``.
        """
        params = {
            "session_id": session_id,
            "chain": chain,
            "residue": int(residue),
            "radius_a": float(radius_a),
        }
        return self._transport.request(
            "GET", "/api/folds/expand-hotspot", params=params,
        ) or {}


class AsyncFolds(AsyncResource):
    """Async mirror of :class:`Folds`."""

    async def partition_by_hotspot(
        self,
        session_id: str,
        hotspots: list[HotspotSpec] | None = None,
        pocket_residues: list[HotspotSpec] | None = None,
        distance_threshold_a: float = 5.0,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "session_id": session_id,
            "hotspots": list(hotspots or []),
            "pocket_residues": list(pocket_residues or []),
            "distance_threshold_a": float(distance_threshold_a),
        }
        return await self._transport.request(
            "POST", "/api/folds/partition-by-hotspot", json=body,
        ) or {}

    async def expand_hotspot(
        self,
        session_id: str,
        chain: str,
        residue: int,
        radius_a: float = 8.0,
    ) -> dict[str, Any]:
        params = {
            "session_id": session_id,
            "chain": chain,
            "residue": int(residue),
            "radius_a": float(radius_a),
        }
        return await self._transport.request(
            "GET", "/api/folds/expand-hotspot", params=params,
        ) or {}
