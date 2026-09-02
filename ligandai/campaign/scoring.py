# Copyright © 2025 Ligandal, Inc. All rights reserved.
"""Scorers, composites and rankings — and the four defects they exist to prevent.

D3  A scorer term that cannot apply to the molecule.  The N-end rule was folded
    into a developability composite for macrocycles, where there is no N
    terminus: it emitted ONE constant for 851 designs and dragged every one of
    them to D/F.  Here a term declares what it applies to; an inapplicable term
    contributes NOT_APPLICABLE (never a constant), and a term that turns out to
    be constant across the cohort is REFUSED entry to a composite.

D4  A metric that orders backwards.  A half-life score ranked negatively against
    measured data on every stratum.  Here a scorer must declare its direction AND
    carry a DirectionEvidence record; an unvalidated scorer is labelled as such
    and, by default, may not enter a composite.  If the evidence's Spearman sign
    contradicts the declared direction, that is a hard error.

D5  A learned scorer reported on a random split.  R^2 0.92 random / -1.95
    clustered.  A learned scorer must carry a SplitEvidence whose type is
    clustered/grouped/temporal; a random split cannot be reported as performance.

D6  Length confounding.  Interface score vs peptide length, rho -0.37.  There is
    no way to obtain a ranking from this module without its length baseline: the
    baseline is a field of the RankingResult.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, Sequence

from .values import Measured, Status
from .gates import Population, assert_population


# ---------------------------------------------------------------------------
# Rank correlation with a confidence interval (no scipy dependency required)
# ---------------------------------------------------------------------------

def _rankdata(xs: Sequence[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


@dataclass(frozen=True)
class Correlation:
    rho: float | None
    n: int
    ci_low: float | None
    ci_high: float | None
    method: str = "spearman"
    note: str = ""

    @property
    def excludes_zero(self) -> bool | None:
        if self.ci_low is None or self.ci_high is None:
            return None
        return (self.ci_low > 0) or (self.ci_high < 0)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["excludes_zero"] = self.excludes_zero
        return d


def spearman(x: Sequence[float], y: Sequence[float], confidence: float = 0.95) -> Correlation:
    """Spearman rho with a Fisher-z interval.  n < 4 yields rho but no interval."""
    pairs = [(a, b) for a, b in zip(x, y) if a is not None and b is not None]
    n = len(pairs)
    if n < 3:
        return Correlation(None, n, None, None, note="fewer than 3 paired observations")
    xr = _rankdata([p[0] for p in pairs])
    yr = _rankdata([p[1] for p in pairs])
    mx, my = statistics.fmean(xr), statistics.fmean(yr)
    num = sum((a - mx) * (b - my) for a, b in zip(xr, yr))
    dx = math.sqrt(sum((a - mx) ** 2 for a in xr))
    dy = math.sqrt(sum((b - my) ** 2 for b in yr))
    if dx == 0 or dy == 0:
        return Correlation(None, n, None, None, note="zero variance in one variable")
    rho = num / (dx * dy)
    if n < 4:
        return Correlation(rho, n, None, None, note="n<4: no interval")
    rho_c = max(-0.999999, min(0.999999, rho))
    z = 0.5 * math.log((1 + rho_c) / (1 - rho_c))
    se = 1.0 / math.sqrt(n - 3)
    from .values import _z_for  # noqa: PLC0415
    crit = _z_for(confidence) * se
    lo, hi = math.tanh(z - crit), math.tanh(z + crit)
    return Correlation(rho, n, lo, hi)


# ---------------------------------------------------------------------------
# D4 / D5 — direction and split evidence
# ---------------------------------------------------------------------------

class Direction(str, Enum):
    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER = "lower_is_better"


class SplitType(str, Enum):
    RANDOM = "random"          # NOT reportable as performance
    CLUSTERED = "clustered"    # sequence/structure identity-clustered
    GROUPED = "grouped"        # grouped by target / family / gene
    TEMPORAL = "temporal"
    LEAVE_ONE_TARGET_OUT = "leave_one_target_out"


@dataclass(frozen=True)
class SplitEvidence:
    """How a learned scorer's held-out performance was measured (D5)."""

    split_type: SplitType
    metric_name: str            # 'r2', 'spearman', 'auroc'
    value: float
    n_test: int
    cluster_identity_threshold: float | None = None
    group_key: str | None = None
    source: str = ""

    @property
    def reportable(self) -> bool:
        return self.split_type is not SplitType.RANDOM

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["split_type"] = self.split_type.value
        d["reportable"] = self.reportable
        return d


@dataclass(frozen=True)
class DirectionEvidence:
    """Evidence that a scorer orders the way it claims to (D4)."""

    validated_against: str        # the MEASURED dataset it was checked on
    correlation: Correlation
    strata: tuple[str, ...] = ()  # strata on which the sign held
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["correlation"] = self.correlation.as_dict()
        return d


class DirectionInvertedError(RuntimeError):
    """A scorer's validation evidence contradicts its declared direction."""


class UnvalidatedScorerError(RuntimeError):
    """A scorer without direction evidence was used where evidence is required."""


class ConstantTermError(RuntimeError):
    """A term is constant across the cohort and cannot contribute to an ordering."""


class OptimisticSplitError(RuntimeError):
    """A learned scorer's only performance evidence comes from a random split."""


@dataclass
class Scorer:
    """One named term.

    ``applies_to`` is the set of molecule classes the term is defined for
    (e.g. {"linear"}).  ``class_of`` on the cohort decides per row; a row outside
    ``applies_to`` receives NOT_APPLICABLE, never a filled-in constant.
    """

    name: str
    direction: Direction
    fn: Callable[[Mapping[str, Any]], float | None] | None = None
    field_name: str | None = None
    applies_to: frozenset[str] | None = None      # None = applies to everything
    is_learned: bool = False
    direction_evidence: DirectionEvidence | None = None
    split_evidence: SplitEvidence | None = None
    units: str | None = None
    method: str = ""

    def __post_init__(self) -> None:
        if self.fn is None and self.field_name is None:
            raise ValueError(f"scorer {self.name!r} needs fn or field_name")
        if self.direction_evidence is not None:
            rho = self.direction_evidence.correlation.rho
            if rho is not None and abs(rho) > 1e-9:
                expect_positive = self.direction is Direction.HIGHER_IS_BETTER
                if (rho > 0) != expect_positive:
                    raise DirectionInvertedError(
                        f"scorer {self.name!r} declares {self.direction.value} but its "
                        f"validation against {self.direction_evidence.validated_against!r} "
                        f"gives Spearman {rho:+.3f} (n={self.direction_evidence.correlation.n}). "
                        f"The metric orders BACKWARDS. Fix the sign or withdraw the scorer."
                    )

    @property
    def validated(self) -> bool:
        return self.direction_evidence is not None

    def value_for(self, row: Mapping[str, Any], *, molecule_class: str | None = None) -> Measured:
        if self.applies_to is not None and molecule_class is not None \
                and molecule_class not in self.applies_to:
            return Measured.not_applicable(
                f"{self.name} is defined for {sorted(self.applies_to)}, "
                f"molecule class is {molecule_class!r}"
            )
        raw = self.fn(row) if self.fn is not None else row.get(self.field_name)  # type: ignore[arg-type]
        if isinstance(raw, Measured):
            return raw
        if raw is None:
            return Measured.absent(f"{self.name} not computed")
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return Measured.failed(f"{self.name} returned non-numeric {raw!r}")
        if math.isnan(v) or math.isinf(v):
            return Measured.failed(f"{self.name} returned {v}")
        return Measured(v, Status.MEASURED, units=self.units, method=self.method or self.name)

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "direction": self.direction.value,
            "is_learned": self.is_learned,
            "validated": self.validated,
            "applies_to": sorted(self.applies_to) if self.applies_to else None,
            "direction_evidence": self.direction_evidence.as_dict() if self.direction_evidence else None,
            "split_evidence": self.split_evidence.as_dict() if self.split_evidence else None,
            "validation_status": (
                "validated" if self.validated else "UNVALIDATED — direction not checked "
                "against measured data"
            ),
        }


@dataclass(frozen=True)
class TermAudit:
    """What one term actually did across the cohort (D3)."""

    name: str
    n_rows: int
    n_measured: int
    n_not_applicable: int
    n_absent: int
    n_failed: int
    n_distinct: int
    variance: float | None
    is_constant: bool
    admitted: bool
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CompositeResult:
    scores: dict[str, Measured]
    term_audits: tuple[TermAudit, ...]
    admitted_terms: tuple[str, ...]
    excluded_terms: tuple[str, ...]
    coverage: dict[str, float]     # design_id -> fraction of admitted terms measured

    def as_dict(self) -> dict[str, Any]:
        return {
            "admitted_terms": list(self.admitted_terms),
            "excluded_terms": list(self.excluded_terms),
            "term_audits": [t.as_dict() for t in self.term_audits],
            "n_scored": sum(1 for m in self.scores.values() if m.is_measured),
            "n_rows": len(self.scores),
        }


def composite_score(
    rows: Mapping[str, Mapping[str, Any]],
    scorers: Sequence[Scorer],
    weights: Mapping[str, float] | None = None,
    *,
    molecule_class_of: Callable[[Mapping[str, Any]], str] | None = None,
    require_validated: bool = True,
    min_term_coverage: float = 0.5,
    strict: bool = True,
) -> CompositeResult:
    """Combine terms into one score, auditing every term first.

    A term is EXCLUDED, loudly, when it is constant across the cohort (D3),
    when it is unvalidated and ``require_validated`` (D4), when a learned term's
    only evidence is a random split (D5), or when it is measured on fewer than
    ``min_term_coverage`` of rows.
    """
    weights = dict(weights or {})
    # 1. compute every term for every row
    per_term: dict[str, dict[str, Measured]] = {}
    for sc in scorers:
        col: dict[str, Measured] = {}
        for did, row in rows.items():
            mc = molecule_class_of(row) if molecule_class_of else None
            col[did] = sc.value_for(row, molecule_class=mc)
        per_term[sc.name] = col

    # 2. audit each term
    audits: list[TermAudit] = []
    admitted: list[Scorer] = []
    excluded: list[str] = []
    n_rows = len(rows)
    for sc in scorers:
        col = per_term[sc.name]
        measured = [m for m in col.values() if m.is_measured]
        vals = [float(m.value) for m in measured]  # type: ignore[arg-type]
        n_na = sum(1 for m in col.values() if m.status is Status.NOT_APPLICABLE)
        n_abs = sum(1 for m in col.values() if m.status is Status.ABSENT)
        n_fail = sum(1 for m in col.values() if m.status is Status.FAILED)
        distinct = len(set(vals))
        var = statistics.pvariance(vals) if len(vals) > 1 else 0.0
        is_const = len(vals) > 0 and (distinct == 1 or var == 0.0)

        reason = ""
        ok = True
        if len(vals) == 0:
            ok, reason = False, (f"no measured value on any of {n_rows} rows "
                                 f"({n_na} not-applicable, {n_abs} absent, {n_fail} failed)")
        elif is_const:
            ok, reason = False, (
                f"CONSTANT across the cohort: {len(vals)} measured rows all equal "
                f"{vals[0]!r}. A term with no variance cannot order anything, and folding "
                f"it into a composite moves every row by the same amount — this is the "
                f"N-end-rule-on-a-macrocycle defect."
            )
        elif len(vals) / n_rows < min_term_coverage:
            ok, reason = False, (f"measured on {len(vals)}/{n_rows} rows "
                                 f"({len(vals)/n_rows:.1%}) < min_term_coverage "
                                 f"{min_term_coverage:.1%}")
        elif require_validated and not sc.validated:
            ok, reason = False, (
                "UNVALIDATED: no DirectionEvidence. The direction of this term has never "
                "been checked against measured data, and a term that orders backwards is "
                "worse than no term. Attach DirectionEvidence or set require_validated=False "
                "and label the output as unvalidated."
            )
        elif sc.is_learned and (sc.split_evidence is None or not sc.split_evidence.reportable):
            st = sc.split_evidence.split_type.value if sc.split_evidence else "none"
            ok, reason = False, (
                f"LEARNED scorer with split evidence '{st}'. A random split is not "
                f"performance: on this platform a model read R^2 0.92 random and -1.95 "
                f"clustered. Supply SplitEvidence with a clustered/grouped/temporal split."
            )

        audits.append(TermAudit(sc.name, n_rows, len(vals), n_na, n_abs, n_fail,
                                distinct, var, is_const, ok, reason))
        (admitted if ok else excluded).append(sc if ok else sc.name)  # type: ignore[arg-type]

    if strict:
        for a in audits:
            if not a.admitted and a.is_constant:
                raise ConstantTermError(f"{a.name}: {a.reason}")
            if not a.admitted and "LEARNED scorer" in a.reason:
                raise OptimisticSplitError(f"{a.name}: {a.reason}")
            if not a.admitted and a.reason.startswith("UNVALIDATED"):
                raise UnvalidatedScorerError(f"{a.name}: {a.reason}")

    # 3. combine admitted terms, z-scored, direction-corrected
    stats: dict[str, tuple[float, float]] = {}
    for sc in admitted:
        vals = [float(m.value) for m in per_term[sc.name].values() if m.is_measured]  # type: ignore[arg-type]
        mu = statistics.fmean(vals)
        sd = statistics.pstdev(vals) or 1.0
        stats[sc.name] = (mu, sd)

    scores: dict[str, Measured] = {}
    coverage: dict[str, float] = {}
    total_w = sum(abs(weights.get(sc.name, 1.0)) for sc in admitted) or 1.0
    for did in rows:
        acc = 0.0
        used_w = 0.0
        n_used = 0
        for sc in admitted:
            m = per_term[sc.name][did]
            if not m.is_measured:
                continue
            mu, sd = stats[sc.name]
            z = (float(m.value) - mu) / sd  # type: ignore[arg-type]
            if sc.direction is Direction.LOWER_IS_BETTER:
                z = -z
            w = weights.get(sc.name, 1.0)
            acc += w * z
            used_w += abs(w)
            n_used += 1
        cov = (n_used / len(admitted)) if admitted else 0.0
        coverage[did] = cov
        if n_used == 0:
            scores[did] = Measured.absent(
                f"no admitted term measured for this design "
                f"({len(admitted)} admitted terms)", denominator=len(admitted))
        else:
            scores[did] = Measured(acc / used_w * (total_w / total_w), Status.MEASURED,
                                   n=n_used, denominator=len(admitted),
                                   method="composite_z")
    return CompositeResult(scores, tuple(audits),
                           tuple(sc.name for sc in admitted), tuple(excluded), coverage)


# ---------------------------------------------------------------------------
# D6 — ranking, which cannot exist without its length baseline
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LengthBaseline:
    """How much of a ranking is explained by peptide length alone."""

    score_vs_length: Correlation
    length_only_top_k_overlap: float | None   # Jaccard of top-k by score vs top-k by length
    k: int | None
    n: int
    confounded: bool
    threshold: float

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["score_vs_length"] = self.score_vs_length.as_dict()
        return d

    def __str__(self) -> str:
        r = self.score_vs_length
        rho = "n/a" if r.rho is None else f"{r.rho:+.3f}"
        ci = "" if r.ci_low is None else f" [{r.ci_low:+.3f}, {r.ci_high:+.3f}]"
        return (f"length baseline: Spearman(score, length) = {rho}{ci}, n={self.n}"
                + (f"; top-{self.k} overlap with a length-only ranker "
                   f"{self.length_only_top_k_overlap:.2f}" if self.length_only_top_k_overlap is not None else "")
                + ("  ⛔ LENGTH-CONFOUNDED" if self.confounded else ""))


@dataclass(frozen=True)
class RankingResult:
    """An ordering, its population, and its length baseline — inseparable."""

    name: str
    population: Population
    ordered_ids: tuple[str, ...]
    scores: dict[str, Measured]
    coverage: float
    length_baseline: LengthBaseline
    stratified_ranks: dict[str, tuple[str, ...]] | None = None
    unvalidated_terms: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def length_confounded(self) -> bool:
        return self.length_baseline.confounded

    def top(self, k: int) -> tuple[str, ...]:
        return self.ordered_ids[:k]

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "population": self.population.as_dict(),
            "n_ranked": len(self.ordered_ids),
            "coverage": self.coverage,
            "length_baseline": self.length_baseline.as_dict(),
            "length_confounded": self.length_confounded,
            "unvalidated_terms": list(self.unvalidated_terms),
            "notes": list(self.notes),
            "stratified": {k: list(v) for k, v in (self.stratified_ranks or {}).items()},
        }


def rank(
    name: str,
    population: Population,
    scores: Mapping[str, Measured],
    lengths: Mapping[str, int],
    *,
    higher_is_better: bool = True,
    require_full_coverage: bool = True,
    length_confound_threshold: float = 0.20,
    stratify_by_length_bins: int = 3,
    top_k: int | None = 25,
    unvalidated_terms: Sequence[str] = (),
) -> RankingResult:
    """Order a Population, and compute the length baseline as part of doing so.

    There is deliberately no code path that returns an ordering without the
    baseline: ``RankingResult.length_baseline`` is a required field.
    """
    coverage = assert_population(scores.keys(), population, operation=f"ranking {name!r}",
                                require_full_coverage=require_full_coverage)
    measured = {d: float(m.value) for d, m in scores.items()  # type: ignore[arg-type]
                if m.is_measured and d in population}
    ordered = tuple(sorted(measured, key=lambda d: measured[d], reverse=higher_is_better))

    pairs = [(measured[d], lengths[d]) for d in ordered if d in lengths]
    corr = spearman([p[0] for p in pairs], [p[1] for p in pairs])
    confounded = corr.rho is not None and abs(corr.rho) >= length_confound_threshold

    overlap = None
    k = None
    if top_k and len(ordered) >= 2 and pairs:
        k = min(top_k, len(ordered))
        by_len = sorted((d for d in ordered if d in lengths),
                        key=lambda d: lengths[d], reverse=(corr.rho or 0) > 0)[:k]
        overlap = len(set(ordered[:k]) & set(by_len)) / k

    baseline = LengthBaseline(corr, overlap, k, len(pairs), confounded,
                              length_confound_threshold)

    strat: dict[str, tuple[str, ...]] | None = None
    if confounded and stratify_by_length_bins > 1 and pairs:
        ls = sorted({lengths[d] for d in ordered if d in lengths})
        if len(ls) >= stratify_by_length_bins:
            per = max(1, len(ls) // stratify_by_length_bins)
            edges = [ls[min(len(ls) - 1, (i + 1) * per - 1)]
                     for i in range(stratify_by_length_bins)]
            strat = {}
            for i, hi in enumerate(edges):
                lo = 0 if i == 0 else edges[i - 1] + 1
                members = [d for d in ordered if d in lengths and lo <= lengths[d] <= hi]
                if members:
                    strat[f"len_{lo}_{hi}"] = tuple(members)

    notes: list[str] = []
    if confounded:
        notes.append(
            f"Spearman(score, peptide_length) = {corr.rho:+.3f} — this ordering is "
            f"partly an ordering of lengths. A length-stratified ranking is supplied; "
            f"quote it alongside the pooled one."
        )
    if unvalidated_terms:
        notes.append("contains unvalidated terms: " + ", ".join(unvalidated_terms))

    return RankingResult(name, population, ordered, dict(scores), coverage, baseline,
                         strat, tuple(unvalidated_terms), tuple(notes))
