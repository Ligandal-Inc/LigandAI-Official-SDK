# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Deciding what to run next, and knowing when to stop trying.

The planner reads the unmet stop metrics and queues the specific work that would
close each one.  Every action carries the metric it serves, a written reason, and
a credit estimate, so each iteration can say — in one line — WHICH metric is
unmet and WHAT was queued to close it.

The budget ledger is the other half: a hard credit cap, an iteration cap, a wall
clock cap, and a no-progress rule.  A loop that cannot demonstrate movement
toward its stop metrics stops, rather than converting an allocation into folds.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from .record import DesignRecord
from .spec import CampaignSpec, StopMetric
from .stopmetrics import StopMetricResult, StopReport
from .values import Outcome, runs_needed_for_upper_bound


class ActionKind(str, Enum):
    GENERATE = "generate"                    # new designs
    REPLICATE = "replicate"                  # more independent seed units
    PARALOG_PANEL = "paralog_panel"          # fold vs a named off-target
    COFACTOR_CONTROL = "cofactor_control"    # paired refold without cofactors
    DEVELOPABILITY = "developability"        # immunogenicity / stability block
    AFFINITY = "affinity"                    # DeltaForge on the gate-passing set
    CONSTRUCT_COMPARISON = "construct_comparison"
    CONTROL_ARM = "control_arm"              # scramble / length-matched random


@dataclass
class Action:
    kind: ActionKind
    reason: str                       # why, in words, tied to a stop metric
    serves_metric: str | None
    design_ids: tuple[str, ...] = ()
    peptide_class: str | None = None
    n_designs: int = 0
    n_runs_each: int = 0
    off_target: str | None = None
    cofactors_removed: tuple[str, ...] = ()
    block: str | None = None
    construct: str | None = None
    estimated_credits: int = 0

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        d["design_ids"] = list(self.design_ids)
        d["cofactors_removed"] = list(self.cofactors_removed)
        d["n_design_ids"] = len(self.design_ids)
        # ids are noise in a log line; keep the count
        d.pop("design_ids", None)
        return d

    def __str__(self) -> str:
        bits = [self.kind.value]
        if self.peptide_class:
            bits.append(f"class={self.peptide_class}")
        if self.off_target:
            bits.append(f"off_target={self.off_target}")
        if self.block:
            bits.append(f"block={self.block}")
        if self.design_ids:
            bits.append(f"n_designs={len(self.design_ids)}")
        elif self.n_designs:
            bits.append(f"n_designs={self.n_designs}")
        if self.n_runs_each:
            bits.append(f"runs_each={self.n_runs_each}")
        bits.append(f"~{self.estimated_credits:,}cr")
        return " ".join(bits) + f"  [{self.serves_metric}] {self.reason}"


class StopReason(str, Enum):
    METRICS_MET = "metrics_met"
    CREDIT_CAP = "credit_cap"
    ITERATION_CAP = "iteration_cap"
    WALL_CLOCK = "wall_clock"
    NO_PROGRESS = "no_progress"
    NOTHING_TO_QUEUE = "nothing_to_queue"
    CANCELLED = "cancelled"


@dataclass
class BudgetLedger:
    """Spend, iterations, wall clock, and the no-progress history."""

    max_credits: int
    max_iterations: int
    max_wall_clock_hours: float
    patience_iterations: int
    min_absolute_progress: float
    reserve_fraction: float = 0.05
    per_iteration_cap: int | None = None
    spent: int = 0
    iterations: int = 0
    started_at: float = field(default_factory=time.time)
    shortfall_history: list[float] = field(default_factory=list)
    entries: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_spec(cls, spec: CampaignSpec) -> "BudgetLedger":
        b = spec.budget
        return cls(b.max_credits, b.max_iterations, b.max_wall_clock_hours,
                   b.no_progress.patience_iterations, b.no_progress.min_absolute_progress,
                   b.reserve_fraction, b.per_iteration_credit_cap)

    @property
    def remaining(self) -> int:
        return max(0, self.max_credits - self.spent)

    @property
    def spendable(self) -> int:
        """Remaining credits minus the untouchable reserve."""
        return max(0, self.remaining - int(self.max_credits * self.reserve_fraction))

    @property
    def elapsed_hours(self) -> float:
        return (time.time() - self.started_at) / 3600.0

    def iteration_allowance(self) -> int:
        cap = self.spendable
        if self.per_iteration_cap is not None:
            cap = min(cap, self.per_iteration_cap)
        return cap

    def charge(self, credits: int, what: str) -> None:
        if credits < 0:
            raise ValueError("cannot charge negative credits")
        self.spent += credits
        self.entries.append({"iteration": self.iterations, "credits": credits,
                             "what": what, "cumulative": self.spent,
                             "at": time.time()})

    def record_shortfall(self, shortfall: float) -> None:
        self.shortfall_history.append(shortfall)

    def no_progress(self) -> tuple[bool, str]:
        """True when the last ``patience_iterations`` closed less than the minimum."""
        h = self.shortfall_history
        k = self.patience_iterations
        if len(h) < k + 1:
            return False, (f"only {len(h)} shortfall readings; need {k + 1} before the "
                           f"no-progress guard can fire")
        closed = h[-(k + 1)] - h[-1]
        if closed < self.min_absolute_progress:
            return True, (f"over the last {k} iterations the total shortfall moved "
                          f"{h[-(k+1)]:.3g} -> {h[-1]:.3g} (closed {closed:.3g}), below "
                          f"the required {self.min_absolute_progress:g}")
        return False, f"closed {closed:.3g} over the last {k} iterations"

    def check(self, stop_report: StopReport | None) -> tuple[StopReason, str] | None:
        if stop_report is not None:
            v = stop_report.all_met
            if v.outcome is Outcome.PASS:
                return StopReason.METRICS_MET, str(v)
        if self.iterations >= self.max_iterations:
            return StopReason.ITERATION_CAP, (
                f"{self.iterations} iterations >= max_iterations {self.max_iterations}")
        if self.elapsed_hours >= self.max_wall_clock_hours:
            return StopReason.WALL_CLOCK, (
                f"{self.elapsed_hours:.1f}h >= max_wall_clock_hours "
                f"{self.max_wall_clock_hours}")
        if self.spendable <= 0:
            return StopReason.CREDIT_CAP, (
                f"spent {self.spent:,} of {self.max_credits:,} credits; the "
                f"{self.reserve_fraction:.0%} reserve is untouchable")
        stalled, why = self.no_progress()
        if stalled:
            return StopReason.NO_PROGRESS, why
        return None

    def as_dict(self) -> dict[str, Any]:
        stalled, why = self.no_progress()
        return {"max_credits": self.max_credits, "spent": self.spent,
                "remaining": self.remaining, "spendable": self.spendable,
                "iterations": self.iterations, "max_iterations": self.max_iterations,
                "elapsed_hours": round(self.elapsed_hours, 3),
                "max_wall_clock_hours": self.max_wall_clock_hours,
                "shortfall_history": list(self.shortfall_history),
                "no_progress": stalled, "no_progress_detail": why,
                "ledger": list(self.entries)}


# ---------------------------------------------------------------------------

def _fold_cost(spec: CampaignSpec, n_designs: int, n_runs_each: int) -> int:
    return int(n_designs * max(1, n_runs_each) * spec.fold.credits_per_fold)


def _needs_more_runs(spec: CampaignSpec, r: DesignRecord) -> int:
    """How many more independent runs this design needs to have a shot at certifying."""
    rel = spec.reliability
    have = r.reliability.n_runs if r.reliability else 0
    rate = (r.reliability.interval.point
            if (r.reliability and r.reliability.interval) else 0.0)
    need = runs_needed_for_upper_bound(rel.certification_bar, rate, rel.confidence)
    if need is None:
        return 0          # cannot certify at this rate — more folds will not help
    return max(0, need - have)


def plan_iteration(spec: CampaignSpec, records: Sequence[DesignRecord],
                   gate_pass_ids: Sequence[str], stop_report: StopReport,
                   ledger: BudgetLedger, *,
                   max_actions: int = 8) -> list[Action]:
    """Turn every unmet stop metric into concrete, costed work.

    Order is cheapest-blocking-first: a missing control or a missing developability
    block is one pass over designs that already exist, while replication and new
    generation are the expensive legs.  Actions are truncated at the iteration
    allowance, and the truncation is itself reported.
    """
    allowance = ledger.iteration_allowance()
    by_id = {r.design_id: r for r in records}
    gp = [by_id[i] for i in gate_pass_ids if i in by_id]
    certified = [r for r in records if r.certified is True]
    actions: list[Action] = []
    rejected: list[str] = []

    def budget_left() -> int:
        return allowance - sum(a.estimated_credits for a in actions)

    def add(a: Action) -> None:
        """Queue an action, or say — out loud — why it could not be queued.

        An action silently dropped for cost looks exactly like an action nobody
        thought of. Every rejection is recorded and printed with the plan.
        """
        if len(actions) >= max_actions:
            rejected.append(f"{a.kind.value}: iteration already holds "
                            f"{max_actions} actions")
            return
        if a.estimated_credits > budget_left():
            rejected.append(
                f"{a.kind.value}"
                + (f"/{a.peptide_class}" if a.peptide_class else "")
                + (f"/{a.off_target}" if a.off_target else "")
                + f": needs {a.estimated_credits:,} credits, only "
                  f"{budget_left():,} left in this iteration's allowance of "
                  f"{allowance:,}")
            return
        actions.append(a)

    def afford_designs(unit_cost: int, want: int, minimum: int = 1) -> int:
        """Largest design count this iteration can pay for, at ``unit_cost`` each."""
        if unit_cost <= 0:
            return want
        return max(0, min(want, budget_left() // unit_cost)) or (
            minimum if budget_left() >= unit_cost * minimum else 0)

    # rank unmet metrics so the cheap structural gaps are closed first
    order = {"control_present": 0, "block_coverage": 1, "margin_coverage": 2,
             "interval_width": 3, "certified_designs": 4}
    unmet = sorted(stop_report.unmet, key=lambda r: order.get(r.metric.kind, 9))

    for res in unmet:
        sm = res.metric
        pool = gp if sm.of_population == "gate_passing" else (
            certified if sm.of_population == "certified" else list(records))

        if sm.kind == "control_present" and sm.control == "cofactor_free":
            missing = [r for r in pool
                       if r.cofactor_control is None or not r.cofactor_control.paired]
            want = int(min(len(missing), max(0, sm.target - res.observed)))
            if want:
                runs = max(1, spec.fold.seeds_per_unit)
                add(Action(ActionKind.COFACTOR_CONTROL,
                           f"cofactor-free control absent on {len(missing)} of "
                           f"{len(pool)} designs; the control is what separates a "
                           f"metal-dependent binder from a surface artefact",
                           sm.id, tuple(r.design_id for r in missing[:want]),
                           n_designs=want, n_runs_each=runs,
                           cofactors_removed=tuple(c.name for c in spec.target.cofactors),
                           estimated_credits=_fold_cost(spec, want, runs)))
            continue

        if sm.kind == "block_coverage":
            missing = [r for r in pool if not getattr(r, sm.block or "", None)]
            if missing:
                kind = (ActionKind.AFFINITY if sm.block == "affinity"
                        else ActionKind.CONSTRUCT_COMPARISON if sm.block == "construct_comparison"
                        else ActionKind.DEVELOPABILITY)
                # affinity and developability are CPU scoring passes, not folds
                cost = (_fold_cost(spec, len(missing), 1)
                        if kind is ActionKind.CONSTRUCT_COMPARISON else 0)
                add(Action(kind,
                           f"{len(missing)} of {len(pool)} designs in "
                           f"{sm.of_population!r} carry no {sm.block!r} block; the "
                           f"score is absent, which is not the same as failing",
                           sm.id, tuple(r.design_id for r in missing),
                           n_designs=len(missing), block=sm.block,
                           estimated_credits=cost))
            continue

        if sm.kind == "margin_coverage":
            for off in sm.off_targets:
                have = sum(1 for r in pool if r.margin_against(off).is_measured)
                shortfall_here = int(max(0, sm.target - res.per_key.get(off, 0.0)))
                if shortfall_here <= 0:
                    continue
                need_panel = [r for r in pool if not r.margin_against(off).is_measured]
                if not need_panel:
                    continue
                # a margin needs BOTH legs on the same construct set
                n = min(len(need_panel), max(shortfall_here * 2, shortfall_here + 5))
                runs = max(1, spec.fold.seeds_per_unit)
                add(Action(ActionKind.PARALOG_PANEL,
                           f"{shortfall_here} short of {int(sm.target)} designs with a "
                           f">= {sm.min_margin} margin vs {off}; {len(need_panel)} of "
                           f"{len(pool)} have no {off} leg at all, so the margin is "
                           f"unmeasured rather than negative",
                           sm.id, tuple(r.design_id for r in need_panel[:n]),
                           n_designs=n, n_runs_each=runs, off_target=off,
                           estimated_credits=_fold_cost(spec, n, runs)))
            continue

        if sm.kind in ("certified_designs", "interval_width"):
            # replicate the designs that are SHORT OF RUNS, not short of quality
            candidates: list[tuple[DesignRecord, int]] = []
            for r in pool:
                if r.certified is True:
                    continue
                need = _needs_more_runs(spec, r)
                if need > 0:
                    candidates.append((r, need))
            candidates.sort(key=lambda t: t[1])          # cheapest to close first
            want = int(max(0, sm.target - res.observed))
            take = candidates[: max(want, 0) + 10] if want else candidates[:10]
            if take:
                # cap the per-design run count so one design cannot eat the iteration
                per_design_cap = max(1, min(64, max(n for _, n in take)))
                batch = [(r, min(n, per_design_cap)) for r, n in take]
                n_runs = max(n for _, n in batch)
                afford = max(0, budget_left() // (spec.fold.credits_per_fold * max(1, n_runs)))
                batch = batch[:afford] if afford else []
                if batch:
                    add(Action(ActionKind.REPLICATE,
                               f"{int(res.observed)} of {int(sm.target)} certified; "
                               f"{len(candidates)} designs are short of RUNS rather than "
                               f"quality — 8 clean runs bound the failure rate no tighter "
                               f"than 0.32 against a {spec.reliability.certification_bar} bar",
                               sm.id, tuple(r.design_id for r, _ in batch),
                               n_designs=len(batch), n_runs_each=n_runs,
                               estimated_credits=_fold_cost(spec, len(batch), n_runs)))
            # if the candidate pool itself is too thin, make more designs
            hopeful = len(certified) + len(candidates)
            if hopeful < sm.target and budget_left() > spec.fold.credits_per_generation:
                for pc in spec.peptide_classes:
                    unit = spec.fold.credits_per_fold * max(1, spec.fold.seeds_per_unit)
                    room = budget_left() - spec.fold.credits_per_generation
                    n = max(0, min(pc.n_per_round, room // unit))
                    if n < 1:
                        rejected.append(
                            f"generate/{pc.name}: {budget_left():,} credits left cannot "
                            f"cover one generation ({spec.fold.credits_per_generation:,}) "
                            f"plus a single design folded at {spec.fold.seeds_per_unit} "
                            f"seed unit(s) ({unit:,})")
                        continue
                    trimmed = ("" if n == pc.n_per_round else
                               f" (trimmed from {pc.n_per_round} to fit the "
                               f"{allowance:,}-credit iteration allowance)")
                    cost = spec.fold.credits_per_generation + _fold_cost(
                        spec, n, spec.fold.seeds_per_unit)
                    add(Action(ActionKind.GENERATE,
                               f"only {hopeful} designs can still reach certification "
                               f"against a target of {int(sm.target)}; the candidate pool "
                               f"is too thin to close the gap by replication alone"
                               + trimmed,
                               sm.id, peptide_class=pc.name, n_designs=n,
                               n_runs_each=spec.fold.seeds_per_unit,
                               estimated_credits=cost))
            continue

    # controls declared in the spec but never run
    ran = {c for r in records for c in r.provenance.get("controls", [])}
    for c in spec.controls:
        if c not in ran and budget_left() > 0:
            n = 20
            n = afford_designs(spec.fold.credits_per_fold, n)
            if n < 1:
                rejected.append(f"control_arm/{c}: no credits left this iteration")
                continue
            add(Action(ActionKind.CONTROL_ARM,
                       f"control arm {c!r} declared in the spec has never run; without "
                       f"it there is no evidence the interface metric is informative on "
                       f"this receptor at all",
                       None, peptide_class=c, n_designs=n, n_runs_each=1,
                       estimated_credits=_fold_cost(spec, n, 1)))
    return actions, rejected


@dataclass
class IterationPlan:
    iteration: int
    actions: tuple[Action, ...]
    stop_report: StopReport
    allowance: int
    truncated: bool
    rejected: tuple[str, ...] = ()
    note: str = ""

    @property
    def estimated_credits(self) -> int:
        return sum(a.estimated_credits for a in self.actions)

    def as_dict(self) -> dict[str, Any]:
        return {"iteration": self.iteration,
                "unmet_metrics": [r.metric.id for r in self.stop_report.unmet],
                "actions": [a.as_dict() for a in self.actions],
                "estimated_credits": self.estimated_credits,
                "allowance": self.allowance, "truncated": self.truncated,
                "rejected": list(self.rejected), "note": self.note}

    def summary(self) -> str:
        lines = [f"iteration {self.iteration}: "
                 f"{len(self.stop_report.unmet)} unmet metric(s), "
                 f"{len(self.actions)} action(s), ~{self.estimated_credits:,} credits "
                 f"of {self.allowance:,} allowed"]
        for r in self.stop_report.unmet:
            lines.append(f"  UNMET  {r}")
        for a in self.actions:
            lines.append(f"  QUEUE  {a}")
        for r in self.rejected:
            lines.append(f"  SKIP   {r}")
        if self.truncated:
            lines.append("  (queue truncated by the per-iteration credit allowance)")
        return "\n".join(lines)
