# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Validated Binder Delivery — a closed design loop sold against a hit count.

Unlike :meth:`ligandai.resources.peptides.Peptides.generate`, which produces a
fixed number of DESIGNS, a Validated Binder Delivery run is bought against a
number of HITS. The platform generates, screens, folds at multiple independent
trajectories, counts how many designs clear the hit bar, and — if it is short —
generates more and repeats, until the hit target is met or a hard compute cap
stops it. You pay for the compute actually consumed, never more than the cap
you set.

Server endpoints (``/api/v1/validated-binder/*``):

- ``POST /quote``              — price a run and see whether the guarantee applies
- ``POST /start``              — reserve credits and launch the loop
- ``GET  /{run_id}/status``    — poll progress, hits, and settlement
- ``POST /{run_id}/cancel``    — stop early; unused reservation is released

Tier: academic, pro, or enterprise (server-gated).

Selection is always on the MEDIAN of the folded trajectories, never the best
one — a design that clears the bar on a single lucky draw is not a hit.

The commercial guarantee
------------------------
A user's FIRST run against a given target is refunded in full if it finishes
under the guaranteed hit count, subject to a lifetime cap on claims. Call
:meth:`ValidatedBinder.quote` first: it reports ``guaranteeApplies`` for your
account and target before any credits move.

⛔ Selecting a custom hit definition (``acceptance_criterion``) runs WITHOUT the
guarantee — see :meth:`ValidatedBinder.start`.
"""

from __future__ import annotations

from typing import Any, Literal

from ligandai.resources._base import AsyncResource, Resource

_BASE = "/api/v1/validated-binder"

_Tier = Literal["screening", "confirmation"]

#: Hit definitions a Validated Binder Delivery run can evaluate. This is the
#: same canonical acceptance ladder
#: :meth:`ligandai.resources.peptides.Peptides.search` filters on, minus the
#: affinity criterion — see :meth:`ValidatedBinder.start` for why.
_AcceptanceCriterion = Literal["great_plus", "elite", "super_elite"]


def _run_body(
    target_key: str,
    hit_target: int | None,
    tier: _Tier | None,
    gpu_hour_cap: float | None,
    acceptance_criterion: _AcceptanceCriterion | None,
    require_linker_leg: bool | None,
    require_ensemble: bool | None,
) -> dict[str, Any]:
    """Assemble the shared quote/start body.

    ⛔ Every optional key is OMITTED when ``None`` rather than sent as null.
    The server treats "no acceptance parameter supplied" as the standard hit
    definition and the standard guarantee, so sending an explicit null would
    opt a caller into a different product than the one they asked for.
    """
    body: dict[str, Any] = {"targetKey": target_key}
    if hit_target is not None:
        body["hitTarget"] = hit_target
    if tier is not None:
        body["tier"] = tier
    if gpu_hour_cap is not None:
        body["gpuHourCap"] = gpu_hour_cap
    if acceptance_criterion is not None:
        body["acceptance_criterion"] = acceptance_criterion
    if require_linker_leg is not None:
        body["require_linker_leg"] = require_linker_leg
    if require_ensemble is not None:
        body["require_ensemble"] = require_ensemble
    return body


class ValidatedBinder(Resource):
    """``/api/v1/validated-binder/*``."""

    def quote(
        self,
        target_key: str,
        *,
        hit_target: int | None = None,
        tier: _Tier | None = None,
        gpu_hour_cap: float | None = None,
        acceptance_criterion: _AcceptanceCriterion | None = None,
        require_linker_leg: bool | None = None,
        require_ensemble: bool | None = None,
    ) -> dict[str, Any]:
        """``POST /api/v1/validated-binder/quote`` — price a run without starting it.

        No credits are reserved and no compute runs. Call this before
        :meth:`start` to see the worst-case cost and whether the guarantee
        applies to your account for this target.

        Args:
            target_key: Gene symbol or target identifier.
            hit_target: How many hits the loop should reach before stopping.
                Defaults to the guaranteed minimum.
            tier: ``"screening"`` (fewer trajectories per design, cheaper) or
                ``"confirmation"`` (more trajectories — use this for anything
                heading to synthesis).
            gpu_hour_cap: Hard compute ceiling for the run. Clamped
                server-side into the supported band.
            acceptance_criterion: The definition of "hit" this run is counted
                against. See :meth:`start` for the full contract, the unit
                traps, and why selecting one voids the guarantee.
            require_linker_leg: Rejected with a 400 when ``True``. See
                :meth:`start`.
            require_ensemble: Rejected with a 400 when ``True``. See
                :meth:`start`.

        Returns:
            The quote. ``capCostCredits`` / ``capCostUsd`` are the
            deterministic worst-case ceiling and are always correct.
            ``estimate`` carries the hit-rate-derived projection, and is
            ``None`` with a populated ``estimateUnavailable`` whenever no
            projection can be made — ⛔ never read ``estimate: None`` as "zero
            hits expected". Selecting an ``acceptance_criterion`` is one such
            case: the measured rates the projection is built from were measured
            against the standard hit definition and do not carry over.
            ``acceptance`` echoes the definition this quote was priced against,
            or ``None`` for the standard one.
        """
        body = _run_body(
            target_key, hit_target, tier, gpu_hour_cap,
            acceptance_criterion, require_linker_leg, require_ensemble,
        )
        return self._transport.request("POST", f"{_BASE}/quote", json=body) or {}

    def start(
        self,
        target_key: str,
        *,
        hit_target: int | None = None,
        tier: _Tier | None = None,
        gpu_hour_cap: float | None = None,
        acceptance_criterion: _AcceptanceCriterion | None = None,
        require_linker_leg: bool | None = None,
        require_ensemble: bool | None = None,
    ) -> dict[str, Any]:
        """``POST /api/v1/validated-binder/start`` — reserve credits and launch the loop.

        Reserves the full ``gpu_hour_cap`` worth of credits up front, because
        the loop is allowed to run all the way to the cap. Whatever is not
        consumed is released when the run settles. Poll :meth:`status` for
        progress; settlement happens automatically on the first poll after the
        run reaches a terminal state.

        Args:
            target_key: Gene symbol or target identifier.
            hit_target: How many hits to reach before stopping. Defaults to the
                guaranteed minimum.
            tier: ``"screening"`` or ``"confirmation"``.
            gpu_hour_cap: Hard compute ceiling for the run.
            acceptance_criterion: The definition of "hit" this run is counted
                against, from the ONE canonical acceptance ladder — the same
                ladder :meth:`ligandai.resources.peptides.Peptides.search`
                filters on. One of ``"great_plus"``
                (``peptide_ipsae >= 0.66``), ``"elite"``
                (``peptide_ipsae >= 0.85``), or ``"super_elite"``
                (``peptide_ipsae >= 0.67`` AND ``iptm >= 0.80`` AND
                ``plddt >= 88`` — the structural/publication gate).

                Omit it to use the platform's standard hit definition, which is
                what every existing run uses and what the guarantee is priced
                against.

                ⛔ SELECTING ONE VOIDS THE HIT GUARANTEE. The guaranteed hit
                count was calibrated against the standard definition, and
                against hit rates measured under it. A stricter definition has
                a strictly lower hit rate by an amount nobody has measured, so
                those runs are sold without the refund. The response's
                ``guaranteeApplies`` is ``False`` and ``guaranteeVoidedReason``
                says so in words. Your compute cap and cost ceiling are
                unaffected.

                ⛔ UNIT TRAPS: ``plddt`` is 0–100 (``0.88`` silently passes
                everything); the structural leg gates ``peptide_ipsae``, never
                whole-complex ``ipsae``.

                ``"super_elite_affinity"`` is REJECTED here with a 400. This
                loop does not predict binding affinity while it runs, so that
                criterion's affinity requirement would be unmeasured for every
                design and the run would report zero hits after spending its
                whole compute budget. Apply it with
                :meth:`ligandai.resources.peptides.Peptides.search` to designs
                that already exist instead.
            require_linker_leg: REJECTED with a 400 when ``True``. This loop
                folds each designed peptide on its own, with no purification
                cassette attached, and does not measure how the interface is
                distributed across the construct — so the designed-region check
                has nothing to measure here. Omit it.
            require_ensemble: REJECTED with a 400 when ``True``, and requires
                enterprise tier to request at all. This loop does not run the
                LigandIQ Pre-Fold confirmatory screen while it regenerates, so
                a second independent opinion does not exist for these designs.
                Requesting it would leave every design unresolved and return
                zero hits after the full compute budget was spent, so the
                platform refuses rather than reporting a dual-gated result it
                is not.

        Returns:
            The run record, including ``runId`` and ``guaranteeApplies``.

        Raises:
            LigandAIError: 402 when the account cannot cover the reservation,
                403 when the tier does not permit this run type (or the dual
                gate), 400 for an acceptance selection this loop cannot
                evaluate.
        """
        body = _run_body(
            target_key, hit_target, tier, gpu_hour_cap,
            acceptance_criterion, require_linker_leg, require_ensemble,
        )
        return self._transport.request("POST", f"{_BASE}/start", json=body) or {}

    def status(self, run_id: str) -> dict[str, Any]:
        """``GET /api/v1/validated-binder/{run_id}/status`` — poll a run.

        Returns progress, billing and the hit count. Settles the run's credits
        the first time it is called after the run reaches a terminal state;
        safe to call repeatedly.

        ⛔ READ ALL THREE BUCKETS, NOT JUST ``hits``. ``hits`` counts designs
        that PASSED the hit definition. When a custom ``acceptance_criterion``
        is in force, ``acceptanceTally`` reports ``pass`` / ``fail`` /
        ``indeterminate`` separately. ``indeterminate`` means the loop could
        not judge the design — it is neither a hit nor a miss, and collapsing
        it into either one misreports the run. ``acceptanceTally`` is ``None``
        when the run used the standard hit definition, and also when the server
        process that ran the rounds is no longer the one answering, so ``None``
        means "not reported here" and never "zero".
        """
        return self._transport.request("GET", f"{_BASE}/{run_id}/status") or {}

    def cancel(self, run_id: str) -> dict[str, Any]:
        """``POST /api/v1/validated-binder/{run_id}/cancel`` — stop a run early.

        Signals the loop to stop at its next checkpoint. Compute already
        consumed is charged; the rest of the reservation is released. A
        cancelled run does not qualify for the hit guarantee — it was never
        given the chance to reach the target.
        """
        return self._transport.request("POST", f"{_BASE}/{run_id}/cancel") or {}


class AsyncValidatedBinder(AsyncResource):
    """Async mirror of :class:`ValidatedBinder`. Same method surface."""

    async def quote(
        self,
        target_key: str,
        *,
        hit_target: int | None = None,
        tier: _Tier | None = None,
        gpu_hour_cap: float | None = None,
        acceptance_criterion: _AcceptanceCriterion | None = None,
        require_linker_leg: bool | None = None,
        require_ensemble: bool | None = None,
    ) -> dict[str, Any]:
        """Async :meth:`ValidatedBinder.quote`. See it for the full contract."""
        body = _run_body(
            target_key, hit_target, tier, gpu_hour_cap,
            acceptance_criterion, require_linker_leg, require_ensemble,
        )
        return await self._transport.request("POST", f"{_BASE}/quote", json=body) or {}

    async def start(
        self,
        target_key: str,
        *,
        hit_target: int | None = None,
        tier: _Tier | None = None,
        gpu_hour_cap: float | None = None,
        acceptance_criterion: _AcceptanceCriterion | None = None,
        require_linker_leg: bool | None = None,
        require_ensemble: bool | None = None,
    ) -> dict[str, Any]:
        """Async :meth:`ValidatedBinder.start`.

        See it for the full contract, including why selecting an
        ``acceptance_criterion`` voids the hit guarantee.
        """
        body = _run_body(
            target_key, hit_target, tier, gpu_hour_cap,
            acceptance_criterion, require_linker_leg, require_ensemble,
        )
        return await self._transport.request("POST", f"{_BASE}/start", json=body) or {}

    async def status(self, run_id: str) -> dict[str, Any]:
        """Async :meth:`ValidatedBinder.status`. Same three-bucket caveat."""
        return await self._transport.request("GET", f"{_BASE}/{run_id}/status") or {}

    async def cancel(self, run_id: str) -> dict[str, Any]:
        """Async :meth:`ValidatedBinder.cancel`."""
        return await self._transport.request("POST", f"{_BASE}/{run_id}/cancel") or {}
