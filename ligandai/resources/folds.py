# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Fold-result filtering and pocket-expansion endpoints.

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

import io
from typing import TYPE_CHECKING, Any, Literal, TypedDict

from ligandai.resources._base import AsyncResource, Resource

if TYPE_CHECKING:
    import numpy as np  # noqa: F401  (used in type hints only)


# Default scale used when the server omits the X-Pae-Scale-Angstrom header.
# Encodes the [0, 32] Å PAE range into a uint8 (32 / 255).
_DEFAULT_PAE_SCALE_ANGSTROM = 32.0 / 255.0


# ⛔ WRONG-IDENTIFIER GUARD.
#
# ``GET /api/v1/folds/:foldId/pae`` parses the path segment with
# ``parseInt(foldIdRaw, 10)`` and looks it up as ``ptf_fold_results.id``
# (server/pae-routes.ts:70-95). It is a fold-ROW primary key, not a job id.
#
# The hazard is not that a job id 404s — it is that job ids are routinely
# all-digit strings, so ``download_pae(fold.job_id)`` parses cleanly to some
# unrelated row number and can return **another fold's matrix with a 200**.
# Silent wrong data beats a loud error every time, so refuse it here.
_PAE_ID_HELP = (
    "This endpoint takes ptf_fold_results.id — the integer primary key of the "
    "fold row. Use `peptide.fold_id` from peptides.search()/list(), or `row['id']` "
    "from structures.list(). FoldResult.job_id is a JOB id and is NOT accepted: "
    "job ids are often all-digit, so the server would parse one as a row id and "
    "return a different fold's matrix."
)


def _resolve_fold_result_id(
    fold_result_id: int | str | None,
    fold_id: int | str | None = None,
) -> int:
    """Validate and normalise the ``ptf_fold_results.id`` path parameter."""
    if fold_result_id is not None and fold_id is not None and str(fold_result_id) != str(fold_id):
        raise ValueError(
            "Pass either fold_result_id or its deprecated alias fold_id, not both "
            f"with different values (got {fold_result_id!r} and {fold_id!r})."
        )
    raw = fold_result_id if fold_result_id is not None else fold_id
    if raw is None:
        raise ValueError(f"fold_result_id is required. {_PAE_ID_HELP}")
    if isinstance(raw, bool):  # bool is an int subclass; never a valid PK.
        raise ValueError(f"fold_result_id must be a positive integer, got {raw!r}. {_PAE_ID_HELP}")
    if isinstance(raw, int):
        value = raw
    else:
        text = str(raw).strip()
        if not text.isdigit():
            raise ValueError(
                f"fold_result_id must be a positive integer primary key, got {raw!r}. "
                f"{_PAE_ID_HELP}"
            )
        value = int(text)
    if value <= 0:
        raise ValueError(
            f"fold_result_id must be a positive integer primary key, got {raw!r}. {_PAE_ID_HELP}"
        )
    return value


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

    def download_pae(
        self,
        fold_result_id: int | str | None = None,
        *,
        decode: bool = True,
        fold_id: int | str | None = None,
    ) -> "np.ndarray | bytes":
        """Download the PAE (Predicted Aligned Error) matrix for a folded structure.

        Tier-gated: academia, pro, or enterprise. Free/basic tiers raise
        :class:`LigandAITierError` client-side before the request is sent.

        ⛔ IDENTIFIER. This takes ``ptf_fold_results.id`` — the integer primary
        key of the fold ROW. It does **not** take a job id. Passing
        :attr:`FoldResult.job_id <ligandai.types.FoldResult.job_id>` is the
        classic error, and because job ids are frequently all-digit strings the
        server would happily parse one as a primary key and return **a
        different fold's matrix**. This method therefore validates the
        identifier client-side and refuses anything that is not a positive
        integer.

        Where to get the right value:

        * ``peptide.fold_id`` from :meth:`Peptides.search` / :meth:`Peptides.list`
          — this is the ``ptf_fold_results`` PK (returned as an int, normalised
          to ``str`` by the model).
        * ``row["id"]`` from :meth:`Structures.list`.

        :class:`FoldResult` — what :meth:`Peptides.fold` returns — carries only
        ``job_id`` and exposes no integer PK, so a fold result cannot be handed
        to this method directly. Look the design up via
        :meth:`Peptides.search` to obtain its ``fold_id``.

        :param fold_result_id: ``ptf_fold_results.id`` (positive integer PK).
        :param decode:  If ``True`` (default), return an NxN ``float32`` numpy
                        array in Ångströms. If ``False``, return raw uint8 bytes
                        (the on-wire ``.npy`` payload).
        :param fold_id: Deprecated alias for ``fold_result_id``, kept so
                        ``download_pae(fold_id=...)`` keyword calls written
                        against <=0.9.0 keep working.
        :returns:       ``np.ndarray`` (NxN ``float32``) when ``decode=True``,
                        else ``bytes`` (raw uint8 ``.npy`` payload).
        :raises ValueError:           identifier is missing, or is not a
                                      positive integer primary key.
        :raises LigandAITierError:    caller's tier < academia.
        :raises LigandAINotFoundError: fold not found or PAE not yet computed.
        """
        resolved = _resolve_fold_result_id(fold_result_id, fold_id)
        if self._client is not None:
            self._client._require_feature("pae_download")
        resp = self._transport.request(
            "GET",
            f"/api/v1/folds/{resolved}/pae",
            headers={"Accept": "application/octet-stream"},
            expect_json=False,
        )
        raw = resp.content
        if not decode:
            return raw
        try:
            import numpy as np  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "numpy is required to decode PAE; install with `pip install numpy` "
                "or call download_pae(decode=False) for raw bytes"
            ) from e
        arr_uint8 = np.load(io.BytesIO(raw))
        scale = float(
            resp.headers.get("X-Pae-Scale-Angstrom", str(_DEFAULT_PAE_SCALE_ANGSTROM))
        )
        return arr_uint8.astype(np.float32) * scale

    def get_pae_summary(
        self,
        fold_result_id: int | str | None = None,
        *,
        fold_id: int | str | None = None,
    ) -> dict[str, Any]:
        """Fetch PAE summary statistics — open to all tiers.

        Same identifier rules as :meth:`download_pae`: this is
        ``ptf_fold_results.id`` (positive integer PK), not a job id.

        :returns: Dict with ``shape``, ``min``, ``max``, ``mean``, ``p95``,
                  ``per_chain_pair_max``, ``scale_angstrom_per_unit``. Useful
                  for AI-chat context, plotting axes, and quick triage without
                  paying for the full matrix download.
        """
        return self._transport.request(
            "GET",
            f"/api/v1/folds/{_resolve_fold_result_id(fold_result_id, fold_id)}/pae/summary",
        ) or {}

    # ─── Fold result recovery ──────────────────────────────────────────────

    def recover(
        self,
        job_id: str,
        *,
        wait: bool = True,
        timeout: float = 120.0,
    ) -> dict[str, Any]:
        """Recover a fold whose compute job succeeded but whose result callback never landed.

        The client-side fix for the case where ``Job.wait(durable=True)`` raises
        :class:`~ligandai.errors.LigandAIWaitTimeout` with a captured
        ``call_id``. This method asks the platform to re-fetch the result for
        that job and finalize it so the structure lands in your account.

        Parameters
        ----------
        job_id : str
            The fold job id (``fold_<ms>_<rand>``) whose result needs re-pulling.
        wait : bool
            When True (default), poll the status endpoint until
            ``has_structure=True`` before returning. When False, fire-and-forget.
        timeout : float
            Maximum seconds to wait for the recovery to land when ``wait=True``.

        Returns
        -------
        dict
            Platform response: ``{success, jobId, status, has_structure,
            call_id?, iptm?, ipsae?, ...}``.

        Notes
        -----
        Only callable for jobs the caller owns. Idempotent — re-running on a
        job that already has a durable PDB returns ``{alreadyComplete: true}``
        immediately.
        """
        import time as _time
        from ligandai.errors import (
            LigandAIError,
            LigandAILegalAcceptanceRequired,
            LigandAITimeoutError,
        )

        path = f"/api/folding/jobs/{job_id}/recover-from-modal"
        body = {"wait": bool(wait), "timeout": float(timeout)}
        resp = self._transport.request("POST", path, json=body) or {}

        if not wait:
            return resp
        # Caller asked us to block — poll the status endpoint until the
        # structure lands or we exceed the budget.
        deadline = _time.monotonic() + max(timeout, 5.0)
        last_state: dict[str, Any] = dict(resp)
        while _time.monotonic() < deadline:
            try:
                state = self._transport.request("GET", f"/api/folding/jobs/{job_id}") or {}
            except LigandAILegalAcceptanceRequired:
                # ⛔ Not a transient poll failure. Retrying every 2s until the
                # budget expires ends in LigandAITimeoutError("did not land
                # structural data"), sending the user to debug a Modal
                # callback that was never broken. Surface the URL now.
                raise
            except LigandAIError:
                _time.sleep(2.0)
                continue
            last_state = state
            if state.get("hasStructure") or state.get("has_structure"):
                return {"success": True, "recovered": True, **state}
            if state.get("status") in ("failed", "cancelled", "error"):
                return {"success": False, "recovered": False, **state}
            _time.sleep(2.0)
        raise LigandAITimeoutError(
            f"Recovery for fold {job_id} did not land structural data within {timeout}s "
            f"(last state: status={last_state.get('status')!r}, "
            f"hasStructure={last_state.get('hasStructure')})"
        )


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

    async def download_pae(
        self,
        fold_result_id: int | str | None = None,
        *,
        decode: bool = True,
        fold_id: int | str | None = None,
    ) -> "np.ndarray | bytes":
        """Async mirror of :meth:`Folds.download_pae`.

        Takes ``ptf_fold_results.id`` (positive integer PK), NOT a job id — see
        :meth:`Folds.download_pae` for the identifier rules and why a job id is
        rejected client-side.
        """
        resolved = _resolve_fold_result_id(fold_result_id, fold_id)
        if self._client is not None:
            self._client._require_feature("pae_download")
        resp = await self._transport.request(
            "GET",
            f"/api/v1/folds/{resolved}/pae",
            headers={"Accept": "application/octet-stream"},
            expect_json=False,
        )
        raw = resp.content
        if not decode:
            return raw
        try:
            import numpy as np  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "numpy is required to decode PAE; install with `pip install numpy` "
                "or call download_pae(decode=False) for raw bytes"
            ) from e
        arr_uint8 = np.load(io.BytesIO(raw))
        scale = float(
            resp.headers.get("X-Pae-Scale-Angstrom", str(_DEFAULT_PAE_SCALE_ANGSTROM))
        )
        return arr_uint8.astype(np.float32) * scale

    async def get_pae_summary(
        self,
        fold_result_id: int | str | None = None,
        *,
        fold_id: int | str | None = None,
    ) -> dict[str, Any]:
        """Async mirror of :meth:`Folds.get_pae_summary` — open to all tiers.

        Takes ``ptf_fold_results.id`` (positive integer PK), not a job id.
        """
        return await self._transport.request(
            "GET",
            f"/api/v1/folds/{_resolve_fold_result_id(fold_result_id, fold_id)}/pae/summary",
        ) or {}
