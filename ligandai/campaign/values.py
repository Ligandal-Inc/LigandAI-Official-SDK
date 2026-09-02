# Copyright © 2025 Ligandal, Inc. All rights reserved.
"""Value and verdict primitives for the campaign runner.

Two defects from the Avammune/ENPP3 campaign are made structurally impossible here:

D8  An aggregate predicate that passes vacuously on an empty set.  Python's
    ``all([])`` is ``True``.  Nothing in this package may return a bare bool from
    an aggregation: every aggregate returns a :class:`Verdict`, which carries the
    row count that produced it, and a zero-row aggregate is ``INDETERMINATE``,
    never ``PASS``.

(absent != zero != failing)  Every scalar in a design record is a
    :class:`Measured`, which distinguishes MEASURED / ABSENT / FAILED /
    NOT_APPLICABLE and carries the denominator over which it was measured.  A
    peptide with no immunogenicity run and a peptide that scored 0.0 are not the
    same row, and neither is a peptide for which the term cannot apply.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence


class Status(str, Enum):
    """Why a value is (or is not) a number."""

    MEASURED = "measured"          # a real observation
    ABSENT = "absent"              # never attempted / not yet run
    FAILED = "failed"              # attempted, the measurement itself errored
    NOT_APPLICABLE = "not_applicable"  # the term cannot apply to this molecule


@dataclass(frozen=True)
class Measured:
    """One scalar, with the reason it does or does not have a value.

    ``value`` is ``None`` for every status other than MEASURED.  Construction
    enforces that, so a caller cannot smuggle 0.0 in as "absent".
    """

    value: float | int | str | bool | None
    status: Status = Status.MEASURED
    n: int | None = None                 # observations behind the value
    denominator: int | None = None       # observations that COULD have contributed
    units: str | None = None
    method: str | None = None            # what produced it (script, model version)
    note: str | None = None

    def __post_init__(self) -> None:
        if self.status is Status.MEASURED and self.value is None:
            raise ValueError("Measured(status=MEASURED) requires a value; use ABSENT")
        if self.status is not Status.MEASURED and self.value is not None:
            raise ValueError(
                f"Measured(status={self.status.value}) must carry value=None, "
                f"got {self.value!r} — absent must be distinguishable from zero"
            )
        if self.n is not None and self.denominator is not None and self.n > self.denominator:
            raise ValueError(f"n={self.n} exceeds denominator={self.denominator}")

    # -- constructors ---------------------------------------------------
    @classmethod
    def absent(cls, note: str | None = None, denominator: int | None = None) -> "Measured":
        return cls(None, Status.ABSENT, n=0, denominator=denominator, note=note)

    @classmethod
    def failed(cls, note: str | None = None, denominator: int | None = None) -> "Measured":
        return cls(None, Status.FAILED, n=0, denominator=denominator, note=note)

    @classmethod
    def not_applicable(cls, note: str | None = None) -> "Measured":
        return cls(None, Status.NOT_APPLICABLE, n=0, note=note)

    # -- accessors ------------------------------------------------------
    @property
    def is_measured(self) -> bool:
        return self.status is Status.MEASURED

    @property
    def coverage(self) -> float | None:
        """Fraction of the possible observations that were actually made."""
        if self.denominator in (None, 0) or self.n is None:
            return None
        return self.n / self.denominator

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        d["coverage"] = self.coverage
        return d

    def __float__(self) -> float:
        if not self.is_measured:
            raise TypeError(
                f"cannot coerce a {self.status.value} Measured to float — "
                "handle the absent case explicitly"
            )
        return float(self.value)  # type: ignore[arg-type]


class Outcome(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    INDETERMINATE = "indeterminate"   # not enough rows to say anything


@dataclass(frozen=True)
class Verdict:
    """The result of an aggregate predicate, with the rows that produced it.

    D8: ``all_of`` / ``any_of`` / ``fraction_at_least`` over zero rows return
    ``INDETERMINATE``.  ``bool(Verdict)`` is True only for ``PASS``, so an
    indeterminate verdict never silently reads as success in an ``if``.
    """

    outcome: Outcome
    n_rows: int                    # rows the predicate actually saw
    n_true: int = 0
    n_eligible: int | None = None  # rows that could have been evaluated
    n_missing: int = 0             # eligible rows with no value
    detail: str = ""

    def __bool__(self) -> bool:
        return self.outcome is Outcome.PASS

    @property
    def is_indeterminate(self) -> bool:
        return self.outcome is Outcome.INDETERMINATE

    @property
    def fraction_true(self) -> float | None:
        return None if self.n_rows == 0 else self.n_true / self.n_rows

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["outcome"] = self.outcome.value
        d["fraction_true"] = self.fraction_true
        return d

    def __str__(self) -> str:
        frac = "n/a" if self.fraction_true is None else f"{self.fraction_true:.3f}"
        return (
            f"{self.outcome.value.upper()} "
            f"[{self.n_true}/{self.n_rows} = {frac}"
            + (f", {self.n_missing} missing of {self.n_eligible} eligible" if self.n_eligible else "")
            + (f"] {self.detail}" if self.detail else "]")
        )


# ---------------------------------------------------------------------------
# Aggregates. None of these ever return a bare bool.
# ---------------------------------------------------------------------------

def _split(rows: Sequence[Any], predicate) -> tuple[int, int, int]:
    n_rows = 0
    n_true = 0
    n_missing = 0
    for r in rows:
        res = predicate(r)
        if res is None:
            n_missing += 1
            continue
        n_rows += 1
        if res:
            n_true += 1
    return n_rows, n_true, n_missing


def all_of(rows: Sequence[Any], predicate, *, detail: str = "") -> Verdict:
    """``all()`` that cannot pass vacuously."""
    n_eligible = len(rows)
    n_rows, n_true, n_missing = _split(rows, predicate)
    if n_rows == 0:
        return Verdict(
            Outcome.INDETERMINATE, 0, 0, n_eligible, n_missing,
            detail or "zero evaluable rows — all() would have returned True; refused",
        )
    return Verdict(
        Outcome.PASS if n_true == n_rows else Outcome.FAIL,
        n_rows, n_true, n_eligible, n_missing, detail,
    )


def any_of(rows: Sequence[Any], predicate, *, detail: str = "") -> Verdict:
    n_eligible = len(rows)
    n_rows, n_true, n_missing = _split(rows, predicate)
    if n_rows == 0:
        return Verdict(Outcome.INDETERMINATE, 0, 0, n_eligible, n_missing,
                       detail or "zero evaluable rows")
    return Verdict(Outcome.PASS if n_true > 0 else Outcome.FAIL,
                   n_rows, n_true, n_eligible, n_missing, detail)


def fraction_at_least(rows: Sequence[Any], predicate, threshold: float,
                      *, min_rows: int = 1, detail: str = "") -> Verdict:
    n_eligible = len(rows)
    n_rows, n_true, n_missing = _split(rows, predicate)
    if n_rows < max(1, min_rows):
        return Verdict(Outcome.INDETERMINATE, n_rows, n_true, n_eligible, n_missing,
                       detail or f"{n_rows} evaluable rows < min_rows={min_rows}")
    frac = n_true / n_rows
    return Verdict(Outcome.PASS if frac >= threshold else Outcome.FAIL,
                   n_rows, n_true, n_eligible, n_missing,
                   detail or f"fraction {frac:.3f} vs threshold {threshold:.3f}")


def count_at_least(rows: Sequence[Any], predicate, target: int, *, detail: str = "") -> Verdict:
    n_eligible = len(rows)
    n_rows, n_true, n_missing = _split(rows, predicate)
    return Verdict(
        Outcome.PASS if n_true >= target else Outcome.FAIL,
        n_rows, n_true, n_eligible, n_missing,
        detail or f"{n_true} of target {target}",
    )


# ---------------------------------------------------------------------------
# Wilson score interval — the campaign's certification arithmetic.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WilsonInterval:
    """Wilson score interval for a binomial proportion.

    The ENPP3 campaign's central lesson: reliability is a RATE with an interval,
    never a label.  With zero failures, 8 runs bound the rate no tighter than
    ~0.32 and 16 runs ~0.19; certifying below 0.15 takes ~22 clean runs.  A
    single-fold "converged / bimodal" label tracks sample size, not the molecule.
    """

    successes: int      # here: FAILURES observed (the event being bounded)
    n: int              # independent runs
    confidence: float
    lower: float
    upper: float
    point: float

    @property
    def width(self) -> float:
        return self.upper - self.lower

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_Z = {0.90: 1.6448536269514722, 0.95: 1.959963984540054, 0.99: 2.5758293035489004}


def _z_for(confidence: float) -> float:
    if confidence in _Z:
        return _Z[confidence]
    try:  # pragma: no cover - scipy is present on this box but keep it optional
        from scipy.stats import norm  # type: ignore

        return float(norm.ppf(1.0 - (1.0 - confidence) / 2.0))
    except Exception as exc:  # pragma: no cover
        raise ValueError(
            f"unsupported confidence {confidence}; install scipy or use one of {sorted(_Z)}"
        ) from exc


def wilson(successes: int, n: int, confidence: float = 0.95) -> WilsonInterval | None:
    """Two-sided Wilson interval.  Returns ``None`` for n == 0.

    Returning None rather than (0, 1) is deliberate: a zero-run peptide has no
    interval at all, and must not be reported as "bounded at 1.0".
    """
    if n < 0 or successes < 0 or successes > n:
        raise ValueError(f"invalid binomial counts successes={successes} n={n}")
    if n == 0:
        return None
    z = _z_for(confidence)
    p = successes / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return WilsonInterval(successes, n, confidence,
                          max(0.0, centre - half), min(1.0, centre + half), p)


def runs_needed_for_upper_bound(target_upper: float, observed_rate: float,
                                confidence: float = 0.95, *, max_n: int = 100_000) -> int | None:
    """Smallest n at which the Wilson upper bound reaches ``target_upper``
    if failures keep arriving at ``observed_rate``.

    This is the arithmetic that the ENPP3 campaign got wrong by asserting, three
    times, that a 10% peptide "could never certify" at a 0.15 bar.  It could, and
    did, at n=192.  Compute the convergence; do not assert it.
    """
    if not 0.0 < target_upper < 1.0:
        raise ValueError("target_upper must be in (0, 1)")
    if not 0.0 <= observed_rate <= 1.0:
        raise ValueError("observed_rate must be in [0, 1]")
    n = 1
    while n <= max_n:
        k = int(round(observed_rate * n))
        iv = wilson(k, n, confidence)
        if iv is not None and iv.upper <= target_upper:
            return n
        n = n + 1 if n < 64 else int(n * 1.15) + 1
    return None
