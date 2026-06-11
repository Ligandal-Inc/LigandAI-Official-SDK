# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Cross-engine fold-score context — WITHOUT adjusting raw scores.

Different structure-prediction engines report interface-confidence metrics
(ipTM / ipSAE / pTM), per-residue confidence (pLDDT), and a DeltaForge binding
energy (dG) on *different effective scales*. Boltz-2 tends to run high on
interface confidence; ESMFold2 and Protenix-V2 run lower for the same
structural quality. DeltaForge dG also varies by engine, because it scores the
structure each engine predicted — a better pose yields a more favorable dG, so
dG inherits the engine's bias just like the confidence metrics.

The naive fix — rescaling each engine's raw value onto a shared 0..1 axis using
a fixed prior — is the wrong fix: it *mutates the number the engine reported*,
hides inflation instead of exposing it, and bakes our guesses into the user's
data. This module therefore NEVER adjusts a raw score. Raw values are reported
and ranked in their own native units, per engine.

The only cross-engine footing we add is ordinal and data-derived: for a given
engine + metric we look at where a raw value sits within THAT engine's own
distribution of folds (a within-engine *percentile*, goodness-oriented so 1.0
is best-in-cohort regardless of metric direction). That percentile is an
annotation displayed beside the raw value — it does not replace it. "Model
agreement" then means: do the engines place a sequence at a *similar standing
within their own distributions*? A Boltz-2 raw of 0.71 that is only median
among your Boltz-2 folds will not out-rank an ESMFold2 raw of 0.46 that sits in
the top decile of your ESMFold2 folds — which is the point.

When a cohort is too small to form a distribution, no percentile is reported
(no synthetic fill); the dashboard simply shows raw values side by side.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

__all__ = [
    "ENGINES",
    "METRICS",
    "METRIC_META",
    "MIN_DISTRIBUTION_SAMPLES",
    "PERCENTILE_AGREEMENT_TOLERANCE",
    "PERCENTILE_TIERS",
    "EngineAgreement",
    "EngineDistributions",
    "EngineStanding",
    "MetricMeta",
    "build_distributions",
    "engine_agreement",
    "metric_higher_is_better",
    "normalize_engine",
    "normalize_metric",
    "percentile_label",
    "standing",
]

ENGINES: tuple[str, ...] = ("esmfold2", "boltz2", "protenix", "openfold3", "promera")
METRICS: tuple[str, ...] = ("iptm", "ipsae", "ptm", "plddt", "deltaforge")

# Goodness-percentile labels (annotation only — derived from the engine's own
# distribution, never from a fixed prior and never used to alter a raw value).
PERCENTILE_TIERS: tuple[str, ...] = ("bottom", "low", "mid", "high", "top")

# Two engines "agree" on a sequence when their within-engine percentiles differ
# by at most this much. This is an ORDINAL tolerance on standing, not on raw.
PERCENTILE_AGREEMENT_TOLERANCE: float = 0.25

# Below this many finite samples an engine has no usable distribution, so no
# percentile is reported for it (raw is still shown).
MIN_DISTRIBUTION_SAMPLES: int = 5

# Generic engine key for unrecognized engines (kept separate; never merged with
# a known engine's distribution).
_GENERIC = "_generic"

_ENGINE_ALIASES: dict[str, str] = {
    "esmfold2": "esmfold2",
    "esmfold": "esmfold2",
    "esmfold2fast": "esmfold2",
    "esm": "esmfold2",
    "esm2": "esmfold2",
    "boltz2": "boltz2",
    "boltz": "boltz2",
    "boltz2x": "boltz2",
    "protenix": "protenix",
    "protenixv2": "protenix",
    "protenix2": "protenix",
    "openfold3": "openfold3",
    "openfold": "openfold3",
    "of3": "openfold3",
    "openfold3fast": "openfold3",
    "promera": "promera",
    "promera2606": "promera",
    "promerav1": "promera",
}

_METRIC_ALIASES: dict[str, str] = {
    "iptm": "iptm",
    "interfaceptm": "iptm",
    "ipsae": "ipsae",
    "overallipsae": "ipsae",
    "peptideipsae": "ipsae",
    "ipsaed0res": "ipsae",
    "ptm": "ptm",
    "overallptm": "ptm",
    "plddt": "plddt",
    "meanplddt": "plddt",
    "complexplddt": "plddt",
    "deltaforge": "deltaforge",
    "dg": "deltaforge",
    "deltag": "deltaforge",
    "v10dgbest": "deltaforge",
    "v10dgboltz2": "deltaforge",
    "v10dgmean": "deltaforge",
    "bindingenergy": "deltaforge",
    "ebmenergy": "deltaforge",
}


@dataclass(frozen=True)
class MetricMeta:
    """Static facts about a metric, independent of engine.

    Every metric is treated as per-engine: the engine's distribution is the
    only context we use. ``higher_is_better`` orients the goodness-percentile so
    that 1.0 is always best-in-cohort.
    """

    name: str
    label: str
    higher_is_better: bool
    autoscale_percent: bool = False  # pLDDT may arrive as 0..1 or 0..100


METRIC_META: dict[str, MetricMeta] = {
    "iptm": MetricMeta("iptm", "ipTM", higher_is_better=True),
    "ipsae": MetricMeta("ipsae", "iPSAE", higher_is_better=True),
    "ptm": MetricMeta("ptm", "pTM", higher_is_better=True),
    "plddt": MetricMeta("plddt", "pLDDT", higher_is_better=True, autoscale_percent=True),
    "deltaforge": MetricMeta("deltaforge", "DeltaForge dG", higher_is_better=False),
}


@dataclass(frozen=True)
class EngineStanding:
    """Where one raw value sits within one engine's own distribution.

    ``raw`` is the value exactly as the engine reported it (never adjusted).
    ``percentile`` is goodness-oriented in [0, 1] (1.0 = best among this
    engine's folds) or ``None`` when the engine has too few samples. ``label``
    is a coarse annotation of the percentile, for display only.
    """

    engine: str
    metric: str
    raw: float | None
    percentile: float | None
    n: int
    label: str | None
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.percentile is not None


@dataclass(frozen=True)
class EngineAgreement:
    """Cross-engine agreement for one sequence on one metric.

    Built entirely from raw values and within-engine percentiles. ``raw`` holds
    each engine's unmodified value; ``percentile`` holds each engine's
    within-engine standing. Agreement is judged on percentile spread (ordinal),
    so it never depends on rescaling any raw score onto a shared axis.
    """

    metric: str
    raw: dict[str, float]
    percentile: dict[str, float]
    n: dict[str, int]
    labels: dict[str, str]
    mean_percentile: float
    percentile_spread: float
    raw_spread: float
    agree: bool
    best_engine: str
    worst_engine: str
    consensus_label: str
    note: str = ""


@dataclass
class EngineDistributions:
    """Per-(engine, metric) empirical samples drawn from real fold records.

    The samples are the user's own folds; nothing synthetic is added. Percentile
    queries are answered against these samples only.
    """

    samples: dict[str, dict[str, list[float]]] = field(default_factory=dict)
    source: str = "empirical"

    def n(self, engine: str, metric: str) -> int:
        eng = normalize_engine(engine)
        met = normalize_metric(metric)
        return len(self.samples.get(met, {}).get(eng, ()))

    def percentile(self, engine: str, metric: str, value: object) -> float | None:
        """Goodness-percentile of ``value`` within (engine, metric) samples.

        Returns a value in [0, 1] where 1.0 is best-in-cohort, or ``None`` when
        the value is missing/non-finite or the engine has fewer than
        :data:`MIN_DISTRIBUTION_SAMPLES` samples. The raw ``value`` is read, not
        modified.
        """
        met = normalize_metric(metric)
        meta = METRIC_META.get(met)
        if meta is None:
            return None
        fvalue = _to_finite_float(value)
        if fvalue is None:
            return None
        eng = normalize_engine(engine)
        samples = self.samples.get(met, {}).get(eng)
        if samples is None or len(samples) < MIN_DISTRIBUTION_SAMPLES:
            return None
        scaled = _scaled_value(met, fvalue)
        return _goodness_percentile(scaled, samples, higher_is_better=meta.higher_is_better)


def metric_higher_is_better(metric: str) -> bool | None:
    meta = METRIC_META.get(normalize_metric(metric))
    return None if meta is None else meta.higher_is_better


def normalize_engine(name: object) -> str:
    if not name:
        return _GENERIC
    text = str(name)
    key = "".join(ch for ch in text.lower() if ch.isalnum())
    return _ENGINE_ALIASES.get(key, text if text in ENGINES else _GENERIC)


def normalize_metric(name: str) -> str:
    key = "".join(ch for ch in str(name).lower() if ch.isalnum())
    if key in _METRIC_ALIASES:
        return _METRIC_ALIASES[key]
    return name  # unknown metric passed through; callers guard via METRIC_META


def percentile_label(percentile: float | None) -> str | None:
    """Coarse annotation of a goodness-percentile (display only)."""
    if percentile is None:
        return None
    if percentile < 0.20:
        return "bottom"
    if percentile < 0.40:
        return "low"
    if percentile < 0.60:
        return "mid"
    if percentile < 0.80:
        return "high"
    return "top"


def build_distributions(
    records: Iterable[Mapping[str, object]],
    *,
    metrics: tuple[str, ...] = METRICS,
    engine_key: str = "engine",
    value_keys: Mapping[str, str] | None = None,
) -> EngineDistributions:
    """Collect per-(engine, metric) samples from real fold records.

    Each record supplies an engine and one raw value per metric it carries.
    Records missing an engine or a metric value simply contribute nothing for
    that cell — no value is invented. The resulting distributions are the sole
    basis for within-engine percentiles and agreement.
    """
    wanted = tuple(normalize_metric(m) for m in metrics)
    samples: dict[str, dict[str, list[float]]] = {m: {} for m in wanted}
    for record in records:
        engine = normalize_engine(_record_get(record, engine_key))
        for met in wanted:
            meta = METRIC_META.get(met)
            if meta is None:
                continue
            vkey = (value_keys or {}).get(met, met)
            value = _to_finite_float(_record_get(record, vkey))
            if value is None:
                continue
            samples[met].setdefault(engine, []).append(_scaled_value(met, value))
    for met in samples:
        for eng in samples[met]:
            samples[met][eng].sort()
    return EngineDistributions(samples=samples, source="empirical")


def standing(
    distributions: EngineDistributions,
    engine: str,
    metric: str,
    value: object,
) -> EngineStanding | None:
    """Within-engine standing of one raw value (raw is preserved, not adjusted)."""
    met = normalize_metric(metric)
    if met not in METRIC_META:
        return None
    raw = _to_finite_float(value)
    eng = normalize_engine(engine)
    n = distributions.n(eng, met)
    pct = distributions.percentile(eng, met, raw)
    note = ""
    if raw is not None and pct is None and n < MIN_DISTRIBUTION_SAMPLES:
        note = f"Only {n} {eng} folds for {met}; showing raw without percentile."
    return EngineStanding(
        engine=eng,
        metric=met,
        raw=raw,
        percentile=pct,
        n=n,
        label=percentile_label(pct),
        note=note,
    )


def engine_agreement(
    per_engine_raw: Mapping[str, object],
    metric: str,
    distributions: EngineDistributions,
) -> EngineAgreement | None:
    """Cross-engine agreement for one sequence, from raw + within-engine percentiles.

    Engines whose raw values look far apart can still agree once each is judged
    against its own distribution (and vice-versa). Raw values are carried
    through unmodified; agreement is decided on percentile spread.
    """
    met = normalize_metric(metric)
    if met not in METRIC_META:
        return None
    raw: dict[str, float] = {}
    pct: dict[str, float] = {}
    n_by_engine: dict[str, int] = {}
    labels: dict[str, str] = {}
    for engine, value in per_engine_raw.items():
        fvalue = _to_finite_float(value)
        if fvalue is None:
            continue
        eng = normalize_engine(engine)
        raw[eng] = fvalue
        n_by_engine[eng] = distributions.n(eng, met)
        p = distributions.percentile(eng, met, fvalue)
        if p is not None:
            pct[eng] = p
            lbl = percentile_label(p)
            if lbl is not None:
                labels[eng] = lbl
    if not raw:
        return None

    raw_values = list(raw.values())
    raw_spread = max(raw_values) - min(raw_values)

    if len(pct) >= 2:
        pct_values = list(pct.values())
        mean_pct = sum(pct_values) / len(pct_values)
        pct_spread = max(pct_values) - min(pct_values)
        best_engine = max(pct, key=lambda e: pct[e])
        worst_engine = min(pct, key=lambda e: pct[e])
        agree = pct_spread <= PERCENTILE_AGREEMENT_TOLERANCE
        consensus_label = percentile_label(mean_pct) or "mid"
        if agree and raw_spread > PERCENTILE_AGREEMENT_TOLERANCE + 0.1:
            note = "Engines agree on standing despite a large raw spread."
        elif not agree:
            note = (
                f"Engines disagree (percentile spread {pct_spread:.2f}); "
                f"inspect {worst_engine} vs {best_engine}."
            )
        else:
            note = ""
    else:
        # Not enough engines with distributions to judge standing; fall back to
        # raw ranking only, and say so. Never invent a percentile.
        mean_pct = next(iter(pct.values())) if pct else 0.0
        pct_spread = 0.0
        meta = METRIC_META[met]
        best_engine = (max if meta.higher_is_better else min)(raw, key=lambda e: raw[e])
        worst_engine = (min if meta.higher_is_better else max)(raw, key=lambda e: raw[e])
        agree = False
        consensus_label = percentile_label(mean_pct) or "mid"
        note = "Too few per-engine distributions for a standing comparison; raw only."

    return EngineAgreement(
        metric=met,
        raw=raw,
        percentile=pct,
        n=n_by_engine,
        labels=labels,
        mean_percentile=mean_pct,
        percentile_spread=pct_spread,
        raw_spread=raw_spread,
        agree=agree,
        best_engine=best_engine,
        worst_engine=worst_engine,
        consensus_label=consensus_label,
        note=note,
    )


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #
def _goodness_percentile(value: float, sorted_samples: list[float], *, higher_is_better: bool) -> float:
    """Mid-rank goodness-percentile in [0, 1]; 1.0 = best in cohort.

    Uses the mid-rank (average-rank) convention so ties map to a single
    fraction. ``sorted_samples`` must be ascending.
    """
    n = len(sorted_samples)
    if n == 0:
        return 0.0
    below = 0
    equal = 0
    for s in sorted_samples:
        if s < value:
            below += 1
        elif s == value:
            equal += 1
    # Fraction of samples this value is "better than or tied with", mid-rank.
    lower_fraction = (below + 0.5 * equal) / n
    return lower_fraction if higher_is_better else 1.0 - lower_fraction


def _scaled_value(metric: str, value: float) -> float:
    meta = METRIC_META[metric]
    if meta.autoscale_percent and 0.0 <= value <= 1.0:
        return value * 100.0
    return value


def _to_finite_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _record_get(record: Mapping[str, object], key: str) -> object:
    if key in record:
        return record[key]
    normalized = "".join(ch for ch in key.lower() if ch.isalnum())
    for rk, rv in record.items():
        if "".join(ch for ch in str(rk).lower() if ch.isalnum()) == normalized:
            return rv
    return None
