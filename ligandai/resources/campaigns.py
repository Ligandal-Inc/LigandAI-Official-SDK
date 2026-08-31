# Copyright © 2025 Ligandal, Inc. All rights reserved.
"""Recursive design campaigns — generate, fold a panel, select on Δ, replicate.

A single-target design run answers "does this bind X". A *campaign* answers the
question a customer actually has: "does this bind X and NOT its paralogs, and
does it still do that when I re-roll the dice". Those are different questions
and only the second one survives contact with a wet lab.

The loop this module makes expressible:

1. **Generate** against the target, steered away from real off-target pockets
   (:class:`~ligandai.contrastive.ContrastiveGuidance`).
2. **Fold the same designs against the target AND every off-target** —
   :meth:`Campaigns.fold_panel`.
3. **Score** each leg on ``peptide_ipsae`` (never whole-complex ``ipsae``) with
   the platform's own quality tiers.
4. **Select on Δ = ipSAE(target) − ipSAE(worst off-target)**, not on the target
   score alone — :meth:`PanelResult.deltas`.
5. **Replicate at independent draws** and keep only what holds —
   :meth:`Campaigns.replicate`, :meth:`PanelResult.hold_rate`.
6. **Feed survivors back** as the next round's references and hard negatives —
   :class:`RecursiveCampaign`.

⛔ FIVE RULES THIS MODULE ENFORCES, each of which a real campaign paid for:

``PARTIAL_PANEL_INFLATES_THE_WORST_CASE``
    A worst case minimised over a SUBSET of comparators is an upper bound
    wearing a lower bound's name. One design read +0.447 over two comparators
    and −0.012 once the panel was complete. :meth:`PanelResult.deltas` admits a
    row ONLY with every comparator present, and reports incomplete rows
    separately, labelled as upper bounds and never ranked with the verdicts.

``RANKING_IS_NOT_POSSIBLE_BELOW_THE_NOISE``
    Fold metrics move between independent draws. A Δ smaller than the measured
    draw-to-draw spread is INDISTINGUISHABLE — never "slightly prefers the
    target". The band is a REQUIRED argument measured from your own replicates
    (:meth:`PanelResult.observed_spread`); this module ships no default, because
    a band measured on one campaign is not a band on yours.

``TOP_OF_A_SINGLE_DRAW_IS_PARTLY_NOISE``
    The top of a single-draw ranking is selected partly by that draw's noise, so
    re-rolling moves it down. Nothing is a "candidate" until it holds at an
    independent draw.

``NEAR_WORST_AS_EXACT_WORST``
    "This off-target was worst in 7 of 8" is a different statistic from "within
    a hair of worst in 7 of 8". Both are emitted, and the tie tolerance is a
    declared constant.

``THE_SCORE_MUST_BE_ABOUT_THE_DESIGN``
    A fold whose interface is carried by an expression cassette is not a hit.
    Rows flagged ``scaffold_mediated`` are excluded from selection and counted,
    never silently dropped.

Refs: bd-dre-if562.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from ligandai.contrastive import (
    AvoidancePolicy,
    ContrastiveGuidance,
    ContrastiveReference,
    PocketFeatureMatrix,
    V6_9_POCKET_FEATURE_DIM,
    model_pocket_feature_dim,
)
from ligandai.errors import (
    LigandAIError,
    LigandAINotFoundError,
    LigandAIParameterNotForwarded,
    LigandAIValidationError,
)
from ligandai.resources._base import AsyncResource, Resource

__all__ = [
    "Campaigns",
    "AsyncCampaigns",
    "PanelLeg",
    "PanelResult",
    "SelectivityDelta",
    "PartialDelta",
    "ReplicationReport",
    "RecursiveCampaign",
    "CampaignRound",
]

_POCKET_FEATURES_PATH = "/api/pocket-features/{gene}"

#: Per-leg aggregation over independent draws.
LegStatistic = Literal["median", "mean", "min", "max"]

#: Verdicts :meth:`PanelResult.deltas` can return for a complete row.
DeltaVerdict = Literal[
    "PASS_BOTH",
    "TARGET_ONLY",
    "DELTA_ONLY",
    "INDISTINGUISHABLE",
    "OFFTARGET_PREFERRED",
]


def _aggregate(values: Sequence[float], statistic: LegStatistic) -> float:
    if statistic == "median":
        return float(statistics.median(values))
    if statistic == "mean":
        return float(statistics.fmean(values))
    if statistic == "min":
        return float(min(values))
    if statistic == "max":
        return float(max(values))
    raise LigandAIValidationError(
        f"unknown leg statistic {statistic!r}; use 'median', 'mean', 'min' or 'max'"
    )


def _pocket_features_unreachable(gene: str) -> LigandAIParameterNotForwarded:
    """The one error for "the featurizer did not answer with a pocket".

    Covers a 404 AND a 200 that carries no features, because an unmounted API
    path on a single-page-app server commonly returns the app shell rather than
    a 404 — the two are indistinguishable to a client and have the same fix.
    """
    return LigandAIParameterNotForwarded(
        f"pocket features for {gene!r} are not reachable from this API surface. "
        f"The featurizer endpoint exists and is what the generation path calls "
        f"internally, but it is not mounted on the public API, so a client cannot "
        f"build its own contrastive reference panel yet.\n"
        f"\n"
        f"What works today: run a v6.9-line model and let the platform derive its "
        f"own real paralog references — it refuses the run rather than generating "
        f"against a fabricated one. What does not work: choosing the panel, or "
        f"reading back which paralogs were used.",
        parameter="pocket_features",
        reached="internal featurizer service",
        dropped_at="public API surface (endpoint not mounted)",
        tracking="bd-dre-eadc7",
    )


def _norm_seq(seq: str) -> str:
    s = str(seq).strip().upper()
    if not s or not s.isalpha():
        raise LigandAIValidationError(f"not an amino-acid sequence: {seq!r}")
    return s


# ─────────────────────────────────────────────────────────────────────────────
# Panel observations
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class PanelLeg:
    """One (design, receptor) cell of the panel, over one or more draws.

    ``peptide_ipsae_draws`` holds ONE value per independent fold submission.
    A leg with fewer draws than requested is INCOMPLETE and is excluded from
    selection rather than averaged over whatever arrived.
    """

    sequence: str
    receptor: str
    fold_job_ids: list[str] = field(default_factory=list)
    peptide_ipsae_draws: list[float] = field(default_factory=list)
    iptm_draws: list[float] = field(default_factory=list)
    plddt_draws: list[float] = field(default_factory=list)
    quality_tiers: list[str | None] = field(default_factory=list)
    scaffold_mediated_draws: list[bool] = field(default_factory=list)
    not_computed: int = 0
    """Draws whose ``peptide_ipsae`` was NULL — indeterminate, not a zero."""

    @property
    def n_draws(self) -> int:
        return len(self.peptide_ipsae_draws)

    @property
    def scaffold_mediated(self) -> bool:
        """True when ANY draw's interface was carried by the expression cassette.

        Any is the right quantifier: one scaffold-mediated draw means the score
        is not reliably about the design.
        """
        return any(self.scaffold_mediated_draws)

    def value(self, statistic: LegStatistic = "median") -> float | None:
        """Aggregate ``peptide_ipsae`` over the draws, or None if there are none."""
        if not self.peptide_ipsae_draws:
            return None
        return _aggregate(self.peptide_ipsae_draws, statistic)

    def spread(self) -> float | None:
        """max − min across draws. None with fewer than two draws.

        This is a RANGE, not a standard error. It does not shrink as √n and must
        not be divided by one.
        """
        if len(self.peptide_ipsae_draws) < 2:
            return None
        return float(max(self.peptide_ipsae_draws) - min(self.peptide_ipsae_draws))

    def __repr__(self) -> str:  # pragma: no cover - repr only
        v = self.value()
        shown = "None" if v is None else f"{v:.4f}"
        return f"<PanelLeg {self.receptor} {self.sequence[:12]}… ipSAE={shown} n={self.n_draws}>"


@dataclass(frozen=True)
class SelectivityDelta:
    """A complete-panel selectivity row. Ranked on ``delta``, never on ``target``."""

    sequence: str
    target: str
    target_ipsae: float
    off_target_ipsae: dict[str, float]
    worst_off_target: str
    worst_off_target_ipsae: float
    delta: float
    ties_within_tolerance: tuple[str, ...]
    target_leg_pass: bool
    delta_leg_pass: bool
    verdict: DeltaVerdict
    delta_resolvable: bool = True
    quality_tier: str | None = None
    n_draws: int = 1

    @property
    def resolvable(self) -> bool:
        """False when |delta| sits inside the measured band.

        ⛔ Read this, not the verdict, before saying a design prefers anything.
        The ladder reports ``TARGET_ONLY`` for a design that clears the on-target
        floor but whose Δ is unreadable — a real and common state, and the one
        most easily misread as "binds the target, mildly selective". It is not
        mildly selective. Its selectivity was not measured.
        """
        return self.delta_resolvable

    def __repr__(self) -> str:  # pragma: no cover - repr only
        return (
            f"<SelectivityDelta {self.sequence[:12]}… Δ={self.delta:+.4f} "
            f"vs {self.worst_off_target} [{self.verdict}]>"
        )


@dataclass(frozen=True)
class PartialDelta:
    """An INCOMPLETE-panel row. An upper bound on the true Δ — never a verdict.

    A margin minimised over a subset of comparators can only FALL when the
    missing comparators arrive. Kept and reported so an in-flight panel is
    legible, in a separate list so the number cannot be mistaken for a result.
    """

    sequence: str
    target: str
    target_ipsae: float
    comparators_present: tuple[str, ...]
    comparators_missing: tuple[str, ...]
    delta_upper_bound: float
    worst_present: str


@dataclass(frozen=True)
class ReplicationReport:
    """How much of a shortlist survived an independent re-roll."""

    tested: tuple[str, ...]
    held: tuple[str, ...]
    lost: tuple[str, ...]
    untested: tuple[str, ...]
    criterion: str

    @property
    def hold_rate(self) -> float | None:
        n = len(self.tested)
        return None if n == 0 else len(self.held) / n

    def __repr__(self) -> str:  # pragma: no cover - repr only
        hr = self.hold_rate
        shown = "n/a" if hr is None else f"{hr:.0%}"
        return (
            f"<ReplicationReport {len(self.held)}/{len(self.tested)} held ({shown}); "
            f"{len(self.untested)} untested>"
        )


class PanelResult:
    """The folded panel: every design against the target and every off-target.

    Build one with :meth:`Campaigns.fold_panel`. Everything downstream —
    Δ selection, tier counts, replication — reads from here.
    """

    def __init__(
        self,
        *,
        target: str,
        off_targets: Sequence[str],
        legs: dict[tuple[str, str], PanelLeg],
        requested_draws: int,
        statistic: LegStatistic = "median",
    ) -> None:
        self.target = target
        self.off_targets = tuple(off_targets)
        self.legs = legs
        self.requested_draws = int(requested_draws)
        self.statistic: LegStatistic = statistic

    # ─── introspection ──────────────────────────────────────────────────────

    @property
    def sequences(self) -> list[str]:
        return sorted({seq for (seq, _r) in self.legs})

    @property
    def receptors(self) -> tuple[str, ...]:
        return (self.target, *self.off_targets)

    def leg(self, sequence: str, receptor: str) -> PanelLeg | None:
        return self.legs.get((_norm_seq(sequence), receptor))

    def observed_spread(self) -> dict[str, Any]:
        """Measured draw-to-draw spread of ``peptide_ipsae``, over every leg.

        THIS is where a noise band comes from — your own replicates on your own
        target, not a constant copied out of someone else's campaign.

        Returns ``{"n_legs", "median_range", "max_range", "pooled_sd"}``.
        ``median_range`` is the usual choice for a one-leg band. When the two
        legs of a Δ come from separate submissions their noise is independent
        and none of it cancels, so the band on a DIFFERENCE is wider — see
        :meth:`cross_draw_band`.
        """
        ranges = [s for leg in self.legs.values() if (s := leg.spread()) is not None]
        pooled = [v for leg in self.legs.values() if len(leg.peptide_ipsae_draws) > 1
                  for v in leg.peptide_ipsae_draws]
        sd: float | None = None
        if len(ranges) >= 1 and len(pooled) >= 2:
            devs: list[float] = []
            for leg in self.legs.values():
                if len(leg.peptide_ipsae_draws) < 2:
                    continue
                m = statistics.fmean(leg.peptide_ipsae_draws)
                devs.extend(v - m for v in leg.peptide_ipsae_draws)
            if len(devs) >= 2:
                sd = float(statistics.pstdev(devs))
        return {
            "n_legs": len(ranges),
            "median_range": None if not ranges else float(statistics.median(ranges)),
            "max_range": None if not ranges else float(max(ranges)),
            "pooled_sd": sd,
        }

    def cross_draw_band(self) -> float | None:
        """One-leg observed range propagated through a difference of two legs.

        ``range × √2``. Use this when the target leg and the off-target legs were
        not folded in the same submission — their noise is independent, so the
        band on the difference is wider than the band on either leg.
        """
        spread = self.observed_spread()
        r = spread.get("median_range")
        return None if r is None else round(float(r) * (2 ** 0.5), 4)

    # ─── selection ──────────────────────────────────────────────────────────

    def deltas(
        self,
        *,
        band: float,
        target_leg_min: float,
        statistic: LegStatistic | None = None,
        tie_tolerance: float = 0.011,
        exclude_scaffold_mediated: bool = True,
    ) -> tuple[list[SelectivityDelta], list[PartialDelta], dict[str, Any]]:
        """Rank designs on Δ = ipSAE(target) − ipSAE(worst off-target).

        Parameters
        ----------
        band
            The resolvable band, in ipSAE units. A |Δ| below this is
            INDISTINGUISHABLE — the design does not "slightly prefer" anything.
            REQUIRED and deliberately without a default: measure it from your own
            replicates with :meth:`observed_spread` / :meth:`cross_draw_band`.
        target_leg_min
            The on-target floor a design must clear on its own. Take it from the
            platform's tier ladder rather than inventing one — the server returns
            ``quality_tier`` on every fold, and ``client.evaluator.filter_folds``
            enforces the named criteria server-side.
        statistic
            Per-leg aggregation over draws. Defaults to the panel's own.
        tie_tolerance
            Off-targets within this of the worst are also reported, so a near-tie
            is never presented as an exact maximum.
        exclude_scaffold_mediated
            Drop designs whose interface was carried by an expression cassette in
            any draw. They are counted in the returned summary, never silently
            discarded.

        Returns
        -------
        ``(complete, partial, summary)`` — ``complete`` sorted by Δ descending,
        ``partial`` upper bounds only, ``summary`` with the counts.
        """
        if band is None:
            raise LigandAIValidationError(
                "band= is required. A Δ smaller than the draw-to-draw spread is not "
                "a small preference, it is no measurement at all. Measure it: run "
                "fold_panel(..., replicates=2+) and read observed_spread() / "
                "cross_draw_band()."
            )
        band = float(band)
        if band < 0:
            raise LigandAIValidationError("band must be >= 0")
        stat: LegStatistic = statistic or self.statistic

        complete: list[SelectivityDelta] = []
        partial: list[PartialDelta] = []
        n_scaffold_excluded = 0
        n_incomplete_target = 0

        for seq in self.sequences:
            t_leg = self.legs.get((seq, self.target))
            t_val = None if t_leg is None else t_leg.value(stat)
            t_complete = t_leg is not None and t_leg.n_draws >= self.requested_draws
            if t_val is None:
                n_incomplete_target += 1
                continue

            legs_present: dict[str, float] = {}
            missing: list[str] = []
            scaffolded = bool(t_leg is not None and t_leg.scaffold_mediated)
            for off in self.off_targets:
                o_leg = self.legs.get((seq, off))
                o_val = None if o_leg is None else o_leg.value(stat)
                if o_val is None or o_leg is None or o_leg.n_draws < self.requested_draws:
                    missing.append(off)
                else:
                    legs_present[off] = o_val
                    scaffolded = scaffolded or o_leg.scaffold_mediated

            if exclude_scaffold_mediated and scaffolded:
                n_scaffold_excluded += 1
                continue

            if missing or not t_complete:
                if legs_present:
                    wr = max(legs_present, key=lambda r: legs_present[r])
                    partial.append(
                        PartialDelta(
                            sequence=seq,
                            target=self.target,
                            target_ipsae=round(t_val, 4),
                            comparators_present=tuple(sorted(legs_present)),
                            comparators_missing=tuple(sorted(set(missing))),
                            delta_upper_bound=round(t_val - legs_present[wr], 4),
                            worst_present=wr,
                        )
                    )
                continue

            worst = max(legs_present, key=lambda r: legs_present[r])
            worst_val = legs_present[worst]
            delta = t_val - worst_val
            ties = tuple(
                r for r in self.off_targets
                if worst_val - legs_present[r] <= tie_tolerance
            )
            target_pass = t_val >= target_leg_min
            delta_pass = delta >= band
            if target_pass and delta_pass:
                verdict: DeltaVerdict = "PASS_BOTH"
            elif target_pass:
                verdict = "TARGET_ONLY"
            elif delta_pass:
                verdict = "DELTA_ONLY"
            elif abs(delta) < band:
                verdict = "INDISTINGUISHABLE"
            else:
                verdict = "OFFTARGET_PREFERRED"

            tiers = [t for t in (t_leg.quality_tiers if t_leg else []) if t]
            complete.append(
                SelectivityDelta(
                    sequence=seq,
                    target=self.target,
                    target_ipsae=round(t_val, 4),
                    off_target_ipsae={k: round(v, 4) for k, v in legs_present.items()},
                    worst_off_target=worst,
                    worst_off_target_ipsae=round(worst_val, 4),
                    delta=round(delta, 4),
                    ties_within_tolerance=ties,
                    target_leg_pass=target_pass,
                    delta_leg_pass=delta_pass,
                    verdict=verdict,
                    delta_resolvable=abs(delta) >= band,
                    quality_tier=tiers[0] if tiers else None,
                    n_draws=t_leg.n_draws if t_leg else 0,
                )
            )

        complete.sort(key=lambda r: -r.delta)
        partial.sort(key=lambda r: -r.delta_upper_bound)

        exact_worst: dict[str, int] = {}
        near_worst: dict[str, int] = {}
        for row in complete:
            exact_worst[row.worst_off_target] = exact_worst.get(row.worst_off_target, 0) + 1
            for t in row.ties_within_tolerance:
                near_worst[t] = near_worst.get(t, 0) + 1

        summary = {
            "target": self.target,
            "off_targets": list(self.off_targets),
            "statistic": stat,
            "draws_required_per_leg": self.requested_draws,
            "band": band,
            "target_leg_min": target_leg_min,
            "tie_tolerance": tie_tolerance,
            "n_complete": len(complete),
            "n_partial_upper_bounds_not_verdicts": len(partial),
            "n_excluded_scaffold_mediated": n_scaffold_excluded,
            "n_no_target_leg": n_incomplete_target,
            "n_pass_both": sum(1 for r in complete if r.verdict == "PASS_BOTH"),
            "n_indistinguishable": sum(
                1 for r in complete if r.verdict == "INDISTINGUISHABLE"
            ),
            # Counted across EVERY verdict, because a design can clear the
            # on-target floor (TARGET_ONLY) and still have an unreadable Δ.
            "n_delta_below_band": sum(1 for r in complete if not r.delta_resolvable),
            "n_offtarget_preferred": sum(
                1 for r in complete if r.verdict == "OFFTARGET_PREFERRED"
            ),
            "exact_worst_offtarget_counts": exact_worst,
            "within_tolerance_of_worst_counts": near_worst,
        }
        return complete, partial, summary

    def survivors(
        self,
        *,
        band: float,
        target_leg_min: float,
        statistic: LegStatistic | None = None,
    ) -> list[str]:
        """Sequences whose row is ``PASS_BOTH`` — target leg AND a resolvable Δ."""
        complete, _partial, _summary = self.deltas(
            band=band, target_leg_min=target_leg_min, statistic=statistic
        )
        return [r.sequence for r in complete if r.verdict == "PASS_BOTH"]

    def hold_rate(
        self,
        shortlist: Sequence[str],
        *,
        band: float,
        target_leg_min: float,
        statistic: LegStatistic | None = None,
    ) -> ReplicationReport:
        """How much of ``shortlist`` still passes in THIS panel.

        Run the shortlist through a fresh :meth:`Campaigns.fold_panel` and pass
        it here: what comes back is the fraction that held. Sequences with no
        complete row in this panel are ``untested``, never counted as lost — an
        absent measurement is not a negative one.
        """
        complete, _partial, _summary = self.deltas(
            band=band, target_leg_min=target_leg_min, statistic=statistic
        )
        by_seq = {r.sequence: r for r in complete}
        want = [_norm_seq(s) for s in shortlist]
        held = [s for s in want if s in by_seq and by_seq[s].verdict == "PASS_BOTH"]
        lost = [s for s in want if s in by_seq and by_seq[s].verdict != "PASS_BOTH"]
        untested = [s for s in want if s not in by_seq]
        return ReplicationReport(
            tested=tuple(held + lost),
            held=tuple(held),
            lost=tuple(lost),
            untested=tuple(untested),
            criterion=(
                f"peptide_ipsae({self.target}) >= {target_leg_min} AND "
                f"delta >= {band} over {list(self.off_targets)}"
            ),
        )

    def to_rows(self, *, statistic: LegStatistic | None = None) -> list[dict[str, Any]]:
        """Flat per-(design, receptor) rows for a DataFrame or a CSV."""
        stat: LegStatistic = statistic or self.statistic
        out: list[dict[str, Any]] = []
        for (seq, recep), leg in sorted(self.legs.items()):
            out.append({
                "sequence": seq,
                "receptor": recep,
                "is_target": recep == self.target,
                "peptide_ipsae": leg.value(stat),
                "peptide_ipsae_draws": list(leg.peptide_ipsae_draws),
                "n_draws": leg.n_draws,
                "range": leg.spread(),
                "iptm": None if not leg.iptm_draws else _aggregate(leg.iptm_draws, stat),
                "plddt": None if not leg.plddt_draws else _aggregate(leg.plddt_draws, stat),
                "quality_tier": next((t for t in leg.quality_tiers if t), None),
                "scaffold_mediated": leg.scaffold_mediated,
                "not_computed_draws": leg.not_computed,
                "fold_job_ids": list(leg.fold_job_ids),
            })
        return out

    def __repr__(self) -> str:  # pragma: no cover - repr only
        return (
            f"<PanelResult {self.target} vs {list(self.off_targets)} "
            f"| {len(self.sequences)} designs, {len(self.legs)} legs>"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Resource
# ─────────────────────────────────────────────────────────────────────────────


class Campaigns(Resource):
    """``client.campaigns`` — the recursive design loop.

    Example::

        panel = client.campaigns.fold_panel(
            sequences, target="ENPP3", off_targets=["ENPP1", "ENPP6"],
            replicates=2, diffusion_samples=4,
        )
        band = panel.cross_draw_band()          # measured, not assumed
        rows, partial, summary = panel.deltas(band=band, target_leg_min=0.66)
    """

    # ─── pocket features / contrastive references ───────────────────────────

    def pocket_features(
        self,
        gene: str,
        *,
        dim: int | None = None,
        model: str | None = None,
        pdb_code: str | None = None,
        mode: str | None = None,
        chains: str | None = None,
        target_residues: Sequence[int] | None = None,
    ) -> PocketFeatureMatrix:
        """Fetch a gene's featurized pocket at the width a generator declares.

        ``GET /api/pocket-features/{gene}``

        Pass ``model=`` to take the width from the model registry, or ``dim=``
        to state it. Whichever you give, the returned matrix is checked against
        it and the call FAILS on a mismatch. It never pads or slices: a 48-wide
        pocket is not a truncated 107-wide one, and converting between them
        hands the generator geometry where it expects composition.
        """
        want = dim if dim is not None else model_pocket_feature_dim(model)
        params: dict[str, Any] = {}
        if pdb_code:
            params["pdb_code"] = pdb_code
        if mode:
            params["mode"] = mode
        if chains:
            params["chains"] = chains
        if target_residues:
            params["target_residues"] = ",".join(str(int(r)) for r in target_residues)
        try:
            payload = self._transport.request(
                "GET", _POCKET_FEATURES_PATH.format(gene=gene), params=params or None
            ) or {}
        except LigandAINotFoundError as exc:
            raise _pocket_features_unreachable(gene) from exc

        # ⛔ An UNMOUNTED route does not reliably 404. A single-page-app server
        # commonly answers an unknown /api path with 200 and the app shell, so
        # "no features in the response" is the same observation as "the endpoint
        # is not there" and must produce the same actionable error — not an
        # opaque one about a missing key.
        feats = (payload or {}).get("features") if isinstance(payload, dict) else None
        if not feats:
            reason = ""
            if isinstance(payload, dict):
                reason = str(payload.get("error") or payload.get("pocket_source") or "")
            if not isinstance(payload, dict) or not reason:
                raise _pocket_features_unreachable(gene)
            raise LigandAIError(
                f"no pocket features returned for {gene!r}: {reason}"
            )
        matrix = PocketFeatureMatrix(
            rows=feats,
            gene=payload.get("gene") or gene,
            source_pdb_id=payload.get("pdb_code"),
            source_chain=(chains or "A").split(",")[0],
            pocket_source=payload.get("pocket_source"),
        )
        if want is not None:
            matrix.require_dim(want, label=f"pocket features for {gene!r}")
        return matrix

    def contrastive_references(
        self,
        target: str,
        paralogs: Sequence[str],
        *,
        model: str | None = "v6.9.21",
        dim: int | None = None,
        pdb_codes: dict[str, str] | None = None,
    ) -> ContrastiveGuidance:
        """Build a contrastive panel from real off-target receptor pockets.

        One reference per paralog, each fetched at the selected model's declared
        width. Refuses an all-zero pocket, a width mismatch, and a duplicate gene
        — all three are silent-corruption modes rather than errors downstream.
        """
        want = dim if dim is not None else (model_pocket_feature_dim(model) or V6_9_POCKET_FEATURE_DIM)
        if not paralogs:
            raise LigandAIValidationError(
                "contrastive_references needs at least one paralog / off-target gene"
            )
        pdb_codes = pdb_codes or {}
        refs: list[ContrastiveReference] = []
        for gene in paralogs:
            matrix = self.pocket_features(gene, dim=want, pdb_code=pdb_codes.get(gene))
            refs.append(ContrastiveReference(gene=gene, features=matrix))
        guidance = ContrastiveGuidance(refs, expected_dim=want)
        guidance.validate_for_model(model)
        return guidance

    # ─── panel folding ──────────────────────────────────────────────────────

    def fold_panel(
        self,
        sequences: Sequence[str],
        *,
        target: str,
        off_targets: Sequence[str],
        replicates: int = 1,
        diffusion_samples: int = 4,
        sampling_steps: int = 50,
        msa_enabled: bool | None = True,
        statistic: LegStatistic = "median",
        wait: bool = True,
        timeout: float = 7200.0,
        poll_interval: float = 15.0,
        **fold_kwargs: Any,
    ) -> PanelResult:
        """Fold every design against the target AND every off-target.

        This is the measurement the selectivity claim rests on. One batch is
        submitted per (receptor × replicate), so every design gets the same
        comparator set — a panel assembled from whatever folds happened to
        finish is the ``PARTIAL_PANEL_INFLATES_THE_WORST_CASE`` defect.

        ``replicates`` submits the SAME (design, receptor) more than once as
        INDEPENDENT draws. Production folds are unseeded, so each submission is
        a fresh roll; the local 24h dedupe is bypassed so the second submission
        is a real second measurement and not a cached echo.

        ⛔ ``replicates`` is how you measure the noise band, and the band is what
        makes a Δ readable. Running with ``replicates=1`` and then asserting a
        small Δ is the error this module exists to prevent.

        ⛔ There is no seed parameter on the fold surface, so these draws are
        independent but NOT reproducible: you cannot re-request draw #2. Record
        the returned ``fold_job_ids`` — they are the only handle on a specific
        draw.

        Cost scales as ``len(sequences) × (1 + len(off_targets)) × replicates``
        fold jobs. Check it with ``client.account`` before a large panel.
        """
        seqs = [_norm_seq(s) for s in sequences]
        if not seqs:
            raise LigandAIValidationError("fold_panel needs at least one sequence")
        seqs = list(dict.fromkeys(seqs))
        target = str(target).strip()
        offs = [str(o).strip() for o in off_targets if str(o).strip()]
        if not offs:
            raise LigandAIValidationError(
                "fold_panel needs at least one off-target. A panel of one receptor "
                "measures affinity, not selectivity — there is no Δ to compute."
            )
        if target in offs:
            raise LigandAIValidationError(
                f"{target!r} appears in both target and off_targets; the Δ against "
                f"itself is zero by construction"
            )
        replicates = int(replicates)
        if replicates < 1:
            raise LigandAIValidationError("replicates must be >= 1")

        peptides_res = getattr(self._client, "peptides", None)
        if peptides_res is None:
            raise LigandAIError("fold_panel requires a full client (client.campaigns)")

        submissions: list[tuple[str, Any]] = []
        for receptor in (target, *offs):
            for draw in range(replicates):
                job = peptides_res.fold_batch(
                    peptides=seqs,
                    target_gene=receptor,
                    diffusion_samples=diffusion_samples,
                    sampling_steps=sampling_steps,
                    msa_enabled=msa_enabled,
                    # Every replicate after the first must be a REAL new fold,
                    # not the dedupe handing back the first one's result.
                    force_resubmit=draw > 0,
                    **fold_kwargs,
                )
                submissions.append((receptor, job))

        legs: dict[tuple[str, str], PanelLeg] = {}
        job_index: dict[str, tuple[str, str]] = {}
        for receptor, job in submissions:
            for entry in job.jobs:
                jid = str(entry.get("job_id") or entry.get("jobId") or "").strip()
                idx = entry.get("peptide_index")
                raw_seq = entry.get("sequence")
                if raw_seq:
                    seq = _norm_seq(raw_seq)
                elif isinstance(idx, int) and 0 <= idx < len(seqs):
                    seq = seqs[idx]
                else:
                    continue
                leg = legs.setdefault((seq, receptor), PanelLeg(sequence=seq, receptor=receptor))
                if jid:
                    leg.fold_job_ids.append(jid)
                    job_index[jid] = (seq, receptor)

        result = PanelResult(
            target=target,
            off_targets=offs,
            legs=legs,
            requested_draws=replicates,
            statistic=statistic,
        )
        if not wait:
            return result

        for _receptor, job in submissions:
            job.wait(timeout=timeout, poll_interval=poll_interval, raise_on_failure=False)

        self._hydrate_metrics(result, job_index)
        return result

    def _hydrate_metrics(
        self,
        panel: PanelResult,
        job_index: dict[str, tuple[str, str]],
    ) -> None:
        """Fill every leg from the platform's own per-fold metric surface.

        ⛔ Reads ``peptide_ipsae`` — the binder metric — and NEVER coalesces it
        into whole-complex ``ipsae``. A NULL means the binder interface was never
        scored: that draw is INDETERMINATE and is counted, not treated as a zero.
        The same surface returns the platform's ``quality_tier`` and the
        ``scaffold_mediated`` flag, so tiers are never re-derived here from
        hardcoded numbers.
        """
        evaluator = getattr(self._client, "evaluator", None)
        if evaluator is None or not job_index:
            return
        ids = list(job_index)
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            try:
                payload = evaluator.filter_folds(chunk, criteria={}) or {}
            except LigandAIError:
                # A metrics read that fails must not be reported as a panel of
                # zeros. Leave the legs empty; deltas() will treat them as
                # incomplete, which is what they are.
                continue
            for row in payload.get("results") or []:
                jid = str(row.get("fold_job_id") or "").strip()
                key = job_index.get(jid)
                if key is None:
                    continue
                leg = panel.legs.get(key)
                if leg is None:
                    continue
                m = row.get("metrics") or {}
                pi = m.get("peptide_ipsae")
                if isinstance(pi, (int, float)):
                    leg.peptide_ipsae_draws.append(float(pi))
                else:
                    leg.not_computed += 1
                for src, dest in (("iptm", leg.iptm_draws), ("plddt_mean", leg.plddt_draws)):
                    v = m.get(src)
                    if isinstance(v, (int, float)):
                        dest.append(float(v))
                tier = row.get("quality_tier")
                if isinstance(tier, dict):
                    tier = tier.get("tier") or tier.get("name")
                leg.quality_tiers.append(tier if isinstance(tier, str) else None)
                leg.scaffold_mediated_draws.append(bool(m.get("scaffold_mediated")))

    def replicate(
        self,
        sequences: Sequence[str],
        *,
        target: str,
        off_targets: Sequence[str],
        band: float,
        target_leg_min: float,
        replicates: int = 1,
        **panel_kwargs: Any,
    ) -> tuple[PanelResult, ReplicationReport]:
        """Re-fold a shortlist at independent draws and report what held.

        The decisive step. The top of a single-draw ranking is selected partly by
        that draw's noise, so re-rolling moves it down; a design is a candidate
        only once it survives that.

        Returns the fresh panel and the hold report. Sequences the fresh panel
        could not measure come back as ``untested``, never as lost.
        """
        panel = self.fold_panel(
            sequences,
            target=target,
            off_targets=off_targets,
            replicates=replicates,
            **panel_kwargs,
        )
        return panel, panel.hold_rate(
            sequences, band=band, target_leg_min=target_leg_min
        )

    # ─── the loop ───────────────────────────────────────────────────────────

    def recursive(
        self,
        *,
        target: str,
        off_targets: Sequence[str],
        band: float,
        target_leg_min: float,
        model: str = "v6.9.21",
        num_peptides: int = 200,
        length_range: tuple[int, int] = (12, 24),
        cysteine_mode: str = "disulfide_only",
        avoidance: AvoidancePolicy | None = None,
        contrastive: ContrastiveGuidance | None = None,
        allow_unforwarded_contrastive: bool = False,
        **generate_kwargs: Any,
    ) -> RecursiveCampaign:
        """Build a :class:`RecursiveCampaign` bound to this client."""
        return RecursiveCampaign(
            campaigns=self,
            target=target,
            off_targets=list(off_targets),
            band=float(band),
            target_leg_min=float(target_leg_min),
            model=model,
            num_peptides=num_peptides,
            length_range=length_range,
            cysteine_mode=cysteine_mode,
            avoidance=avoidance or AvoidancePolicy(off_target_genes=list(off_targets)),
            contrastive=contrastive,
            allow_unforwarded_contrastive=allow_unforwarded_contrastive,
            generate_kwargs=dict(generate_kwargs),
        )


class AsyncCampaigns(AsyncResource):
    """Async counterpart of :class:`Campaigns`.

    Only the pure-analysis surface is mirrored today: :class:`PanelResult` and
    everything it computes are transport-free and usable from either client.
    Submission helpers stay on the sync resource rather than shipping an async
    façade that quietly blocks.
    """

    async def pocket_features(
        self,
        gene: str,
        *,
        dim: int | None = None,
        model: str | None = None,
        pdb_code: str | None = None,
    ) -> PocketFeatureMatrix:
        """Async :meth:`Campaigns.pocket_features` (fetch + width check only)."""
        want = dim if dim is not None else model_pocket_feature_dim(model)
        params: dict[str, Any] = {}
        if pdb_code:
            params["pdb_code"] = pdb_code
        try:
            payload = await self._transport.request(
                "GET", _POCKET_FEATURES_PATH.format(gene=gene), params=params or None
            ) or {}
        except LigandAINotFoundError as exc:
            raise _pocket_features_unreachable(gene) from exc
        feats = (payload or {}).get("features") if isinstance(payload, dict) else None
        if not feats:
            raise _pocket_features_unreachable(gene)
        matrix = PocketFeatureMatrix(
            rows=feats,
            gene=payload.get("gene") or gene,
            source_pdb_id=payload.get("pdb_code"),
            pocket_source=payload.get("pocket_source"),
        )
        if want is not None:
            matrix.require_dim(want, label=f"pocket features for {gene!r}")
        return matrix


# ─────────────────────────────────────────────────────────────────────────────
# The recursive driver
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class CampaignRound:
    """What one round produced, and what it hands to the next one."""

    index: int
    generated: list[str]
    filtered_as_hard_negative: list[str]
    panel: PanelResult | None
    complete: list[SelectivityDelta]
    partial: list[PartialDelta]
    summary: dict[str, Any]
    survivors: list[str]
    replication: ReplicationReport | None = None
    confirmed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def __repr__(self) -> str:  # pragma: no cover - repr only
        return (
            f"<CampaignRound {self.index}: {len(self.generated)} generated, "
            f"{len(self.survivors)} survived, {len(self.confirmed)} confirmed>"
        )


class RecursiveCampaign:
    """Drive the design loop for as many rounds as you want.

    Each round: generate → fold the panel → rank on Δ → replicate the shortlist
    → carry survivors forward as hard negatives' complement and, once the
    platform forwards them, as the next round's contrastive references.

    Nothing in here invents a threshold. ``band`` and ``target_leg_min`` are
    supplied by the caller, measured (:meth:`PanelResult.observed_spread`) or
    taken from the platform's tier ladder.
    """

    def __init__(
        self,
        *,
        campaigns: Campaigns,
        target: str,
        off_targets: list[str],
        band: float,
        target_leg_min: float,
        model: str = "v6.9.21",
        num_peptides: int = 200,
        length_range: tuple[int, int] = (12, 24),
        cysteine_mode: str = "disulfide_only",
        avoidance: AvoidancePolicy | None = None,
        contrastive: ContrastiveGuidance | None = None,
        allow_unforwarded_contrastive: bool = False,
        generate_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self._campaigns = campaigns
        self._client = campaigns._client
        self.target = target
        self.off_targets = list(off_targets)
        self.band = float(band)
        self.target_leg_min = float(target_leg_min)
        self.model = model
        self.num_peptides = int(num_peptides)
        self.length_range = length_range
        self.cysteine_mode = cysteine_mode
        self.avoidance = avoidance or AvoidancePolicy(off_target_genes=list(off_targets))
        self.contrastive = contrastive
        self.allow_unforwarded_contrastive = allow_unforwarded_contrastive
        self.generate_kwargs = dict(generate_kwargs or {})
        self.rounds: list[CampaignRound] = []

    # ─── state carried between rounds ───────────────────────────────────────

    @property
    def confirmed(self) -> list[str]:
        """Designs that passed a panel AND held at an independent re-fold."""
        out: list[str] = []
        for rnd in self.rounds:
            out.extend(rnd.confirmed)
        return list(dict.fromkeys(out))

    @property
    def hard_negatives(self) -> list[str]:
        """Everything measured to prefer an off-target, plus the caller's list."""
        neg = list(self.avoidance.hard_negatives)
        for rnd in self.rounds:
            neg.extend(
                r.sequence for r in rnd.complete if r.verdict == "OFFTARGET_PREFERRED"
            )
        return list(dict.fromkeys(neg))

    # ─── one round ──────────────────────────────────────────────────────────

    def run_round(
        self,
        *,
        sequences: Sequence[str] | None = None,
        replicates: int = 1,
        confirm_replicates: int = 1,
        confirm: bool = True,
        panel_kwargs: dict[str, Any] | None = None,
    ) -> CampaignRound:
        """Run one full round and append it to :attr:`rounds`.

        ``sequences`` skips generation and folds a set you already have — which
        is how the loop stays usable while contrastive generation is still
        server-blocked.
        """
        index = len(self.rounds) + 1
        notes: list[str] = []
        panel_kwargs = dict(panel_kwargs or {})

        if sequences is None:
            generated = self._generate(notes)
        else:
            generated = [_norm_seq(s) for s in sequences]

        kept, rejected = self.avoidance.filter_sequences(generated)
        if rejected:
            notes.append(
                f"{len(rejected)} design(s) removed as known hard negatives before folding"
            )
        # Carry forward what earlier rounds measured, not just what was declared.
        blocked = set(self.hard_negatives)
        kept2 = [s for s in kept if s not in blocked]
        if len(kept2) != len(kept):
            rejected += [s for s in kept if s in blocked]
            kept = kept2

        if not kept:
            rnd = CampaignRound(
                index=index, generated=generated, filtered_as_hard_negative=rejected,
                panel=None, complete=[], partial=[], summary={}, survivors=[],
                notes=notes + ["no designs left to fold after hard-negative filtering"],
            )
            self.rounds.append(rnd)
            return rnd

        panel = self._campaigns.fold_panel(
            kept,
            target=self.target,
            off_targets=self.off_targets,
            replicates=replicates,
            **panel_kwargs,
        )
        complete, partial, summary = panel.deltas(
            band=self.band, target_leg_min=self.target_leg_min
        )
        survivors = [r.sequence for r in complete if r.verdict == "PASS_BOTH"]

        spread = panel.observed_spread()
        if spread["median_range"] is not None:
            notes.append(
                f"measured draw-to-draw range (median over legs): "
                f"{spread['median_range']:.4f}; band in use: {self.band:.4f}"
            )
            if spread["median_range"] > self.band:
                notes.append(
                    "⛔ the measured spread EXCEEDS the band in use — the Δ verdicts "
                    "in this round are not resolvable at this band. Re-read "
                    "panel.observed_spread() and raise band before quoting them."
                )
        elif replicates < 2:
            notes.append(
                "band not verified this round: replicates=1 measures no spread. "
                "Run replicates>=2 to measure it on this target."
            )

        replication: ReplicationReport | None = None
        confirmed: list[str] = []
        if confirm and survivors:
            fresh, replication = self._campaigns.replicate(
                survivors,
                target=self.target,
                off_targets=self.off_targets,
                band=self.band,
                target_leg_min=self.target_leg_min,
                replicates=confirm_replicates,
                **panel_kwargs,
            )
            confirmed = list(replication.held)
            notes.append(
                f"replication: {len(replication.held)}/{len(replication.tested)} held "
                f"at an independent draw ({len(replication.untested)} untested)"
            )
            del fresh

        rnd = CampaignRound(
            index=index,
            generated=generated,
            filtered_as_hard_negative=rejected,
            panel=panel,
            complete=complete,
            partial=partial,
            summary=summary,
            survivors=survivors,
            replication=replication,
            confirmed=confirmed,
            notes=notes,
        )
        self.rounds.append(rnd)
        return rnd

    def _generate(self, notes: list[str]) -> list[str]:
        peptides = getattr(self._client, "peptides", None)
        if peptides is None:
            raise LigandAIError("RecursiveCampaign requires a full client")
        kwargs: dict[str, Any] = {
            "gene": self.target,
            "num_peptides": self.num_peptides,
            "length_range": self.length_range,
            "model": self.model,
            "cysteine_mode": self.cysteine_mode,
            **self.avoidance.generation_kwargs(),
            **self.generate_kwargs,
        }
        if self.contrastive is not None:
            kwargs["contrastive"] = self.contrastive
            kwargs["allow_unforwarded_contrastive"] = self.allow_unforwarded_contrastive
            notes.append(f"contrastive guidance: {self.contrastive.describe()}")
        else:
            notes.append(
                f"no explicit contrastive panel supplied; a {self.model} run is "
                f"steered against references the platform derives itself, and the "
                f"chosen panel is not reported back"
            )
        job = peptides.generate(**kwargs)
        result = job.wait() if hasattr(job, "wait") else job
        seqs: list[str] = []
        for p in (getattr(result, "peptides", None) or []):
            s = getattr(p, "sequence", None) or (p.get("sequence") if isinstance(p, dict) else None)
            if s:
                seqs.append(_norm_seq(s))
        return list(dict.fromkeys(seqs))

    # ─── reporting ──────────────────────────────────────────────────────────

    def report(self) -> dict[str, Any]:
        """A compact, quotable record of every round."""
        return {
            "target": self.target,
            "off_targets": list(self.off_targets),
            "model": self.model,
            "band": self.band,
            "target_leg_min": self.target_leg_min,
            "n_rounds": len(self.rounds),
            "confirmed": self.confirmed,
            "hard_negatives": self.hard_negatives,
            "rounds": [
                {
                    "index": r.index,
                    "n_generated": len(r.generated),
                    "n_filtered_hard_negative": len(r.filtered_as_hard_negative),
                    "summary": r.summary,
                    "n_survivors_single_panel": len(r.survivors),
                    "n_confirmed_at_fresh_draw": len(r.confirmed),
                    "hold_rate": None if r.replication is None else r.replication.hold_rate,
                    "notes": r.notes,
                }
                for r in self.rounds
            ],
        }

    def __repr__(self) -> str:  # pragma: no cover - repr only
        return (
            f"<RecursiveCampaign {self.target} vs {self.off_targets} "
            f"| {len(self.rounds)} round(s), {len(self.confirmed)} confirmed>"
        )
