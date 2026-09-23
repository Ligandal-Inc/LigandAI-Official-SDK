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
from typing import Any, ClassVar, Literal

from ligandai.contrastive import (
    AvoidancePolicy,
    ContrastiveGuidance,
    ContrastiveReference,
    PocketFeatureMatrix,
    V6_9_POCKET_FEATURE_DIM,
    model_pocket_feature_dim,
)
from ligandai.multitarget import (
    CROSS_TARGET_AGGREGATES,
    CoOptimization,
    CrossTargetAggregate,
    TargetSpec,
    aggregate_across_targets,
    coerce_target,
)
from ligandai.errors import (
    LigandAISurfaceNotInThisPackage,
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
    "MultiTargetDelta",
    "MultiTargetPartial",
    "ReplicationReport",
    "RecursiveCampaign",
    "CampaignRound",
    # Re-exported so one import reaches the whole co-optimization surface.
    "CoOptimization",
    "TargetSpec",
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


#: Error codes the mounted featurizer returns when it answered, looked, and
#: found nothing for THIS gene. Distinct from "the route is not there", which
#: is the same HTTP status and a completely different fix.
_POCKET_FEATURES_LOOKED_AND_FAILED = frozenset({
    "pocket_features_unavailable",
    "pocket_selection_failed",
    "pocket_config_error",
})


def _pocket_features_no_pocket(gene: str, detail: str | None = None) -> LigandAIError:
    """The featurizer ANSWERED and has no pocket for this gene.

    ⛔ Not the same fact as "the endpoint is unreachable", and the two used to
    share one error. A gene symbol resolves (BRD4); a domain construct name
    does not (BRD4ET), and telling that caller the API is unmounted sends them
    to the wrong place entirely.
    """
    return LigandAIError(
        f"no pocket features could be extracted for {gene!r}"
        + (f": {detail}" if detail else "")
        + ".\n"
        "\n"
        "This endpoint takes a gene SYMBOL. BRD4 resolves; BRD4ET does not, "
        "because it names a domain construct rather than a gene. Upload a "
        "construct with POST /api/proteins/upload-pdb and target the id it "
        "returns, or pass pdb_code= to pin the structure."
    )


def _pocket_features_unreachable(gene: str) -> LigandAIParameterNotForwarded:
    """The featurizer route is NOT THERE — nothing answered with a pocket.

    Reserved for the case where the response cannot have come from the
    featurizer at all: an unmounted API path on a single-page-app server
    commonly returns 200 with the app shell rather than a 404, so "a response
    with no features and no featurizer error code" is the observable signature
    of a missing route.

    ⛔ It is NOT raised for a gene the mounted featurizer simply cannot
    featurize — see :func:`_pocket_features_no_pocket`. Collapsing the two made
    every unresolvable gene look like a platform gap.
    """
    return LigandAIParameterNotForwarded(
        f"pocket features for {gene!r} are not reachable from this API surface. "
        f"The featurizer endpoint exists and is what the generation path calls "
        f"internally, but this server does not mount it on the public API, so a "
        f"client cannot build its own contrastive reference panel here.\n"
        f"\n"
        f"What works on such a server: run a v6.9-line model and let the platform "
        f"derive its own real paralog references — it refuses the run rather than "
        f"generating against a fabricated one. What does not work: choosing the "
        f"panel, or reading back which paralogs were used.",
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
    peptide_ipsae_statuses: list[str] = field(default_factory=list)
    """The platform's OWN reason per NULL draw, from ``peptide_ipsae_status``.

    ⛔ Without this, "the binder metric was never computed" and "this design has
    no measurable interface" are the same empty panel to the caller. The platform
    distinguishes them and says which; dropping that turned a known unmeasured
    run into a silent zero-survivor result. [bd-dre-if562]
    """

    @property
    def not_computed_reasons(self) -> tuple[str, ...]:
        """Distinct reasons this leg has no ``peptide_ipsae``, platform-stated."""
        return tuple(dict.fromkeys(s for s in self.peptide_ipsae_statuses if s))

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
class MultiTargetDelta:
    """A complete-panel row for a CO-OPTIMIZATION over several positive targets.

    ⛔ THE AGGREGATION RULE TRAVELS WITH THE NUMBER. There is no field on this
    row that holds "the target score" without ``aggregate_rule`` next to it
    saying how the positive legs were combined, and ``per_target_ipsae`` /
    ``per_target_pass`` are always populated so a ``mean``-rule pass carried by
    one strong leg is visible rather than inferred.

    Δ = ``aggregate_ipsae`` − ipSAE(worst off-target). Ranked on Δ, never on any
    single positive leg.
    """

    sequence: str
    targets: tuple[str, ...]
    per_target_ipsae: dict[str, float]
    aggregate_rule: CrossTargetAggregate
    aggregate_ipsae: float
    limiting_target: str
    """The weakest positive leg — the argmin, present under BOTH rules.

    Under ``worst_case`` it is the leg that SET ``aggregate_ipsae``. Under
    ``mean`` it is the leg the average hid, which is the one to act on.
    """
    per_target_pass: dict[str, bool]
    off_target_ipsae: dict[str, float]
    worst_off_target: str
    worst_off_target_ipsae: float
    delta: float
    ties_within_tolerance: tuple[str, ...]
    target_leg_pass: bool
    delta_leg_pass: bool
    verdict: DeltaVerdict
    frames: dict[str, str]
    """Residue-numbering frame per positive target. A residue number without
    its frame is not a location, and orthologs do not share one."""
    delta_resolvable: bool = True
    quality_tier: str | None = None
    n_draws: int = 1

    @property
    def resolvable(self) -> bool:
        """False when |Δ| sits inside the measured band. Read this, not the verdict."""
        return self.delta_resolvable

    @property
    def all_targets_pass(self) -> bool:
        """Every positive leg cleared the floor on its own.

        Identical to ``target_leg_pass`` under ``worst_case``; under ``mean``
        it is the stricter statement the average does not make.
        """
        return all(self.per_target_pass.values())

    def __repr__(self) -> str:  # pragma: no cover - repr only
        return (
            f"<MultiTargetDelta {self.sequence[:12]}… {self.aggregate_rule}="
            f"{self.aggregate_ipsae:.4f} (limited by {self.limiting_target}) "
            f"Δ={self.delta:+.4f} vs {self.worst_off_target} [{self.verdict}]>"
        )


@dataclass(frozen=True)
class MultiTargetPartial:
    """An INCOMPLETE multi-target row. Never a verdict.

    ⛔ UNDER ``worst_case`` THIS IS AN UPPER BOUND; UNDER ``mean`` IT IS NOT A
    BOUND AT ALL, and that difference is the whole reason ``bound_kind`` exists.

    * ``worst_case`` — a min over a SUBSET of positives can only FALL when the
      missing legs arrive, and a max over a subset of off-targets can only RISE.
      Both push Δ down, so ``delta_upper_bound`` is a real upper bound.
    * ``mean`` — a mean over a subset can move either way when the missing legs
      arrive. There is no bound, so ``delta_upper_bound`` is ``None`` and
      ``bound_kind`` is ``"indeterminate"``. Reporting the partial mean as a
      bound is exactly the ``PARTIAL_PANEL_INFLATES_THE_WORST_CASE`` defect
      wearing a different rule.
    """

    sequence: str
    targets_present: tuple[str, ...]
    targets_missing: tuple[str, ...]
    comparators_present: tuple[str, ...]
    comparators_missing: tuple[str, ...]
    aggregate_rule: CrossTargetAggregate
    per_target_ipsae: dict[str, float]
    bound_kind: Literal["upper_bound", "indeterminate"]
    delta_upper_bound: float | None
    worst_present: str | None


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
    """The folded panel: every design against every positive target and every
    off-target.

    Build one with :meth:`Campaigns.fold_panel`. Everything downstream —
    Δ selection, tier counts, replication — reads from here.

    ONE or MANY positive targets. A single-positive panel behaves exactly as it
    always has and :meth:`deltas` returns :class:`SelectivityDelta` rows. With
    several positive targets it is a CO-OPTIMIZATION panel: the positive legs
    are combined by :attr:`aggregate` and :meth:`deltas` returns
    :class:`MultiTargetDelta` rows, which carry the rule, the per-target scores
    and each target's numbering frame.
    """

    def __init__(
        self,
        *,
        target: str | None = None,
        targets: Sequence[str] | None = None,
        off_targets: Sequence[str],
        legs: dict[tuple[str, str], PanelLeg],
        requested_draws: int,
        statistic: LegStatistic = "median",
        aggregate: CrossTargetAggregate | None = None,
        specs: dict[str, TargetSpec] | None = None,
    ) -> None:
        # ⛔ Exactly one of target= / targets=. Accepting both would make the
        # panel's arity ambiguous, and the arity decides which selection rule
        # runs — the one thing that must never be guessed here.
        if (target is None) == (targets is None):
            raise LigandAIValidationError(
                "PanelResult needs exactly one of target= (one positive target) "
                "or targets= (a co-optimization over several)."
            )
        keys = [str(target)] if targets is None else [str(t) for t in targets]
        if not keys:
            raise LigandAIValidationError("PanelResult needs at least one positive target")
        if len(set(keys)) != len(keys):
            dupes = sorted({k for k in keys if keys.count(k) > 1})
            raise LigandAIValidationError(
                f"duplicate positive target key(s) {dupes}. Legs are keyed by "
                f"(sequence, receptor), so a repeated key means two targets share "
                f"one leg and one measurement is reported as two."
            )
        # ⛔ REQUIRED once there is more than one positive target, and
        # deliberately WITHOUT a default. A silent aggregation rule is the
        # defect class this module exists to remove: "it binds both" and "it
        # binds one of them well" are different claims and the same number.
        if len(keys) > 1 and aggregate is None:
            raise LigandAIValidationError(
                "a multi-target panel needs aggregate= stated explicitly; there is "
                "no default.\n"
                "  aggregate='worst_case' — min across the positive targets. What "
                "'binds BOTH' means, and it names the limiting target.\n"
                "  aggregate='mean'       — average across them. Weaker: one strong "
                "leg can mask a failed one (every row still carries per_target_pass)."
            )
        if aggregate is not None and aggregate not in CROSS_TARGET_AGGREGATES:
            raise LigandAIValidationError(
                f"aggregate must be one of {list(CROSS_TARGET_AGGREGATES)}; got "
                f"{aggregate!r}"
            )
        self.targets = tuple(keys)
        self.off_targets = tuple(off_targets)
        self.legs = legs
        self.requested_draws = int(requested_draws)
        self.statistic: LegStatistic = statistic
        #: The cross-target rule this panel was built for. None only when there
        #: is a single positive target, where there is nothing to combine.
        self.aggregate: CrossTargetAggregate | None = aggregate
        #: Per-key :class:`~ligandai.multitarget.TargetSpec`, when the panel was
        #: built from specs. Source of the numbering frames reported on rows.
        self.specs: dict[str, TargetSpec] = dict(specs or {})
        #: ``(receptor, error)`` for every leg whose fold wait did not complete.
        #: Non-empty means this panel is missing measurements it asked for — the
        #: affected rows come back incomplete, never as a zero.
        self.wait_errors: list[tuple[str, Exception]] = []
        #: Receptors whose batch handle carried no per-sub-job entries, so no leg
        #: could be built from it. Today the only way that happens is the local
        #: 24h dedupe returning a cached batch id; see :meth:`Campaigns.fold_panel`.
        self.unmapped_submissions: list[str] = []

    # ─── introspection ──────────────────────────────────────────────────────

    @property
    def target(self) -> str:
        """The FIRST positive target.

        ⛔ On a multi-target panel this is one leg of several and is NOT the
        thing selection runs on — read :attr:`targets`, and read a row's
        ``aggregate_rule`` / ``per_target_ipsae``. Kept so single-target callers
        (every caller before co-optimization existed) are unchanged.
        """
        return self.targets[0]

    @property
    def is_multi_target(self) -> bool:
        return len(self.targets) > 1

    @property
    def frames(self) -> dict[str, str]:
        """Residue-numbering frame per positive target, from its spec.

        A target built from a bare gene symbol reports the canonical frame for
        that symbol; the point is that the frame is always STATED rather than
        assumed to be shared. Orthologs do not share one.
        """
        out: dict[str, str] = {}
        for key in self.targets:
            spec = self.specs.get(key)
            out[key] = spec.frame if spec is not None else f"{key} (frame unstated)"
        return out

    @property
    def sequences(self) -> list[str]:
        return sorted({seq for (seq, _r) in self.legs})

    @property
    def receptors(self) -> tuple[str, ...]:
        return (*self.targets, *self.off_targets)

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
        aggregate: CrossTargetAggregate | None = None,
    ) -> tuple[
        list[SelectivityDelta] | list[MultiTargetDelta],
        list[PartialDelta] | list[MultiTargetPartial],
        dict[str, Any],
    ]:
        """Rank designs on Δ = positive-side score − ipSAE(worst off-target).

        With ONE positive target the positive-side score is that target's leg
        and the rows are :class:`SelectivityDelta` — unchanged behaviour. With
        SEVERAL it is the legs combined by ``aggregate`` and the rows are
        :class:`MultiTargetDelta`, which carry the rule, every per-target score
        and each target's numbering frame.

        ⛔ ONE ORDERING, ONE IMPLEMENTATION. Both arities run through the same
        loop below and the same ``-delta`` sort; the arity only decides which
        dataclass is materialised at the end. Two implementations of one
        ordering is how a ranking fix lands in one of them and the other keeps
        selecting.

        Parameters
        ----------
        band
            The resolvable band, in ipSAE units. A |Δ| below this is
            INDISTINGUISHABLE — the design does not "slightly prefer" anything.
            REQUIRED and deliberately without a default: measure it from your own
            replicates with :meth:`observed_spread` / :meth:`cross_draw_band`.
        target_leg_min
            The on-target floor a design must clear. Take it from the platform's
            tier ladder rather than inventing one — the server returns
            ``quality_tier`` on every fold, and ``client.evaluator.filter_folds``
            enforces the named criteria server-side.

            ⛔ Under ``aggregate="worst_case"`` this floor is applied to EVERY
            positive leg. Under ``aggregate="mean"`` it is applied to the MEAN,
            which a failing leg can pass; ``per_target_pass`` on the row says so
            and ``MultiTargetDelta.all_targets_pass`` is the stricter statement.
        statistic
            Per-leg aggregation over DRAWS (a different axis from ``aggregate``,
            which combines TARGETS). Defaults to the panel's own.
        tie_tolerance
            Off-targets within this of the worst are also reported, so a near-tie
            is never presented as an exact maximum.
        exclude_scaffold_mediated
            Drop designs whose interface was carried by an expression cassette in
            any draw — on ANY leg, positive or off-target. Counted in the summary,
            never silently discarded.
        aggregate
            Override the panel's cross-target rule for this call. Required only
            if the panel somehow carries none and has several positive targets.

        Returns
        -------
        ``(complete, partial, summary)`` — ``complete`` sorted by Δ descending,
        ``partial`` bounds/indeterminates only, ``summary`` with the counts and
        the rule that produced them.
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

        multi = self.is_multi_target
        rule: CrossTargetAggregate | None = aggregate or self.aggregate
        if aggregate is not None and aggregate not in CROSS_TARGET_AGGREGATES:
            raise LigandAIValidationError(
                f"aggregate must be one of {list(CROSS_TARGET_AGGREGATES)}; got "
                f"{aggregate!r}"
            )
        if multi and rule is None:
            raise LigandAIValidationError(
                "this panel has several positive targets and no cross-target rule. "
                "Pass aggregate='worst_case' (min across targets — what 'binds "
                "BOTH' means) or aggregate='mean' (average; one strong leg can "
                "mask a failed one). There is no default."
            )
        # A single-positive panel has nothing to combine. Naming the rule
        # "single" rather than reusing "worst_case" keeps the summary honest:
        # min over one value is not a worst case over anything.
        effective_rule: str = rule if multi else "single"

        complete_rows: list[dict[str, Any]] = []
        partial_rows: list[dict[str, Any]] = []
        n_scaffold_excluded = 0
        n_no_positive_leg = 0
        frames = self.frames

        for seq in self.sequences:
            # ─── positive side ───────────────────────────────────────────────
            pos_present: dict[str, float] = {}
            pos_missing: list[str] = []
            scaffolded = False
            primary_leg: PanelLeg | None = None
            for key in self.targets:
                leg = self.legs.get((seq, key))
                val = None if leg is None else leg.value(stat)
                if leg is None or val is None or leg.n_draws < self.requested_draws:
                    pos_missing.append(key)
                    continue
                pos_present[key] = val
                scaffolded = scaffolded or leg.scaffold_mediated
                if primary_leg is None:
                    primary_leg = leg

            if not pos_present:
                # ⛔ Not a zero and not a negative: nothing was measured on the
                # positive side, so there is no row of any kind to report.
                n_no_positive_leg += 1
                continue

            # ─── off-target side ─────────────────────────────────────────────
            off_present: dict[str, float] = {}
            off_missing: list[str] = []
            for off in self.off_targets:
                leg = self.legs.get((seq, off))
                val = None if leg is None else leg.value(stat)
                if leg is None or val is None or leg.n_draws < self.requested_draws:
                    off_missing.append(off)
                    continue
                off_present[off] = val
                scaffolded = scaffolded or leg.scaffold_mediated

            if exclude_scaffold_mediated and scaffolded:
                n_scaffold_excluded += 1
                continue

            # ─── complete or not ─────────────────────────────────────────────
            if pos_missing or off_missing:
                worst_present = (
                    max(off_present, key=lambda r: off_present[r]) if off_present else None
                )
                # ⛔ WHETHER THIS IS EVEN A BOUND DEPENDS ON THE RULE.
                #   worst_case: min over a subset of positives can only FALL and
                #     max over a subset of off-targets can only RISE — both push
                #     Δ down, so the number is a genuine UPPER bound.
                #   mean:       a mean over a subset moves either way when the
                #     missing legs arrive. There is no bound. Emitting one would
                #     be PARTIAL_PANEL_INFLATES_THE_WORST_CASE under a new name.
                bound_kind: str = "upper_bound"
                bound: float | None = None
                if pos_missing and effective_rule == "mean":
                    bound_kind = "indeterminate"
                elif worst_present is not None:
                    agg_present, _lim = (
                        aggregate_across_targets(pos_present, rule)  # type: ignore[arg-type]
                        if multi
                        else (next(iter(pos_present.values())), None)
                    )
                    bound = round(agg_present - off_present[worst_present], 4)
                partial_rows.append({
                    "sequence": seq,
                    "per_target_ipsae": {k: round(v, 4) for k, v in pos_present.items()},
                    "targets_present": tuple(sorted(pos_present)),
                    "targets_missing": tuple(sorted(set(pos_missing))),
                    "comparators_present": tuple(sorted(off_present)),
                    "comparators_missing": tuple(sorted(set(off_missing))),
                    "bound_kind": bound_kind,
                    "delta_upper_bound": bound,
                    "worst_present": worst_present,
                })
                continue

            if multi:
                agg_val, limiting = aggregate_across_targets(
                    pos_present, rule  # type: ignore[arg-type]
                )
                per_target_pass = {k: v >= target_leg_min for k, v in pos_present.items()}
                target_pass = (
                    all(per_target_pass.values())
                    if rule == "worst_case"
                    else agg_val >= target_leg_min
                )
            else:
                agg_val = next(iter(pos_present.values()))
                limiting = next(iter(pos_present))
                per_target_pass = {limiting: agg_val >= target_leg_min}
                target_pass = agg_val >= target_leg_min

            worst = max(off_present, key=lambda r: off_present[r])
            worst_val = off_present[worst]
            delta = agg_val - worst_val
            ties = tuple(
                r for r in self.off_targets
                if worst_val - off_present[r] <= tie_tolerance
            )
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

            tiers = [t for t in (primary_leg.quality_tiers if primary_leg else []) if t]
            complete_rows.append({
                "sequence": seq,
                "per_target_ipsae": {k: round(v, 4) for k, v in pos_present.items()},
                "aggregate_ipsae": round(agg_val, 4),
                "limiting_target": limiting,
                "per_target_pass": per_target_pass,
                "off_target_ipsae": {k: round(v, 4) for k, v in off_present.items()},
                "worst_off_target": worst,
                "worst_off_target_ipsae": round(worst_val, 4),
                "delta": round(delta, 4),
                "ties_within_tolerance": ties,
                "target_leg_pass": target_pass,
                "delta_leg_pass": delta_pass,
                "verdict": verdict,
                "delta_resolvable": abs(delta) >= band,
                "quality_tier": tiers[0] if tiers else None,
                "n_draws": primary_leg.n_draws if primary_leg else 0,
            })

        # ⛔ ONE SORT for both arities. Ranked on Δ, never on a positive leg.
        complete_rows.sort(key=lambda r: -r["delta"])
        partial_rows.sort(
            key=lambda r: (
                r["delta_upper_bound"] is None,
                -(r["delta_upper_bound"] if r["delta_upper_bound"] is not None else 0.0),
            )
        )

        complete: list[Any]
        partial: list[Any]
        if multi:
            complete = [
                MultiTargetDelta(
                    sequence=r["sequence"],
                    targets=self.targets,
                    per_target_ipsae=r["per_target_ipsae"],
                    aggregate_rule=rule,  # type: ignore[arg-type]
                    aggregate_ipsae=r["aggregate_ipsae"],
                    limiting_target=r["limiting_target"],
                    per_target_pass=r["per_target_pass"],
                    off_target_ipsae=r["off_target_ipsae"],
                    worst_off_target=r["worst_off_target"],
                    worst_off_target_ipsae=r["worst_off_target_ipsae"],
                    delta=r["delta"],
                    ties_within_tolerance=r["ties_within_tolerance"],
                    target_leg_pass=r["target_leg_pass"],
                    delta_leg_pass=r["delta_leg_pass"],
                    verdict=r["verdict"],
                    frames=frames,
                    delta_resolvable=r["delta_resolvable"],
                    quality_tier=r["quality_tier"],
                    n_draws=r["n_draws"],
                )
                for r in complete_rows
            ]
            partial = [
                MultiTargetPartial(
                    sequence=r["sequence"],
                    targets_present=r["targets_present"],
                    targets_missing=r["targets_missing"],
                    comparators_present=r["comparators_present"],
                    comparators_missing=r["comparators_missing"],
                    aggregate_rule=rule,  # type: ignore[arg-type]
                    per_target_ipsae=r["per_target_ipsae"],
                    bound_kind=r["bound_kind"],  # type: ignore[arg-type]
                    delta_upper_bound=r["delta_upper_bound"],
                    worst_present=r["worst_present"],
                )
                for r in partial_rows
            ]
        else:
            only = self.targets[0]
            complete = [
                SelectivityDelta(
                    sequence=r["sequence"],
                    target=only,
                    target_ipsae=r["aggregate_ipsae"],
                    off_target_ipsae=r["off_target_ipsae"],
                    worst_off_target=r["worst_off_target"],
                    worst_off_target_ipsae=r["worst_off_target_ipsae"],
                    delta=r["delta"],
                    ties_within_tolerance=r["ties_within_tolerance"],
                    target_leg_pass=r["target_leg_pass"],
                    delta_leg_pass=r["delta_leg_pass"],
                    verdict=r["verdict"],
                    delta_resolvable=r["delta_resolvable"],
                    quality_tier=r["quality_tier"],
                    n_draws=r["n_draws"],
                )
                for r in complete_rows
            ]
            partial = [
                PartialDelta(
                    sequence=r["sequence"],
                    target=only,
                    target_ipsae=next(iter(r["per_target_ipsae"].values())),
                    comparators_present=r["comparators_present"],
                    comparators_missing=r["comparators_missing"],
                    delta_upper_bound=(
                        r["delta_upper_bound"] if r["delta_upper_bound"] is not None else 0.0
                    ),
                    worst_present=r["worst_present"] or "",
                )
                for r in partial_rows
                # A single-positive panel with no comparator measured has no
                # bound to report, exactly as before.
                if r["worst_present"] is not None
            ]

        exact_worst: dict[str, int] = {}
        near_worst: dict[str, int] = {}
        limiting_counts: dict[str, int] = {}
        for row in complete_rows:
            exact_worst[row["worst_off_target"]] = exact_worst.get(row["worst_off_target"], 0) + 1
            for t in row["ties_within_tolerance"]:
                near_worst[t] = near_worst.get(t, 0) + 1
            limiting_counts[row["limiting_target"]] = (
                limiting_counts.get(row["limiting_target"], 0) + 1
            )

        summary = {
            # Both spellings. `target` is what every existing caller reads;
            # `targets` is the truth once there is more than one, and on a
            # single-positive panel the two agree.
            "target": self.targets[0],
            "targets": list(self.targets),
            "off_targets": list(self.off_targets),
            # ⛔ THE RULE IS ALWAYS PRESENT next to the numbers it produced.
            "positive_aggregate": effective_rule,
            "target_frames": frames,
            "statistic": stat,
            "draws_required_per_leg": self.requested_draws,
            "band": band,
            "target_leg_min": target_leg_min,
            "target_leg_min_applied_to": (
                "every positive target" if effective_rule in ("worst_case", "single")
                else "the mean across positive targets"
            ),
            "tie_tolerance": tie_tolerance,
            "n_complete": len(complete),
            "n_partial_upper_bounds_not_verdicts": len(partial),
            "n_partial_indeterminate": sum(
                1 for r in partial_rows if r["bound_kind"] == "indeterminate"
            ),
            "n_excluded_scaffold_mediated": n_scaffold_excluded,
            "n_no_target_leg": n_no_positive_leg,
            # ⛔ Reading this is the difference between "nothing passed" and
            # "nothing was scored". Keyed by the platform's own status string.
            "peptide_ipsae_not_computed_reasons": {
                why: sum(
                    1 for leg in self.legs.values()
                    if why in leg.not_computed_reasons
                )
                for why in dict.fromkeys(
                    w for leg in self.legs.values() for w in leg.not_computed_reasons
                )
            },
            "n_pass_both": sum(1 for r in complete_rows if r["verdict"] == "PASS_BOTH"),
            "n_indistinguishable": sum(
                1 for r in complete_rows if r["verdict"] == "INDISTINGUISHABLE"
            ),
            # Counted across EVERY verdict, because a design can clear the
            # on-target floor (TARGET_ONLY) and still have an unreadable Δ.
            "n_delta_below_band": sum(
                1 for r in complete_rows if not r["delta_resolvable"]
            ),
            "n_offtarget_preferred": sum(
                1 for r in complete_rows if r["verdict"] == "OFFTARGET_PREFERRED"
            ),
            "exact_worst_offtarget_counts": exact_worst,
            "within_tolerance_of_worst_counts": near_worst,
            # Which positive target limits each design. On a mean-aggregated
            # panel this is the half the average hides.
            "limiting_target_counts": limiting_counts,
            "n_all_targets_pass": sum(
                1 for r in complete_rows if all(r["per_target_pass"].values())
            ),
        }
        return complete, partial, summary

    def survivors(
        self,
        *,
        band: float,
        target_leg_min: float,
        statistic: LegStatistic | None = None,
        aggregate: CrossTargetAggregate | None = None,
        require_all_targets: bool = True,
    ) -> list[str]:
        """Sequences whose row is ``PASS_BOTH`` — positive side AND a resolvable Δ.

        ``require_all_targets`` (multi-target panels only) additionally demands
        that EVERY positive leg cleared ``target_leg_min`` on its own. It is the
        default and it matters under ``aggregate="mean"``, where the average can
        pass while one required target does not: a "co-optimized" survivor that
        misses one of its targets is not a survivor of the thing that was asked.
        """
        complete, _partial, _summary = self.deltas(
            band=band, target_leg_min=target_leg_min, statistic=statistic,
            aggregate=aggregate,
        )
        out: list[str] = []
        for r in complete:
            if r.verdict != "PASS_BOTH":
                continue
            if require_all_targets and not getattr(r, "all_targets_pass", True):
                continue
            out.append(r.sequence)
        return out

    def hold_rate(
        self,
        shortlist: Sequence[str],
        *,
        band: float,
        target_leg_min: float,
        statistic: LegStatistic | None = None,
        aggregate: CrossTargetAggregate | None = None,
        require_all_targets: bool = True,
    ) -> ReplicationReport:
        """How much of ``shortlist`` still passes in THIS panel.

        Run the shortlist through a fresh :meth:`Campaigns.fold_panel` and pass
        it here: what comes back is the fraction that held. Sequences with no
        complete row in this panel are ``untested``, never counted as lost — an
        absent measurement is not a negative one.
        """
        complete, _partial, summary = self.deltas(
            band=band, target_leg_min=target_leg_min, statistic=statistic,
            aggregate=aggregate,
        )
        by_seq = {r.sequence: r for r in complete}

        def _passes(row: Any) -> bool:
            if row.verdict != "PASS_BOTH":
                return False
            if require_all_targets and not getattr(row, "all_targets_pass", True):
                return False
            return True

        want = [_norm_seq(s) for s in shortlist]
        held = [s for s in want if s in by_seq and _passes(by_seq[s])]
        lost = [s for s in want if s in by_seq and not _passes(by_seq[s])]
        untested = [s for s in want if s not in by_seq]
        rule = summary.get("positive_aggregate")
        if self.is_multi_target:
            positive = (
                f"peptide_ipsae({rule} over {list(self.targets)}) >= {target_leg_min}"
                + (" AND every target >= it" if require_all_targets else "")
            )
        else:
            positive = f"peptide_ipsae({self.targets[0]}) >= {target_leg_min}"
        return ReplicationReport(
            tested=tuple(held + lost),
            held=tuple(held),
            lost=tuple(lost),
            untested=tuple(untested),
            criterion=(
                f"{positive} AND delta >= {band} over {list(self.off_targets)}"
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
                "is_target": recep in self.targets,
                # Which positive target (or None for an off-target leg). On a
                # co-optimization panel `is_target` alone cannot say WHICH.
                "positive_target": recep if recep in self.targets else None,
                "frame": self.frames.get(recep),
                "peptide_ipsae": leg.value(stat),
                "peptide_ipsae_draws": list(leg.peptide_ipsae_draws),
                "n_draws": leg.n_draws,
                "range": leg.spread(),
                "iptm": None if not leg.iptm_draws else _aggregate(leg.iptm_draws, stat),
                "plddt": None if not leg.plddt_draws else _aggregate(leg.plddt_draws, stat),
                "quality_tier": next((t for t in leg.quality_tiers if t), None),
                "scaffold_mediated": leg.scaffold_mediated,
                "not_computed_draws": leg.not_computed,
                "peptide_ipsae_status": list(leg.not_computed_reasons),
                "fold_job_ids": list(leg.fold_job_ids),
            })
        return out

    def __repr__(self) -> str:  # pragma: no cover - repr only
        pos = (
            f"{self.targets[0]}" if not self.is_multi_target
            else f"{list(self.targets)}[{self.aggregate}]"
        )
        return (
            f"<PanelResult {pos} vs {list(self.off_targets)} "
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

    # ⛔ [bd-dre-rp11j.9] THE PLATFORM CAMPAIGN API IS NOT IN THIS PACKAGE.
    #
    # The server exposes a campaign/maturation surface — POST /api/campaign/start,
    # POST /api/campaign/mature, GET /api/campaign, GET /{id}/status,
    # /{id}/maturation, /{id}/leaderboard, /{id}/designs/{id}/structure,
    # POST /{id}/stop, GET /{id}/stream. The published `ligandai` package ships
    # NO client for any of it.
    #
    # This namespace is an unrelated local panel-folding helper that happens to
    # answer to the same word. Without this guard, `client.campaigns.mature(...)`
    # raises a bare AttributeError that reads like a typo, and the user cannot
    # tell "this package cannot do campaigns" from "I called it wrong" — the
    # epic's defect class, at the namespace level.
    _PLATFORM_CAMPAIGN_SURFACE: ClassVar[dict[str, str]] = {
        "start": "POST /api/campaign/start",
        "mature": "POST /api/campaign/mature",
        "maturation": "GET /api/campaign/{campaign_id}/maturation",
        "list": "GET /api/campaign",
        "status": "GET /api/campaign/{campaign_id}/status",
        "leaderboard": "GET /api/campaign/{campaign_id}/leaderboard",
        "structure": "GET /api/campaign/{campaign_id}/designs/{design_id}/structure",
        "stop": "POST /api/campaign/{campaign_id}/stop",
        "stream": "GET /api/campaign/{campaign_id}/stream",
        "designs": "GET /api/campaign/{campaign_id}/leaderboard",
    }

    def __getattr__(self, name: str) -> Any:
        route = self._PLATFORM_CAMPAIGN_SURFACE.get(name)
        if route is None:
            raise AttributeError(
                f"{type(self).__name__!r} object has no attribute {name!r}"
            )
        raise LigandAISurfaceNotInThisPackage(
            f"client.campaigns.{name}() does not exist in the `ligandai` package, "
            f"but the platform endpoint it maps to DOES exist.\n"
            f"  you reached for : client.campaigns.{name}\n"
            f"  server endpoint : {route}\n"
            f"  the situation   : `client.campaigns` here is a LOCAL panel-folding "
            f"helper (pocket_features, contrastive_references, fold_panel, "
            f"replicate, recursive). It is NOT a client for the platform "
            f"campaign/maturation API — the two only share a name.\n"
            f"  this is not a typo on your part, and it is not a transient "
            f"failure: no version of this package has ever carried that surface.\n"
            f"  what to do      : drive the endpoint directly over the SDK's own "
            f"transport, which carries your auth, retries and error mapping:\n"
            f"      client.transport.request(\"POST\", \"/api/campaign/start\", json={{...}})\n"
            f"  tracking        : bd-dre-rp11j.9",
            surface=name,
            namespace="client.campaigns",
            server_routes=tuple(sorted(set(self._PLATFORM_CAMPAIGN_SURFACE.values()))),
        )


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
        to state it. Either way the width is REQUESTED from the featurizer and
        the returned matrix is then checked against it, so a mismatch is an
        error on both sides. Nothing pads or slices anywhere on this path: a
        48-wide pocket is not a truncated 107-wide one, and converting between
        them hands the generator geometry where it expects composition.

        ⛔ The width is a SERVER-SIDE resolution, and that is the whole point.
        It used to be a client-side assertion only — the request went out
        without it, the endpoint answered with its native 48, and every v6.9
        panel failed the check it could never have passed. The featurizer
        resolves a width from its cached per-width tensors, from provably
        related blocks, or by recomputing from the stored structure, and
        refuses widths it cannot build.

        Widths other than 48 are entitled: the geometric / all-atom blocks the
        v6.9 line consumes are Ligandal-internal, so a caller without that
        entitlement gets a 403 naming the alternative (run a v6.9-line model
        and let the platform derive its own real panel) rather than a reshaped
        matrix.
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
        # ⛔ ASK for the width. `want` is None only when the caller named neither
        # a dim nor a known model, and then the featurizer's native width is the
        # right answer; sending dim= in that case would pin a width nobody chose.
        if want is not None:
            params["dim"] = int(want)
        try:
            payload = self._transport.request(
                "GET", _POCKET_FEATURES_PATH.format(gene=gene), params=params or None
            ) or {}
        except LigandAINotFoundError as exc:
            # ⛔ TWO DIFFERENT FACTS, ONE STATUS CODE. The mounted featurizer
            # returns 404 + a named error code when it looked and this gene has
            # no extractable pocket; an unmounted route returns 404 with nothing
            # of the sort. Reporting the first as "the API does not carry this
            # surface" sent callers to the platform instead of to their gene
            # name, which is how "BRD4ET" read as a missing endpoint.
            body = getattr(exc, "response", None) or {}
            code = str((body or {}).get("error") or "").lower()
            if code in _POCKET_FEATURES_LOOKED_AND_FAILED:
                raise _pocket_features_no_pocket(
                    gene, str((body or {}).get("message") or "") or None
                ) from exc
            raise _pocket_features_unreachable(gene) from exc

        # ⛔ An UNMOUNTED route does not reliably 404. A single-page-app server
        # commonly answers an unknown /api path with 200 and the app shell, so
        # "no features and no featurizer error code in the response" is the same
        # observation as "the endpoint is not there" and must produce the same
        # actionable error — not an opaque one about a missing key.
        feats = (payload or {}).get("features") if isinstance(payload, dict) else None
        if not feats:
            reason = ""
            if isinstance(payload, dict):
                reason = str(payload.get("error") or payload.get("pocket_source") or "")
            if not isinstance(payload, dict) or not reason:
                raise _pocket_features_unreachable(gene)
            raise _pocket_features_no_pocket(gene, reason)
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
        target: str | TargetSpec | None = None,
        paralogs: Sequence[str] | None = None,
        *,
        targets: Sequence[TargetSpec | str] | CoOptimization | None = None,
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
        # ─── multi-positive-target form ─────────────────────────────────────
        # `targets=` names EVERY positive target of a co-optimization. The
        # reference panel is shared (it is the set of receptors to AVOID), and
        # the role check below is the one that matters: a positive target inside
        # the reference panel steers designs away from a receptor they are being
        # co-optimized FOR, because cfg_reference='paralog_mean' guides by
        # target − mean(references). That request is well-formed and inverted.
        if targets is not None and target is not None:
            raise LigandAIValidationError(
                "pass either target= (one positive target) or targets= (a "
                "co-optimization over several), not both."
            )
        positives: list[TargetSpec]
        if targets is not None:
            co = (
                targets if isinstance(targets, CoOptimization)
                # aggregate is irrelevant to building a reference panel, but
                # CoOptimization is where the key-collision and frame checks
                # live, so the panel is built through it rather than beside it.
                else CoOptimization(list(targets), aggregate="worst_case")
            )
            positives = list(co.targets)
        elif target is not None:
            positives = [coerce_target(target)]
        else:
            raise LigandAIValidationError(
                "contrastive_references needs target= or targets= — it names the "
                "receptor(s) the designs are FOR; the panel is what they are "
                "steered away from, and the two must be distinguishable."
            )
        if not paralogs:
            raise LigandAIValidationError(
                "contrastive_references needs at least one paralog / off-target gene"
            )
        if targets is not None:
            CoOptimization(positives, aggregate="worst_case").check_reference_roles(
                list(paralogs)
            )
            # The single-target clash guard below reads `target`; give it the
            # first positive so one code path validates both forms.
            target = positives[0].gene
        # ⛔ `target` was DECLARED AND NEVER READ — proven by AST: it appeared
        # in the signature and in prose, and nowhere in the body. Its sibling
        # `fold_panel` carries exactly the guard that was missing here, so the
        # two halves of one selectivity workflow disagreed about whether the
        # target may appear among its own off-targets.
        #
        # The consequence was not cosmetic. `cfg_reference="paralog_mean"`
        # averages the reference pockets and steers generation AWAY from that
        # mean. Passing the target's own gene in `paralogs` therefore steers
        # designs away from the receptor they are being designed against — a
        # campaign that silently optimises for the opposite of its goal, with
        # a perfectly well-formed ContrastiveGuidance to show for it.
        # [bd-dre-rp11j.3]
        target = positives[0].gene.strip()
        if not target:
            raise LigandAIValidationError(
                "contrastive_references needs the target gene. It names the "
                "receptor the designs are FOR; the panel is what they are "
                "steered away from, and the two must be distinguishable."
            )
        _offs = [str(g).strip() for g in paralogs]
        _clash = [g for g in _offs if g.upper() == target.upper()]
        if _clash:
            raise LigandAIValidationError(
                f"{target!r} appears in both target and paralogs. "
                f"cfg_reference='paralog_mean' steers generation AWAY from the "
                f"mean of the reference pockets, so including the target in its "
                f"own panel steers designs away from the receptor they are "
                f"being designed against — the exact opposite of the intent, "
                f"and silent. Remove {_clash[0]!r} from paralogs."
            )
        _dupes = {g.upper() for g in _offs if _offs.count(g) > 1}
        if _dupes:
            raise LigandAIValidationError(
                f"duplicate paralog(s) {sorted(_dupes)} would be double-weighted "
                f"in the paralog mean, silently biasing the panel toward them. "
                f"Pass each off-target once."
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
        target: str | TargetSpec | None = None,
        targets: Sequence[TargetSpec | str] | CoOptimization | None = None,
        off_targets: Sequence[str | TargetSpec],
        aggregate: CrossTargetAggregate | None = None,
        replicates: int = 1,
        diffusion_samples: int = 4,
        sampling_steps: int = 50,
        msa_enabled: bool | None = True,
        statistic: LegStatistic = "median",
        force_fresh: bool = False,
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

        ``force_fresh=True`` bypasses the dedupe for EVERY draw, not just the
        second onward. Use it whenever these designs may already have been folded
        against these receptors in the last 24h — otherwise draw 0 comes back as a
        cached batch handle with no sub-jobs, no leg can be built from it, and the
        receptor lands in :attr:`PanelResult.unmapped_submissions` with nothing
        measured. :meth:`replicate` sets it for you.

        MULTI-TARGET CO-OPTIMIZATION
        ----------------------------
        Pass ``targets=[...]`` instead of ``target=`` (never both) to fold every
        design against SEVERAL positive targets as well as every off-target.
        ``aggregate=`` is then REQUIRED — ``"worst_case"`` (min across the
        positives; what "binds BOTH" means) or ``"mean"`` — and is recorded on
        the panel so no number is ever reported without the rule that made it::

            human = TargetSpec(gene="BRD4", organism="human")
            mouse = TargetSpec(gene="BRD4", organism="mouse",
                               receptor_pdb=mouse_pdb, receptor_name="BRD4_MOUSE")
            panel = client.campaigns.fold_panel(
                designs, targets=[human, mouse], aggregate="worst_case",
                off_targets=["BRD2", "BRD3"], replicates=2,
            )

        Every positive target is folded through its OWN structure route — a
        ``TargetSpec`` with ``receptor_pdb`` / ``user_pdb_id`` folds those
        coordinates, a bare gene symbol folds the canonical one. That is why an
        ortholog leg cannot be requested with a species keyword: no fold surface
        has one, so the ortholog's structure travels instead.

        Cost scales as
        ``len(sequences) × (len(targets) + len(off_targets)) × replicates``
        fold jobs. Check it with ``client.account.credits()`` before a large panel.
        """
        seqs = [_norm_seq(s) for s in sequences]
        if not seqs:
            raise LigandAIValidationError("fold_panel needs at least one sequence")
        seqs = list(dict.fromkeys(seqs))

        # ─── the positive side ──────────────────────────────────────────────
        if (target is None) == (targets is None):
            raise LigandAIValidationError(
                "fold_panel needs exactly one of target= (one positive target) or "
                "targets= (a co-optimization over several). Passing both leaves the "
                "panel's arity ambiguous, and the arity decides which selection "
                "rule runs."
            )
        if targets is not None:
            if isinstance(targets, CoOptimization):
                if aggregate is not None and aggregate != targets.aggregate:
                    raise LigandAIValidationError(
                        f"aggregate={aggregate!r} contradicts the CoOptimization's "
                        f"own rule {targets.aggregate!r}. One request, one rule — "
                        f"drop the kwarg or rebuild the CoOptimization."
                    )
                co = targets
            else:
                specs = [coerce_target(t) for t in targets]
                if len(specs) > 1 and aggregate is None:
                    raise LigandAIValidationError(
                        "targets= has several positive targets, so aggregate= is "
                        "required and has no default.\n"
                        "  aggregate='worst_case' — min across the positive "
                        "targets. What 'binds BOTH' means, and it names the "
                        "limiting target on every row.\n"
                        "  aggregate='mean'       — average across them. Weaker: "
                        "one strong leg can mask a failed one."
                    )
                co = CoOptimization(specs, aggregate=aggregate or "worst_case")
            positives = list(co.targets)
            panel_aggregate = co.aggregate if len(positives) > 1 else None
        else:
            positives = [coerce_target(target)]
            panel_aggregate = None

        off_specs = [coerce_target(o) for o in off_targets]
        offs = [o.key for o in off_specs]
        if not offs:
            raise LigandAIValidationError(
                "fold_panel needs at least one off-target. A panel of one receptor "
                "measures affinity, not selectivity — there is no Δ to compute."
            )
        if len(set(offs)) != len(offs):
            dupes = sorted({o for o in offs if offs.count(o) > 1})
            raise LigandAIValidationError(
                f"duplicate off-target key(s) {dupes}. Legs are keyed by receptor, "
                f"so a repeated comparator folds twice into one leg and the "
                f"worst-case is computed over a shorter panel than it appears to be."
            )
        # ⛔ SIDES DO NOT OVERLAP. Δ against a positive target is zero by
        # construction, and as a contrastive reference the same receptor steers
        # generation away from something being co-optimized FOR.
        CoOptimization(positives, aggregate="worst_case").check_reference_roles(off_specs)

        pos_keys = [p.key for p in positives]
        specs_by_key: dict[str, TargetSpec] = {p.key: p for p in positives}
        for o in off_specs:
            specs_by_key.setdefault(o.key, o)
        replicates = int(replicates)
        if replicates < 1:
            raise LigandAIValidationError("replicates must be >= 1")
        if "force_resubmit" in fold_kwargs:
            # It would otherwise reach fold_batch twice and die with a bare
            # "got multiple values for keyword argument", which reads like a
            # bug in the SDK rather than the wrong knob name.
            raise LigandAIValidationError(
                "pass force_fresh=True, not force_resubmit. fold_panel sets "
                "force_resubmit per draw itself (draw 0 reuses the local 24h "
                "dedupe, later draws must not), so it is not a knob a caller "
                "can set per submission. force_fresh=True bypasses the dedupe "
                "for EVERY draw, which is what you want when re-measuring "
                "designs you have already folded."
            )

        peptides_res = getattr(self._client, "peptides", None)
        if peptides_res is None:
            raise LigandAIError("fold_panel requires a full client (client.campaigns)")

        submissions: list[tuple[str, Any]] = []
        for receptor in (*pos_keys, *offs):
            # ⛔ EVERY RECEPTOR THROUGH ITS OWN STRUCTURE ROUTE. `target_gene=`
            # resolves the canonical (human) structure, so an ortholog leg must
            # travel as receptor_pdb/receptor_name — fold_batch has no species
            # parameter, and sending the gene symbol for a leg labelled "mouse"
            # would fold the human receptor and report it as the mouse one.
            receptor_kwargs = specs_by_key[receptor].fold_kwargs()
            for draw in range(replicates):
                job = peptides_res.fold_batch(
                    peptides=seqs,
                    **receptor_kwargs,
                    diffusion_samples=diffusion_samples,
                    sampling_steps=sampling_steps,
                    msa_enabled=msa_enabled,
                    # Every replicate after the first must be a REAL new fold,
                    # not the dedupe handing back the first one's result.
                    # ⛔ ...and so must EVERY draw when the caller asked for a
                    # fresh measurement of designs it has already folded, which
                    # is precisely what `replicate()` is for. Without
                    # `force_fresh`, draw 0 of a re-fold inside the 24h dedupe
                    # window comes back as a cached batch handle carrying NO
                    # per-sub-job entries, so the panel builds zero legs and the
                    # "independent draw" measures nothing. [bd-dre-if562]
                    force_resubmit=bool(force_fresh) or draw > 0,
                    **fold_kwargs,
                )
                submissions.append((receptor, job))

        legs: dict[tuple[str, str], PanelLeg] = {}
        job_index: dict[str, tuple[str, str]] = {}
        unmapped: list[str] = []
        for receptor, job in submissions:
            entries = list(getattr(job, "jobs", None) or [])
            if not entries:
                # A batch handle with no sub-job entries cannot be mapped back to
                # a (sequence, receptor) leg. Recorded rather than silently
                # yielding an empty panel that reads like a measured null.
                unmapped.append(receptor)
                continue
            for entry in entries:
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
            targets=pos_keys,
            off_targets=offs,
            legs=legs,
            requested_draws=replicates,
            statistic=statistic,
            aggregate=panel_aggregate,
            specs=specs_by_key,
        )
        result.unmapped_submissions = unmapped
        if not wait:
            return result

        # ⛔ `BatchFoldJob.wait()` takes (timeout, poll_interval, on_progress) and
        # NOTHING ELSE. It was called here with `raise_on_failure=False`, a kwarg
        # that has never existed on it, so every waiting panel — and therefore
        # `replicate()` and every `RecursiveCampaign.run_round()` — died with a
        # bare TypeError AFTER submitting (and paying for) the whole panel. The
        # kwarg was also unnecessary: `wait()` already counts failed sub-jobs and
        # returns, raising only on timeout. [bd-dre-if562]
        for _receptor, job in submissions:
            try:
                job.wait(timeout=timeout, poll_interval=poll_interval)
            except LigandAIError as exc:
                # One receptor timing out must not discard the rest of the panel:
                # those folds are submitted, paid for, and real. The leg stays
                # empty and `deltas()` reports the row as incomplete — which is
                # exactly what it is — while `wait_errors` says why.
                result.wait_errors.append((_receptor, exc))

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
                    # The platform says WHY, and it is the difference between
                    # "not measurable" and "never measured". Observed live:
                    # peptide_ipsae_status == "not_computed" on folds whose own
                    # record carries interface_scores.ipsae. [bd-dre-if562]
                    why = m.get("peptide_ipsae_status")
                    if isinstance(why, str) and why.strip():
                        leg.peptide_ipsae_statuses.append(why.strip())
                for src, dest in (("iptm", leg.iptm_draws), ("plddt_mean", leg.plddt_draws)):
                    v = m.get(src)
                    if isinstance(v, (int, float)):
                        dest.append(float(v))
                # ⛔ VERIFIED against a live filter_folds response (2026-09-22):
                # quality_tier is {"criterion": "great_plus", "verdict":
                # "indeterminate", "coValidated": false, "summary": "..."}. It
                # carries neither "tier" nor "name", so reading only those two
                # keys made the tier silently None on EVERY real row — while the
                # module's own docstring promises the platform's tier is used
                # rather than re-derived. `verdict` is the judgement; `criterion`
                # is only the bar it was judged against, so verdict wins.
                tier = row.get("quality_tier")
                if isinstance(tier, dict):
                    tier = (
                        tier.get("verdict")
                        or tier.get("tier")
                        or tier.get("name")
                        or tier.get("criterion")
                    )
                leg.quality_tiers.append(tier if isinstance(tier, str) else None)
                leg.scaffold_mediated_draws.append(bool(m.get("scaffold_mediated")))

    def replicate(
        self,
        sequences: Sequence[str],
        *,
        target: str | TargetSpec | None = None,
        targets: Sequence[TargetSpec | str] | CoOptimization | None = None,
        off_targets: Sequence[str | TargetSpec],
        band: float,
        target_leg_min: float,
        replicates: int = 1,
        aggregate: CrossTargetAggregate | None = None,
        require_all_targets: bool = True,
        **panel_kwargs: Any,
    ) -> tuple[PanelResult, ReplicationReport]:
        """Re-fold a shortlist at independent draws and report what held.

        The decisive step. The top of a single-draw ranking is selected partly by
        that draw's noise, so re-rolling moves it down; a design is a candidate
        only once it survives that.

        Returns the fresh panel and the hold report. Sequences the fresh panel
        could not measure come back as ``untested``, never as lost.

        ⛔ Every draw here is submitted with the local 24h dedupe BYPASSED. The
        shortlist reaching this method was, by construction, folded minutes ago
        in the panel that selected it — so without the bypass the dedupe returns
        the selecting panel's own cached batch and the "independent draw" is an
        echo of the measurement it was supposed to test. Pass
        ``force_fresh=False`` explicitly only when you want the cached handle.
        """
        panel_kwargs.setdefault("force_fresh", True)
        panel = self.fold_panel(
            sequences,
            target=target,
            targets=targets,
            off_targets=off_targets,
            replicates=replicates,
            aggregate=aggregate,
            **panel_kwargs,
        )
        return panel, panel.hold_rate(
            sequences,
            band=band,
            target_leg_min=target_leg_min,
            require_all_targets=require_all_targets,
        )

    # ─── the loop ───────────────────────────────────────────────────────────

    def recursive(
        self,
        *,
        target: str | TargetSpec | None = None,
        targets: Sequence[TargetSpec | str] | CoOptimization | None = None,
        off_targets: Sequence[str | TargetSpec],
        band: float,
        target_leg_min: float,
        aggregate: CrossTargetAggregate | None = None,
        require_all_targets: bool = True,
        model: str = "v6.9.21",
        num_peptides: int = 200,
        length_range: tuple[int, int] = (12, 24),
        cysteine_mode: str = "disulfide_only",
        avoidance: AvoidancePolicy | None = None,
        contrastive: ContrastiveGuidance | None = None,
        contrastive_by_target: dict[str, ContrastiveGuidance] | None = None,
        allow_unforwarded_contrastive: bool = False,
        **generate_kwargs: Any,
    ) -> RecursiveCampaign:
        """Build a :class:`RecursiveCampaign` bound to this client.

        Pass ``targets=[...]`` + ``aggregate=`` for a multi-target
        co-optimization: one generation leg per positive target (each with its
        own pocket and, optionally, its own reference panel via
        ``contrastive_by_target``), the pools unioned, then ONE panel folded
        against every positive target and every off-target.
        """
        if (target is None) == (targets is None):
            raise LigandAIValidationError(
                "recursive() needs exactly one of target= or targets=."
            )
        if targets is not None:
            if isinstance(targets, CoOptimization):
                co = targets
                if aggregate is not None and aggregate != co.aggregate:
                    raise LigandAIValidationError(
                        f"aggregate={aggregate!r} contradicts the CoOptimization's "
                        f"own rule {co.aggregate!r}."
                    )
            else:
                specs = [coerce_target(t) for t in targets]
                if len(specs) > 1 and aggregate is None:
                    raise LigandAIValidationError(
                        "targets= has several positive targets, so aggregate= is "
                        "required and has no default: 'worst_case' (min across "
                        "them — what 'binds BOTH' means) or 'mean'."
                    )
                co = CoOptimization(specs, aggregate=aggregate or "worst_case")
        else:
            co = CoOptimization([coerce_target(target)], aggregate="worst_case")
        off_specs = [coerce_target(o) for o in off_targets]
        co.check_reference_roles(off_specs)
        # ⛔ GENERATION-ONLY LIMIT, checked here and NOT in fold_panel. Two
        # positives under one gene symbol cannot be persisted as one generation
        # session; the panel over those same two targets measures fine. See
        # CoOptimization.check_generation_persistable.
        co.check_generation_persistable()
        return RecursiveCampaign(
            campaigns=self,
            co_optimization=co,
            off_targets=[o.key for o in off_specs],
            off_target_specs=off_specs,
            band=float(band),
            target_leg_min=float(target_leg_min),
            require_all_targets=bool(require_all_targets),
            model=model,
            num_peptides=num_peptides,
            length_range=length_range,
            cysteine_mode=cysteine_mode,
            avoidance=avoidance or AvoidancePolicy(
                off_target_genes=[o.gene for o in off_specs]
            ),
            contrastive=contrastive,
            contrastive_by_target=dict(contrastive_by_target or {}),
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

    # ⛔ [bd-dre-rp11j.9] THE PLATFORM CAMPAIGN API IS NOT IN THIS PACKAGE.
    #
    # The server exposes a campaign/maturation surface — POST /api/campaign/start,
    # POST /api/campaign/mature, GET /api/campaign, GET /{id}/status,
    # /{id}/maturation, /{id}/leaderboard, /{id}/designs/{id}/structure,
    # POST /{id}/stop, GET /{id}/stream. The published `ligandai` package ships
    # NO client for any of it.
    #
    # This namespace is an unrelated local panel-folding helper that happens to
    # answer to the same word. Without this guard, `client.campaigns.mature(...)`
    # raises a bare AttributeError that reads like a typo, and the user cannot
    # tell "this package cannot do campaigns" from "I called it wrong" — the
    # epic's defect class, at the namespace level.
    _PLATFORM_CAMPAIGN_SURFACE: ClassVar[dict[str, str]] = {
        "start": "POST /api/campaign/start",
        "mature": "POST /api/campaign/mature",
        "maturation": "GET /api/campaign/{campaign_id}/maturation",
        "list": "GET /api/campaign",
        "status": "GET /api/campaign/{campaign_id}/status",
        "leaderboard": "GET /api/campaign/{campaign_id}/leaderboard",
        "structure": "GET /api/campaign/{campaign_id}/designs/{design_id}/structure",
        "stop": "POST /api/campaign/{campaign_id}/stop",
        "stream": "GET /api/campaign/{campaign_id}/stream",
        "designs": "GET /api/campaign/{campaign_id}/leaderboard",
    }

    #: Surfaces that exist on the SYNC :class:`Campaigns` and deliberately have
    #: no async facade (see the class docstring). Named here so reaching for one
    #: says "sync only" instead of raising a bare AttributeError that reads like
    #: a typo -- the same defect class the guard below exists to prevent, one
    #: attribute lookup away from it.
    _SYNC_ONLY_SURFACE: ClassVar[tuple[str, ...]] = (
        "contrastive_references",
        "fold_panel",
        "replicate",
        "recursive",
    )

    def __getattr__(self, name: str) -> Any:
        if name in self._SYNC_ONLY_SURFACE:
            raise LigandAISurfaceNotInThisPackage(
                f"client.campaigns.{name}() exists, but only on the SYNC client.\n"
                f"  you reached for : AsyncLigandAI(...).campaigns.{name}\n"
                f"  where it lives  : LigandAI(...).campaigns.{name}\n"
                f"  why             : {name} submits and waits on fold jobs, and an "
                f"async facade over a blocking submit-and-wait would quietly block "
                f"the event loop — so it is not shipped rather than shipped broken. "
                f"AsyncCampaigns mirrors only the transport-free analysis surface "
                f"(pocket_features, plus everything PanelResult computes — a "
                f"PanelResult is usable from either client).\n"
                f"  what to do      : build the panel on a sync LigandAI client, or "
                f"run it in a worker thread (asyncio.to_thread).\n"
                f"  tracking        : bd-dre-if562",
                surface=name,
                namespace="client.campaigns (async)",
                server_routes=(),
            )
        route = self._PLATFORM_CAMPAIGN_SURFACE.get(name)
        if route is None:
            raise AttributeError(
                f"{type(self).__name__!r} object has no attribute {name!r}"
            )
        raise LigandAISurfaceNotInThisPackage(
            f"client.campaigns.{name}() does not exist in the `ligandai` package, "
            f"but the platform endpoint it maps to DOES exist.\n"
            f"  you reached for : client.campaigns.{name}\n"
            f"  server endpoint : {route}\n"
            f"  the situation   : `client.campaigns` here is a LOCAL panel-folding "
            f"helper (pocket_features, contrastive_references, fold_panel, "
            f"replicate, recursive). It is NOT a client for the platform "
            f"campaign/maturation API — the two only share a name.\n"
            f"  this is not a typo on your part, and it is not a transient "
            f"failure: no version of this package has ever carried that surface.\n"
            f"  what to do      : drive the endpoint directly over the SDK's own "
            f"transport, which carries your auth, retries and error mapping:\n"
            f"      client.transport.request(\"POST\", \"/api/campaign/start\", json={{...}})\n"
            f"  tracking        : bd-dre-rp11j.9",
            surface=name,
            namespace="client.campaigns",
            server_routes=tuple(sorted(set(self._PLATFORM_CAMPAIGN_SURFACE.values()))),
        )


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
        # Same contract as the sync surface: REQUEST the width, then check it.
        # A client-side-only assertion can never pass for a v6.9 panel.
        if want is not None:
            params["dim"] = int(want)
        try:
            payload = await self._transport.request(
                "GET", _POCKET_FEATURES_PATH.format(gene=gene), params=params or None
            ) or {}
        except LigandAINotFoundError as exc:
            # Same two-facts-one-status split as the sync surface above.
            body = getattr(exc, "response", None) or {}
            code = str((body or {}).get("error") or "").lower()
            if code in _POCKET_FEATURES_LOOKED_AND_FAILED:
                raise _pocket_features_no_pocket(
                    gene, str((body or {}).get("message") or "") or None
                ) from exc
            raise _pocket_features_unreachable(gene) from exc
        feats = (payload or {}).get("features") if isinstance(payload, dict) else None
        if not feats:
            reason = ""
            if isinstance(payload, dict):
                reason = str(payload.get("error") or payload.get("pocket_source") or "")
            if not reason:
                raise _pocket_features_unreachable(gene)
            raise _pocket_features_no_pocket(gene, reason)
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
        target: str | TargetSpec | None = None,
        co_optimization: CoOptimization | None = None,
        off_targets: list[str],
        off_target_specs: list[TargetSpec] | None = None,
        band: float,
        target_leg_min: float,
        require_all_targets: bool = True,
        model: str = "v6.9.21",
        num_peptides: int = 200,
        length_range: tuple[int, int] = (12, 24),
        cysteine_mode: str = "disulfide_only",
        avoidance: AvoidancePolicy | None = None,
        contrastive: ContrastiveGuidance | None = None,
        contrastive_by_target: dict[str, ContrastiveGuidance] | None = None,
        allow_unforwarded_contrastive: bool = False,
        generate_kwargs: dict[str, Any] | None = None,
    ) -> None:
        if (target is None) == (co_optimization is None):
            raise LigandAIValidationError(
                "RecursiveCampaign needs exactly one of target= or co_optimization=."
            )
        if co_optimization is None:
            co_optimization = CoOptimization(
                [coerce_target(target)], aggregate="worst_case"
            )
        self._campaigns = campaigns
        self._client = campaigns._client
        #: Every positive target plus the rule that combines them.
        self.co_optimization: CoOptimization = co_optimization
        self.off_targets = list(off_targets)
        self.off_target_specs = list(
            off_target_specs or [coerce_target(o) for o in off_targets]
        )
        self.band = float(band)
        self.target_leg_min = float(target_leg_min)
        self.require_all_targets = bool(require_all_targets)
        self.model = model
        self.num_peptides = int(num_peptides)
        self.length_range = length_range
        self.cysteine_mode = cysteine_mode
        self.avoidance = avoidance or AvoidancePolicy(
            off_target_genes=[o.gene for o in self.off_target_specs]
        )
        self.contrastive = contrastive
        #: Per-positive-target reference panels, keyed by ``TargetSpec.key``.
        #: ⛔ A panel built for one target is NOT reusable on another: the
        #: guidance is ``target − mean(references)``, so the same reference set
        #: pulls in a different direction from a different target pocket, and
        #: the widths must match the model either way. Unkeyed ``contrastive=``
        #: is applied to every leg only because a shared OFF-TARGET set is
        #: exactly what a co-optimization usually wants; residue-level specs are
        #: never shared this way (see :class:`CoOptimization`).
        self.contrastive_by_target = dict(contrastive_by_target or {})
        unknown = set(self.contrastive_by_target) - set(co_optimization.keys)
        if unknown:
            raise LigandAIValidationError(
                f"contrastive_by_target has key(s) {sorted(unknown)} that are not "
                f"positive targets of this campaign ({co_optimization.keys}). A "
                f"panel keyed to nothing is silently never applied."
            )
        self.allow_unforwarded_contrastive = allow_unforwarded_contrastive
        self.generate_kwargs = dict(generate_kwargs or {})
        self.rounds: list[CampaignRound] = []

    # ─── the positive side ──────────────────────────────────────────────────

    @property
    def targets(self) -> list[str]:
        """Panel keys of every positive target."""
        return self.co_optimization.keys

    @property
    def aggregate(self) -> CrossTargetAggregate:
        return self.co_optimization.aggregate

    @property
    def target(self) -> str:
        """The FIRST positive target.

        ⛔ One leg of several on a co-optimization. Kept for callers that
        predate multi-target; read :attr:`targets` otherwise.
        """
        return self.co_optimization.keys[0]

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
            targets=self.co_optimization,
            off_targets=self.off_target_specs,
            replicates=replicates,
            **panel_kwargs,
        )
        if panel.unmapped_submissions:
            # ⛔ An unmeasured panel and a panel of nulls produce the SAME empty
            # verdict list, and the round would go on to report "0 survivors"
            # as though it had looked. Say which receptors were never measured.
            notes.append(
                f"⛔ no legs could be built for receptor(s) "
                f"{sorted(set(panel.unmapped_submissions))} — the batch came back "
                f"as a cached handle with no sub-jobs (local 24h dedupe). Those "
                f"rows are UNMEASURED, not negative. Re-run with "
                f"panel_kwargs={{'force_fresh': True}}."
            )
        if panel.wait_errors:
            notes.append(
                f"⛔ fold wait did not complete for receptor(s) "
                f"{sorted({r for r, _e in panel.wait_errors})}; affected rows are "
                f"incomplete, not negative"
            )
        complete, partial, summary = panel.deltas(
            band=self.band, target_leg_min=self.target_leg_min
        )
        if self.co_optimization.is_multi:
            notes.append(
                f"co-optimization: {self.co_optimization.describe()}; frames "
                f"{self.co_optimization.frames}"
            )
        why = summary.get("peptide_ipsae_not_computed_reasons") or {}
        if why and not complete:
            # ⛔ "0 survivors" reads as a measurement. It is not one when the
            # binder metric was never computed, and the platform said so.
            notes.append(
                f"⛔ NO design could be scored: the platform returned no "
                f"peptide_ipsae for any leg (reasons {dict(why)}). This round "
                f"measured NOTHING — it did not find that nothing binds. "
                f"peptide_ipsae is the only metric selection runs on; iptm/plddt "
                f"in the rows are not substitutes for it."
            )
        elif why:
            notes.append(
                f"peptide_ipsae unavailable on some legs (reasons {dict(why)}); "
                f"those rows are incomplete, not negative"
            )
        survivors = [
            r.sequence for r in complete
            if r.verdict == "PASS_BOTH"
            and (not self.require_all_targets or getattr(r, "all_targets_pass", True))
        ]
        if self.co_optimization.is_multi:
            # ⛔ "n survived" over several targets is meaningless without saying
            # WHICH target held the rest back. Under aggregate='mean' this is
            # the half the average hides.
            limiting = summary.get("limiting_target_counts") or {}
            if limiting:
                notes.append(f"limiting positive target per complete row: {dict(limiting)}")
            n_agg_only = sum(
                1 for r in complete
                if r.verdict == "PASS_BOTH" and not getattr(r, "all_targets_pass", True)
            )
            if n_agg_only:
                notes.append(
                    f"⛔ {n_agg_only} design(s) passed on the aggregated score "
                    f"({self.aggregate}) while MISSING at least one positive "
                    f"target's own floor. "
                    + ("They are excluded (require_all_targets=True)."
                       if self.require_all_targets
                       else "They are INCLUDED (require_all_targets=False) — a "
                            "co-optimized hit that misses one of its targets is "
                            "not a hit against that target.")
                )

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
                targets=self.co_optimization,
                off_targets=self.off_target_specs,
                band=self.band,
                target_leg_min=self.target_leg_min,
                replicates=confirm_replicates,
                require_all_targets=self.require_all_targets,
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
        """Generate the round's pool — ONE leg per positive target, unioned.

        ⛔ WHY LEGS AND NOT ONE JOINT RUN. The served generator conditions on
        ONE receptor pocket per run (``pocket_features``) plus N references to
        avoid. There is no operator that takes several POSITIVE pockets and
        conditions on all of them at once, so a "joint" generation request would
        be a shape the platform has no implementation for. What IS real is that
        the co-optimization is decided by the joint MEASUREMENT: every design
        from every leg is folded against every positive target, and a design
        only survives if it clears all of them under the stated rule. Generating
        per-target and selecting jointly is therefore the honest expression of
        the ask, not an approximation of a capability that exists elsewhere.

        ⛔ AND THE REFERENCE PANEL NEVER CONTAINS A SIBLING POSITIVE. Guidance
        is ``target − mean(references)``: putting target B in target A's panel
        steers A's designs away from B, which is the opposite of co-optimizing
        for both. :meth:`CoOptimization.check_reference_roles` refuses it at
        construction, and each leg's panel comes from
        ``contrastive_by_target[key]`` or the shared ``contrastive``.
        """
        peptides = getattr(self._client, "peptides", None)
        if peptides is None:
            raise LigandAIError("RecursiveCampaign requires a full client")

        specs = list(self.co_optimization.targets)
        n_targets = len(specs)
        # Split the round's budget across the legs so `num_peptides` keeps
        # meaning "this round's pool size" rather than silently multiplying by
        # the number of targets (and the credit spend with it).
        per_leg = max(1, self.num_peptides // n_targets)
        if n_targets > 1:
            notes.append(
                f"generation: {n_targets} leg(s), {per_leg} peptide(s) each "
                f"(pool ≈ {per_leg * n_targets}); the co-optimization is decided "
                f"by the joint fold panel, not by the generator"
            )

        pooled: list[str] = []
        for spec in specs:
            kwargs: dict[str, Any] = {
                "num_peptides": per_leg,
                "length_range": self.length_range,
                "model": self.model,
                "cysteine_mode": self.cysteine_mode,
                **spec.generate_kwargs(),
                **self.avoidance.generation_kwargs(),
                **self.generate_kwargs,
            }
            panel = self.contrastive_by_target.get(spec.key, self.contrastive)
            if panel is not None:
                kwargs["contrastive"] = panel
                kwargs["allow_unforwarded_contrastive"] = self.allow_unforwarded_contrastive
                notes.append(f"{spec.key}: contrastive guidance {panel.describe()}")
            else:
                notes.append(
                    f"{spec.key}: no explicit contrastive panel supplied; a "
                    f"{self.model} run is steered against references the platform "
                    f"derives itself, and the chosen panel is not reported back"
                )
            job = peptides.generate(**kwargs)
            result = job.wait() if hasattr(job, "wait") else job
            leg_seqs: list[str] = []
            for pep in (getattr(result, "peptides", None) or []):
                seq = getattr(pep, "sequence", None) or (
                    pep.get("sequence") if isinstance(pep, dict) else None
                )
                if seq:
                    leg_seqs.append(_norm_seq(seq))
            if n_targets > 1:
                notes.append(f"{spec.key}: generated {len(set(leg_seqs))} unique design(s)")
            pooled.extend(leg_seqs)
        return list(dict.fromkeys(pooled))

    # ─── reporting ──────────────────────────────────────────────────────────

    def report(self) -> dict[str, Any]:
        """A compact, quotable record of every round."""
        return {
            "target": self.target,
            "targets": list(self.targets),
            # ⛔ The rule that combined the positive targets, next to every
            # number it produced. Never reported without it.
            "positive_aggregate": (
                self.aggregate if self.co_optimization.is_multi else "single"
            ),
            "require_all_targets": self.require_all_targets,
            "target_frames": self.co_optimization.frames,
            "cross_species": self.co_optimization.is_cross_species,
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
        pos = (
            self.target if not self.co_optimization.is_multi
            else f"{self.targets}[{self.aggregate}]"
        )
        return (
            f"<RecursiveCampaign {pos} vs {self.off_targets} "
            f"| {len(self.rounds)} round(s), {len(self.confirmed)} confirmed>"
        )
