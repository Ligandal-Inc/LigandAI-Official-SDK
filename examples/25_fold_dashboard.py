# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""
25 — Fold charts + clickable local comparison dashboard (raw is never adjusted).

End-to-end: take fold results (from ``client.peptides.generate`` /
``client.peptides.cofold`` across up to four engines), render distribution and
linked-line charts, and write a clickable LOCAL HTML dashboard that summarizes
everything on RAW scores in native units.

Why raw is never rescaled: each engine reports metrics on its own scale —
Boltz-2 runs high ("inflated"), ESMFold2 / Protenix-V2 run lower, and DeltaForge
dG also varies by engine. Mapping those raws onto a shared "calibrated" axis
would mutate the numbers the engines reported and hide the inflation. Instead,
the only cross-engine footing is ordinal and data-derived: where a raw value
sits within THAT engine's own distribution of your folds (a within-engine
percentile). "Best aggregate" ranks by mean within-engine percentile (consensus
standing), never by averaged raw. DeltaForge dG is shown per engine with its own
standing.

Charts require the optional ``viz`` extra::

    pip install "ligandai[viz]"

Run with a live key to fold fresh sequences, or with no key to use the inline
example fold records below::

    LIGANDAI_API_KEY=lgai_pro_... python 25_fold_dashboard.py
    python 25_fold_dashboard.py   # offline: uses the example records
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from ligandai import (
    PeptideCandidate,
    build_fold_comparison,
    distribution_figure,
    linked_line_figure,
    serve_comparison_dashboard,
    write_comparison_dashboard,
)
from ligandai.errors import LigandAIError

# Inline example fold records: several sequences folded by three engines (one
# fold each per engine) so a within-engine distribution can form. These are
# illustrative literal outputs (not synthetic fill) so the example runs offline.
# Note Boltz-2's higher raw ipTM — the dashboard shows that raw directly and
# annotates each engine's within-engine percentile rather than correcting it.
_SEQUENCES = [
    "ACDEFGHIKLMN", "MNPQRSTVWYAC", "GHIKLMNPQRST", "DEFGHIKLMNPQ",
    "RSTVWYACDEFG", "KLMNPQRSTVWY", "FGHIKLMNPQRS", "VWYACDEFGHIK",
]
# Per-engine raw ipTM bases: esmfold2/protenix lower, boltz2 higher (inflation).
_ENGINE_BASE = {"esmfold2": 0.42, "protenix": 0.40, "boltz2": 0.64}


def _example_records() -> list[dict[str, object]]:
    """Explicit literal per-engine fold rows (no random fill)."""
    records: list[dict[str, object]] = []
    for index, sequence in enumerate(_SEQUENCES):
        step = 0.015 * index
        for engine, base in _ENGINE_BASE.items():
            iptm = round(base + step, 3)
            records.append(
                {
                    "sequence": sequence,
                    "engine": engine,
                    "iptm": iptm,
                    "ipsae": round(iptm + 0.03, 3),
                    "plddt": round(78.0 + step * 40, 1),
                    "delta_g": round(-7.5 - step * 8, 2),
                }
            )
    return records


EXAMPLE_RECORDS = _example_records()


def _candidates_from_records(records: list[dict[str, object]]) -> list[PeptideCandidate]:
    """Wrap plain fold records as PeptideCandidate objects for the dashboard."""
    candidates: list[PeptideCandidate] = []
    for index, record in enumerate(records, start=1):
        candidates.append(
            PeptideCandidate(
                id=f"{record.get('engine', 'engine')}-{index}",
                sequence=str(record.get("sequence", "")),
                gene="EGFR",
                scores=dict(record),
            )
        )
    return candidates


def _load_live_records(api_key: str) -> list[dict[str, object]]:
    """Generate-and-fold a small batch, returning flattened fold records."""
    from ligandai import LigandAI

    client = LigandAI(api_key=api_key)
    print(f"Authenticated (tier={client.tier}, credits={client.credits})")
    job = client.peptides.generate(gene="EGFR", num_peptides=8, auto_fold=True)
    result = job.wait()
    records: list[dict[str, object]] = []
    for peptide in getattr(result, "peptides", []) or []:
        scores = getattr(peptide, "scores", None) or {}
        records.append({"sequence": getattr(peptide, "sequence", ""), **dict(scores)})
    return records or EXAMPLE_RECORDS


def main() -> int:
    out_dir = Path(os.environ.get("LIGANDAI_DASHBOARD_DIR", "./fold_dashboard")).expanduser()

    key = os.environ.get("LIGANDAI_API_KEY")
    if key:
        try:
            records = _load_live_records(key)
        except LigandAIError as exc:
            print(f"(live fold skipped: {type(exc).__name__}: {exc}) — using example records")
            records = EXAMPLE_RECORDS
    else:
        print("No LIGANDAI_API_KEY — using inline example fold records.")
        records = EXAMPLE_RECORDS

    # 1) Normalize into the comparison model (sequence x engine x seed). -------
    comparison = build_fold_comparison(records)
    print(f"Engines: {comparison.engines()}  Sequences: {len(comparison.sequences())}")

    # 2) Charts (optional viz extra) — RAW values in native units. ------------
    try:
        dist_path = out_dir / "distribution.png"
        linked_path = out_dir / "linked_lines.png"
        out_dir.mkdir(parents=True, exist_ok=True)
        distribution_figure(comparison, save_path=dist_path, title="EGFR fold metric distribution (raw)")
        linked_line_figure(
            comparison, metric="iptm", mode="per-sequence",
            annotate_percentiles=True, save_path=linked_path,
        )
        print(f"Charts written: {dist_path}  {linked_path}")
    except ImportError as exc:
        print(f"(charts skipped: {exc})")

    # 3) Clickable local comparison dashboard (raw + within-engine standing). --
    candidates = _candidates_from_records(records)
    handle = write_comparison_dashboard(candidates, out_dir, title="EGFR Fold Comparison")
    print(f"Dashboard: {handle.index_path}")

    if os.environ.get("LIGANDAI_SERVE_DASHBOARD") == "1":
        serve_comparison_dashboard(handle, open_browser=True)
        print(f"Serving at {handle.url} — Ctrl+C to stop.")
        try:
            import time

            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            handle.stop()
            print("\nStopped.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
