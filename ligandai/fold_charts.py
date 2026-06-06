# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Distribution and cross-engine charts for fold results — raw is never adjusted.

Two chart families are provided for users who run :meth:`peptides.generate` /
:meth:`peptides.cofold` and want to SEE their fold results without the web app:

- :func:`distribution_figure` — a 4-panel violin + box + strip overlay of the
  RAW values across the seeds/trajectories of each engine (or, per panel, one
  engine across metrics). The plotted data is raw, in each engine's native
  units. Within-engine percentile thresholds (where "high"/"top" standing
  begins for THAT engine, derived from the user's own folds) may be drawn as
  faint reference lines — but the points themselves are never rescaled.
- :func:`linked_line_figure` — a parallel-coordinates / linked-line chart that
  traces each sequence across engines. Each engine gets its OWN independent raw
  axis in its native range, so raw values are shown side by side without forcing
  a shared scale and without adjusting anything. There is deliberately no
  single shared raw y-axis (that would misrepresent Boltz-2 inflation) and no
  "calibrated" value anywhere.

The only cross-engine footing is the within-engine percentile from
:mod:`ligandai.fold_calibration` (an annotation derived from the user's real
folds). Score flattening/extraction reuses
:func:`ligandai.peptide_viewer.extract_score` so there is one score-alias map.

matplotlib is an OPTIONAL dependency. It is imported lazily inside the plotting
functions, so ``import ligandai`` (and ``import ligandai.fold_charts``) never
fails when matplotlib is absent. Install the extra with::

    pip install "ligandai[viz]"

Real data only: empty (sequence x engine) cells are left empty and annotated,
never padded with fabricated points.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ligandai.fold_calibration import (
    ENGINES,
    METRIC_META,
    EngineDistributions,
    build_distributions,
    normalize_engine,
    normalize_metric,
    standing,
)
from ligandai.peptide_viewer import (
    PeptideCandidate,
    extract_score,
    load_peptide_results,
    score_direction,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from matplotlib.axes import Axes
    from matplotlib.figure import Figure

__all__ = [
    "FoldComparison",
    "FoldPoint",
    "build_fold_comparison",
    "distribution_figure",
    "linked_line_figure",
]

# Metrics charted by default in the 4-panel distribution figure.
DEFAULT_PANEL_METRICS: tuple[str, ...] = ("iptm", "ipsae", "plddt", "deltaforge")

# Within-engine percentile thresholds drawn as faint reference lines. These are
# ORDINAL goodness cut-offs ("high" standing starts at the 60th percentile of
# THAT engine's own folds, "top" at the 80th). They annotate raw axes; they
# never rescale a point.
_HIGH_PERCENTILE = 0.60
_TOP_PERCENTILE = 0.80


def _require_matplotlib() -> Any:
    """Import matplotlib.pyplot lazily; raise a clear error if it is missing."""
    try:
        import matplotlib.pyplot as plt
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise ImportError(
            "Charting requires matplotlib, which is an optional dependency. "
            'Install it with: pip install "ligandai[viz]"'
        ) from exc
    return plt


@dataclass(frozen=True)
class FoldPoint:
    """One fold observation: a single (sequence, engine, seed) metric value.

    ``raw`` is the value exactly as the engine reported it (never adjusted).
    ``seed`` carries whatever per-trajectory identity the source provides (seed,
    trajectory index, or sample id); it is informational only.
    """

    sequence: str
    engine: str
    metric: str
    raw: float
    seed: str | None = None
    gene: str | None = None
    candidate_id: str | None = None


@dataclass
class FoldComparison:
    """Normalized fold results grouped by (sequence x engine x seed/trajectory).

    Build via :func:`build_fold_comparison`. Reuses the score-alias map from
    :mod:`ligandai.peptide_viewer`; it does not invent a parallel one. Raw values
    are stored and returned unchanged — distributions for within-engine
    percentiles are built lazily from these same raws.
    """

    points: list[FoldPoint] = field(default_factory=list)

    # -- introspection ----------------------------------------------------- #
    def engines(self) -> list[str]:
        """Engines present, ordered by canonical ENGINES then first-seen."""
        seen = {point.engine for point in self.points}
        ordered = [engine for engine in ENGINES if engine in seen]
        extras = sorted(seen - set(ordered))
        return ordered + extras

    def sequences(self) -> list[str]:
        """Distinct sequences in first-seen order."""
        ordered: list[str] = []
        seen: set[str] = set()
        for point in self.points:
            if point.sequence not in seen:
                seen.add(point.sequence)
                ordered.append(point.sequence)
        return ordered

    def metrics(self) -> list[str]:
        """Distinct metrics in first-seen order."""
        ordered: list[str] = []
        seen: set[str] = set()
        for point in self.points:
            if point.metric not in seen:
                seen.add(point.metric)
                ordered.append(point.metric)
        return ordered

    def raw_values(self, engine: str, metric: str) -> list[float]:
        """All raw values for one (engine, metric) across seeds/sequences."""
        canonical_engine = normalize_engine(engine)
        canonical_metric = normalize_metric(metric)
        return [
            point.raw
            for point in self.points
            if point.engine == canonical_engine and point.metric == canonical_metric
        ]

    def per_engine_raw(self, sequence: str, metric: str) -> dict[str, float]:
        """Mean raw value per engine for one sequence on one metric.

        Multiple seeds for the same engine are averaged so each engine
        contributes one raw point to a cross-engine line. The average is of raw
        values only — no rescaling is applied.
        """
        canonical_metric = normalize_metric(metric)
        buckets: dict[str, list[float]] = {}
        for point in self.points:
            if point.sequence == sequence and point.metric == canonical_metric:
                buckets.setdefault(point.engine, []).append(point.raw)
        return {engine: sum(vals) / len(vals) for engine, vals in buckets.items() if vals}

    def distributions(self, metrics: Sequence[str] | None = None) -> EngineDistributions:
        """Per-(engine, metric) distributions built from this cohort's raw folds.

        Delegates to :func:`ligandai.fold_calibration.build_distributions`; the
        only context is the user's own data. Used for within-engine percentile
        annotations on the charts.
        """
        wanted = tuple(metrics) if metrics is not None else tuple(self.metrics())
        records = [
            {"engine": point.engine, point.metric: point.raw}
            for point in self.points
            if not wanted or point.metric in {normalize_metric(m) for m in wanted}
        ]
        return build_distributions(records, metrics=wanted or DEFAULT_PANEL_METRICS)


def _coerce_records(
    inputs: object,
) -> tuple[list[PeptideCandidate], list[Mapping[str, Any]]]:
    """Split inputs into PeptideCandidate objects and plain dict records.

    Accepts a list of PeptideCandidate, a list of plain dict fold records, or
    anything :func:`load_peptide_results` accepts (paths / strings). A
    FoldComparison is handled by the caller before this is reached.
    """
    candidates: list[PeptideCandidate] = []
    records: list[Mapping[str, Any]] = []
    items: list[Any]
    if isinstance(inputs, (str, Path, Mapping)):
        items = [inputs]
    elif isinstance(inputs, Iterable):
        items = list(inputs)
    else:
        items = [inputs]

    path_like: list[str | Path] = []
    for item in items:
        if isinstance(item, PeptideCandidate):
            candidates.append(item)
        elif isinstance(item, Mapping):
            records.append(item)
        elif isinstance(item, (str, Path)):
            path_like.append(item)
        else:
            raise TypeError(f"Unsupported fold input element: {type(item).__name__}")
    if path_like:
        candidates.extend(load_peptide_results(path_like))
    return candidates, records


def _engine_of(scores: Mapping[str, Any], metadata: Mapping[str, Any] | None = None) -> str:
    """Resolve the engine name from a record's scores/metadata."""
    for key in ("engine", "method", "fold_method", "foldMethod", "model", "predictor"):
        if scores.get(key):
            return normalize_engine(scores[key])
        if metadata and key in metadata and metadata[key]:
            return normalize_engine(metadata[key])
    return normalize_engine(None)


def _seed_of(scores: Mapping[str, Any], metadata: Mapping[str, Any] | None = None) -> str | None:
    """Resolve a per-trajectory identity (seed / trajectory / sample) if any."""
    for key in ("seed", "trajectory", "trajectory_index", "trajectoryIndex", "sample", "replica"):
        if key in scores and scores[key] is not None:
            return str(scores[key])
        if metadata and key in metadata and metadata[key] is not None:
            return str(metadata[key])
    return None


def _emit_points(
    sequence: str,
    engine: str,
    seed: str | None,
    gene: str | None,
    candidate_id: str | None,
    scores: Mapping[str, Any],
    metrics: Sequence[str],
) -> list[FoldPoint]:
    """Pull every requested metric from one record into FoldPoint rows."""
    out: list[FoldPoint] = []
    for metric in metrics:
        canonical_metric = normalize_metric(metric)
        if canonical_metric not in METRIC_META:
            continue
        value = extract_score(dict(scores), canonical_metric)
        if value is None:
            continue
        out.append(
            FoldPoint(
                sequence=sequence,
                engine=engine,
                metric=canonical_metric,
                raw=float(value),
                seed=seed,
                gene=gene,
                candidate_id=candidate_id,
            )
        )
    return out


def build_fold_comparison(
    inputs: object,
    metrics: Sequence[str] = DEFAULT_PANEL_METRICS,
) -> FoldComparison:
    """Normalize fold results into a :class:`FoldComparison`.

    ``inputs`` may be a :class:`FoldComparison` (returned as-is), a list of
    :class:`~ligandai.peptide_viewer.PeptideCandidate`, a list of plain dict
    fold records, or anything :func:`load_peptide_results` accepts. Per-seed /
    per-trajectory rows are preserved as distinct :class:`FoldPoint` entries so
    distributions reflect real per-trajectory spread. Raw values are stored
    unchanged.
    """
    if isinstance(inputs, FoldComparison):
        return inputs

    canonical_metrics = [normalize_metric(metric) for metric in metrics]
    candidates, records = _coerce_records(inputs)
    points: list[FoldPoint] = []

    for candidate in candidates:
        sequence = candidate.sequence or candidate.id
        engine = _engine_of(candidate.scores, candidate.metadata)
        seed = _seed_of(candidate.scores, candidate.metadata)
        points.extend(
            _emit_points(
                sequence,
                engine,
                seed,
                candidate.gene,
                candidate.id,
                candidate.scores,
                canonical_metrics,
            )
        )

    for record in records:
        scores = dict(record)
        nested = record.get("scores")
        if isinstance(nested, Mapping):
            scores.update(nested)
        sequence = str(
            scores.get("sequence")
            or scores.get("peptide_sequence")
            or scores.get("peptideSequence")
            or scores.get("id")
            or ""
        )
        if not sequence:
            continue
        engine = _engine_of(scores)
        seed = _seed_of(scores)
        gene = scores.get("gene")
        candidate_id = scores.get("id")
        points.extend(
            _emit_points(
                sequence,
                engine,
                seed,
                str(gene) if gene is not None else None,
                str(candidate_id) if candidate_id is not None else None,
                scores,
                canonical_metrics,
            )
        )

    return FoldComparison(points=points)


def _raw_at_percentile(
    engine: str,
    metric: str,
    target_percentile: float,
    samples: list[float],
) -> float | None:
    """Find the RAW value whose within-engine goodness-percentile == target.

    This inverts the ordinal percentile against the engine's OWN sorted samples;
    it never rescales a value onto a shared axis. Returns ``None`` if the engine
    has too few samples to form a distribution (handled by the caller via the
    :class:`EngineDistributions` floor). The raw value returned is a real cohort
    quantile, so the reference line always sits on an actually-observed raw
    value's neighbourhood.
    """
    n = len(samples)
    if n == 0:
        return None
    meta = METRIC_META.get(normalize_metric(metric))
    if meta is None:
        return None
    ordered = sorted(samples)
    # goodness-percentile p -> lower-fraction of the value within the engine.
    lower_fraction = target_percentile if meta.higher_is_better else 1.0 - target_percentile
    lower_fraction = min(max(lower_fraction, 0.0), 1.0)
    index = lower_fraction * (n - 1)
    low = int(index)
    high = min(low + 1, n - 1)
    frac = index - low
    return ordered[low] + frac * (ordered[high] - ordered[low])


def _annotate_percentile_thresholds(
    ax: Axes,
    engine: str,
    metric: str,
    distributions: EngineDistributions,
) -> None:
    """Draw faint within-engine percentile reference lines on a RAW axis."""
    samples = distributions.samples.get(normalize_metric(metric), {}).get(
        normalize_engine(engine), []
    )
    if len(samples) < 5:
        return
    for target, label, color in (
        (_HIGH_PERCENTILE, "p60", "#facc15"),
        (_TOP_PERCENTILE, "p80", "#22c55e"),
    ):
        raw = _raw_at_percentile(engine, metric, target, samples)
        if raw is None:
            continue
        ax.axhline(raw, color=color, linewidth=0.9, linestyle="--", alpha=0.55, zorder=1)
        ax.text(
            0.99,
            raw,
            label,
            color=color,
            fontsize=7,
            ha="right",
            va="bottom",
            transform=ax.get_yaxis_transform(),
        )


def _draw_distribution_panel(
    ax: Axes,
    label: str,
    groups: Sequence[tuple[str, list[float]]],
    title: str,
    threshold_groups: Sequence[tuple[str, str]] | None,
    distributions: EngineDistributions,
) -> None:
    """Render one violin+box+strip panel for a list of (group_label, values).

    All plotted values are RAW. ``threshold_groups``, when given, names the
    (engine, metric) pair whose within-engine percentile reference lines to
    annotate; this only applies when every group on the panel shares one engine.
    """
    import numpy as np

    positions = list(range(1, len(groups) + 1))
    populated = [(pos, vals) for pos, (_, vals) in zip(positions, groups, strict=True) if vals]

    if populated:
        violin_positions = [pos for pos, _ in populated]
        violin_data = [vals for _, vals in populated]
        parts = ax.violinplot(violin_data, positions=violin_positions, showextrema=False, widths=0.8)
        bodies: Any = parts["bodies"]
        for body in bodies:
            body.set_facecolor("#38bdf8")
            body.set_alpha(0.25)
        ax.boxplot(
            violin_data,
            positions=violin_positions,
            widths=0.25,
            showfliers=False,
            patch_artist=True,
            boxprops={"facecolor": "#1e293b", "edgecolor": "#94a3b8"},
            medianprops={"color": "#f8fafc"},
            whiskerprops={"color": "#94a3b8"},
            capprops={"color": "#94a3b8"},
        )
        rng = np.random.default_rng(0)
        for pos, vals in populated:
            jitter = rng.uniform(-0.12, 0.12, size=len(vals))
            ax.scatter(
                np.full(len(vals), pos) + jitter,
                vals,
                s=14,
                color="#c4b5fd",
                edgecolor="#312e81",
                linewidth=0.3,
                alpha=0.8,
                zorder=3,
            )

    # Annotate empty groups instead of fabricating points.
    for pos, (_, vals) in zip(positions, groups, strict=True):
        if not vals:
            ax.text(pos, 0.5, "no data", rotation=90, ha="center", va="center",
                    transform=ax.get_xaxis_transform(), color="#64748b", fontsize=7)

    if threshold_groups:
        for engine, metric in threshold_groups:
            _annotate_percentile_thresholds(ax, engine, metric, distributions)

    ax.set_xticks(positions)
    ax.set_xticklabels([group_label for group_label, _ in groups], rotation=20, ha="right", fontsize=8)
    ax.set_title(title, fontsize=10)
    ax.set_ylabel(label, fontsize=9)
    ax.grid(True, axis="y", alpha=0.15)


def distribution_figure(
    inputs: object,
    metrics: Sequence[str] = DEFAULT_PANEL_METRICS,
    *,
    by: str = "metric",
    annotate_percentiles: bool = True,
    save_path: str | Path | None = None,
    title: str = "Fold metric distribution (raw)",
) -> Figure:
    """4-panel violin+box+strip distribution of RAW values across seeds/trajectories.

    ``by="metric"`` (default) draws one panel per metric, with engines as the
    in-panel groups. ``by="engine"`` draws one panel per engine, with metrics as
    the in-panel groups; in that mode each engine's within-engine percentile
    thresholds can be annotated (``annotate_percentiles``).

    All plotted data is raw in each engine's native units — nothing is rescaled.
    Returns the matplotlib :class:`~matplotlib.figure.Figure`. If ``save_path``
    is given the figure is written there. Empty cells are annotated, never
    padded with fabricated points.
    """
    plt = _require_matplotlib()
    comparison = build_fold_comparison(inputs, metrics=metrics)
    canonical_metrics = [normalize_metric(metric) for metric in metrics]
    distributions = comparison.distributions(canonical_metrics)

    if by not in {"metric", "engine"}:
        raise ValueError(f"by must be 'metric' or 'engine', got {by!r}")

    if by == "metric":
        panels: list[str] = canonical_metrics
    else:
        panels = comparison.engines() or [normalize_engine(None)]

    n_panels = max(1, len(panels))
    ncols = 2 if n_panels > 1 else 1
    nrows = (n_panels + ncols - 1) // ncols
    subplots = plt.subplots(nrows, ncols, figsize=(6.4 * ncols, 4.2 * nrows), squeeze=False)
    fig: Figure = subplots[0]
    axes = subplots[1]
    flat_axes = [ax for row in axes for ax in row]

    engines = comparison.engines() or [normalize_engine(None)]
    for index, panel in enumerate(panels):
        ax = flat_axes[index]
        if by == "metric":
            metric = panel
            meta = METRIC_META.get(metric)
            label = meta.label if meta else metric
            groups = [(engine, comparison.raw_values(engine, metric)) for engine in engines]
            # Per-engine percentile thresholds share a y-axis only when one
            # engine is present; with multiple engines the raws are native and
            # incomparable, so we do not draw cross-engine threshold lines.
            thresholds = (
                [(engines[0], metric)] if (annotate_percentiles and len(engines) == 1) else None
            )
            _draw_distribution_panel(ax, label, groups, f"{label}", thresholds, distributions)
        else:
            engine = panel
            groups = [
                (METRIC_META[m].label if m in METRIC_META else m, comparison.raw_values(engine, m))
                for m in canonical_metrics
            ]
            # One engine per panel -> each metric column is that engine's native
            # scale; threshold lines are meaningful per metric column only when
            # the panel holds a single metric, so we skip them in engine mode.
            _draw_distribution_panel(ax, "raw value", groups, f"{engine}", None, distributions)

    for index in range(len(panels), len(flat_axes)):
        flat_axes[index].axis("off")

    fig.suptitle(title, fontsize=13)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    if save_path is not None:
        fig.savefig(str(save_path), dpi=150, bbox_inches="tight")
    return fig


def _percentile_for(
    distributions: EngineDistributions,
    engine: str,
    metric: str,
    raw_value: float,
) -> float | None:
    """Within-engine percentile of one raw value, or None below the floor."""
    info = standing(distributions, engine, metric, raw_value)
    return info.percentile if info is not None else None


def linked_line_figure(
    inputs: object,
    metric: str = "iptm",
    *,
    mode: str = "per-sequence",
    annotate_percentiles: bool = False,
    save_path: str | Path | None = None,
    title: str | None = None,
) -> Figure:
    """Linked-line (parallel-coordinates) chart across engines, raw on independent axes.

    Each sequence becomes one line traced across engines. Every engine gets its
    OWN independent raw axis (its native range), so raw values are shown side by
    side without a shared scale and without adjusting anything. ``mode``:

    - ``"per-sequence"`` — one line per sequence.
    - ``"aggregate"`` — a median line with a shaded inter-quartile band per axis.
    - ``"best"`` — highlight the top sequence per engine (ranked within that
      engine on its own raw, respecting the metric's direction).

    There is deliberately no single shared raw y-axis (that would misrepresent
    Boltz-2 inflation) and no "calibrated" value. When ``annotate_percentiles``
    is set, each engine's within-engine percentile of the plotted raw is printed
    beside its marker (an ordinal annotation, never a replacement for raw).
    """
    plt = _require_matplotlib()
    import numpy as np

    comparison = build_fold_comparison(inputs, metrics=[metric])
    canonical_metric = normalize_metric(metric)
    engines = comparison.engines()
    if not engines:
        raise ValueError("No engines present in fold results; nothing to chart.")
    if mode not in {"per-sequence", "aggregate", "best"}:
        raise ValueError(f"mode must be 'per-sequence', 'aggregate', or 'best', got {mode!r}")

    meta = METRIC_META.get(canonical_metric)
    metric_label = meta.label if meta else canonical_metric
    descending = score_direction(canonical_metric) == "desc"
    distributions = comparison.distributions([canonical_metric])

    # Per-sequence raw series: {sequence: {engine: raw}}.
    series: dict[str, dict[str, float]] = {}
    for sequence in comparison.sequences():
        per_engine = comparison.per_engine_raw(sequence, canonical_metric)
        if per_engine:
            series[sequence] = dict(per_engine)
    if not series:
        raise ValueError("No chartable values for the requested metric.")

    # Each engine axis spans that engine's own raw range (padded). Independent
    # axes are the mechanism that shows raw without a shared scale.
    engine_ranges: dict[str, tuple[float, float]] = {}
    for engine in engines:
        vals = comparison.raw_values(engine, canonical_metric)
        if not vals:
            engine_ranges[engine] = (0.0, 1.0)
            continue
        lo, hi = min(vals), max(vals)
        if lo == hi:
            pad = abs(lo) * 0.05 or 0.5
            lo, hi = lo - pad, hi + pad
        else:
            pad = (hi - lo) * 0.08
            lo, hi = lo - pad, hi + pad
        engine_ranges[engine] = (lo, hi)

    def to_axis_fraction(engine: str, raw_value: float) -> float:
        lo, hi = engine_ranges[engine]
        if hi == lo:
            return 0.5
        return (raw_value - lo) / (hi - lo)

    line_subplots = plt.subplots(figsize=(1.7 * len(engines) + 4.0, 5.2))
    fig: Figure = line_subplots[0]
    host: Axes = line_subplots[1]
    x_positions = list(range(len(engines)))

    # Draw one vertical native-range axis per engine.
    host.set_xlim(-0.4, len(engines) - 0.6)
    host.set_ylim(0.0, 1.0)
    host.set_yticks([])
    for x, engine in zip(x_positions, engines, strict=True):
        host.axvline(x, color="#334155", linewidth=1.0, zorder=1)
        lo, hi = engine_ranges[engine]
        for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
            tick_raw = lo + frac * (hi - lo)
            host.text(x - 0.04, frac, f"{tick_raw:.2f}", fontsize=6.5, ha="right", va="center",
                      color="#94a3b8")

    def plot_line(xs: list[int], fracs: list[float], color: Any, label: str | None,
                  raws: list[float], engs: list[str]) -> None:
        host.plot(xs, fracs, marker="o", linewidth=1.3, markersize=4, color=color, alpha=0.85,
                  label=label, zorder=3)
        if annotate_percentiles:
            for x, frac, raw_value, engine in zip(xs, fracs, raws, engs, strict=True):
                pct = _percentile_for(distributions, engine, canonical_metric, raw_value)
                if pct is not None:
                    host.text(x + 0.05, frac, f"p{round(pct * 100)}", fontsize=6.5,
                              ha="left", va="center", color="#a5b4fc")

    if mode == "per-sequence":
        cmap = plt.get_cmap("viridis")
        sequences = list(series)
        for sidx, sequence in enumerate(sequences):
            row = series[sequence]
            present = [(x, engine) for x, engine in zip(x_positions, engines, strict=True)
                       if engine in row]
            xs = [x for x, _ in present]
            engs = [engine for _, engine in present]
            raws = [row[engine] for engine in engs]
            fracs = [to_axis_fraction(engine, row[engine]) for engine in engs]
            color = cmap(sidx / max(1, len(sequences) - 1))
            plot_line(xs, fracs, color, sequence[:16] if len(sequences) <= 12 else None, raws, engs)
        if len(sequences) <= 12:
            host.legend(fontsize=7, loc="upper right", framealpha=0.3)
    elif mode == "aggregate":
        med_fracs: list[float] = []
        q1_fracs: list[float] = []
        q3_fracs: list[float] = []
        valid_x: list[int] = []
        med_raws: list[float] = []
        med_engs: list[str] = []
        for x, engine in zip(x_positions, engines, strict=True):
            vals = comparison.raw_values(engine, canonical_metric)
            if not vals:
                continue
            valid_x.append(x)
            median = float(np.median(vals))
            q1 = float(np.percentile(vals, 25))
            q3 = float(np.percentile(vals, 75))
            med_fracs.append(to_axis_fraction(engine, median))
            q1_fracs.append(to_axis_fraction(engine, q1))
            q3_fracs.append(to_axis_fraction(engine, q3))
            med_raws.append(median)
            med_engs.append(engine)
        host.fill_between(valid_x, q1_fracs, q3_fracs, color="#38bdf8", alpha=0.2, label="IQR")
        plot_line(valid_x, med_fracs, "#38bdf8", "median", med_raws, med_engs)
        host.legend(fontsize=8, loc="upper right", framealpha=0.3)
    else:  # best
        best_fracs: list[float] = []
        valid_x = []
        best_raws: list[float] = []
        best_engs: list[str] = []
        for x, engine in zip(x_positions, engines, strict=True):
            vals = comparison.raw_values(engine, canonical_metric)
            if not vals:
                continue
            valid_x.append(x)
            best_raw = max(vals) if descending else min(vals)
            best_fracs.append(to_axis_fraction(engine, best_raw))
            best_raws.append(best_raw)
            best_engs.append(engine)
        plot_line(valid_x, best_fracs, "#22c55e", "best per engine", best_raws, best_engs)
        host.legend(fontsize=8, loc="upper right", framealpha=0.3)

    host.set_xticks(x_positions)
    host.set_xticklabels(engines, rotation=15, ha="right")
    host.set_title(title or f"{metric_label} across engines — raw, independent axes ({mode})",
                   fontsize=12)
    fig.tight_layout()
    if save_path is not None:
        fig.savefig(str(save_path), dpi=150, bbox_inches="tight")
    return fig
