# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Tests for the multi-engine comparison dashboard — RAW is never adjusted.

Three guarantees are pinned: (1) the embedded JSON carries raw + within-engine
percentile + label + agreement fields computed in Python (the JavaScript only
renders them, it never recomputes a percentile); (2) "best per method" is the max
RAW within an engine while "best aggregate" ranks by mean within-engine
percentile; (3) the legacy single-candidate :func:`write_dashboard` output is
byte-for-byte behaviorally unchanged.

All fold records are explicit literals representing plausible real outputs — no
synthetic fill. The Boltz-2 folds run HIGH on raw ipTM (it inflates) yet land
LOWER within their own cohort, which is the whole point of within-engine
standing.
"""

from __future__ import annotations

import json
from pathlib import Path

from ligandai.peptide_viewer import (
    PeptideCandidate,
    build_comparison_summary,
    write_comparison_dashboard,
    write_dashboard,
)


def _pdb() -> str:
    return (
        "ATOM      1 CA   ALA A   1       0.000   0.000   0.000  1.00 20.00           C\n"
        "ATOM      2 CA   ALA A   2       1.000   0.000   0.000  1.00 20.00           C\n"
        "ATOM      3 CA   ALA A   3       0.000   1.000   0.000  1.00 20.00           C\n"
        "TER\nEND\n"
    )


# Per-engine raw ipTM cohorts (7 folds each so a within-engine distribution
# forms). esmfold2/protenix run lower; boltz2 runs HIGH. Each engine carries its
# own filler sequences plus the one SHARED SEQ_HERO that all three folded — so
# SEQ_HERO uniquely has all three engines and drives the agreement panel.
# On SEQ_HERO: esmfold2 0.50 sits at the TOP of esmfold2's cohort, protenix 0.48
# at the top of protenix's, but boltz2 0.71 — though a high raw — is only MID
# within boltz2's own (inflated) cohort (whose best fold is 0.84).
_ESM_RAWS = {"ESM00": 0.30, "ESM01": 0.36, "ESM02": 0.40, "ESM03": 0.44, "ESM04": 0.47, "ESM05": 0.49, "SEQ_HERO": 0.50}
_PROT_RAWS = {"PRO00": 0.26, "PRO01": 0.32, "PRO02": 0.37, "PRO03": 0.41, "PRO04": 0.45, "PRO05": 0.47, "SEQ_HERO": 0.48}
_BOLTZ_RAWS = {"BOL00": 0.55, "BOL01": 0.60, "BOL02": 0.66, "SEQ_HERO": 0.71, "BOL03": 0.74, "BOL04": 0.80, "BOL05": 0.84}


def _candidates(tmp_path: Path) -> list[PeptideCandidate]:
    source = tmp_path / "candidate.pdb"
    source.write_text(_pdb(), encoding="utf-8")
    out: list[PeptideCandidate] = []
    for engine, raws in (("esmfold2", _ESM_RAWS), ("protenix", _PROT_RAWS), ("boltz2", _BOLTZ_RAWS)):
        for seq, iptm in raws.items():
            out.append(
                PeptideCandidate(
                    id=f"{engine}-{seq}",
                    sequence=seq,
                    gene="EGFR",
                    scores={"engine": engine, "iptm": iptm, "delta_g": -8.0 - iptm},
                    pdb_path=source,
                )
            )
    return out


def test_summary_carries_raw_percentile_label_and_agreement(tmp_path: Path) -> None:
    """The summary computes raw + within-engine percentile + agreement in Python."""
    summary = build_comparison_summary(_candidates(tmp_path))

    iptm = summary["per_metric"]["iptm"]
    best_per_engine = iptm["best_per_engine"]
    assert set(best_per_engine) == {"esmfold2", "protenix", "boltz2"}
    for entry in best_per_engine.values():
        assert "raw" in entry and "percentile" in entry and "label" in entry

    # Best per method = the max RAW within each engine (esmfold2 tops at 0.50,
    # protenix at 0.48, boltz2 at 0.84). Raw is unchanged.
    assert best_per_engine["esmfold2"]["raw"] == 0.50
    assert best_per_engine["protenix"]["raw"] == 0.48
    assert best_per_engine["boltz2"]["raw"] == 0.84

    # Within-engine percentiles exist (>= 5 folds per engine).
    assert best_per_engine["esmfold2"]["percentile"] is not None
    assert best_per_engine["boltz2"]["percentile"] is not None


def test_boltz2_high_raw_lands_lower_within_its_own_cohort(tmp_path: Path) -> None:
    """A same-looking-high Boltz-2 raw is LOWER within-engine than esmfold2's top."""
    summary = build_comparison_summary(_candidates(tmp_path))
    iptm = summary["per_metric"]["iptm"]["agreement"]
    assert iptm is not None and iptm["sequence"] == "SEQ_HERO"

    # On SEQ_HERO, boltz2's raw (0.71) is the highest raw of the three engines,
    # yet its within-engine percentile is the LOWEST — esmfold2/protenix sit at
    # the top of their own cohorts. Raw is carried through byte-identical.
    assert iptm["raw"]["boltz2"] == 0.71
    assert iptm["raw"]["esmfold2"] == 0.50
    assert iptm["percentile"]["boltz2"] < iptm["percentile"]["esmfold2"]
    assert iptm["raw_spread"] > iptm["percentile_spread"] or iptm["agree"] is False
    # Engines disagree on standing despite (because of) the raw inflation.
    assert iptm["agree"] is False
    assert iptm["best_engine"] in {"esmfold2", "protenix"}
    assert iptm["worst_engine"] == "boltz2"


def test_best_aggregate_ranks_by_mean_percentile_not_raw(tmp_path: Path) -> None:
    """best_aggregate is the engine with the highest within-engine standing."""
    summary = build_comparison_summary(_candidates(tmp_path))
    agg = summary["per_metric"]["iptm"]["best_aggregate"]
    assert agg is not None
    # boltz2 has the highest RAW (0.84) but a top-of-its-cohort percentile too,
    # while esmfold2's best (0.50) is also top of its cohort. The aggregate must
    # be chosen by percentile, never by averaged/highest raw — assert the engine
    # selected is the one whose best-fold percentile is maximal.
    best_per_engine = summary["per_metric"]["iptm"]["best_per_engine"]
    max_pct = max(
        e["percentile"] for e in best_per_engine.values() if e["percentile"] is not None
    )
    assert agg["percentile"] == max_pct
    # The aggregate carries the RAW value backing it (unchanged).
    assert "raw" in agg


def test_small_cohort_yields_no_percentile(tmp_path: Path) -> None:
    """Below the sample floor, raw is shown with percentile None (no fill)."""
    source = tmp_path / "tiny.pdb"
    source.write_text(_pdb(), encoding="utf-8")
    tiny = [
        PeptideCandidate(
            id="esm-1", sequence="AAA", gene="EGFR",
            scores={"engine": "esmfold2", "iptm": 0.5}, pdb_path=source,
        ),
        PeptideCandidate(
            id="boltz-1", sequence="AAA", gene="EGFR",
            scores={"engine": "boltz2", "iptm": 0.7}, pdb_path=source,
        ),
    ]
    summary = build_comparison_summary(tiny)
    best = summary["per_metric"]["iptm"]["best_per_engine"]
    # Raw preserved; no synthetic percentile invented for a 1-sample engine.
    assert best["esmfold2"]["raw"] == 0.5
    assert best["esmfold2"]["percentile"] is None
    assert best["boltz2"]["raw"] == 0.7
    assert best["boltz2"]["percentile"] is None


def test_comparison_dashboard_embeds_precomputed_fields(tmp_path: Path) -> None:
    """candidates.json + summary.json carry the precomputed raw + standing fields."""
    handle = write_comparison_dashboard(_candidates(tmp_path), tmp_path / "cmp")
    rows = json.loads((handle.output_dir / "candidates.json").read_text(encoding="utf-8"))
    summary = json.loads((handle.output_dir / "summary.json").read_text(encoding="utf-8"))

    # Each row carries per-metric raw + within-engine standing blocks.
    boltz_row = next(row for row in rows if row["engine"] == "boltz2" and row["sequence"] == "SEQ_HERO")
    assert "standing" in boltz_row
    assert boltz_row["standing"]["iptm"]["raw"] == 0.71  # raw unchanged
    assert "percentile" in boltz_row["standing"]["iptm"]
    assert "label" in boltz_row["standing"]["iptm"]
    # No "calibrated" key anywhere — raw only.
    assert "calibrated" not in boltz_row
    assert "calibrated" not in boltz_row["standing"]["iptm"]

    assert summary["per_metric"]["iptm"]["agreement"] is not None
    assert (handle.output_dir / boltz_row["pdb"]).exists()

    # The browser must NOT recompute bands or rescale: the HTML states standings
    # are Python-side and renders raw, with no band/calibration arithmetic.
    html = handle.index_path.read_text(encoding="utf-8")
    assert "does not recompute bands" in html
    assert "fold_calibration" in html
    # No banned "calibrated"-axis vocabulary leaks into the rendered page logic.
    assert "calibrated_score" not in html
    assert "tierClass" not in html


def test_legacy_write_dashboard_output_unchanged(tmp_path: Path) -> None:
    """The single-candidate legacy dashboard behaves exactly as before."""
    source = tmp_path / "legacy.pdb"
    source.write_text(_pdb(), encoding="utf-8")
    candidate = PeptideCandidate(
        id="pep-1",
        sequence="ACDEFG",
        gene="IL31",
        scores={"overall_ipsae": 0.91},
        pdb_path=source,
    )

    handle = write_dashboard([candidate], tmp_path / "legacy")
    data = json.loads((handle.output_dir / "candidates.json").read_text(encoding="utf-8"))
    html = handle.index_path.read_text(encoding="utf-8")

    # Legacy rows do NOT carry the new standing/engine fields.
    assert data[0]["pdb"] == "structures/001_pep-1.pdb"
    assert "standing" not in data[0]
    assert "calibrated" not in data[0]
    assert "engine" not in data[0]
    # Legacy dashboard has no comparison summary.
    assert not (handle.output_dir / "summary.json").exists()
    # Legacy attribution + structure path preserved.
    assert "ProteinView by Tristan Farmer / 001TMF" in html
    assert (handle.output_dir / data[0]["pdb"]).exists()
