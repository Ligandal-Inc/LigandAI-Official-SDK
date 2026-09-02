# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Evaluating the stop metrics — what "done" means, numerically.

Every result carries the row count that produced it (D8): a stop metric checked
against zero rows is INDETERMINATE and does not read as met.  Each unmet metric
also reports its SHORTFALL, which is what the planner turns into work.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Iterable, Mapping, Sequence

from .record import DesignRecord
from .spec import CampaignSpec, StopMetric
from .values import Outcome, Verdict


@dataclass(frozen=True)
class StopMetricResult:
    metric: StopMetric
    outcome: Outcome
    observed: float
    target: float
    shortfall: float
    n_rows: int              # rows the metric actually saw
    n_population: int        # rows in the population it was evaluated on
    detail: str = ""
    per_key: dict[str, float] = field(default_factory=dict)   # e.g. per off-target

    @property
    def met(self) -> bool:
        return self.outcome is Outcome.PASS

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["metric"] = self.metric.as_dict()
        d["outcome"] = self.outcome.value
        d["met"] = self.met
        return d

    def __str__(self) -> str:
        mark = {Outcome.PASS: "MET", Outcome.FAIL: "UNMET",
                Outcome.INDETERMINATE: "INDETERMINATE"}[self.outcome]
        return (f"[{mark}] {self.metric.id} ({self.metric.kind}): "
                f"observed {self.observed:g} / target {self.target:g}"
                f"  (n={self.n_rows} of population {self.n_population})"
                + (f" — {self.detail}" if self.detail else ""))


def _select(records: Sequence[DesignRecord], population: str,
            gate_pass_ids: Iterable[str]) -> list[DesignRecord]:
    if population == "all":
        return list(records)
    if population == "gate_passing":
        ids = set(gate_pass_ids)
        return [r for r in records if r.design_id in ids]
    if population == "certified":
        return [r for r in records if r.certified is True]
    raise ValueError(f"unknown population {population!r} "
                     "(use all | gate_passing | certified)")


def evaluate_stop_metric(sm: StopMetric, records: Sequence[DesignRecord],
                         gate_pass_ids: Iterable[str],
                         spec: CampaignSpec) -> StopMetricResult:
    pool = _select(records, sm.of_population, gate_pass_ids)
    n_pop = len(pool)

    if sm.kind == "certified_designs":
        evaluable = [r for r in pool if r.reliability is not None]
        hit = [r for r in evaluable if r.certified is True]
        obs = float(len(hit))
        if not evaluable:
            return StopMetricResult(sm, Outcome.INDETERMINATE, 0.0, sm.target, sm.target,
                                    0, n_pop,
                                    "no design in the population carries any replication "
                                    "data — certification is unmeasured, not failed")
        out = Outcome.PASS if obs >= sm.target else Outcome.FAIL
        near = [r for r in evaluable
                if r.certified is False and r.runs_needed_to_certify_hint() is not None]
        return StopMetricResult(sm, out, obs, sm.target, max(0.0, sm.target - obs),
                                len(evaluable), n_pop,
                                f"{len(near)} more are on track and short only of runs")

    if sm.kind == "margin_coverage":
        per: dict[str, float] = {}
        worst = None
        n_eval = 0
        for off in sm.off_targets:
            got = 0
            for r in pool:
                m = r.margin_against(off)
                if m.is_measured and float(m.value) >= (sm.min_margin or 0.0):
                    got += 1
            per[off] = float(got)
            worst = got if worst is None else min(worst, got)
        n_eval = sum(1 for r in pool
                     if any(r.margin_against(o).is_measured for o in sm.off_targets))
        if n_eval == 0:
            return StopMetricResult(sm, Outcome.INDETERMINATE, 0.0, sm.target, sm.target,
                                    0, n_pop,
                                    f"no design has been folded against any of "
                                    f"{list(sm.off_targets)} — the panel has not run",
                                    per)
        obs = float(worst or 0)
        out = Outcome.PASS if obs >= sm.target else Outcome.FAIL
        lag = [o for o, v in per.items() if v < sm.target]
        return StopMetricResult(sm, out, obs, sm.target, max(0.0, sm.target - obs),
                                n_eval, n_pop,
                                f"binding off-target(s): {lag}" if lag else "", per)

    if sm.kind == "control_present":
        if sm.control == "cofactor_free":
            have = [r for r in pool if r.cofactor_control is not None
                    and r.cofactor_control.paired]
            n_eval = len(pool)
        else:
            have = [r for r in pool if sm.control in r.provenance.get("controls", [])]
            n_eval = len(pool)
        obs = float(len(have))
        if n_eval == 0:
            return StopMetricResult(sm, Outcome.INDETERMINATE, 0.0, sm.target, sm.target,
                                    0, n_pop, "population is empty")
        out = Outcome.PASS if obs >= sm.target else Outcome.FAIL
        return StopMetricResult(sm, out, obs, sm.target, max(0.0, sm.target - obs),
                                n_eval, n_pop,
                                f"control {sm.control!r} paired on {len(have)} of {n_eval}")

    if sm.kind == "block_coverage":
        if not pool:
            return StopMetricResult(sm, Outcome.INDETERMINATE, 0.0, sm.target, sm.target,
                                    0, n_pop, "population is empty — an all() over zero "
                                              "rows would have returned True")
        have = [r for r in pool if getattr(r, sm.block or "", None)]
        obs = len(have) / len(pool)
        out = Outcome.PASS if obs >= sm.target else Outcome.FAIL
        return StopMetricResult(sm, out, obs, sm.target,
                                max(0.0, (sm.target - obs) * len(pool)),
                                len(pool), n_pop,
                                f"{len(have)}/{len(pool)} carry the {sm.block!r} block")

    if sm.kind == "interval_width":
        evaluable = [r for r in pool if r.reliability and r.reliability.interval]
        if not evaluable:
            return StopMetricResult(sm, Outcome.INDETERMINATE, 0.0, sm.target, sm.target,
                                    0, n_pop, "no design has a reliability interval yet")
        w = sm.max_interval_width or 0.0
        hit = [r for r in evaluable if r.reliability.interval.width <= w]  # type: ignore[union-attr]
        obs = float(len(hit))
        out = Outcome.PASS if obs >= sm.target else Outcome.FAIL
        return StopMetricResult(sm, out, obs, sm.target, max(0.0, sm.target - obs),
                                len(evaluable), n_pop,
                                f"{len(hit)} of {len(evaluable)} intervals narrower than {w}")

    raise ValueError(f"unhandled stop metric kind {sm.kind!r}")


@dataclass(frozen=True)
class StopReport:
    results: tuple[StopMetricResult, ...]

    @property
    def all_met(self) -> Verdict:
        """A Verdict, never a bare bool — zero metrics is INDETERMINATE (D8)."""
        n = len(self.results)
        if n == 0:
            return Verdict(Outcome.INDETERMINATE, 0, 0, 0, 0,
                           "no stop metrics evaluated")
        n_true = sum(1 for r in self.results if r.met)
        n_ind = sum(1 for r in self.results if r.outcome is Outcome.INDETERMINATE)
        if n_ind:
            return Verdict(Outcome.INDETERMINATE, n, n_true, n, n_ind,
                           f"{n_ind} metric(s) indeterminate — not enough rows to decide")
        return Verdict(Outcome.PASS if n_true == n else Outcome.FAIL, n, n_true, n, 0,
                       f"{n_true}/{n} stop metrics met")

    @property
    def unmet(self) -> tuple[StopMetricResult, ...]:
        return tuple(r for r in self.results if not r.met)

    def total_shortfall(self) -> float:
        """A single scalar distance-to-done, used by the no-progress guard."""
        return sum(r.shortfall for r in self.results)

    def as_dict(self) -> dict[str, Any]:
        return {"results": [r.as_dict() for r in self.results],
                "all_met": self.all_met.as_dict(),
                "total_shortfall": self.total_shortfall()}

    def summary(self) -> str:
        return "\n".join(["  " + str(r) for r in self.results])


def evaluate_all(spec: CampaignSpec, records: Sequence[DesignRecord],
                 gate_pass_ids: Iterable[str]) -> StopReport:
    ids = list(gate_pass_ids)
    return StopReport(tuple(evaluate_stop_metric(sm, records, ids, spec)
                            for sm in spec.stop_metrics))
