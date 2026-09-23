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
import math
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



# ── Panel depth / leg deepening [bd-dre-ykrlo] ──────────────────────────────

#: The SELECTIVITY gate on the binder metric, matching the server default.
#:
#: ⛔ ``peptide_ipsae`` is the binder metric. ``ipsae`` is complex-level — it
#: folds in receptor↔receptor confidence, so a detached peptide can read
#: ``ipsae`` 0.93 at ``peptide_ipsae`` 0.00. Gating a Kd ratio on ``ipsae``
#: would pool junk into the numerator.
SELECTIVITY_GATE_PEPTIDE_IPSAE = 0.67

#: Measured SD of log10(predicted Kd) WITHIN one (design × isoform) cell.
#: Between designs it is 0.569 — an ICC of 0.437, i.e. most of the spread is
#: pose noise that more trajectories average down.
SD_LOG10_KD_WITHIN_CELL = 0.646

_Z_95 = 1.96


def poses_needed_per_leg(fold_change: float) -> int:
    """Poses needed PER LEG to resolve a ``fold_change``-fold margin at 95%.

    Both legs are assumed measured to the same depth::

        SE_log10(ratio) = SD_WITHIN * sqrt(1/n_on + 1/n_off)
        half-width      = 1.96 * SE_log10

    Reproduces the platform's measured targets: 3x → 15, 2x → 36, 1.5x → 104.

    :raises ValueError: when ``fold_change`` is not greater than 1 — there is no
        finite depth at which a 1x margin becomes resolvable.
    """
    if not (fold_change > 1):
        raise ValueError(f"fold_change must be > 1, got {fold_change!r}")
    half = math.log10(fold_change)
    return math.ceil(2 * ((_Z_95 * SD_LOG10_KD_WITHIN_CELL) / half) ** 2)


def resolvable_fold_change(n_on_target: int, n_off_target: int) -> float | None:
    """Smallest margin a pair of legs at these depths can resolve at 95%.

    Returns ``None`` when either leg has no gated poses — "resolves nothing" is
    not the same as "resolves an infinite margin".

    At 10 poses per leg (a 20-pose comparison) this is 3.68x, which is why real
    panels running 11-28 poses per leg could not support a 2x claim.
    """
    if n_on_target <= 0 or n_off_target <= 0:
        return None
    se = SD_LOG10_KD_WITHIN_CELL * math.sqrt(1 / n_on_target + 1 / n_off_target)
    return 10 ** (_Z_95 * se)


class DeepenAggregation(TypedDict, total=False):
    """Where the new poses land relative to the leg's existing ones."""

    sample_idx_from: int
    sample_idx_to: int
    poses_before: int
    gated_before: int
    gate: float
    aggregates_with_existing: bool


class DeepenLeg(TypedDict, total=False):
    """The (design, receptor) leg the added trajectories were filed onto."""

    gene: str
    conformation: str | None
    peptide_length: int
    session_id: str
    leg_key: str


class GenerationEra(TypedDict, total=False):
    """The generation facts a deepen carried forward UNCHANGED.

    A deepen does not re-run the generator — the peptide is already fixed — so
    the v6.5 / v6.9 distinction is enforced by carrying this record forward
    verbatim and refusing when it cannot be established. ``pocketFeatureDim`` is
    48 for the v6.5 line and 107 for v6.9; those are three different vectors,
    not three lengths of one, so poses generated under them are not replicates
    of one estimator and must not be pooled behind one pose count.
    """

    generatorModelId: str | None
    generatorVersion: str | None
    generatorCheckpointSha256: str | None
    pocketFeatureDim: int | None
    cfgGuidanceWeight: float | None
    cfgReference: str | None
    contactHead: bool | None
    featureDenoise: bool | None
    fingerprint: str
    source: str


class DeepenResult(TypedDict, total=False):
    """Return shape of :meth:`Folds.deepen`."""

    jobId: str
    deepenOf: str
    status: str
    model: str
    seed: int
    diffusionSamples: int
    leg: DeepenLeg
    aggregation: DeepenAggregation
    era: GenerationEra
    estimatedCredits: float


class PanelLegRow(TypedDict, total=False):
    """One (design, receptor/conformation) leg of a panel."""

    design: str
    gene: str
    conformation: str | None
    total_poses: int
    gated_poses: int
    unscored_poses: int
    kd_poses: int
    geomean_kd_gated: float | None
    sd_log10_kd_observed: float | None
    mean_plddt_gated: float | None
    median_peptide_ipsae: float | None
    max_peptide_ipsae: float | None
    max_sample_idx: int | None
    distinct_structures: int
    session_ids: list[str]
    job_ids: list[str]
    first_fold_at: str | None
    last_fold_at: str | None


class PanelDesignRow(TypedDict, total=False):
    """Panel depth for one design across its isoform legs."""

    design: str
    on_target_leg: dict[str, Any] | None
    paralogs: list[dict[str, Any]]
    deciding_paralog: dict[str, Any] | None
    depth: int | None
    resolvable_fold_change: float | None
    poses_to_add: dict[str, Any] | None
    unresolved_reason: str | None
    shared_structure_warning: str | None


class PanelResult(TypedDict, total=False):
    """Return shape of :meth:`Folds.panel`."""

    gate: float
    on_target: str | None
    on_target_source: str
    on_target_note: str | None
    statistics: dict[str, Any]
    legs: list[PanelLegRow]
    designs: list[PanelDesignRow]


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


    # ─── Panel depth / leg deepening [bd-dre-ykrlo] ─────────────────────────

    def deepen(
        self,
        job_id: str,
        *,
        diffusion_samples: int,
        seed: int | None = None,
        gate: float = SELECTIVITY_GATE_PEPTIDE_IPSAE,
        wait: bool = True,
        timeout: float = 1800.0,
        poll_interval: float = 10.0,
    ) -> DeepenResult:
        """Add ``diffusion_samples`` more trajectories to the leg ``job_id`` folded.

        Selectivity is ``paralog Kd / on-target Kd`` over poses clearing
        ``peptide_ipsae >= gate``. The SD of log10(Kd) WITHIN one
        (design × isoform) cell is 0.646 against 0.569 BETWEEN designs — an ICC
        of 0.437 — so most of the spread is pose noise, and a 20-pose comparison
        resolves only ~3.7x. Resolving 2x needs 36 gated poses per leg; real
        panels on this platform ran 11-28. This is the operation that closes
        that gap on ONE leg without re-running the whole panel.

        The new poses AGGREGATE with the leg rather than replacing it: the
        platform starts their ``sample_idx`` one past the leg's existing
        high-water mark, which is the column the live uniqueness index keys on.
        Re-submitting the fold by hand does not do this — it collides with the
        index, or lands as an unrelated job whose poses cannot be pooled.

        Parameters
        ----------
        job_id :
            A COMPLETED Boltz-2 fold job (``fold_<ms>_<rand>``). Its receptor
            sequence/conformation, peptide, MSA settings, sampling steps and
            step scale are reused exactly; only the seed and the trajectory
            count change.
        diffusion_samples :
            Trajectories to ADD, 1-64. Use :func:`poses_needed_per_leg` or the
            ``poses_to_add`` field of :meth:`panel` to size it.
        seed :
            Explicit Boltz-2 seed. Omit for a deterministic seed derived from
            ``(job_id, sample offset)``, which keeps a re-run of the same
            request reproducible. The platform REFUSES a seed the leg has
            already run: at a fixed seed and a fixed spec Boltz-2 redraws the
            same trajectories, which would raise the pose count without
            shrinking the standard error the count is used to estimate.
        gate :
            Binder-metric gate used for the before/after pose counts.
        wait :
            Block until the deepen job reaches a terminal status.
        timeout, poll_interval :
            Wait budget and poll cadence in seconds.

        Returns
        -------
        DeepenResult
            ``jobId`` (the NEW job), ``deepenOf``, ``seed``, ``leg``,
            ``aggregation`` (the ``sample_idx`` window and the before-counts),
            and ``era`` — the generation provenance asserted unchanged.

        Raises
        ------
        ligandai.errors.LigandAIError
            409 when the leg cannot be identified, when the job is not a
            completed Boltz-2 fold, when the design's generation provenance is
            ambiguous (two generator eras for one sequence), when the era cannot
            be carried forward unchanged, or when the seed was already used.
        ligandai.errors.LigandAITimeoutError
            ``wait=True`` and the job did not finish inside ``timeout``.

        Notes
        -----
        Boltz-2 only. ``diffusion_samples`` is a Boltz-2 diffusion control:
        AlphaFold2 has no equivalent and ESMFold replicates via ``num_seeds``,
        so poses added that way would not be trajectories of the same estimator.

        The deepen does NOT re-run the generator — the peptide is already fixed.
        What it guarantees is that the fold spec is reused byte-for-byte and the
        design's generator model id and conditioning (48-dim v6.5 vs 107-dim
        v6.9 + CFG) are carried forward verbatim, failing loudly rather than
        pooling two eras behind one pose count.
        """
        import time as _time

        from ligandai.errors import LigandAIError, LigandAITimeoutError

        n = int(diffusion_samples)
        if not (1 <= n <= 64):
            raise ValueError(
                f"diffusion_samples must be in [1, 64], got {diffusion_samples!r}. "
                f"Resolving a 2x margin needs {poses_needed_per_leg(2)} gated poses per leg "
                f"(3x: {poses_needed_per_leg(3)}, 1.5x: {poses_needed_per_leg(1.5)}); "
                f"issue several deepens if you need more than 64 at once."
            )

        body: dict[str, Any] = {"diffusion_samples": n, "gate": float(gate)}
        if seed is not None:
            body["seed"] = int(seed)

        resp: DeepenResult = (
            self._transport.request(
                "POST", f"/api/folding/jobs/{job_id}/deepen", json=body,
            )
            or {}
        )
        if not wait:
            return resp

        new_job_id = resp.get("jobId")
        if not new_job_id:
            return resp
        deadline = _time.monotonic() + max(float(timeout), 30.0)
        last: dict[str, Any] = {}
        while _time.monotonic() < deadline:
            try:
                last = self._transport.request("GET", f"/api/folding/jobs/{new_job_id}") or {}
            except LigandAIError:
                _time.sleep(poll_interval)
                continue
            status = last.get("status")
            if status in ("completed", "failed", "cancelled", "error"):
                return {**resp, "status": status, "job": last}  # type: ignore[typeddict-item]
            _time.sleep(poll_interval)
        raise LigandAITimeoutError(
            f"deepen job {new_job_id} (deepen of {job_id}, +{n} trajectories at seed "
            f"{resp.get('seed')}) did not reach a terminal status within {timeout}s "
            f"(last status: {last.get('status')!r}). The job is still running — poll "
            f"client.folding.status({new_job_id!r}) or re-read the panel; its poses will "
            f"aggregate onto the leg when the fold lands."
        )

    def panel(
        self,
        *,
        session_id: str | None = None,
        peptide: str | None = None,
        gene: str | None = None,
        on_target: str | None = None,
        gate: float = SELECTIVITY_GATE_PEPTIDE_IPSAE,
        target_fold_change: float = 2.0,
        limit: int = 500,
    ) -> PanelResult:
        """Per-leg depth for a per-isoform / off-target fold panel.

        One of ``session_id``, ``peptide`` or ``gene`` is required — an unscoped
        query would scan every fold the account owns.

        Each leg is one (design, receptor/conformation) cell and reports
        ``total_poses``, ``gated_poses`` (``peptide_ipsae >= gate``),
        ``unscored_poses`` (``peptide_ipsae`` NULL — NOT computed, so neither
        pass nor fail), the geometric-mean ``predicted_kd`` over gated poses, and
        ``distinct_structures``.

        With ``on_target`` naming the designed-against gene, each design also
        gets its ``deciding_paralog`` (the tightest off-target, i.e. the smallest
        margin), ``depth`` = min(on-target gated, deciding-paralog gated),
        ``resolvable_fold_change`` at that depth, and ``poses_to_add`` — the
        shortfall on each of the two deciding legs, which is what you feed
        straight into :meth:`deepen`.

        Parameters
        ----------
        on_target :
            The on-target GENE. It is NOT inferred: ``ptf_fold_results`` carries
            no on-target flag, and calling the tightest-binding leg "the
            on-target" would make every panel selective by construction. Without
            it, per-leg depth is still returned and every design carries an
            explicit ``unresolved_reason``.
        gate :
            Binder-metric gate. Default 0.67 — the value the platform's
            selectivity analysis was run at.
        target_fold_change :
            The margin ``poses_to_add`` is sized for. Default 2.0 (36 per leg).

        Returns
        -------
        PanelResult
            ``legs``, ``designs``, and ``statistics`` (the measured SDs plus the
            per-leg pose targets for 3x / 2x / 1.5x).

        Notes
        -----
        ``mean_plddt_gated`` is on the 0-100 scale. ``ptf_fold_results.plddt`` is
        DUAL-SCALE — some rows 0-1, some 0-100 — and the server normalises each
        row before averaging, so this value is comparable across legs where a raw
        column average would not be.

        A ``shared_structure_warning`` on a design means the on-target leg and
        the deciding paralog leg share an identical ``md5(pdb_content)``: one
        fold filed under two gene labels. That selectivity ratio is an artifact
        of the duplicate, and deepening will NOT fix it — more trajectories of
        the on-target fold are not a paralog fold. Re-fold the paralog leg.
        """
        if not any((session_id, peptide, gene)):
            raise ValueError(
                "panel() requires one of session_id, peptide or gene — an unscoped "
                "query would scan every fold this account owns"
            )
        params: dict[str, Any] = {
            "gate": float(gate),
            "target_fold_change": float(target_fold_change),
            "limit": int(limit),
        }
        if session_id:
            params["session_id"] = session_id
        if peptide:
            params["peptide"] = peptide
        if gene:
            params["gene"] = gene
        if on_target:
            params["on_target"] = on_target
        return self._transport.request("GET", "/api/folding/panels", params=params) or {}

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

    # ─── Panel depth / leg deepening [bd-dre-ykrlo] ─────────────────────────

    async def deepen(
        self,
        job_id: str,
        *,
        diffusion_samples: int,
        seed: int | None = None,
        gate: float = SELECTIVITY_GATE_PEPTIDE_IPSAE,
        wait: bool = True,
        timeout: float = 1800.0,
        poll_interval: float = 10.0,
    ) -> DeepenResult:
        """Async mirror of :meth:`Folds.deepen`.

        Adds ``diffusion_samples`` trajectories to the leg ``job_id`` folded, at
        a different seed and on the job's exact spec. The poses aggregate onto
        the leg — the platform starts their ``sample_idx`` past the leg's
        high-water mark, which is what a hand re-submission cannot do.
        """
        import asyncio as _asyncio

        from ligandai.errors import LigandAIError, LigandAITimeoutError

        n = int(diffusion_samples)
        if not (1 <= n <= 64):
            raise ValueError(
                f"diffusion_samples must be in [1, 64], got {diffusion_samples!r}. "
                f"Resolving a 2x margin needs {poses_needed_per_leg(2)} gated poses per leg "
                f"(3x: {poses_needed_per_leg(3)}, 1.5x: {poses_needed_per_leg(1.5)})."
            )

        body: dict[str, Any] = {"diffusion_samples": n, "gate": float(gate)}
        if seed is not None:
            body["seed"] = int(seed)

        resp: DeepenResult = (
            await self._transport.request(
                "POST", f"/api/folding/jobs/{job_id}/deepen", json=body,
            )
            or {}
        )
        if not wait:
            return resp

        new_job_id = resp.get("jobId")
        if not new_job_id:
            return resp
        loop = _asyncio.get_event_loop()
        deadline = loop.time() + max(float(timeout), 30.0)
        last: dict[str, Any] = {}
        while loop.time() < deadline:
            try:
                last = await self._transport.request("GET", f"/api/folding/jobs/{new_job_id}") or {}
            except LigandAIError:
                await _asyncio.sleep(poll_interval)
                continue
            status = last.get("status")
            if status in ("completed", "failed", "cancelled", "error"):
                return {**resp, "status": status, "job": last}  # type: ignore[typeddict-item]
            await _asyncio.sleep(poll_interval)
        raise LigandAITimeoutError(
            f"deepen job {new_job_id} (deepen of {job_id}, +{n} trajectories at seed "
            f"{resp.get('seed')}) did not reach a terminal status within {timeout}s "
            f"(last status: {last.get('status')!r}). The job is still running and its "
            f"poses will aggregate onto the leg when the fold lands."
        )

    async def panel(
        self,
        *,
        session_id: str | None = None,
        peptide: str | None = None,
        gene: str | None = None,
        on_target: str | None = None,
        gate: float = SELECTIVITY_GATE_PEPTIDE_IPSAE,
        target_fold_change: float = 2.0,
        limit: int = 500,
    ) -> PanelResult:
        """Async mirror of :meth:`Folds.panel`.

        ``on_target`` is never inferred — see the sync docstring for why naming
        the tightest-binding leg "the on-target" would make every panel
        selective by construction.
        """
        if not any((session_id, peptide, gene)):
            raise ValueError(
                "panel() requires one of session_id, peptide or gene — an unscoped "
                "query would scan every fold this account owns"
            )
        params: dict[str, Any] = {
            "gate": float(gate),
            "target_fold_change": float(target_fold_change),
            "limit": int(limit),
        }
        if session_id:
            params["session_id"] = session_id
        if peptide:
            params["peptide"] = peptide
        if gene:
            params["gene"] = gene
        if on_target:
            params["on_target"] = on_target
        return await self._transport.request("GET", "/api/folding/panels", params=params) or {}
