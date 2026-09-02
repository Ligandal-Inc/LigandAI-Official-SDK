# Copyright © 2025 Ligandal, Inc. All rights reserved.
"""Gates, and the Population a gate produces.

D1  A gate whose threshold sits outside the metric's observed range is not a
    gate.  In the ENPP3 campaign ``complex_plddt >= 88`` removed 1 design of
    2,997 — it looked like a quality bar and was decoration.  Here a gate cannot
    be evaluated without also computing the metric's observed range, and a gate
    that removes nothing is reported as VACUOUS and, by default, refused.

D2  A ranking computed on the wrong population.  DeltaForge was run on 679
    designs the interface gate had REJECTED (ipSAE median 0.379) while skipping
    415 that passed.  The fix is typed: gating returns a :class:`Population`
    with an identity set and a provenance chain, and the ranker in
    ``scoring.py`` will only rank a Population — never a bare table.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, Sequence

from .values import Measured, Outcome, Status, Verdict

# ---------------------------------------------------------------------------
# Population
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Population:
    """A named, immutable set of design ids with the provenance that made it.

    ``provenance`` is the ordered list of operations that produced this set,
    starting from the source cohort.  Anything that consumes a Population can
    print exactly which rows it is operating on and why.
    """

    name: str
    ids: frozenset[str]
    provenance: tuple[str, ...] = ()
    parent: str | None = None

    def __len__(self) -> int:
        return len(self.ids)

    def __contains__(self, design_id: object) -> bool:
        return design_id in self.ids

    def restrict(self, keep: Iterable[str], *, name: str, why: str) -> "Population":
        keep_set = frozenset(keep) & self.ids
        return Population(name, keep_set, self.provenance + (why,), parent=self.name)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "n": len(self.ids),
            "parent": self.parent,
            "provenance": list(self.provenance),
        }


class WrongPopulationError(RuntimeError):
    """Raised when an operation is applied to rows outside its declared population."""


def assert_population(
    ids: Iterable[str],
    population: Population,
    *,
    operation: str,
    require_full_coverage: bool = True,
) -> float:
    """Check that ``ids`` lie inside ``population`` and cover it.

    Returns the coverage fraction.  Raises if any id is OUTSIDE the population
    (that is the DeltaForge defect), and — unless waived — if coverage is short
    of 1.0 (that is the other half of it: 415 passing designs never scored).
    """
    id_set = set(ids)
    stray = id_set - population.ids
    if stray:
        sample = ", ".join(sorted(stray)[:5])
        raise WrongPopulationError(
            f"{operation}: {len(stray)} of {len(id_set)} rows are NOT in population "
            f"{population.name!r} (n={len(population)}). Examples: {sample}. "
            f"An ordering must be computed on, and only on, the population it "
            f"exists to order."
        )
    covered = len(id_set & population.ids)
    coverage = covered / len(population) if len(population) else 0.0
    if require_full_coverage and coverage < 1.0:
        missing = len(population) - covered
        raise WrongPopulationError(
            f"{operation}: covers {covered}/{len(population)} of population "
            f"{population.name!r} — {missing} rows in the population were never "
            f"scored. Pass require_full_coverage=False only with a recorded reason."
        )
    return coverage


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------

_OPS: dict[str, Callable[[float, float], bool]] = {
    ">=": lambda v, t: v >= t,
    ">": lambda v, t: v > t,
    "<=": lambda v, t: v <= t,
    "<": lambda v, t: v < t,
    "==": lambda v, t: v == t,
    "!=": lambda v, t: v != t,
}


class GateStatus(str, Enum):
    ACTIVE = "active"            # removed a meaningful share of rows
    VACUOUS = "vacuous"          # removed NOTHING — not a gate
    DECORATIVE = "decorative"    # removed a token number of rows — the ENPP3 case:
                                 # complex_plddt >= 88 removed 1 design of 2,997
    ANNIHILATING = "annihilating"  # removed everything — the bar is off-scale high
    UNMEASURABLE = "unmeasurable"  # no row carried the metric at all


@dataclass(frozen=True)
class GateSpec:
    """One threshold on one metric."""

    metric: str
    op: str
    threshold: float
    label: str | None = None
    # A gate must remove at least this share of the rows that reach it to count
    # as a gate.  Below it the gate is DECORATIVE and is refused in strict mode.
    # 0.01 = the ENPP3 complex_plddt>=88 gate (1/2997 = 0.03%) is refused.
    min_effect_fraction: float = 0.01
    # Set only with a written reason; a vacuous/decorative gate then warns.
    allow_vacuous: bool = False
    allow_vacuous_reason: str | None = None
    # Rows missing the metric: 'reject' (default, conservative) or 'pass' or 'drop'
    on_missing: str = "reject"

    def __post_init__(self) -> None:
        if self.op not in _OPS:
            raise ValueError(f"unknown gate operator {self.op!r}; use one of {sorted(_OPS)}")
        if self.on_missing not in ("reject", "pass", "drop"):
            raise ValueError("on_missing must be one of reject|pass|drop")
        if not 0.0 <= self.min_effect_fraction < 1.0:
            raise ValueError("min_effect_fraction must be in [0, 1)")
        if self.allow_vacuous and not self.allow_vacuous_reason:
            raise ValueError(
                f"gate {self.name!r}: allow_vacuous requires allow_vacuous_reason — "
                "a gate that removes nothing (or almost nothing) must be justified "
                "in writing, not left to be read as a quality bar"
            )

    @property
    def name(self) -> str:
        return self.label or f"{self.metric}{self.op}{self.threshold}"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GateReport:
    """What a gate actually did, including the metric's observed range.

    There is no way to get a pass/fail set out of :func:`apply_gate` without this
    report, which is the structural half of D1.
    """

    spec: GateSpec
    status: GateStatus
    n_evaluated: int
    n_measured: int
    n_missing: int
    n_pass: int
    n_removed: int
    observed_min: float | None
    observed_max: float | None
    observed_median: float | None
    observed_p05: float | None
    observed_p95: float | None
    threshold_percentile: float | None   # where the threshold sits in the observed distribution
    message: str = ""

    @property
    def removed_fraction(self) -> float | None:
        return None if self.n_evaluated == 0 else self.n_removed / self.n_evaluated

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["spec"] = self.spec.as_dict()
        d["status"] = self.status.value
        d["removed_fraction"] = self.removed_fraction
        return d

    def __str__(self) -> str:
        rng = ("no measured values" if self.observed_min is None
               else f"observed [{self.observed_min:.4g}, {self.observed_max:.4g}] "
                    f"median {self.observed_median:.4g}")
        return (f"{self.spec.name}: {self.status.value.upper()} — "
                f"{self.n_pass}/{self.n_evaluated} pass, {self.n_removed} removed; {rng}"
                + (f"; {self.message}" if self.message else ""))


class VacuousGateError(RuntimeError):
    """A declared gate removed nothing, so it is not a gate."""


def _percentile(sorted_vals: Sequence[float], q: float) -> float:
    if not sorted_vals:
        raise ValueError("empty")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def _value_of(row: Mapping[str, Any], metric: str) -> float | None:
    v = row.get(metric)
    if isinstance(v, Measured):
        return float(v.value) if v.is_measured else None  # type: ignore[arg-type]
    if v is None:
        return None
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v) if not (isinstance(v, float) and math.isnan(v)) else None
    return None


def apply_gate(
    rows: Mapping[str, Mapping[str, Any]],
    spec: GateSpec,
    *,
    strict: bool = True,
) -> tuple[set[str], GateReport]:
    """Apply one gate to ``{design_id: row}``.

    Always computes the metric's observed range.  ``strict`` (the default) raises
    :class:`VacuousGateError` when the gate removes nothing and the spec has not
    declared ``allow_vacuous`` with a reason.
    """
    op = _OPS[spec.op]
    passing: set[str] = set()
    values: list[float] = []
    n_missing = 0
    n_dropped = 0

    for did, row in rows.items():
        v = _value_of(row, spec.metric)
        if v is None:
            n_missing += 1
            if spec.on_missing == "pass":
                passing.add(did)
            elif spec.on_missing == "drop":
                n_dropped += 1
            continue
        values.append(v)
        if op(v, spec.threshold):
            passing.add(did)

    n_evaluated = len(rows) - n_dropped
    n_measured = len(values)
    n_pass = len(passing)
    n_removed = n_evaluated - n_pass

    if n_measured:
        sv = sorted(values)
        omin, omax = sv[0], sv[-1]
        omed = _percentile(sv, 0.5)
        p05, p95 = _percentile(sv, 0.05), _percentile(sv, 0.95)
        below = sum(1 for x in sv if x < spec.threshold)
        tpct = below / n_measured
    else:
        omin = omax = omed = p05 = p95 = tpct = None

    msg = ""
    if n_measured == 0:
        status = GateStatus.UNMEASURABLE
        msg = (f"no row carried metric {spec.metric!r}; "
               f"{n_missing} missing of {len(rows)}")
    elif n_removed == 0:
        status = GateStatus.VACUOUS
        msg = (f"threshold {spec.threshold:g} removes nothing — the metric's "
               f"observed range is [{omin:.4g}, {omax:.4g}]. This is not a gate.")
    elif n_evaluated and (n_removed / n_evaluated) < spec.min_effect_fraction and n_pass > 0:
        status = GateStatus.DECORATIVE
        msg = (f"threshold {spec.threshold:g} removes only {n_removed} of "
               f"{n_evaluated} rows ({n_removed / n_evaluated:.3%}, below "
               f"min_effect_fraction={spec.min_effect_fraction:.3%}) — the metric's "
               f"observed range is [{omin:.4g}, {omax:.4g}] with median {omed:.4g}. "
               f"This reads as a quality bar and is decoration.")
    elif n_pass == 0:
        status = GateStatus.ANNIHILATING
        msg = (f"threshold {spec.threshold:g} removes every row — the metric's "
               f"observed range is [{omin:.4g}, {omax:.4g}]. Nothing can pass.")
    else:
        status = GateStatus.ACTIVE

    report = GateReport(spec, status, n_evaluated, n_measured, n_missing, n_pass,
                        n_removed, omin, omax, omed, p05, p95, tpct, msg)

    if strict and status in (GateStatus.VACUOUS, GateStatus.DECORATIVE) and not spec.allow_vacuous:
        raise VacuousGateError(str(report))
    if strict and status is GateStatus.UNMEASURABLE:
        raise VacuousGateError(str(report))
    return passing, report


@dataclass(frozen=True)
class GatePanelResult:
    population: Population
    reports: tuple[GateReport, ...]
    n_input: int

    @property
    def vacuous(self) -> tuple[GateReport, ...]:
        return tuple(r for r in self.reports
                     if r.status in (GateStatus.VACUOUS, GateStatus.DECORATIVE))

    @property
    def annihilating(self) -> tuple[GateReport, ...]:
        return tuple(r for r in self.reports if r.status is GateStatus.ANNIHILATING)

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_input": self.n_input,
            "population": self.population.as_dict(),
            "gates": [r.as_dict() for r in self.reports],
            "n_vacuous": len(self.vacuous),
            "n_annihilating": len(self.annihilating),
        }

    def summary(self) -> str:
        lines = [f"gate panel: {self.n_input} in -> {len(self.population)} out"]
        lines += ["  " + str(r) for r in self.reports]
        return "\n".join(lines)


def apply_gates(
    rows: Mapping[str, Mapping[str, Any]],
    specs: Sequence[GateSpec],
    *,
    population_name: str = "gate_passing",
    strict: bool = True,
) -> GatePanelResult:
    """Apply gates in sequence, each measured on the rows that reached it.

    Each gate's observed range is computed on its own INPUT — a gate that looks
    active on the raw cohort may be vacuous once earlier gates have run, and that
    is exactly the case worth knowing about.
    """
    current: dict[str, Mapping[str, Any]] = dict(rows)
    reports: list[GateReport] = []
    prov: list[str] = []
    for spec in specs:
        keep, rep = apply_gate(current, spec, strict=strict)
        reports.append(rep)
        prov.append(f"{spec.name} [{rep.status.value}] {rep.n_pass}/{rep.n_evaluated}")
        current = {k: v for k, v in current.items() if k in keep}
    pop = Population(population_name, frozenset(current), tuple(prov), parent="cohort")
    return GatePanelResult(pop, tuple(reports), len(rows))
