# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Goal-directed campaign engine and persistent AutoResearch runs.  [bd-dre-1vij1]

Two surfaces live here:

1. **AutoResearch (persistent runs)** — ``start()``, ``list()``, ``get()``,
   ``graph()``, ``stream()``, ``pause()``, ``resume()``, ``stop()``.
   These wrap ``/api/autoresearch/*`` and are the async, multi-step orchestrator
   for long-running goal-directed research.  They existed before this revision.

2. **Goal-campaign engine (iterative design loops)** — ``campaign_scan()``,
   ``campaign_start()``, ``campaign_status()``, ``campaign_stream()``,
   ``campaign_cancel()``, ``campaign_cross_validate()``, and the one-call
   high-level entry point ``drive()``.
   These wrap ``/api/v1/goal-campaign/*`` — a new set of endpoints added in
   server commit ``bd-LIGANDAI_ALPHA_V2-o2vd5``.

The headline use-case is **allosteric-site binders**:

- Express a goal such as "bind the allosteric pocket on EGFR, avoid the
  orthosteric ATP site."
- The campaign engine seeds from provided peptides (or generates them via
  ``client.peptides.generate()``), iterates fold → score → select → next-batch
  loop across ``max_iterations`` rounds, and gates advancement with the
  ``goal`` criteria (hotspot contact, avoidance of orthosteric residues, iPSAE
  threshold).
- After completion, ``campaign_cross_validate()`` runs the survivors and hits
  on a second engine (e.g. ``protenix``) to confirm the fold-quality agreement
  before declaring a final hit list.

⚠ **How a campaign gets there matters, and it is not by aiming generation.**
The ``hotspots`` and ``avoid_residues`` you supply are *acceptance criteria*
measured on each iteration's folded complexes, and the campaign converges by
selecting the designs that satisfy them and reseeding from those. They are not
placement controls: measured on the deployed generator, ``avoid_residues`` does
not change which sequences are produced at all, and hotspot selection shifts the
generated distribution without aiming it at the named face. The site-selectivity
of a campaign comes from the fold → measure → select loop, so budget enough
iterations and batch size for that selection to do the work.

Endpoint mapping:

- :meth:`Goals.campaign_scan`            → ``POST /api/v1/goal-campaign/scan``
- :meth:`Goals.campaign_start`           → ``POST /api/v1/goal-campaign/start``
- :meth:`Goals.campaign_status`          → ``GET  /api/v1/goal-campaign/{id}/status``
- :meth:`Goals.campaign_stream`          → ``GET  /api/v1/goal-campaign/{id}/stream``
- :meth:`Goals.campaign_cancel`          → ``POST /api/v1/goal-campaign/{id}/cancel``
- :meth:`Goals.campaign_cross_validate`` → ``POST /api/v1/goal-campaign/{id}/cross-validate``
- :meth:`Goals.drive`                    → high-level one-call driver
- :meth:`Goals.start`                    → ``POST /api/autoresearch/start``
- :meth:`Goals.list`                     → ``GET  /api/autoresearch/runs``
- :meth:`Goals.get`                      → ``GET  /api/autoresearch/runs/{id}``
- :meth:`Goals.graph`                    → ``GET  /api/autoresearch/runs/{id}/graph``
- :meth:`Goals.stream`                   → ``GET  /api/autoresearch/runs/{id}/stream`` (SSE)
- :meth:`Goals.pause`                    → ``POST /api/autoresearch/runs/{id}/pause``
- :meth:`Goals.resume`                   → ``POST /api/autoresearch/runs/{id}/resume``
- :meth:`Goals.stop`                     → ``POST /api/autoresearch/runs/{id}/stop``

Domain rules (enforced in docstrings, not overridden by client):

- **peptide/binder-chain iPSAE (``peptide_ipsae``) is THE binder metric.**
  Never use overall ``ipsae`` — it is dominated by receptor self-confidence on
  multimeric targets and is not a reliable binding indicator. iPSAE is more
  reliable than ipTM for Boltz-2 interface assessment.
- **Receptor chains MUST be folded with MSA.** Peptide chains do not require
  MSA (they are de-novo sequences).  The campaign engine respects this via the
  ``msa_csv`` receptor field and the shared-MSA mode in the fold engine.
- ``seed_peptides`` is optional for abstract goals when the server-side
  automated LigandForge generation integration is active. If the server returns
  HTTP 400 with ``"seed_peptides required"`` the SDK surfaces a clear, actionable
  :class:`~ligandai.errors.LigandAIValidationError` rather than a raw HTTP error.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Generator, Iterator
from typing import Any, Literal
from urllib.parse import urlencode

from ligandai._http import parse_sse_data
from ligandai._targeting import validate_and_normalize_goal_targeting
from ligandai.errors import LigandAIError, LigandAIValidationError
from ligandai.resources._base import AsyncResource, Resource
from ligandai.types import GoalProjectState, GoalRun, GoalRunEvent, GoalRunStart

# ─── Internal public-parameter scrub ────────────────────────────────────────
# The server strips these keys for non-superadmin callers (defence in depth
# also applied on the Python SDK side). Must NEVER appear in user-visible output.
# Refs: bd-dre-o9cc4 / bd-LIGANDAI_ALPHA_V2-pd4va — Tiburon hit
# ``internalPoolPeptidesPerTarget`` in the echoed job config and concluded the
# SDK was silently overriding their numPeptides. Field name MUST NOT appear in
# user-visible output.
_NON_PUBLIC_PARAMETER_KEYS: frozenset[str] = frozenset({
    "internalPoolPeptidesPerTarget",
})


def _scrub_public_parameters(params: Any) -> Any:
    """Drop internal server bookkeeping keys from an echoed config object."""
    if not isinstance(params, dict):
        return params
    return {k: v for k, v in params.items() if k not in _NON_PUBLIC_PARAMETER_KEYS}


def _scrub_campaign_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Scrub server-internal keys from any campaign response that echoes config."""
    out = dict(payload)
    for key in ("parameters", "config"):
        if key in out:
            out[key] = _scrub_public_parameters(out[key])
    return out


# ─── Shared body builders ────────────────────────────────────────────────────


def _start_body(
    *,
    goal: str,
    automatic_mode: bool,
    budget_cap_credits: int | None,
    program_db_id: int | None,
    project_db_id: int | None,
    program_id: str | None,
    project_id: str | None,
    conversation_id: str | None,
    max_iterations: int | None,
) -> dict[str, Any]:
    if not goal or len(goal.strip()) < 4:
        raise ValueError("goal must be at least 4 characters")
    if not automatic_mode:
        raise ValueError(
            "Starting a persistent goal run can spend credits after your process exits. "
            "Pass automatic_mode=True to acknowledge this."
        )
    if budget_cap_credits is not None and budget_cap_credits < 0:
        raise ValueError("budget_cap_credits must be non-negative")
    if max_iterations is not None and max_iterations < 0:
        raise ValueError("max_iterations must be non-negative")

    body: dict[str, Any] = {
        "goal": goal.strip(),
        "automaticMode": True,
        "automaticModeAcknowledged": True,
    }
    if budget_cap_credits is not None:
        body["budgetCapCredits"] = int(budget_cap_credits)
    if program_db_id is not None:
        body["programDbId"] = int(program_db_id)
    if project_db_id is not None:
        body["projectDbId"] = int(project_db_id)
    if program_id:
        body["programId"] = program_id
    if project_id:
        body["projectId"] = project_id
    if conversation_id:
        body["conversationId"] = conversation_id
    if max_iterations is not None:
        body["maxIterations"] = int(max_iterations)
    return body


def _runs_path(
    *,
    program_id: str | None = None,
    project_id: str | None = None,
    program_db_id: int | None = None,
    project_db_id: int | None = None,
    conversation_id: str | None = None,
) -> str:
    params: dict[str, Any] = {}
    if program_id:
        params["programId"] = program_id
    if project_id:
        params["projectId"] = project_id
    if program_db_id is not None:
        params["programDbId"] = int(program_db_id)
    if project_db_id is not None:
        params["projectDbId"] = int(project_db_id)
    if conversation_id:
        params["conversationId"] = conversation_id
    return f"/api/autoresearch/runs?{urlencode(params)}" if params else "/api/autoresearch/runs"


def _campaign_start_body(
    *,
    receptor_sequence: str,
    goal_type: str,
    receptor_chain: str | None,
    receptor_gene: str | None,
    receptor_msa_csv: str | None,
    seed_peptides: list[str] | None,
    peptide_length: int | None,
    engine: str,
    diffusion_samples: int,
    batch_size: int,
    max_iterations: int,
    max_credits: int,
    survivor_ipsae_threshold: float | None,
    cross_validate_engine: str | None,
    hotspots: list[dict[str, Any]] | None,
    avoid_residues: list[dict[str, Any]] | None,
    # Ca-Ca packing-shell radius (Angstrom) grown around avoid_residues, same
    # semantics as Peptides.generate's avoid_shell_angstrom — the goal-campaign
    # engine calls the same FastAPI /api/generate/stream endpoint under the
    # hood (server/goal-campaign/campaign-engine.ts). [bd-LIGANDAI_ALPHA_V2-x4vt8]
    avoid_shell_angstrom: float | None = None,
    target_ipsae: float | None,
    target_iptm: float | None,
    contact_threshold_a: float | None,
    goal_description: str | None,
) -> dict[str, Any]:
    """Build the wire-format body for POST /api/v1/goal-campaign/start.

    Validates receptor sequence, engine, goal type, and numeric bounds.
    Parameters use the server's snake_case format confirmed from goal-campaign-routes.ts.
    """
    _VALID_ENGINES = {"boltz2", "protenix", "esmfold2", "openfold3"}
    _VALID_GOAL_TYPES = {"allosteric", "hotspot", "avoidance", "custom"}

    seq = str(receptor_sequence).strip().upper()
    if not seq or len(seq) < 6:
        raise ValueError("receptor_sequence must be a valid amino-acid sequence (>= 6 residues)")

    engine_norm = str(engine).strip().lower()
    if engine_norm not in _VALID_ENGINES:
        raise ValueError(
            f"engine must be one of: {', '.join(sorted(_VALID_ENGINES))} — got '{engine}'"
        )

    goal_type_norm = str(goal_type).strip().lower()
    if goal_type_norm not in _VALID_GOAL_TYPES:
        raise ValueError(
            f"goal_type must be one of: {', '.join(sorted(_VALID_GOAL_TYPES))} — got '{goal_type}'"
        )

    # Client-side targeting validation — BEFORE any HTTP call.
    # [bd-LIGANDAI_ALPHA_V2-x4vt8] ``hotspots``/``avoid_residues`` here are
    # single-residue {"chain","residue"} dicts (not {chain,start,end} ranges
    # like Peptides.generate), but the same PDB-numbering rule applies: a
    # residue number is a PDB resnum, never an array index. Validate ONLY —
    # keep the caller's original dicts on the wire (tests assert byte-exact
    # passthrough, e.g. label/extra keys the caller included).
    #
    # [bd-LIGANDAI_ALPHA_V2-x4vt8.3.2] Goes through the AGGREGATE entry point
    # now. Calling validate_region_list/validate_avoid_shell_angstrom piecemeal
    # skipped the two CROSS-FIELD checks (missing-chain ambiguity,
    # degenerate-pocket risk) that only run when all fields are seen together —
    # the goal-campaign engine drives the same /api/generate/stream pocket
    # machinery, so both apply here identically.
    _goal_targeting = validate_and_normalize_goal_targeting(
        hotspots=hotspots,
        avoid_residues=avoid_residues,
        avoid_shell_angstrom=avoid_shell_angstrom,
    )
    avoid_shell_angstrom = _goal_targeting["avoid_shell_angstrom"]

    receptor: dict[str, Any] = {"sequence": seq}
    if receptor_chain is not None:
        receptor["chain"] = str(receptor_chain).strip().upper()
    if receptor_gene is not None:
        receptor["gene"] = str(receptor_gene)
    if receptor_msa_csv is not None:
        receptor["msa_csv"] = str(receptor_msa_csv)

    goal: dict[str, Any] = {"type": goal_type_norm}
    if hotspots:
        goal["hotspots"] = hotspots
    if avoid_residues:
        goal["avoid_residues"] = avoid_residues
    if avoid_shell_angstrom is not None:
        goal["avoid_shell_angstrom"] = avoid_shell_angstrom
    # Falsy-zero discipline: use is not None for all numerics.
    if target_ipsae is not None:
        goal["target_ipsae"] = float(target_ipsae)
    if target_iptm is not None:
        goal["target_iptm"] = float(target_iptm)
    if contact_threshold_a is not None:
        goal["contact_threshold_a"] = float(contact_threshold_a)
    if goal_description is not None:
        goal["description"] = str(goal_description)

    body: dict[str, Any] = {
        "receptor": receptor,
        "engine": engine_norm,
        "diffusion_samples": int(diffusion_samples),
        "batch_size": int(batch_size),
        "max_iterations": int(max_iterations),
        "max_credits": int(max_credits),
        "goal": goal,
    }
    if seed_peptides is not None:
        body["seed_peptides"] = [str(s).strip().upper() for s in seed_peptides if str(s).strip()]
    if peptide_length is not None:
        body["peptide_length"] = int(peptide_length)
    if survivor_ipsae_threshold is not None:
        body["survivor_ipsae_threshold"] = float(survivor_ipsae_threshold)
    if cross_validate_engine is not None:
        body["cross_validate_engine"] = str(cross_validate_engine).strip().lower()

    return body


def _interpret_campaign_400(response_body: Any) -> None:
    """Raise a clear, actionable error when the server returns 400 on campaign start.

    The server currently returns 400 when ``seed_peptides`` are not supplied
    (automated LigandForge generation is planned but not yet live). Surface this
    as a :class:`~ligandai.errors.LigandAIValidationError` with an instructive
    message rather than a raw HTTP error.
    """
    if isinstance(response_body, dict):
        msg = response_body.get("error") or response_body.get("message") or str(response_body)
    else:
        msg = str(response_body)

    if "seed_peptides" in msg.lower():
        raise LigandAIValidationError(
            "The goal-campaign server requires seed_peptides — provide at least one "
            "amino-acid sequence to bootstrap the campaign.  Example: "
            "seed_peptides=['ACDEFGHIKLMNPQRST']. "
            "Automated LigandForge generation integration is planned for a future release. "
            f"Server message: {msg}",
            errors=[{"field": "seed_peptides", "message": msg}],
        )
    raise LigandAIValidationError(msg)


# ─── GoalCampaign handle ─────────────────────────────────────────────────────


class GoalCampaign:
    """Handle for a running or completed goal campaign.

    Returned by :meth:`Goals.drive` (sync) and :meth:`AsyncGoals.drive` (async).
    Mirrors the :class:`~ligandai.jobs.Job` / :class:`~ligandai.resources.peptides.BatchFoldJob`
    pattern used elsewhere in this SDK: create the resource, then call
    ``.wait()`` or ``.stream()`` to observe progress.

    Parameters
    ----------
    campaign_id
        The ``campaign_id`` string returned by ``POST /api/v1/goal-campaign/start``.
    goals_resource
        The sync :class:`Goals` instance that created this handle.
    gene
        Gene symbol that was targeted (informational).
    goal
        Goal description string (informational).
    """

    def __init__(
        self,
        campaign_id: str,
        goals_resource: Goals,
        *,
        gene: str | None = None,
        goal: str | None = None,
    ) -> None:
        self._campaign_id = campaign_id
        self._goals = goals_resource
        self._gene = gene
        self._goal = goal

    @property
    def campaign_id(self) -> str:
        """The campaign identifier string."""
        return self._campaign_id

    @property
    def gene(self) -> str | None:
        """The gene symbol that was targeted."""
        return self._gene

    @property
    def goal(self) -> str | None:
        """The goal description string."""
        return self._goal

    def status(self) -> dict[str, Any]:
        """Return the current campaign snapshot.

        Calls ``GET /api/v1/goal-campaign/{campaign_id}/status``.
        """
        return self._goals.campaign_status(self._campaign_id)

    def stream(self, *, timeout: float | None = None) -> Iterator[dict[str, Any]]:
        """Stream live SSE events from the campaign.

        Calls ``GET /api/v1/goal-campaign/{campaign_id}/stream``.

        Parameters
        ----------
        timeout
            Optional SSE stream timeout in seconds.

        Yields
        ------
        dict
            Parsed SSE event payloads: ``design_scored``, ``iteration_complete``,
            ``result``, ``cross_validation_*``, etc.
        """
        yield from self._goals.campaign_stream(self._campaign_id, timeout=timeout)

    def wait(
        self,
        *,
        timeout: float | None = None,
        poll_interval: float = 5.0,
    ) -> dict[str, Any]:
        """Block until the campaign reaches a terminal state and return the final snapshot.

        Polls ``GET /api/v1/goal-campaign/{campaign_id}/status`` every
        ``poll_interval`` seconds until ``status`` is one of ``"completed"``,
        ``"failed"``, ``"cancelled"``, or ``"budget_exhausted"``.

        Parameters
        ----------
        timeout
            Maximum wall-clock seconds to wait. ``None`` means wait indefinitely.
        poll_interval
            Seconds between status polls (default 5).

        Returns
        -------
        dict
            The final campaign snapshot from ``campaign_status()``.

        Raises
        ------
        LigandAIWaitTimeout
            If ``timeout`` is exceeded before reaching a terminal state.
        """
        from ligandai.errors import LigandAIWaitTimeout

        _TERMINAL = {"completed", "failed", "cancelled", "budget_exhausted"}
        deadline = (time.monotonic() + timeout) if timeout is not None else None

        while True:
            snap = self.status()
            if snap.get("status") in _TERMINAL:
                return snap
            if deadline is not None and time.monotonic() >= deadline:
                raise LigandAIWaitTimeout(
                    f"Campaign {self._campaign_id!r} did not finish within {timeout}s "
                    f"(current status: {snap.get('status')!r})"
                )
            time.sleep(poll_interval)

    def cancel(self) -> dict[str, Any]:
        """Cancel the campaign.

        Calls ``POST /api/v1/goal-campaign/{campaign_id}/cancel``.
        """
        return self._goals.campaign_cancel(self._campaign_id)

    def cross_validate(
        self,
        *,
        secondary_engine: str = "protenix",
        agreement_threshold: float | None = None,
        peptides: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run cross-engine validation on campaign survivors after completion.

        Calls ``POST /api/v1/goal-campaign/{campaign_id}/cross-validate``.

        Parameters
        ----------
        secondary_engine
            Engine to use for cross-validation (default ``"protenix"``). Must
            differ from the primary campaign engine.
        agreement_threshold
            iPSAE agreement threshold for calling concordance (default 0.4 on
            the server side).
        peptides
            Explicit peptide list to validate. Defaults to all campaign hits
            and survivors.
        """
        return self._goals.campaign_cross_validate(
            self._campaign_id,
            secondary_engine=secondary_engine,
            agreement_threshold=agreement_threshold,
            peptides=peptides,
        )

    def __repr__(self) -> str:
        return (
            f"GoalCampaign(campaign_id={self._campaign_id!r}, "
            f"gene={self._gene!r}, goal={self._goal!r})"
        )


class AsyncGoalCampaign:
    """Async version of :class:`GoalCampaign`.

    Returned by :meth:`AsyncGoals.drive`.
    """

    def __init__(
        self,
        campaign_id: str,
        goals_resource: AsyncGoals,
        *,
        gene: str | None = None,
        goal: str | None = None,
    ) -> None:
        self._campaign_id = campaign_id
        self._goals = goals_resource
        self._gene = gene
        self._goal = goal

    @property
    def campaign_id(self) -> str:
        return self._campaign_id

    @property
    def gene(self) -> str | None:
        return self._gene

    @property
    def goal(self) -> str | None:
        return self._goal

    async def status(self) -> dict[str, Any]:
        """Return the current campaign snapshot (async)."""
        return await self._goals.campaign_status(self._campaign_id)

    async def stream(self, *, timeout: float | None = None) -> AsyncIterator[dict[str, Any]]:
        """Stream live SSE events from the campaign (async)."""
        async for event in self._goals.campaign_stream(self._campaign_id, timeout=timeout):
            yield event

    async def wait(
        self,
        *,
        timeout: float | None = None,
        poll_interval: float = 5.0,
    ) -> dict[str, Any]:
        """Async-poll until the campaign reaches a terminal state."""
        import asyncio
        from ligandai.errors import LigandAIWaitTimeout

        _TERMINAL = {"completed", "failed", "cancelled", "budget_exhausted"}
        deadline = (time.monotonic() + timeout) if timeout is not None else None

        while True:
            snap = await self.status()
            if snap.get("status") in _TERMINAL:
                return snap
            if deadline is not None and time.monotonic() >= deadline:
                raise LigandAIWaitTimeout(
                    f"Campaign {self._campaign_id!r} did not finish within {timeout}s "
                    f"(current status: {snap.get('status')!r})"
                )
            await asyncio.sleep(poll_interval)

    async def cancel(self) -> dict[str, Any]:
        """Cancel the campaign (async)."""
        return await self._goals.campaign_cancel(self._campaign_id)

    async def cross_validate(
        self,
        *,
        secondary_engine: str = "protenix",
        agreement_threshold: float | None = None,
        peptides: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run cross-engine validation after completion (async)."""
        return await self._goals.campaign_cross_validate(
            self._campaign_id,
            secondary_engine=secondary_engine,
            agreement_threshold=agreement_threshold,
            peptides=peptides,
        )

    def __repr__(self) -> str:
        return (
            f"AsyncGoalCampaign(campaign_id={self._campaign_id!r}, "
            f"gene={self._gene!r}, goal={self._goal!r})"
        )


# ─── Sync resource ───────────────────────────────────────────────────────────


class Goals(Resource):
    def estimate(
        self,
        *,
        target_hits: int,
        criterion: str = "super_elite",
        max_rounds: int = 3,
        max_credits: float,
        peptides_per_round: int | None = None,
        folds_per_round: int | None = None,
        trajectories: int | None = None,
        sampling_steps: int | None = None,
    ) -> dict:
        """What does this objective cost if it runs to its bound?

        ``POST /api/objectives/estimate`` — [bd-LIGANDAI_ALPHA_V2-vkp6w]

        An objective is N designs passing a named acceptance criterion, bounded
        by ``max_rounds`` and ``max_credits``. This returns the deterministic
        worst-case credit projection for that shape, and says which of the two
        ceilings actually binds::

            est = client.goals.estimate(target_hits=25, criterion="super_elite",
                                        max_rounds=3, max_credits=50000)
            est["projection"]["perRoundCredits"]
            est["projection"]["maxTotalCredits"]
            est["projection"]["bindingConstraint"]   # "maxRounds" | "maxCredits"

        ⛔ **No yield is assumed.** This is arithmetic on the round
        configuration priced through the platform's own credit pricing, times
        the round ceiling. It is what the objective costs if every round runs —
        never a prediction of when the target is met, which would need a
        per-target hit rate the platform cannot predict. Read
        ``est["basis"]`` before presenting the number as an "estimated cost".

        Because no yield is involved it holds for **every** acceptance
        criterion, including ones a measured-hit-rate projection must refuse.

        Read-only: no run is created and no credits are reserved.
        """
        body: dict = {
            "objective": {
                "criterion": criterion,
                "targetHits": target_hits,
                "maxRounds": max_rounds,
                "maxCredits": max_credits,
            }
        }
        round_cfg = {
            "peptidesPerRound": peptides_per_round,
            "foldsPerRound": folds_per_round,
            "trajectories": trajectories,
            "samplingSteps": sampling_steps,
        }
        round_cfg = {k: v for k, v in round_cfg.items() if v is not None}
        if round_cfg:
            body["round"] = round_cfg
        return self._transport.request("POST", "/api/objectives/estimate", json=body) or {}


    """Sync ``client.goals`` — goal-directed campaign engine and AutoResearch runs.

    Two surfaces:

    **AutoResearch (persistent multi-step runs):**
    ``start()``, ``list()``, ``get()``, ``graph()``, ``stream()``,
    ``pause()``, ``resume()``, ``stop()``.

    **Goal-campaign engine (iterative design loops):**
    ``campaign_scan()``, ``campaign_start()``, ``campaign_status()``,
    ``campaign_stream()``, ``campaign_cancel()``, ``campaign_cross_validate()``.

    **High-level one-call driver:**
    ``drive()`` — express an abstract objective and receive a
    :class:`GoalCampaign` handle you can ``.wait()`` or ``.stream()``.
    """

    # ── AutoResearch (persistent runs) ───────────────────────────────────────

    def start(
        self,
        goal: str,
        *,
        automatic_mode: bool = False,
        budget_cap_credits: int | None = 10_000,
        program_db_id: int | None = None,
        project_db_id: int | None = None,
        program_id: str | None = None,
        project_id: str | None = None,
        conversation_id: str | None = None,
        max_iterations: int | None = None,
    ) -> GoalRunStart:
        """Start a persistent goal-directed AutoResearch run.

        ``automatic_mode=True`` is required intentionally: these runs can keep
        working and consuming credits after the Python process exits until they
        are stopped or hit ``budget_cap_credits``.

        ``POST /api/autoresearch/start``
        """
        payload = self._transport.request(
            "POST",
            "/api/autoresearch/start",
            json=_start_body(
                goal=goal,
                automatic_mode=automatic_mode,
                budget_cap_credits=budget_cap_credits,
                program_db_id=program_db_id,
                project_db_id=project_db_id,
                program_id=program_id,
                project_id=project_id,
                conversation_id=conversation_id,
                max_iterations=max_iterations,
            ),
        )
        return GoalRunStart.model_validate(payload or {})

    def list(
        self,
        *,
        program_id: str | None = None,
        project_id: str | None = None,
        program_db_id: int | None = None,
        project_db_id: int | None = None,
        conversation_id: str | None = None,
    ) -> list[GoalRun]:
        """List persistent AutoResearch goal runs.  ``GET /api/autoresearch/runs``"""
        payload = self._transport.request(
            "GET",
            _runs_path(
                program_id=program_id,
                project_id=project_id,
                program_db_id=program_db_id,
                project_db_id=project_db_id,
                conversation_id=conversation_id,
            ),
        ) or {}
        return [GoalRun.model_validate(run) for run in payload.get("runs", [])]

    def get(self, run_id: str) -> GoalRun:
        """Fetch a single AutoResearch run.  ``GET /api/autoresearch/runs/{run_id}``"""
        payload = self._transport.request("GET", f"/api/autoresearch/runs/{run_id}") or {}
        return GoalRun.model_validate(payload.get("run") or payload)

    def graph(self, run_id: str) -> GoalProjectState:
        """Return the derived checklist/dependency/evidence graph.
        ``GET /api/autoresearch/runs/{run_id}/graph``
        """
        payload = self._transport.request("GET", f"/api/autoresearch/runs/{run_id}/graph") or {}
        return GoalProjectState.model_validate(payload.get("goalState") or payload)

    def stream(self, run_id: str, *, timeout: float | None = None) -> Iterator[GoalRunEvent]:
        """Stream live AutoResearch events.  ``GET /api/autoresearch/runs/{run_id}/stream``

        The first server event is usually ``hello`` and includes the latest
        run snapshot; subsequent events include planning, step, evaluation, and
        terminal status updates.
        """
        for line in self._transport.stream_lines(
            "GET",
            f"/api/autoresearch/runs/{run_id}/stream",
            timeout=timeout,
        ):
            data = parse_sse_data(line)
            if data is None:
                continue
            yield GoalRunEvent.model_validate({**data, "payload": data})

    def pause(self, run_id: str) -> bool:
        """Pause an AutoResearch run.  ``POST /api/autoresearch/runs/{run_id}/pause``"""
        payload = self._transport.request("POST", f"/api/autoresearch/runs/{run_id}/pause") or {}
        return bool(payload.get("ok", True))

    def resume(self, run_id: str) -> bool:
        """Resume a paused AutoResearch run.  ``POST /api/autoresearch/runs/{run_id}/resume``"""
        payload = self._transport.request("POST", f"/api/autoresearch/runs/{run_id}/resume") or {}
        return bool(payload.get("ok", True))

    def stop(self, run_id: str) -> bool:
        """Stop an AutoResearch run.  ``POST /api/autoresearch/runs/{run_id}/stop``"""
        payload = self._transport.request("POST", f"/api/autoresearch/runs/{run_id}/stop") or {}
        return bool(payload.get("ok", True))

    # ── Goal-campaign engine ──────────────────────────────────────────────────

    def campaign_scan(
        self,
        structure: str,
        *,
        format: Literal["pdb", "cif"] = "cif",  # noqa: A002
        peptide_chain: str = "B",
        hotspots: list[dict[str, Any]] | None = None,
        contact_threshold_a: float | None = None,
    ) -> dict[str, Any]:
        """Ad-hoc binding-site scan on a single structure.  Zero GPU cost.

        ``POST /api/v1/goal-campaign/scan``

        Runs pure geometry analysis on an already-computed structure string —
        no GPU, no campaign context, no credits consumed.  Use this to scan
        any completed co-fold's best structure to determine where the peptide
        actually bound and whether it hit your hotspots.

        Parameters
        ----------
        structure
            PDB or mmCIF text of the co-folded complex.
        format
            Structure format: ``"pdb"`` or ``"cif"`` (default ``"cif"``).
        peptide_chain
            Chain ID of the peptide chain in the structure (default ``"B"``).
        hotspots
            Optional list of hotspot residue specs to check contact against.
            Shape: ``[{"chain": "A", "residue": 148}, ...]``.
            ``residue`` must be a positive integer (PDB numbering).
        contact_threshold_a
            Contact distance threshold in Å (default 5.0 on the server side).
            Falsy-zero discipline: ``0.0`` is a valid value and is transmitted.

        Returns
        -------
        dict
            Keys include: ``hotspot_hit``, ``hotspot_coverage``,
            ``hotspot_contact_count``, ``hotspot_miss_count``,
            ``non_hotspot_contact_count``, ``orthosteric_drift``,
            ``contacted_residues``, ``hotspot_contacts``.

        Examples
        --------
        ::

            scan = client.goals.campaign_scan(
                structure=pdb_text,
                format="pdb",
                peptide_chain="B",
                hotspots=[{"chain": "A", "residue": 792}, {"chain": "A", "residue": 855}],
            )
            print("Hotspot hit:", scan["hotspot_hit"])
            print("Coverage:", scan["hotspot_coverage"])
        """
        body: dict[str, Any] = {
            "structure": structure,
            "format": format,
            "peptide_chain": peptide_chain.strip().upper() if peptide_chain else "B",
        }
        if hotspots is not None:
            body["hotspots"] = hotspots
        # Falsy-zero: use is not None, not truthiness (0.0 is a valid threshold).
        if contact_threshold_a is not None:
            body["contact_threshold_a"] = float(contact_threshold_a)

        return _scrub_campaign_response(
            self._transport.request("POST", "/api/v1/goal-campaign/scan", json=body) or {}
        )

    def campaign_start(
        self,
        receptor_sequence: str,
        goal_type: Literal["allosteric", "hotspot", "avoidance", "custom"],
        *,
        receptor_chain: str | None = None,
        receptor_gene: str | None = None,
        receptor_msa_csv: str | None = None,
        seed_peptides: list[str] | None = None,
        peptide_length: int | None = None,
        engine: str = "boltz2",
        diffusion_samples: int = 4,
        batch_size: int = 8,
        max_iterations: int = 5,
        max_credits: int = 5000,
        survivor_ipsae_threshold: float | None = None,
        cross_validate_engine: str | None = None,
        hotspots: list[dict[str, Any]] | None = None,
        avoid_residues: list[dict[str, Any]] | None = None,
        # Ca-Ca packing-shell radius (Angstrom) around avoid_residues. None =
        # server default (8.0 A); 0 disables. [bd-LIGANDAI_ALPHA_V2-x4vt8]
        avoid_shell_angstrom: float | None = None,
        target_ipsae: float | None = None,
        target_iptm: float | None = None,
        contact_threshold_a: float | None = None,
        goal_description: str | None = None,
    ) -> dict[str, Any]:
        """Start a new goal-directed campaign.

        ``POST /api/v1/goal-campaign/start``

        The campaign iteratively folds peptides against the receptor, scores
        them against the goal criteria, selects survivors, and generates the
        next batch — repeating until ``max_iterations`` or ``max_credits`` is
        reached.

        Domain rules:
        - **peptide_ipsae (not overall ipsae) is THE binder metric.**
          Use ``survivor_ipsae_threshold`` to gate which peptides advance;
          the server's default is 0.67.
        - **Receptor chains MUST use MSA.**  Supply ``receptor_msa_csv`` or
          ``receptor_gene`` so the server fetches its own MSA.  Peptide chains
          do not require MSA (they are de-novo).
        - ``seed_peptides`` is required until the automated LigandForge
          generation integration is live.  If the server returns 400 with
          ``"seed_peptides required"``, the SDK raises a clear
          :class:`~ligandai.errors.LigandAIValidationError`.

        Parameters
        ----------
        receptor_sequence
            Amino-acid sequence of the receptor chain (>= 6 residues).
        goal_type
            One of ``"allosteric"``, ``"hotspot"``, ``"avoidance"``,
            ``"custom"``.
        receptor_chain
            Chain ID of the receptor in the structure (default ``"A"`` on the
            server side).
        receptor_gene
            Gene symbol for the receptor (used for MSA lookup if
            ``receptor_msa_csv`` is not supplied).
        receptor_msa_csv
            Pre-computed MSA in CSV format.  If not supplied, the server uses
            its own MSA database via ``receptor_gene``.
        seed_peptides
            Amino-acid sequences to seed the first iteration. Currently
            required; automated generation integration is planned.
        peptide_length
            Fixed peptide length if you want the iteration to generate peptides
            of a specific length (otherwise the server auto-sizes).
        engine
            Fold engine for the campaign — one of ``"boltz2"`` (default),
            ``"protenix"``, ``"esmfold2"``, ``"openfold3"``.
        diffusion_samples
            Boltz-2 diffusion trajectories per fold (default 4, max 5).
            Production default is 4 per MEMORY.md.
        batch_size
            Peptides folded per iteration (default 8, max 20).
        max_iterations
            Maximum campaign iterations (default 5, max 10).
        max_credits
            Credit cap for the whole campaign (default 5000, max 50000).
        survivor_ipsae_threshold
            Minimum peptide iPSAE for a peptide to advance to the next
            iteration. Server default is 0.67.
        cross_validate_engine
            If set, the campaign automatically cross-validates survivors on
            this engine after completion (e.g. ``"protenix"``).
        hotspots
            Hotspot residues for ``"allosteric"`` or ``"hotspot"`` goals — the
            residues a surviving design has to CONTACT, measured on each
            iteration's folded complex.
            Shape: ``[{"chain": "A", "residue": 792, "label": "K792"}, ...]``.
            ``residue`` is a positive integer (PDB numbering). This is an
            acceptance criterion, not a placement control.
        avoid_residues
            Residues a surviving design must NOT contact (for allosteric goals,
            the orthosteric site) — likewise measured on the folded complex and
            applied as a rejection criterion. It does not steer generation:
            measured on the deployed generator, supplying it does not change
            which sequences are produced.
            Shape: ``[{"chain": "A", "residue": 719, "label": "G719"}, ...]``.
        target_ipsae
            Acceptance iPSAE threshold (the goal is "met" when a peptide
            reaches this score). ``None`` uses the server default.
        target_iptm
            Acceptance ipTM threshold (alternative to iPSAE).
        contact_threshold_a
            Å distance for hotspot contact detection (default 5.0).
        goal_description
            Human-readable description echoed in campaign status.

        Returns
        -------
        dict
            Keys: ``campaign_id``, ``status``, ``engine``, ``max_iterations``,
            ``max_credits``, ``batch_size``, ``seed_count``, ``goal``.

        Raises
        ------
        LigandAIValidationError
            When the server returns 400, especially for missing ``seed_peptides``.
        """
        body = _campaign_start_body(
            receptor_sequence=receptor_sequence,
            goal_type=goal_type,
            receptor_chain=receptor_chain,
            receptor_gene=receptor_gene,
            receptor_msa_csv=receptor_msa_csv,
            seed_peptides=seed_peptides,
            peptide_length=peptide_length,
            engine=engine,
            diffusion_samples=diffusion_samples,
            batch_size=batch_size,
            max_iterations=max_iterations,
            max_credits=max_credits,
            survivor_ipsae_threshold=survivor_ipsae_threshold,
            cross_validate_engine=cross_validate_engine,
            hotspots=hotspots,
            avoid_residues=avoid_residues,
            avoid_shell_angstrom=avoid_shell_angstrom,
            target_ipsae=target_ipsae,
            target_iptm=target_iptm,
            contact_threshold_a=contact_threshold_a,
            goal_description=goal_description,
        )
        try:
            result = self._transport.request(
                "POST", "/api/v1/goal-campaign/start", json=body
            ) or {}
        except LigandAIError as exc:
            # Surface a clear error if the server rejected seed_peptides.
            if getattr(exc, "status_code", None) == 400:
                _interpret_campaign_400(getattr(exc, "response", None) or str(exc))
            raise
        return _scrub_campaign_response(result)

    def campaign_status(self, campaign_id: str) -> dict[str, Any]:
        """Return a full campaign snapshot.

        ``GET /api/v1/goal-campaign/{campaign_id}/status``
        """
        return _scrub_campaign_response(
            self._transport.request(
                "GET", f"/api/v1/goal-campaign/{campaign_id}/status"
            ) or {}
        )

    def campaign_stream(
        self, campaign_id: str, *, timeout: float | None = None
    ) -> Iterator[dict[str, Any]]:
        """Stream live SSE events from a campaign.

        ``GET /api/v1/goal-campaign/{campaign_id}/stream``

        Yields parsed event dicts. Event types include:
        - ``"status"`` — initial campaign snapshot on connect
        - ``"design_scored"`` — one fold result scored against the goal
        - ``"iteration_complete"`` — iteration summary (survivors, hits, budget)
        - ``"result"`` — terminal event with the final campaign snapshot
        - ``"cross_validation_*"`` — cross-validation progress / result

        Parameters
        ----------
        campaign_id
            Campaign identifier from ``campaign_start()`` or ``drive()``.
        timeout
            Optional SSE stream timeout in seconds.

        Yields
        ------
        dict
            Parsed SSE event payload.
        """
        for line in self._transport.stream_lines(
            "GET",
            f"/api/v1/goal-campaign/{campaign_id}/stream",
            timeout=timeout,
        ):
            data = parse_sse_data(line)
            if data is None:
                continue
            yield _scrub_campaign_response(data if isinstance(data, dict) else {"data": data})

    def campaign_cancel(self, campaign_id: str) -> dict[str, Any]:
        """Cancel a running campaign.

        ``POST /api/v1/goal-campaign/{campaign_id}/cancel``

        The campaign stops after the current fold completes.  Returns a dict
        with ``campaign_id``, ``cancelled``, and ``status``.
        """
        return self._transport.request(
            "POST", f"/api/v1/goal-campaign/{campaign_id}/cancel"
        ) or {}

    def campaign_cross_validate(
        self,
        campaign_id: str,
        *,
        secondary_engine: str = "protenix",
        agreement_threshold: float | None = None,
        peptides: list[str] | None = None,
    ) -> dict[str, Any]:
        """Cross-validate campaign survivors on a second fold engine.

        ``POST /api/v1/goal-campaign/{campaign_id}/cross-validate``

        The campaign must be in a terminal state (``"completed"`` or
        ``"budget_exhausted"``) before calling this.  The server runs the
        survivor and hit peptides on ``secondary_engine`` and reports iPSAE
        agreement between the primary and secondary engines.

        Parameters
        ----------
        campaign_id
            Campaign identifier.
        secondary_engine
            Engine to use for cross-validation (default ``"protenix"``).
            Must differ from the primary campaign engine.
        agreement_threshold
            iPSAE agreement threshold for concordance (default 0.4 on the
            server side).  Pass ``None`` to use the server default.
        peptides
            Explicit peptide sequences to validate (default: all campaign
            hits + survivors).

        Returns
        -------
        dict
            Cross-validation report.  Keys include ``agreement_rate``,
            ``concordant``, ``discordant``, and per-peptide results.
        """
        body: dict[str, Any] = {
            "secondary_engine": str(secondary_engine).strip().lower(),
        }
        # Falsy-zero discipline: agreement_threshold=0.0 is a valid value.
        if agreement_threshold is not None:
            body["agreement_threshold"] = float(agreement_threshold)
        if peptides is not None:
            body["peptides"] = [str(p).strip().upper() for p in peptides if str(p).strip()]
        return self._transport.request(
            "POST", f"/api/v1/goal-campaign/{campaign_id}/cross-validate", json=body
        ) or {}

    # ── High-level driver ─────────────────────────────────────────────────────

    def drive(
        self,
        receptor_sequence: str,
        goal_type: Literal["allosteric", "hotspot", "avoidance", "custom"] = "allosteric",
        *,
        gene: str | None = None,
        goal: str | None = None,
        hotspots: list[dict[str, Any]] | None = None,
        avoid_residues: list[dict[str, Any]] | None = None,
        # Ca-Ca packing-shell radius (Angstrom) around avoid_residues. None =
        # server default (8.0 A); 0 disables. [bd-LIGANDAI_ALPHA_V2-x4vt8]
        avoid_shell_angstrom: float | None = None,
        seed_peptides: list[str] | None = None,
        engine: str = "boltz2",
        diffusion_samples: int = 4,
        batch_size: int = 8,
        max_iterations: int = 5,
        max_credits: int = 5000,
        survivor_ipsae_threshold: float | None = None,
        cross_validate_engine: str | None = None,
        target_ipsae: float | None = None,
        contact_threshold_a: float | None = None,
        receptor_chain: str | None = None,
        receptor_gene: str | None = None,
        receptor_msa_csv: str | None = None,
        goal_description: str | None = None,
    ) -> GoalCampaign:
        """Drive an abstract design goal — the one-call high-level entry point.

        Translates a high-level objective (e.g. "allosteric binder — avoid the
        orthosteric site") into a running goal campaign and returns a
        :class:`GoalCampaign` handle.  Call ``.wait()`` to block for the final
        result, or ``.stream()`` to observe progress event by event.

        **Domain rules (must be stated correctly in all caller code):**

        1. **peptide_ipsae (interface iPSAE of the binder chain) is THE binder
           metric.**  Never use overall ``ipsae`` — it is dominated by receptor
           self-confidence on multimeric targets and is NOT a reliable binding
           indicator.  iPSAE is also more reliable than ipTM for Boltz-2
           interface assessment.

        2. **Receptor chains MUST be folded with MSA.**  Peptide chains do
           NOT require MSA (they are de-novo sequences).  Supply
           ``receptor_gene`` or ``receptor_msa_csv`` so the server fetches or
           uses its own MSA for the receptor.

        3. **seed_peptides is optional when the automated LigandForge
           generation integration is active server-side.**  If the server
           returns ``"seed_peptides required"``, a clear
           :class:`~ligandai.errors.LigandAIValidationError` is raised.

        Parameters
        ----------
        receptor_sequence
            Amino-acid sequence of the receptor chain (>= 6 residues).
        goal_type
            Campaign goal type: ``"allosteric"`` (default), ``"hotspot"``,
            ``"avoidance"``, or ``"custom"``.
        gene
            Gene symbol (informational, also used as ``receptor_gene`` if not
            supplied separately). Example: ``"EGFR"``.
        goal
            Human-readable description of the goal. Echoed in campaign status.
        hotspots
            Residues a surviving design has to CONTACT, checked on each
            iteration's folded complex.
            Shape: ``[{"chain": "A", "residue": 792}, ...]``.
            PDB residue numbering.  For allosteric binders, list the allosteric
            pocket here.  This is what the campaign selects ON; it does not aim
            generation at the pocket.
        avoid_residues
            Residues a surviving design must NOT contact, checked on the same
            folded complexes and applied as a rejection criterion.
            Shape: ``[{"chain": "A", "residue": 719}, ...]``.
            For allosteric binders, list the ATP/orthosteric site here.  Supplying
            it does not change what the generator produces — the exclusion is
            realised by rejecting folds that violate it.
        seed_peptides
            Amino-acid sequences to seed the first iteration. Currently
            required until the automated generation integration is live.
        engine
            Fold engine: ``"boltz2"`` (default), ``"protenix"``,
            ``"esmfold2"``, ``"openfold3"``.
        diffusion_samples
            Boltz-2 trajectories per fold (default 4 — production default).
        batch_size
            Peptides folded per iteration (default 8, max 20).
        max_iterations
            Maximum campaign iterations (default 5, max 10).
        max_credits
            Credit cap for the whole campaign (default 5000, max 50000).
        survivor_ipsae_threshold
            Minimum peptide iPSAE to advance to the next iteration (server
            default 0.67).
        cross_validate_engine
            If set, the campaign auto cross-validates survivors on this engine
            after completion (e.g. ``"protenix"`` when primary is ``"boltz2"``).
        target_ipsae
            iPSAE acceptance threshold for declaring the goal "met".
        contact_threshold_a
            Hotspot contact distance in Å (default 5.0 on the server).
            Falsy-zero discipline: ``0.0`` is valid and is transmitted.
        receptor_chain
            Receptor chain ID (default ``"A"`` on the server).
        receptor_gene
            Gene symbol for MSA lookup.  Falls back to ``gene`` when not set.
        receptor_msa_csv
            Pre-computed MSA in CSV format (overrides server MSA lookup).
        goal_description
            Additional description forwarded to the goal struct.

        Returns
        -------
        GoalCampaign
            A handle with ``.wait()``, ``.stream()``, ``.status()``,
            ``.cancel()``, and ``.cross_validate()`` methods.

        Raises
        ------
        LigandAIValidationError
            When the server rejects the request (e.g. missing seed_peptides).

        Examples
        --------
        **Allosteric EGFR binder — avoid the orthosteric ATP-binding site:**

        ::

            import ligandai
            client = ligandai.LigandAI()

            # Domain rules:
            #   - peptide_ipsae (interface iPSAE of the binder chain) is THE binder
            #     metric; never use overall ipsae.  iPSAE is more reliable than ipTM
            #     for Boltz-2 interface assessment.
            #   - Receptor chains MUST use MSA; peptide chains do not.
            #   - hotspots/avoid_residues below are ACCEPTANCE CRITERIA scored on
            #     each iteration's folded complexes. They do not aim generation;
            #     the site selectivity comes from the fold-measure-select loop, so
            #     give it iterations and batch size to work with.

            campaign = client.goals.drive(
                receptor_sequence=egfr_sequence,  # full EC domain AA sequence
                goal_type="allosteric",
                gene="EGFR",
                goal="allosteric site binder — avoid the orthosteric ATP site",
                hotspots=[
                    {"chain": "A", "residue": 792},  # allosteric pocket
                    {"chain": "A", "residue": 855},
                ],
                avoid_residues=[
                    {"chain": "A", "residue": 719},  # kinase hinge (orthosteric)
                    {"chain": "A", "residue": 724},
                ],
                seed_peptides=my_seed_peptides,   # or None when generation is live
                diffusion_samples=4,              # production default
                max_iterations=5,
                survivor_ipsae_threshold=0.67,    # gate on peptide_ipsae, not ipsae
                cross_validate_engine="protenix", # fold-engine agreement check
                receptor_gene="EGFR",             # for MSA lookup (receptor MUST use MSA)
            )

            # Block for the final result.
            result = campaign.wait(timeout=3600)
            print("Campaign status:", result["status"])
            print("Hits:", result.get("hits", []))

            # Or stream events to observe progress:
            for event in campaign.stream():
                if event.get("event") == "design_scored":
                    seq = event["data"]["peptide_seq"]
                    ipsae = event["data"]["peptide_ipsae"]  # THE binder metric
                    print(f"Scored {seq}: peptide_ipsae={ipsae:.3f}")

            # After completion, scan the best structure to see where binding occurred:
            scan = client.goals.campaign_scan(
                structure=result["hits"][0]["pdb_text"],
                hotspots=[{"chain": "A", "residue": 792}],
            )
            print("Hotspot hit:", scan["hotspot_hit"])
            print("Coverage:", scan["hotspot_coverage"])

            # Cross-validate the top hits across engines for agreement:
            cv = campaign.cross_validate(secondary_engine="protenix")
            print("Agreement rate:", cv["agreement_rate"])
        """
        effective_receptor_gene = receptor_gene or gene
        effective_description = goal_description or goal

        result = self.campaign_start(
            receptor_sequence=receptor_sequence,
            goal_type=goal_type,
            receptor_chain=receptor_chain,
            receptor_gene=effective_receptor_gene,
            receptor_msa_csv=receptor_msa_csv,
            seed_peptides=seed_peptides,
            engine=engine,
            diffusion_samples=diffusion_samples,
            batch_size=batch_size,
            max_iterations=max_iterations,
            max_credits=max_credits,
            survivor_ipsae_threshold=survivor_ipsae_threshold,
            cross_validate_engine=cross_validate_engine,
            hotspots=hotspots,
            avoid_residues=avoid_residues,
            avoid_shell_angstrom=avoid_shell_angstrom,
            target_ipsae=target_ipsae,
            contact_threshold_a=contact_threshold_a,
            goal_description=effective_description,
        )

        campaign_id = result.get("campaign_id") or result.get("id") or ""
        if not campaign_id:
            raise LigandAIError(
                "Server did not return a campaign_id for goal campaign start",
                response=result,
            )

        return GoalCampaign(
            campaign_id=campaign_id,
            goals_resource=self,
            gene=gene,
            goal=goal or effective_description,
        )


# ─── Async resource ───────────────────────────────────────────────────────────


class AsyncGoals(AsyncResource):
    """Async ``client.goals`` — same API as :class:`Goals`, fully awaitable.

    See :class:`Goals` for complete documentation.
    """

    # ── AutoResearch (persistent runs) ───────────────────────────────────────

    async def start(
        self,
        goal: str,
        *,
        automatic_mode: bool = False,
        budget_cap_credits: int | None = 10_000,
        program_db_id: int | None = None,
        project_db_id: int | None = None,
        program_id: str | None = None,
        project_id: str | None = None,
        conversation_id: str | None = None,
        max_iterations: int | None = None,
    ) -> GoalRunStart:
        """Async: start a persistent AutoResearch run.  ``POST /api/autoresearch/start``"""
        payload = await self._transport.request(
            "POST",
            "/api/autoresearch/start",
            json=_start_body(
                goal=goal,
                automatic_mode=automatic_mode,
                budget_cap_credits=budget_cap_credits,
                program_db_id=program_db_id,
                project_db_id=project_db_id,
                program_id=program_id,
                project_id=project_id,
                conversation_id=conversation_id,
                max_iterations=max_iterations,
            ),
        )
        return GoalRunStart.model_validate(payload or {})

    async def list(
        self,
        *,
        program_id: str | None = None,
        project_id: str | None = None,
        program_db_id: int | None = None,
        project_db_id: int | None = None,
        conversation_id: str | None = None,
    ) -> list[GoalRun]:
        """Async: list AutoResearch runs.  ``GET /api/autoresearch/runs``"""
        payload = await self._transport.request(
            "GET",
            _runs_path(
                program_id=program_id,
                project_id=project_id,
                program_db_id=program_db_id,
                project_db_id=project_db_id,
                conversation_id=conversation_id,
            ),
        ) or {}
        return [GoalRun.model_validate(run) for run in payload.get("runs", [])]

    async def get(self, run_id: str) -> GoalRun:
        """Async: fetch a single AutoResearch run.  ``GET /api/autoresearch/runs/{run_id}``"""
        payload = await self._transport.request("GET", f"/api/autoresearch/runs/{run_id}") or {}
        return GoalRun.model_validate(payload.get("run") or payload)

    async def graph(self, run_id: str) -> GoalProjectState:
        """Async: return the goal graph.  ``GET /api/autoresearch/runs/{run_id}/graph``"""
        payload = await self._transport.request(
            "GET", f"/api/autoresearch/runs/{run_id}/graph"
        ) or {}
        return GoalProjectState.model_validate(payload.get("goalState") or payload)

    async def stream(  # type: ignore[override]
        self, run_id: str, *, timeout: float | None = None
    ) -> AsyncIterator[GoalRunEvent]:
        """Async: stream AutoResearch events.  ``GET /api/autoresearch/runs/{run_id}/stream``"""
        async for line in self._transport.stream_lines(
            "GET",
            f"/api/autoresearch/runs/{run_id}/stream",
            timeout=timeout,
        ):
            data = parse_sse_data(line)
            if data is None:
                continue
            yield GoalRunEvent.model_validate({**data, "payload": data})

    async def pause(self, run_id: str) -> bool:
        """Async: pause an AutoResearch run.  ``POST /api/autoresearch/runs/{run_id}/pause``"""
        payload = await self._transport.request(
            "POST", f"/api/autoresearch/runs/{run_id}/pause"
        ) or {}
        return bool(payload.get("ok", True))

    async def resume(self, run_id: str) -> bool:
        """Async: resume an AutoResearch run.  ``POST /api/autoresearch/runs/{run_id}/resume``"""
        payload = await self._transport.request(
            "POST", f"/api/autoresearch/runs/{run_id}/resume"
        ) or {}
        return bool(payload.get("ok", True))

    async def stop(self, run_id: str) -> bool:
        """Async: stop an AutoResearch run.  ``POST /api/autoresearch/runs/{run_id}/stop``"""
        payload = await self._transport.request(
            "POST", f"/api/autoresearch/runs/{run_id}/stop"
        ) or {}
        return bool(payload.get("ok", True))

    # ── Goal-campaign engine ──────────────────────────────────────────────────

    async def campaign_scan(
        self,
        structure: str,
        *,
        format: Literal["pdb", "cif"] = "cif",  # noqa: A002
        peptide_chain: str = "B",
        hotspots: list[dict[str, Any]] | None = None,
        contact_threshold_a: float | None = None,
    ) -> dict[str, Any]:
        """Async: ad-hoc binding-site scan.  ``POST /api/v1/goal-campaign/scan``"""
        body: dict[str, Any] = {
            "structure": structure,
            "format": format,
            "peptide_chain": peptide_chain.strip().upper() if peptide_chain else "B",
        }
        if hotspots is not None:
            body["hotspots"] = hotspots
        if contact_threshold_a is not None:
            body["contact_threshold_a"] = float(contact_threshold_a)
        return _scrub_campaign_response(
            await self._transport.request("POST", "/api/v1/goal-campaign/scan", json=body) or {}
        )

    async def campaign_start(
        self,
        receptor_sequence: str,
        goal_type: Literal["allosteric", "hotspot", "avoidance", "custom"],
        *,
        receptor_chain: str | None = None,
        receptor_gene: str | None = None,
        receptor_msa_csv: str | None = None,
        seed_peptides: list[str] | None = None,
        peptide_length: int | None = None,
        engine: str = "boltz2",
        diffusion_samples: int = 4,
        batch_size: int = 8,
        max_iterations: int = 5,
        max_credits: int = 5000,
        survivor_ipsae_threshold: float | None = None,
        cross_validate_engine: str | None = None,
        hotspots: list[dict[str, Any]] | None = None,
        avoid_residues: list[dict[str, Any]] | None = None,
        # Ca-Ca packing-shell radius (Angstrom) around avoid_residues. None =
        # server default (8.0 A); 0 disables. [bd-LIGANDAI_ALPHA_V2-x4vt8]
        avoid_shell_angstrom: float | None = None,
        target_ipsae: float | None = None,
        target_iptm: float | None = None,
        contact_threshold_a: float | None = None,
        goal_description: str | None = None,
    ) -> dict[str, Any]:
        """Async: start a goal campaign.  ``POST /api/v1/goal-campaign/start``"""
        body = _campaign_start_body(
            receptor_sequence=receptor_sequence,
            goal_type=goal_type,
            receptor_chain=receptor_chain,
            receptor_gene=receptor_gene,
            receptor_msa_csv=receptor_msa_csv,
            seed_peptides=seed_peptides,
            peptide_length=peptide_length,
            engine=engine,
            diffusion_samples=diffusion_samples,
            batch_size=batch_size,
            max_iterations=max_iterations,
            max_credits=max_credits,
            survivor_ipsae_threshold=survivor_ipsae_threshold,
            cross_validate_engine=cross_validate_engine,
            hotspots=hotspots,
            avoid_residues=avoid_residues,
            avoid_shell_angstrom=avoid_shell_angstrom,
            target_ipsae=target_ipsae,
            target_iptm=target_iptm,
            contact_threshold_a=contact_threshold_a,
            goal_description=goal_description,
        )
        try:
            result = await self._transport.request(
                "POST", "/api/v1/goal-campaign/start", json=body
            ) or {}
        except LigandAIError as exc:
            if getattr(exc, "status_code", None) == 400:
                _interpret_campaign_400(getattr(exc, "response", None) or str(exc))
            raise
        return _scrub_campaign_response(result)

    async def campaign_status(self, campaign_id: str) -> dict[str, Any]:
        """Async: campaign snapshot.  ``GET /api/v1/goal-campaign/{id}/status``"""
        return _scrub_campaign_response(
            await self._transport.request(
                "GET", f"/api/v1/goal-campaign/{campaign_id}/status"
            ) or {}
        )

    async def campaign_stream(
        self, campaign_id: str, *, timeout: float | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Async: stream campaign SSE events.  ``GET /api/v1/goal-campaign/{id}/stream``"""
        async for line in self._transport.stream_lines(
            "GET",
            f"/api/v1/goal-campaign/{campaign_id}/stream",
            timeout=timeout,
        ):
            data = parse_sse_data(line)
            if data is None:
                continue
            yield _scrub_campaign_response(data if isinstance(data, dict) else {"data": data})

    async def campaign_cancel(self, campaign_id: str) -> dict[str, Any]:
        """Async: cancel a campaign.  ``POST /api/v1/goal-campaign/{id}/cancel``"""
        return await self._transport.request(
            "POST", f"/api/v1/goal-campaign/{campaign_id}/cancel"
        ) or {}

    async def campaign_cross_validate(
        self,
        campaign_id: str,
        *,
        secondary_engine: str = "protenix",
        agreement_threshold: float | None = None,
        peptides: list[str] | None = None,
    ) -> dict[str, Any]:
        """Async: cross-validate campaign survivors.
        ``POST /api/v1/goal-campaign/{id}/cross-validate``
        """
        body: dict[str, Any] = {
            "secondary_engine": str(secondary_engine).strip().lower(),
        }
        if agreement_threshold is not None:
            body["agreement_threshold"] = float(agreement_threshold)
        if peptides is not None:
            body["peptides"] = [str(p).strip().upper() for p in peptides if str(p).strip()]
        return await self._transport.request(
            "POST", f"/api/v1/goal-campaign/{campaign_id}/cross-validate", json=body
        ) or {}

    # ── High-level async driver ───────────────────────────────────────────────

    async def drive(
        self,
        receptor_sequence: str,
        goal_type: Literal["allosteric", "hotspot", "avoidance", "custom"] = "allosteric",
        *,
        gene: str | None = None,
        goal: str | None = None,
        hotspots: list[dict[str, Any]] | None = None,
        avoid_residues: list[dict[str, Any]] | None = None,
        # Ca-Ca packing-shell radius (Angstrom) around avoid_residues. None =
        # server default (8.0 A); 0 disables. [bd-LIGANDAI_ALPHA_V2-x4vt8]
        avoid_shell_angstrom: float | None = None,
        seed_peptides: list[str] | None = None,
        engine: str = "boltz2",
        diffusion_samples: int = 4,
        batch_size: int = 8,
        max_iterations: int = 5,
        max_credits: int = 5000,
        survivor_ipsae_threshold: float | None = None,
        cross_validate_engine: str | None = None,
        target_ipsae: float | None = None,
        contact_threshold_a: float | None = None,
        receptor_chain: str | None = None,
        receptor_gene: str | None = None,
        receptor_msa_csv: str | None = None,
        goal_description: str | None = None,
    ) -> AsyncGoalCampaign:
        """Async: drive an abstract design goal.  Returns :class:`AsyncGoalCampaign`.

        See :meth:`Goals.drive` for full documentation and examples.
        """
        effective_receptor_gene = receptor_gene or gene
        effective_description = goal_description or goal

        result = await self.campaign_start(
            receptor_sequence=receptor_sequence,
            goal_type=goal_type,
            receptor_chain=receptor_chain,
            receptor_gene=effective_receptor_gene,
            receptor_msa_csv=receptor_msa_csv,
            seed_peptides=seed_peptides,
            engine=engine,
            diffusion_samples=diffusion_samples,
            batch_size=batch_size,
            max_iterations=max_iterations,
            max_credits=max_credits,
            survivor_ipsae_threshold=survivor_ipsae_threshold,
            cross_validate_engine=cross_validate_engine,
            hotspots=hotspots,
            avoid_residues=avoid_residues,
            avoid_shell_angstrom=avoid_shell_angstrom,
            target_ipsae=target_ipsae,
            contact_threshold_a=contact_threshold_a,
            goal_description=effective_description,
        )

        campaign_id = result.get("campaign_id") or result.get("id") or ""
        if not campaign_id:
            raise LigandAIError(
                "Server did not return a campaign_id for goal campaign start",
                response=result,
            )

        return AsyncGoalCampaign(
            campaign_id=campaign_id,
            goals_resource=self,
            gene=gene,
            goal=goal or effective_description,
        )
