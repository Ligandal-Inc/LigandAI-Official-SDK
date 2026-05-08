# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Fold-result filtering and pocket-expansion endpoints.

Stream D / W4 (dre-twv8o) + Stream Q (LIGANDAI_ALPHA_V2-7dat3, native PPI bucket).
Wraps:

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

    # 2. Partition the session's folds by direct contact with CYS148 (5 Å),
    #    proximity to the auto-expanded pocket, and the native PPI interface
    #    (e.g. BMPR1A↔RGMB chain interface for BMPR1A+RGMB folds).
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
          f"{result['stats']['passes_pocket']} more landed in the pocket; "
          f"{result['stats']['passes_native_ppi']} more landed on the "
          f"native multi-chain receptor interface.")

The four buckets are mutually exclusive — each fold lands in exactly one.
Order of precedence (highest to lowest):

    1. ``passes_hotspot``     — direct contact with a user hotspot
    2. ``passes_pocket``      — contact with a user-supplied pocket residue
    3. ``passes_native_ppi``  — contact with the native multi-chain receptor
                                 interface (auto-detected from the fold PDB
                                 by inter-chain Cα ≤ 8 Å contacts among
                                 receptor chains)
    4. ``wrong_interface``    — fold contacted something else (or had no
                                 contact data)

``passes_native_ppi`` requires a multi-chain receptor — single-chain receptors
will have an empty ``passes_native_ppi`` list. Folds in this bucket carry an
``interface_match_residues`` array describing which receptor residues from
the native interface the peptide contacted.
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
        """Partition a session's fold results into four mutually exclusive buckets.

        :param session_id:           PTF session ID.
        :param hotspots:             Residues the peptide MUST directly contact
                                     (≤ ``distance_threshold_a``) to land in
                                     ``passes_hotspot``.
        :param pocket_residues:      Residues that count for ``passes_pocket``.
                                     Folds that pass ``hotspots`` are NOT also
                                     placed here — hotspot match takes priority.
        :param distance_threshold_a: Heavy-atom min-distance cutoff in Å.

        :returns: Dict with the four bucket arrays plus ``stats``:

                  - ``passes_hotspot``     — fold contacted a user hotspot
                  - ``passes_pocket``      — fold contacted a pocket residue
                  - ``passes_native_ppi``  — fold contacted the native
                    multi-chain receptor interface (e.g. BMPR1A↔RGMB).
                    Each entry includes ``interface_match_residues`` listing
                    which native interface residues the peptide touched.
                    Empty for single-chain receptors. Added in SDK 0.5.2.
                  - ``wrong_interface``    — fold contacted something else,
                    or had no contact data (``reason='no_contact_data'``).

                  Order of precedence: hotspot > pocket > native_ppi > wrong.
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
