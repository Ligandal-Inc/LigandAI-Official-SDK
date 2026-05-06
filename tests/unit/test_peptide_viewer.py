# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Tests for local peptide viewing helpers."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ligandai.peptide_viewer import (
    PROTEINVIEW_ATTRIBUTION,
    PeptideCandidate,
    align_pdb_to_receptor,
    launch_proteinview,
    load_peptide_results,
    rank_peptides,
    write_dashboard,
)


def _atom(serial: int, atom: str, chain: str, resseq: int, xyz: tuple[float, float, float]) -> str:
    x, y, z = xyz
    return (
        f"ATOM  {serial:5d} {atom:<4} ALA {chain}{resseq:4d}    "
        f"{x:8.3f}{y:8.3f}{z:8.3f}  1.00 20.00           C"
    )


def _pdb(chain_points: dict[str, list[tuple[float, float, float]]]) -> str:
    lines = []
    serial = 1
    for chain, points in chain_points.items():
        for resseq, xyz in enumerate(points, start=1):
            lines.append(_atom(serial, "CA", chain, resseq, xyz))
            serial += 1
        lines.append("TER")
    lines.append("END")
    return "\n".join(lines) + "\n"


def _coords(path: Path) -> list[tuple[float, float, float]]:
    coords = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("ATOM"):
            coords.append((float(line[30:38]), float(line[38:46]), float(line[46:54])))
    return coords


def test_load_and_rank_peptide_results_from_fold_directory(tmp_path: Path) -> None:
    pdb_path = tmp_path / "candidate_high.pdb"
    pdb_path.write_text(_pdb({"A": [(0, 0, 0), (1, 0, 0), (0, 1, 0)]}), encoding="utf-8")
    (tmp_path / "candidate_high_meta.json").write_text(
        json.dumps(
            {
                "id": "high",
                "gene": "IL31",
                "sequence": "ACDEFG",
                "overall_ipsae": 0.82,
                "delta_g": -8.4,
                "pdbFile": "candidate_high.pdb",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "candidate_low_meta.json").write_text(
        json.dumps({"id": "low", "sequence": "LMNPQR", "overall_ipsae": 0.43}),
        encoding="utf-8",
    )
    (tmp_path / "candidate_missing_meta.json").write_text(
        json.dumps({"id": "missing", "sequence": "STVWY"}),
        encoding="utf-8",
    )

    ranked = rank_peptides(load_peptide_results([tmp_path]), score="ipsae")
    delta_ranked = rank_peptides(ranked, score="deltaforge")

    assert [candidate.id for candidate in ranked] == ["high", "low", "missing"]
    assert delta_ranked[0].id == "high"
    assert ranked[0].score("ipsae") == pytest.approx(0.82)
    assert ranked[0].score("deltaforge") == pytest.approx(-8.4)
    assert ranked[0].pdb_path == pdb_path.resolve()


def test_align_pdb_to_receptor_transforms_complex_into_base_frame(tmp_path: Path) -> None:
    base_points = [(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)]

    def transform(point: tuple[float, float, float]) -> tuple[float, float, float]:
        x, y, z = point
        return (10 - y, -3 + x, 2 + z)

    base_path = tmp_path / "base.pdb"
    complex_path = tmp_path / "complex.pdb"
    output_path = tmp_path / "aligned.pdb"
    base_path.write_text(_pdb({"A": base_points}), encoding="utf-8")
    complex_path.write_text(
        _pdb({"A": [transform(point) for point in base_points], "Z": [transform((2, 2, 2))]}),
        encoding="utf-8",
    )

    result = align_pdb_to_receptor(
        complex_pdb=complex_path,
        base_receptor_pdb=base_path,
        output_pdb=output_path,
        receptor_chains=["A"],
        peptide_chain="Z",
    )
    aligned = _coords(output_path)

    assert result["atoms"] == 4
    assert result["rmsd"] < 1e-6
    assert aligned[:4] == pytest.approx(base_points, abs=1e-3)
    assert aligned[4] == pytest.approx((2, 2, 2), abs=1e-3)


def test_write_dashboard_materializes_structures_and_attribution(tmp_path: Path) -> None:
    source = tmp_path / "candidate.pdb"
    source.write_text(_pdb({"A": [(0, 0, 0), (1, 0, 0), (0, 1, 0)]}), encoding="utf-8")
    candidate = PeptideCandidate(
        id="pep-1",
        sequence="ACDEFG",
        gene="IL31",
        scores={"overall_ipsae": 0.91},
        pdb_path=source,
    )

    handle = write_dashboard([candidate], tmp_path / "dashboard")
    data = json.loads((handle.output_dir / "candidates.json").read_text(encoding="utf-8"))
    html = handle.index_path.read_text(encoding="utf-8")

    assert data[0]["pdb"] == "structures/001_pep-1.pdb"
    assert (handle.output_dir / data[0]["pdb"]).exists()
    assert "ProteinView by Tristan Farmer / 001TMF" in html
    assert "MIT License" in PROTEINVIEW_ATTRIBUTION


def test_launch_proteinview_builds_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = tmp_path / "candidate.pdb"
    source.write_text(_pdb({"A": [(0, 0, 0), (1, 0, 0), (0, 1, 0)]}), encoding="utf-8")
    candidate = PeptideCandidate(id="pep", sequence="AAA", pdb_path=source)
    captured = {}

    monkeypatch.setattr("ligandai.peptide_viewer.shutil.which", lambda _: "/usr/bin/proteinview")

    def fake_run(command: list[str], check: bool) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["check"] = check
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("ligandai.peptide_viewer.subprocess.run", fake_run)

    completed = launch_proteinview(
        candidate,
        render="halfblock",
        mode="cartoon",
        color="chain",
        fullhd=True,
        extra_args=["--spin"],
    )

    assert completed.returncode == 0
    assert captured["command"] == [
        "proteinview",
        str(source),
        "--render",
        "halfblock",
        "--mode",
        "cartoon",
        "--color",
        "chain",
        "--fullhd",
        "--spin",
    ]
    assert captured["check"] is True
