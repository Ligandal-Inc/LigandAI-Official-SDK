# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""The auto-population loop.

Each iteration:

  1. aggregate every pose held so far into one record per design, grouping by
     RECEPTOR HASH and refusing to pool across constructs
  2. apply the spec's gates, reporting each metric's observed range and refusing
     a gate that removes nothing
  3. rank the gate-PASSING population — and only that population — with a length
     baseline attached
  4. evaluate the stop metrics, each carrying the row count that produced it
  5. ask the budget whether to continue at all
  6. plan: turn each unmet metric into costed work, saying which metric and why
  7. execute, charge the ledger, and write the iteration record to disk

It stops on: metrics met, credit cap, iteration cap, wall clock, no progress, or
nothing left to queue.  It cannot stop silently — every exit carries a reason.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from .aggregate import AggregationReport, build_records
from .gates import (GatePanelResult, GateStatus, Population, VacuousGateError,
                    apply_gates)
from .planner import (Action, ActionKind, BudgetLedger, IterationPlan, StopReason,
                      plan_iteration)
from .platform import GeneratedDesign, Platform, PoseResult
from .record import DesignRecord, write_csv, write_jsonl
from .scoring import LengthBaseline, RankingResult, rank
from .spec import CampaignSpec
from .stopmetrics import StopReport, evaluate_all
from .values import Measured, Outcome, Status


@dataclass
class IterationRecord:
    iteration: int
    started_at: float
    stop_report: StopReport
    gate_panel: GatePanelResult | None
    ranking: RankingResult | None
    plan: IterationPlan | None
    aggregation: AggregationReport
    spent_this_iteration: int
    n_records: int
    n_poses: int
    messages: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "iteration": self.iteration,
            "started_at": self.started_at,
            "n_records": self.n_records,
            "n_poses": self.n_poses,
            "aggregation": self.aggregation.as_dict(),
            "gates": self.gate_panel.as_dict() if self.gate_panel else None,
            "ranking": self.ranking.as_dict() if self.ranking else None,
            "stop_metrics": self.stop_report.as_dict(),
            "plan": self.plan.as_dict() if self.plan else None,
            "spent_this_iteration": self.spent_this_iteration,
            "messages": list(self.messages),
        }


@dataclass
class CampaignResult:
    spec: CampaignSpec
    records: list[DesignRecord]
    iterations: list[IterationRecord]
    ledger: BudgetLedger
    stop_reason: StopReason
    stop_detail: str
    output_dir: str

    @property
    def certified(self) -> list[DesignRecord]:
        return [r for r in self.records if r.certified is True]

    def as_dict(self) -> dict[str, Any]:
        return {
            "campaign": self.spec.name,
            "spec_fingerprint": self.spec.fingerprint(),
            "stop_reason": self.stop_reason.value,
            "stop_detail": self.stop_detail,
            "n_designs": len(self.records),
            "n_certified": len(self.certified),
            "budget": self.ledger.as_dict(),
            "iterations": [i.as_dict() for i in self.iterations],
            "output_dir": self.output_dir,
        }

    def summary(self) -> str:
        last = self.iterations[-1] if self.iterations else None
        lines = [
            f"campaign {self.spec.name}: STOPPED — {self.stop_reason.value}",
            f"  {self.stop_detail}",
            f"  {len(self.records)} designs, {len(self.certified)} certified at a "
            f"{self.spec.reliability.certification_bar} bar",
            f"  spent {self.ledger.spent:,} of {self.ledger.max_credits:,} credits "
            f"over {self.ledger.iterations} iteration(s), "
            f"{self.ledger.elapsed_hours:.2f}h",
        ]
        if last:
            lines.append("  stop metrics at exit:")
            lines.append(last.stop_report.summary())
        lines.append(f"  outputs: {self.output_dir}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------

class CampaignEngine:
    def __init__(self, spec: CampaignSpec, platform: Platform, *,
                 output_dir: str | None = None,
                 log: Callable[[str], None] | None = None,
                 strict_gates: bool = True):
        self.spec = spec
        self.platform = platform
        self.output_dir = os.path.abspath(output_dir or spec.output_dir)
        self.log = log or (lambda m: print(m, flush=True))
        self.strict_gates = strict_gates
        self.ledger = BudgetLedger.from_spec(spec)
        self.poses: list[PoseResult] = []
        self.design_meta: dict[str, dict[str, Any]] = {}
        self.developability: dict[str, dict[str, Any]] = {}
        self.affinity: dict[str, dict[str, Any]] = {}
        self.iterations: list[IterationRecord] = []
        self._last_records: list[DesignRecord] = []
        os.makedirs(self.output_dir, exist_ok=True)

    # -- seeding --------------------------------------------------------
    def seed_poses(self, poses: Sequence[PoseResult],
                   meta: Mapping[str, Mapping[str, Any]] | None = None) -> None:
        """Load work that already exists, so a campaign can be resumed or audited."""
        self.poses.extend(poses)
        for k, v in (meta or {}).items():
            self.design_meta.setdefault(k, {}).update(v)

    # -- one pass over the evidence ------------------------------------
    def assess(self) -> tuple[list[DesignRecord], AggregationReport,
                             GatePanelResult | None, RankingResult | None, StopReport]:
        records, agg = build_records(
            self.spec, self.poses, design_meta=self.design_meta,
            developability=self.developability, affinity=self.affinity)
        for w in agg.warnings:
            self.log(f"  ! {w}")

        rows = {r.design_id: _gate_row(r) for r in records}
        panel: GatePanelResult | None = None
        if rows:
            try:
                panel = apply_gates(rows, list(self.spec.gates),
                                    strict=self.strict_gates)
            except VacuousGateError as exc:
                # A gate that removes nothing is a spec defect, not a run failure.
                self.log(f"  ⛔ GATE REFUSED: {exc}")
                raise
            for r in records:
                for rep in panel.reports:
                    v = rows[r.design_id].get(rep.spec.metric)
                    r.gate_results[rep.spec.name] = (
                        "pass" if r.design_id in panel.population
                        else ("absent" if v is None else "fail"))

        ranking: RankingResult | None = None
        if panel and len(panel.population):
            ranking = self._rank(records, panel.population)

        gate_ids = list(panel.population.ids) if panel else []
        stop = evaluate_all(self.spec, records, gate_ids)
        self._last_records = records
        return records, agg, panel, ranking, stop

    def _rank(self, records: Sequence[DesignRecord],
              population: Population) -> RankingResult | None:
        rs = self.spec.ranking
        by_id = {r.design_id: r for r in records}
        scores: dict[str, Measured] = {}
        lengths: dict[str, int] = {}
        for did in population.ids:
            r = by_id[did]
            lengths[did] = r.length
            scores[did] = _score_field(r, rs.score)
        n_measured = sum(1 for m in scores.values() if m.is_measured)
        if n_measured < 2:
            self.log(f"  ranking on {rs.score!r}: only {n_measured} of "
                     f"{len(population)} designs in {population.name!r} carry it — "
                     f"absent, not zero; no ordering emitted this iteration")
            return None
        try:
            res = rank(f"{self.spec.name}:{rs.score}", population, scores, lengths,
                       higher_is_better=rs.higher_is_better,
                       require_full_coverage=rs.require_full_coverage,
                       length_confound_threshold=rs.length_confound_threshold)
        except Exception as exc:                              # noqa: BLE001
            self.log(f"  ranking refused: {exc}")
            return None
        self.log(f"  ranking {res.name}: {len(res.ordered_ids)} designs, "
                 f"coverage {res.coverage:.2f}; {res.length_baseline}")
        for n in res.notes:
            self.log(f"    ! {n}")
        return res

    # -- execution ------------------------------------------------------
    def _execute(self, actions: Sequence[Action]) -> int:
        spent = 0
        for a in actions:
            self.log(f"  RUN  {a}")
            try:
                spent += self._execute_one(a)
            except Exception as exc:                          # noqa: BLE001
                self.log(f"    action FAILED ({type(exc).__name__}): {exc}")
        return spent

    def _execute_one(self, a: Action) -> int:
        spec = self.spec
        f = spec.fold
        if a.kind is ActionKind.GENERATE:
            pc = next(p for p in spec.peptide_classes if p.name == a.peptide_class)
            designs = self.platform.generate(spec, pc, a.n_designs)
            for d in designs:
                self.design_meta.setdefault(d.design_id, {}).update(
                    {"topology": d.topology, "molecule_class": d.molecule_class,
                     "peptide_class": d.peptide_class, **d.provenance})
            self.ledger.charge(f.credits_per_generation, f"generate {pc.name}")
            if designs:
                poses = self.platform.fold(
                    spec, [d.sequence for d in designs],
                    construct=spec.target.construct,
                    cofactors=[c.name for c in spec.target.cofactors],
                    n_seed_units=a.n_runs_each, label=f"gen:{pc.name}")
                self.poses.extend(poses)
                cost = len(designs) * max(1, a.n_runs_each) * f.credits_per_fold
                self.ledger.charge(cost, f"fold new {pc.name}")
                return f.credits_per_generation + cost
            return f.credits_per_generation

        if a.kind is ActionKind.REPLICATE:
            seqs = self._seqs(a.design_ids)
            poses = self.platform.fold(
                spec, seqs, construct=spec.target.construct,
                cofactors=[c.name for c in spec.target.cofactors],
                n_seed_units=a.n_runs_each, label=f"rep:i{self.ledger.iterations}")
            self.poses.extend(poses)
            cost = len(seqs) * max(1, a.n_runs_each) * f.credits_per_fold
            self.ledger.charge(cost, "replicate")
            return cost

        if a.kind is ActionKind.PARALOG_PANEL:
            off = next(o for o in spec.off_targets if o.gene == a.off_target)
            seqs = self._seqs(a.design_ids)
            poses = self.platform.fold(
                spec, seqs, construct=off.construct,
                cofactors=[c.name for c in (off.cofactors or spec.target.cofactors)],
                n_seed_units=a.n_runs_each, label=f"panel:{off.gene}")
            self.poses.extend(poses)
            cost = len(seqs) * max(1, a.n_runs_each) * f.credits_per_fold
            self.ledger.charge(cost, f"paralog panel {off.gene}")
            return cost

        if a.kind is ActionKind.COFACTOR_CONTROL:
            seqs = self._seqs(a.design_ids)
            poses = self.platform.fold(
                spec, seqs, construct=spec.target.construct, cofactors=[],
                n_seed_units=a.n_runs_each, label="cofactor_free")
            self.poses.extend(poses)
            cost = len(seqs) * max(1, a.n_runs_each) * f.credits_per_fold
            self.ledger.charge(cost, "cofactor-free control")
            return cost

        if a.kind is ActionKind.DEVELOPABILITY:
            seqs = self._seqs(a.design_ids)
            self.developability.update(self.platform.score_developability(spec, seqs))
            return 0

        if a.kind is ActionKind.AFFINITY:
            ids = set(a.design_ids)
            poses = [p for p in self.poses if p.design_id in ids]
            self.affinity.update(self.platform.score_affinity(spec, poses))
            return 0

        if a.kind is ActionKind.CONSTRUCT_COMPARISON:
            if not spec.target.comparison_constructs:
                return 0
            c = spec.target.comparison_constructs[0]
            seqs = self._seqs(a.design_ids)
            poses = self.platform.fold(
                spec, seqs, construct=c,
                cofactors=[cc.name for cc in spec.target.cofactors],
                n_seed_units=1, label=f"construct:{c.name}")
            self.poses.extend(poses)
            cost = len(seqs) * f.credits_per_fold
            self.ledger.charge(cost, f"construct comparison {c.name}")
            return cost

        if a.kind is ActionKind.CONTROL_ARM:
            controls = self._build_control(a.peptide_class or "scramble", a.n_designs)
            if not controls:
                return 0
            for did, seq in controls.items():
                self.design_meta.setdefault(did, {}).update(
                    {"topology": "linear", "molecule_class": "linear",
                     "controls": [a.peptide_class]})
            poses = self.platform.fold(
                spec, list(controls.values()), construct=spec.target.construct,
                cofactors=[c.name for c in spec.target.cofactors],
                n_seed_units=1, label=f"control:{a.peptide_class}")
            self.poses.extend(poses)
            cost = len(controls) * f.credits_per_fold
            self.ledger.charge(cost, f"control arm {a.peptide_class}")
            return cost
        return 0

    def _seqs(self, ids: Sequence[str]) -> list[str]:
        by = {p.design_id: p.sequence for p in self.poses}
        return [by[i] for i in ids if i in by]

    def _build_control(self, kind: str, n: int) -> dict[str, str]:
        """Controls derived from the REAL designs.  Never invented sequences."""
        import random
        from .identity import sha256_seq

        seen: dict[str, str] = {}
        pool = sorted({p.sequence for p in self.poses})
        if not pool:
            return {}
        rng = random.Random(0xC0FFEE)
        for seq in pool[:n]:
            if kind == "scramble":
                chars = list(seq)
                rng.shuffle(chars)
                s = "".join(chars)
            elif kind == "length_matched_random":
                alphabet = "ACDEFGHIKLMNPQRSTVWY"
                s = "".join(rng.choice(alphabet) for _ in range(len(seq)))
            else:
                continue
            seen[sha256_seq(s)[:12]] = s
        return seen

    # -- the loop -------------------------------------------------------
    def run(self, *, dry_run: bool = False) -> CampaignResult:
        spec = self.spec
        self.log(f"campaign {spec.name} (spec {spec.fingerprint()[:12]}) "
                 f"target {spec.target.gene}/{spec.target.construct.name}")
        for w in spec.validate():
            self.log(f"  ! spec warning: {w}")
        self.log(f"  budget {spec.budget.max_credits:,} credits, "
                 f"<= {spec.budget.max_iterations} iterations, "
                 f"<= {spec.budget.max_wall_clock_hours}h; no-progress after "
                 f"{spec.budget.no_progress.patience_iterations} iterations closing "
                 f"< {spec.budget.no_progress.min_absolute_progress:g}")

        stop_reason = StopReason.ITERATION_CAP
        stop_detail = "loop did not execute"

        while True:
            t0 = time.time()
            records, agg, panel, ranking, stop = self.assess()
            self.ledger.record_shortfall(stop.total_shortfall())

            self.log(f"iteration {self.ledger.iterations}: {len(records)} designs, "
                     f"{len(self.poses)} poses"
                     + (f", {len(panel.population)} pass the gates" if panel else ""))
            if panel:
                for rep in panel.reports:
                    self.log(f"  gate {rep}")
            self.log("  stop metrics:")
            self.log(stop.summary())

            verdict = self.ledger.check(stop)
            if verdict:
                stop_reason, stop_detail = verdict
                self.iterations.append(IterationRecord(
                    self.ledger.iterations, t0, stop, panel, ranking, None, agg,
                    0, len(records), len(self.poses),
                    [f"stopped: {stop_reason.value} — {stop_detail}"]))
                break

            actions, rejected = plan_iteration(
                spec, records, list(panel.population.ids) if panel else [],
                stop, self.ledger)
            allowance = self.ledger.iteration_allowance()
            plan = IterationPlan(self.ledger.iterations, tuple(actions), stop, allowance,
                                 truncated=bool(actions) and
                                 sum(a.estimated_credits for a in actions) >= allowance,
                                 rejected=tuple(rejected))
            self.log(plan.summary())

            if not actions:
                stop_reason = StopReason.NOTHING_TO_QUEUE
                stop_detail = ("no action could close any unmet stop metric within the "
                               f"iteration allowance of {allowance:,} credits"
                               + (" — rejected: " + "; ".join(rejected) if rejected else ""))
                self.iterations.append(IterationRecord(
                    self.ledger.iterations, t0, stop, panel, ranking, plan, agg,
                    0, len(records), len(self.poses),
                    [f"stopped: {stop_reason.value} — {stop_detail}"]))
                break

            spent = 0
            if dry_run:
                self.log("  (dry run — nothing submitted, nothing charged)")
            else:
                spent = self._execute(actions)

            self.iterations.append(IterationRecord(
                self.ledger.iterations, t0, stop, panel, ranking, plan, agg,
                spent, len(records), len(self.poses)))
            self._write_iteration(self.iterations[-1])
            self.ledger.iterations += 1

            if dry_run:
                stop_reason = StopReason.NOTHING_TO_QUEUE
                stop_detail = "dry run: plan printed, nothing executed"
                break

        records, _agg, _panel, _rank, _stop = self.assess()
        result = CampaignResult(spec, records, self.iterations, self.ledger,
                                stop_reason, stop_detail, self.output_dir)
        if self.iterations:
            self._write_status(self.iterations[-1],
                               final=f"{stop_reason.value} — {stop_detail}")
        self._write_outputs(result)
        self.log(result.summary())
        return result

    # -- outputs --------------------------------------------------------
    def _write_iteration(self, it: IterationRecord) -> None:
        d = os.path.join(self.output_dir, "iterations")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"iteration_{it.iteration:03d}.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(it.as_dict(), fh, indent=2, default=str)
        self._write_status(it)

    def _write_status(self, it: IterationRecord, *, final: str = "") -> None:
        """A single small file an unattended watcher can poll.

        Written after every iteration so progress is visible without reading a
        log, and so a supervisor can decide whether to notify a human.
        """
        met = sum(1 for r in it.stop_report.results if r.met)
        total = len(it.stop_report.results)
        status = {
            "campaign": self.spec.name,
            "spec_fingerprint": self.spec.fingerprint(),
            "iteration": it.iteration,
            "updated_at": time.time(),
            "running": not final,
            "final": final or None,
            "n_designs": it.n_records,
            "n_poses": it.n_poses,
            "n_certified": sum(1 for r in self._last_records
                               if r.certified is True),
            "stop_metrics_met": met,
            "stop_metrics_total": total,
            "total_shortfall": it.stop_report.total_shortfall(),
            "credits_spent": self.ledger.spent,
            "credits_cap": self.ledger.max_credits,
            "elapsed_hours": round(self.ledger.elapsed_hours, 3),
            "unmet": [{"id": r.metric.id, "kind": r.metric.kind,
                       "observed": r.observed, "target": r.target,
                       "outcome": r.outcome.value, "n_rows": r.n_rows,
                       "detail": r.detail}
                      for r in it.stop_report.unmet],
            "queued": [a.as_dict() for a in (it.plan.actions if it.plan else ())],
            "rejected": list(it.plan.rejected) if it.plan else [],
        }
        with open(os.path.join(self.output_dir, "STATUS.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(status, fh, indent=2, default=str)
        lines = [
            f"# {self.spec.name} — iteration {it.iteration}",
            "",
            f"- state: **{'stopped: ' + final if final else 'running'}**",
            f"- stop metrics met: **{met} of {total}**",
            f"- designs: {it.n_records} ({status['n_certified']} certified), "
            f"poses: {it.n_poses}",
            f"- credits: {self.ledger.spent:,} of {self.ledger.max_credits:,} "
            f"({self.ledger.elapsed_hours:.2f}h elapsed)",
            "",
            "## Unmet",
        ]
        lines += [f"- {r}" for r in it.stop_report.unmet] or ["- (none)"]
        lines += ["", "## Queued this iteration"]
        lines += [f"- {a}" for a in (it.plan.actions if it.plan else ())] or ["- (none)"]
        if it.plan and it.plan.rejected:
            lines += ["", "## Not queued"] + [f"- {r}" for r in it.plan.rejected]
        with open(os.path.join(self.output_dir, "STATUS.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    def _write_outputs(self, result: CampaignResult) -> None:
        o = self.output_dir
        with open(os.path.join(o, "campaign_spec.resolved.json"), "w", encoding="utf-8") as fh:
            json.dump(self.spec.as_dict(), fh, indent=2, default=str)
        with open(os.path.join(o, "campaign_result.json"), "w", encoding="utf-8") as fh:
            json.dump(result.as_dict(), fh, indent=2, default=str)
        write_csv(result.records, os.path.join(o, "designs_all_scores.csv"))
        write_jsonl(result.records, os.path.join(o, "designs_all_scores.jsonl"))
        cert = result.certified
        if cert:
            write_csv(cert, os.path.join(o, "designs_certified.csv"))
        self.log(f"  wrote {len(result.records)} records "
                 f"({len(cert)} certified) to {o}")


def _gate_row(r: DesignRecord) -> dict[str, Any]:
    """Flat metric view a gate can read.  Unmeasured stays None, never 0."""
    i = r.interface
    row: dict[str, Any] = {
        "peptide_ipsae_median": i.peptide_ipsae_median,
        "peptide_ipsae_best": i.peptide_ipsae_best,
        "peptide_ipsae_worst": i.peptide_ipsae_worst,
        "iptm_median": i.iptm_median,
        "complex_plddt_median": i.complex_plddt_median,
        "peptide_plddt_median": i.peptide_plddt_median,
        "peptide_interface_plddt_median": i.peptide_interface_plddt_median,
        "length": r.length,
        "n_poses": i.n_poses,
        "n_seed_units": i.n_independent_seed_units,
    }
    if r.reliability and r.reliability.interval:
        row["fold_failure_rate"] = r.reliability.interval.point
        row["fold_failure_upper"] = r.reliability.interval.upper
    if r.affinity:
        row["deltaforge_predicted_kd_nm"] = r.affinity.predicted_kd_nm
    for s in r.selectivity:
        row[f"margin_vs_{s.off_target}"] = s.margin
    return row


def _score_field(r: DesignRecord, name: str) -> Measured:
    row = _gate_row(r)
    v = row.get(name)
    if isinstance(v, Measured):
        return v
    if v is None:
        return Measured.absent(f"{name} not present on this design")
    return Measured(float(v), Status.MEASURED, n=1)
