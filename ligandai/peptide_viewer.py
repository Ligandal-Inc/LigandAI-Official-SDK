# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Local peptide fold viewing helpers.

These helpers load LigandForge/PTF fold result JSON, JSONL, PDB, or result
directories; rank peptides by iPSAE or DeltaForge-style scores; optionally align
receptor+peptide complexes into a base receptor frame; and write a lightweight
localhost dashboard.

Terminal rendering can launch ProteinView by Tristan Farmer / 001TMF, MIT
License, https://github.com/001TMF/ProteinView.
"""

from __future__ import annotations

import html
import json
import math
import shutil
import subprocess
import threading
import webbrowser
from dataclasses import dataclass, field
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

PROTEINVIEW_ATTRIBUTION = (
    "ProteinView by Tristan Farmer / 001TMF, MIT License, "
    "https://github.com/001TMF/ProteinView"
)


@dataclass
class PeptideCandidate:
    """One generated or folded peptide candidate."""

    id: str
    sequence: str
    gene: str | None = None
    target: str | None = None
    conformation: str | None = None
    scores: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    pdb_path: Path | None = None
    pdb_content: str | None = None
    source_path: Path | None = None
    aligned_pdb_path: Path | None = None
    alignment_rmsd: float | None = None
    alignment_atoms: int = 0

    def score(self, score_name: str) -> float | None:
        return extract_score(self.scores, score_name)

    def pdb_for_viewing(self) -> Path | None:
        return self.aligned_pdb_path or self.pdb_path


@dataclass
class DashboardHandle:
    """Dashboard files and optional localhost server state."""

    output_dir: Path
    index_path: Path
    url: str | None = None
    server: ThreadingHTTPServer | None = None
    thread: threading.Thread | None = None

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()


def load_peptide_results(inputs: list[str | Path]) -> list[PeptideCandidate]:
    """Load peptide candidates from JSON, JSONL, PDB, or result directories."""
    candidates: list[PeptideCandidate] = []
    for raw in inputs:
        path = Path(raw).expanduser().resolve()
        if not path.exists():
            raise ValueError(f"Input does not exist: {path}")
        if path.is_dir():
            candidates.extend(_load_directory(path))
        elif path.suffix.lower() == ".jsonl":
            candidates.extend(_load_jsonl(path))
        elif path.suffix.lower() == ".json":
            candidates.extend(_load_json(path))
        elif path.suffix.lower() in {".pdb", ".ent"}:
            candidates.append(_candidate_from_mapping({"pdbFile": str(path)}, path.parent, path))
        else:
            raise ValueError(f"Unsupported peptide result input: {path}")
    return candidates


def rank_peptides(
    candidates: list[PeptideCandidate],
    score: str = "ipsae",
    descending: bool | None = None,
    limit: int | None = None,
) -> list[PeptideCandidate]:
    """Sort candidates by a score alias or concrete score field."""
    ranked = list(candidates)
    if descending is None:
        descending = score_direction(score) == "desc"

    def key(candidate: PeptideCandidate) -> tuple[int, float]:
        value = candidate.score(score)
        if value is None:
            return (1, 0.0)
        return (0, -value if descending else value)

    ranked.sort(key=key)
    if limit is not None:
        ranked = ranked[: max(0, limit)]
    return ranked


def align_candidates_to_receptor(
    candidates: list[PeptideCandidate],
    base_receptor: str | Path,
    output_dir: str | Path,
    receptor_chains: list[str] | None = None,
    peptide_chain: str | None = None,
) -> list[PeptideCandidate]:
    """Align each candidate complex onto the base receptor frame when possible."""
    base_path = Path(base_receptor).expanduser().resolve()
    out_dir = Path(output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    aligned: list[PeptideCandidate] = []
    for index, candidate in enumerate(candidates, start=1):
        source = candidate.pdb_for_viewing()
        if not source and candidate.pdb_content:
            source = out_dir / f"{_safe_id(candidate.id)}_source.pdb"
            source.write_text(candidate.pdb_content, encoding="utf-8")
            candidate.pdb_path = source
        if not source:
            aligned.append(candidate)
            continue
        out_path = out_dir / f"{index:03d}_{_safe_id(candidate.id)}_aligned.pdb"
        result = align_pdb_to_receptor(
            complex_pdb=source,
            base_receptor_pdb=base_path,
            output_pdb=out_path,
            receptor_chains=receptor_chains,
            peptide_chain=peptide_chain,
        )
        candidate.aligned_pdb_path = out_path
        candidate.alignment_rmsd = result["rmsd"]
        candidate.alignment_atoms = int(result["atoms"])
        aligned.append(candidate)
    return aligned


def align_pdb_to_receptor(
    complex_pdb: str | Path,
    base_receptor_pdb: str | Path,
    output_pdb: str | Path,
    receptor_chains: list[str] | None = None,
    peptide_chain: str | None = None,
) -> dict[str, float]:
    """Rigidly align a receptor+peptide complex to a base receptor using CA atoms."""
    complex_path = Path(complex_pdb).expanduser().resolve()
    base_path = Path(base_receptor_pdb).expanduser().resolve()
    output_path = Path(output_pdb).expanduser().resolve()

    complex_text = complex_path.read_text(encoding="utf-8")
    moving_atoms = _parse_pdb_atoms(complex_text)
    fixed_atoms = _parse_pdb_atoms(base_path.read_text(encoding="utf-8"))
    chains = _resolve_receptor_chains(moving_atoms, fixed_atoms, receptor_chains, peptide_chain)
    moving_points, fixed_points = _matching_ca_points(moving_atoms, fixed_atoms, chains)
    if len(moving_points) < 3:
        raise ValueError(f"Need at least 3 matching receptor CA atoms; found {len(moving_points)}")

    rotation, translation, rmsd = _kabsch_quaternion(moving_points, fixed_points)
    lines = []
    for line in complex_text.splitlines():
        if line.startswith(("ATOM  ", "HETATM")) and len(line) >= 54:
            xyz = _parse_xyz(line)
            if xyz is not None:
                x, y, z = _transform_point(xyz, rotation, translation)
                line = f"{line[:30]}{x:8.3f}{y:8.3f}{z:8.3f}{line[54:]}"
        lines.append(line)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"rmsd": rmsd, "atoms": float(len(moving_points))}


def write_dashboard(
    candidates: list[PeptideCandidate],
    output_dir: str | Path,
    title: str = "LigandAI Peptide Viewer",
) -> DashboardHandle:
    """Write a 3Dmol.js dashboard plus local PDB assets."""
    out_dir = Path(output_dir).expanduser().resolve()
    structures_dir = out_dir / "structures"
    structures_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    for rank, candidate in enumerate(candidates, start=1):
        pdb_path = _materialize_candidate_pdb(candidate, structures_dir, rank)
        rows.append(
            {
                "rank": rank,
                "id": candidate.id,
                "gene": candidate.gene,
                "target": candidate.target,
                "sequence": candidate.sequence,
                "conformation": candidate.conformation,
                "scores": candidate.scores,
                "pdb": pdb_path.relative_to(out_dir).as_posix() if pdb_path else None,
                "alignmentRmsd": candidate.alignment_rmsd,
                "alignmentAtoms": candidate.alignment_atoms,
            }
        )

    (out_dir / "candidates.json").write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
    index_path = out_dir / "index.html"
    index_path.write_text(_dashboard_html(title), encoding="utf-8")
    return DashboardHandle(output_dir=out_dir, index_path=index_path)


def serve_dashboard(
    handle: DashboardHandle,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> DashboardHandle:
    """Serve a generated dashboard on localhost."""
    directory = str(handle.output_dir)

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=directory, **kwargs)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    handle.server = server
    handle.thread = thread
    handle.url = f"http://{host}:{server.server_port}/"
    if open_browser:
        webbrowser.open(handle.url)
    return handle


def launch_proteinview(
    candidate: PeptideCandidate,
    proteinview_bin: str = "proteinview",
    render: str | None = None,
    mode: str | None = None,
    color: str | None = None,
    hd: bool = False,
    fullhd: bool = False,
    extra_args: list[str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[Any]:
    """Launch ProteinView for one candidate structure."""
    if not Path(proteinview_bin).is_absolute() and shutil.which(proteinview_bin) is None:
        raise ValueError(f"ProteinView binary not found: {proteinview_bin}. {PROTEINVIEW_ATTRIBUTION}")
    pdb_path = candidate.pdb_for_viewing()
    if not pdb_path:
        raise ValueError(f"No PDB file available for candidate {candidate.id}")
    command = [proteinview_bin, str(pdb_path)]
    if render:
        command.extend(["--render", render])
    if mode:
        command.extend(["--mode", mode])
    if color:
        command.extend(["--color", color])
    if hd:
        command.append("--hd")
    if fullhd:
        command.append("--fullhd")
    if extra_args:
        command.extend(extra_args)
    return subprocess.run(command, check=check)


def extract_score(scores: dict[str, Any], score_name: str) -> float | None:
    flat = _flatten_scores(scores)
    for alias in _score_aliases(score_name):
        number = _to_float(flat.get(_normalize_key(alias)))
        if number is not None:
            return number
    return None


def score_direction(score_name: str) -> str:
    normalized = _normalize_key(score_name)
    if normalized in {"deltaforge", "deltag", "dg", "bindingenergy", "ebmenergy"}:
        return "asc"
    if "dg" in normalized or "energy" in normalized or normalized.endswith("kd"):
        return "asc"
    return "desc"


def _load_directory(path: Path) -> list[PeptideCandidate]:
    files = sorted(path.rglob("*_meta.json")) or sorted(path.rglob("*.json"))
    candidates: list[PeptideCandidate] = []
    for file_path in files:
        candidates.extend(_load_json(file_path))
    return candidates


def _load_jsonl(path: Path) -> list[PeptideCandidate]:
    candidates: list[PeptideCandidate] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                candidates.append(_candidate_from_mapping(json.loads(line), path.parent, path))
    return candidates


def _load_json(path: Path) -> list[PeptideCandidate]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [_candidate_from_mapping(item, path.parent, path) for item in _extract_candidate_items(data)]


def _extract_candidate_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("results", "peptides", "fold_results", "foldResults", "merged_entries", "entries"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            return [item for item in value.values() if isinstance(item, dict)]
    if "sequence" in data or "pdbFile" in data or "pdbContent" in data:
        return [data]
    return []


def _candidate_from_mapping(data: dict[str, Any], base_dir: Path, source_path: Path) -> PeptideCandidate:
    scores = _score_payload(data)
    sequence = str(_first(data, "sequence", "peptide_sequence", "peptideSequence", "mutatedSequence") or "")
    pdb_file = _first(data, "pdbFile", "pdb_file", "pdbPath", "pdb_path", "structurePath")
    pdb_path = _resolve_pdb_path(pdb_file, base_dir)
    if not pdb_path and source_path.name.endswith("_meta.json"):
        inferred = source_path.with_name(source_path.name.replace("_meta.json", ".pdb"))
        if inferred.exists():
            pdb_path = inferred
    candidate_id = str(
        _first(data, "id", "design_id", "designId", "peptide_id", "peptideId")
        or (pdb_path.stem if pdb_path else source_path.stem)
    )
    return PeptideCandidate(
        id=candidate_id,
        sequence=sequence,
        gene=_optional_str(_first(data, "gene", "targetGeneName", "target_gene_name")),
        target=_optional_str(_first(data, "target", "targetName", "target_gene", "gene")),
        conformation=_optional_str(_first(data, "conformation", "foldingConformation")),
        scores=scores,
        metadata=data,
        pdb_path=pdb_path,
        pdb_content=_optional_str(_first(data, "pdbContent", "pdb_content")),
        source_path=source_path,
    )


def _score_payload(data: dict[str, Any]) -> dict[str, Any]:
    scores = dict(data)
    for key in (
        "scores",
        "quality_scores",
        "qualityScores",
        "ipsae_scores",
        "ipsaeScores",
        "deltaforge",
        "deltaForge",
        "deltaforgeScores",
        "fold_metric_details",
        "foldMetricDetails",
        "plddt_details",
        "plddtDetails",
        "raw",
    ):
        value = data.get(key)
        if isinstance(value, dict):
            scores[key] = value
            scores.update(value)
    return scores


def _score_aliases(score_name: str) -> list[str]:
    normalized = _normalize_key(score_name)
    if normalized == "ipsae":
        return [
            "ipsae",
            "overall_ipsae",
            "overallIpsae",
            "peptide_ipsae",
            "peptideIpsae",
            "ipsae_d0res",
            "ipsae_scores.overall_ipsae",
            "scores.overall_ipsae",
        ]
    if normalized == "deltaforge":
        return [
            "delta_g",
            "deltaG",
            "deltaforge_dG",
            "v10_dg_best",
            "v10_dg_boltz2",
            "v10_dg_mean",
            "bindingEnergy",
            "binding_energy",
            "ebm_energy",
            "ligandiq_score",
        ]
    return [score_name]


def _flatten_scores(value: Any, prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            key_str = str(key)
            joined = f"{prefix}.{key_str}" if prefix else key_str
            flat[_normalize_key(joined)] = child
            flat[_normalize_key(key_str)] = child
            flat.update(_flatten_scores(child, joined))
    return flat


def _normalize_key(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch.isalnum())


def _first(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _resolve_pdb_path(value: Any, base_dir: Path) -> Path | None:
    if not value:
        return None
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve() if path.exists() else None


def _materialize_candidate_pdb(candidate: PeptideCandidate, structures_dir: Path, rank: int) -> Path | None:
    source = candidate.pdb_for_viewing()
    destination = structures_dir / f"{rank:03d}_{_safe_id(candidate.id)}.pdb"
    if source and source.exists():
        if source.resolve() != destination.resolve():
            shutil.copyfile(source, destination)
        return destination
    if candidate.pdb_content:
        destination.write_text(candidate.pdb_content, encoding="utf-8")
        return destination
    return None


def _safe_id(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value)
    return safe[:80] or "candidate"


def _parse_pdb_atoms(pdb_text: str) -> list[dict[str, Any]]:
    atoms = []
    for line in pdb_text.splitlines():
        if not line.startswith(("ATOM  ", "HETATM")) or len(line) < 54:
            continue
        xyz = _parse_xyz(line)
        if xyz is None:
            continue
        atoms.append(
            {
                "atom": line[12:16].strip(),
                "chain": line[21].strip() or "_",
                "resseq": line[22:26].strip(),
                "icode": line[26].strip(),
                "xyz": xyz,
            }
        )
    return atoms


def _parse_xyz(line: str) -> tuple[float, float, float] | None:
    try:
        return (float(line[30:38]), float(line[38:46]), float(line[46:54]))
    except ValueError:
        return None


def _resolve_receptor_chains(
    moving_atoms: list[dict[str, Any]],
    fixed_atoms: list[dict[str, Any]],
    receptor_chains: list[str] | None,
    peptide_chain: str | None,
) -> list[str]:
    if receptor_chains:
        return [str(chain) for chain in receptor_chains]
    moving = {atom["chain"] for atom in moving_atoms}
    fixed = {atom["chain"] for atom in fixed_atoms}
    common = sorted(moving.intersection(fixed))
    if peptide_chain:
        common = [chain for chain in common if chain != peptide_chain]
    return common or sorted(fixed)


def _matching_ca_points(
    moving_atoms: list[dict[str, Any]],
    fixed_atoms: list[dict[str, Any]],
    receptor_chains: list[str],
) -> tuple[list[tuple[float, float, float]], list[tuple[float, float, float]]]:
    chains = set(receptor_chains)
    moving = {
        (atom["chain"], atom["resseq"], atom["icode"]): atom["xyz"]
        for atom in moving_atoms
        if atom["atom"] == "CA" and atom["chain"] in chains
    }
    fixed = {
        (atom["chain"], atom["resseq"], atom["icode"]): atom["xyz"]
        for atom in fixed_atoms
        if atom["atom"] == "CA" and atom["chain"] in chains
    }
    keys = sorted(set(moving).intersection(fixed))
    return [moving[key] for key in keys], [fixed[key] for key in keys]


def _kabsch_quaternion(
    moving_points: list[tuple[float, float, float]],
    fixed_points: list[tuple[float, float, float]],
) -> tuple[list[list[float]], tuple[float, float, float], float]:
    moving_centroid = _centroid(moving_points)
    fixed_centroid = _centroid(fixed_points)
    p = [_sub(point, moving_centroid) for point in moving_points]
    q = [_sub(point, fixed_centroid) for point in fixed_points]

    sxx = sum(a[0] * b[0] for a, b in zip(p, q, strict=True))
    sxy = sum(a[0] * b[1] for a, b in zip(p, q, strict=True))
    sxz = sum(a[0] * b[2] for a, b in zip(p, q, strict=True))
    syx = sum(a[1] * b[0] for a, b in zip(p, q, strict=True))
    syy = sum(a[1] * b[1] for a, b in zip(p, q, strict=True))
    syz = sum(a[1] * b[2] for a, b in zip(p, q, strict=True))
    szx = sum(a[2] * b[0] for a, b in zip(p, q, strict=True))
    szy = sum(a[2] * b[1] for a, b in zip(p, q, strict=True))
    szz = sum(a[2] * b[2] for a, b in zip(p, q, strict=True))

    matrix = [
        [sxx + syy + szz, syz - szy, szx - sxz, sxy - syx],
        [syz - szy, sxx - syy - szz, sxy + syx, szx + sxz],
        [szx - sxz, sxy + syx, -sxx + syy - szz, syz + szy],
        [sxy - syx, szx + sxz, syz + szy, -sxx - syy + szz],
    ]
    quat = _dominant_eigenvector(matrix)
    rotation = _quaternion_to_matrix(quat)
    translation = _sub(fixed_centroid, _matvec(rotation, moving_centroid))
    transformed = [_transform_point(point, rotation, translation) for point in moving_points]
    rmsd = math.sqrt(sum(_dist2(a, b) for a, b in zip(transformed, fixed_points, strict=True)) / len(fixed_points))
    return rotation, translation, rmsd


def _dominant_eigenvector(matrix: list[list[float]]) -> tuple[float, float, float, float]:
    n = 4
    a = [row[:] for row in matrix]
    v = [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    for _ in range(80):
        p, q = 0, 1
        max_value = abs(a[p][q])
        for i in range(n):
            for j in range(i + 1, n):
                if abs(a[i][j]) > max_value:
                    max_value = abs(a[i][j])
                    p, q = i, j
        if max_value < 1e-12:
            break
        theta = 0.5 * math.atan2(2.0 * a[p][q], a[q][q] - a[p][p])
        c = math.cos(theta)
        s = math.sin(theta)
        app = c * c * a[p][p] - 2 * s * c * a[p][q] + s * s * a[q][q]
        aqq = s * s * a[p][p] + 2 * s * c * a[p][q] + c * c * a[q][q]
        a[p][p], a[q][q] = app, aqq
        a[p][q], a[q][p] = 0.0, 0.0
        for r in range(n):
            if r in (p, q):
                continue
            arp = c * a[r][p] - s * a[r][q]
            arq = s * a[r][p] + c * a[r][q]
            a[r][p] = a[p][r] = arp
            a[r][q] = a[q][r] = arq
        for r in range(n):
            vrp = c * v[r][p] - s * v[r][q]
            vrq = s * v[r][p] + c * v[r][q]
            v[r][p], v[r][q] = vrp, vrq
    idx = max(range(n), key=lambda i: a[i][i])
    raw = [v[i][idx] for i in range(n)]
    norm = math.sqrt(sum(x * x for x in raw)) or 1.0
    return (raw[0] / norm, raw[1] / norm, raw[2] / norm, raw[3] / norm)


def _quaternion_to_matrix(q: tuple[float, float, float, float]) -> list[list[float]]:
    w, x, y, z = q
    return [
        [w * w + x * x - y * y - z * z, 2 * (x * y - w * z), 2 * (x * z + w * y)],
        [2 * (x * y + w * z), w * w - x * x + y * y - z * z, 2 * (y * z - w * x)],
        [2 * (x * z - w * y), 2 * (y * z + w * x), w * w - x * x - y * y + z * z],
    ]


def _centroid(points: list[tuple[float, float, float]]) -> tuple[float, float, float]:
    count = float(len(points))
    return (
        sum(point[0] for point in points) / count,
        sum(point[1] for point in points) / count,
        sum(point[2] for point in points) / count,
    )


def _sub(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _matvec(matrix: list[list[float]] | tuple[tuple[float, ...], ...], point: tuple[float, float, float]) -> tuple[float, float, float]:
    return (
        matrix[0][0] * point[0] + matrix[0][1] * point[1] + matrix[0][2] * point[2],
        matrix[1][0] * point[0] + matrix[1][1] * point[1] + matrix[1][2] * point[2],
        matrix[2][0] * point[0] + matrix[2][1] * point[1] + matrix[2][2] * point[2],
    )


def _transform_point(
    point: tuple[float, float, float],
    rotation: list[list[float]],
    translation: tuple[float, float, float],
) -> tuple[float, float, float]:
    rotated = _matvec(rotation, point)
    return (rotated[0] + translation[0], rotated[1] + translation[1], rotated[2] + translation[2])


def _dist2(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2


def build_comparison_summary(
    candidates: list[PeptideCandidate],
    metrics: tuple[str, ...] = ("iptm", "ipsae", "ptm", "plddt", "deltaforge"),
) -> dict[str, Any]:
    """Assemble the multi-engine comparison summary — RAW is never adjusted.

    Every number is computed in PYTHON here, at write-time, from
    :mod:`ligandai.fold_calibration`. Raw scores are carried through unchanged;
    the only cross-engine footing is the within-engine percentile (where a raw
    value sits in THAT engine's own distribution of the user's folds). The
    returned structure carries, per metric:

    - ``best_per_engine`` — the best RAW fold within each engine (ranked on raw,
      respecting the metric direction). Each entry carries its within-engine
      percentile + label as an annotation.
    - ``best_aggregate`` — the engine whose best fold has the highest mean
      within-engine percentile (consensus standing), NOT averaged raw and NOT a
      rescaled score. The raw value backing it is shown.
    - ``agreement`` — from :func:`ligandai.fold_calibration.engine_agreement`,
      judged on percentile spread, so it surfaces "high raw spread but engines
      agree on standing" and the inverse.

    DeltaForge dG is reported per engine with its own within-engine standing
    (no shared band). The browser only renders these precomputed fields — it
    never recomputes a percentile.
    """
    from ligandai.fold_calibration import (
        METRIC_META,
        build_distributions,
        engine_agreement,
        normalize_engine,
        normalize_metric,
        standing,
    )

    canonical_metrics = [normalize_metric(metric) for metric in metrics]

    # Build within-engine distributions from the user's own folds. One record
    # per (candidate, metric) carrying the raw value the engine reported.
    distribution_records: list[dict[str, Any]] = []
    for candidate in candidates:
        engine = normalize_engine(_candidate_engine(candidate))
        record: dict[str, Any] = {"engine": engine}
        for metric in canonical_metrics:
            if metric not in METRIC_META:
                continue
            raw = candidate.score(metric)
            if raw is not None:
                record[metric] = raw
        distribution_records.append(record)
    distributions = build_distributions(distribution_records, metrics=tuple(canonical_metrics))

    def _entry(engine: str, metric: str, raw: float, candidate_id: str, sequence: str) -> dict[str, Any]:
        info = standing(distributions, engine, metric, raw)
        return {
            "raw": raw,  # unchanged, byte-identical to what the engine reported
            "percentile": info.percentile if info else None,
            "label": info.label if info else None,
            "n": info.n if info else 0,
            "candidate_id": candidate_id,
            "sequence": sequence,
        }

    # records[seq][metric][engine] -> best RAW entry (per metric direction).
    records: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
    sequences: list[str] = []
    for candidate in candidates:
        sequence = candidate.sequence or candidate.id
        engine = normalize_engine(_candidate_engine(candidate))
        if sequence not in records:
            records[sequence] = {}
            sequences.append(sequence)
        for metric in canonical_metrics:
            meta = METRIC_META.get(metric)
            if meta is None:
                continue
            raw = candidate.score(metric)
            if raw is None:
                continue
            higher_is_better = meta.higher_is_better
            slot = records[sequence].setdefault(metric, {})
            existing = slot.get(engine)
            keep = (
                existing is None
                or (higher_is_better and raw > existing["raw"])
                or (not higher_is_better and raw < existing["raw"])
            )
            if keep:
                slot[engine] = _entry(engine, metric, raw, candidate.id, sequence)

    per_metric: dict[str, Any] = {}
    for metric in canonical_metrics:
        meta = METRIC_META.get(metric)
        if meta is None:
            continue
        higher_is_better = meta.higher_is_better
        # Best fold per engine = best RAW within that engine, across sequences.
        per_engine_best: dict[str, dict[str, Any]] = {}
        for sequence in sequences:
            slot = records.get(sequence, {}).get(metric, {})
            for engine, entry in slot.items():
                current = per_engine_best.get(engine)
                better_raw = (
                    current is None
                    or (higher_is_better and entry["raw"] > current["raw"])
                    or (not higher_is_better and entry["raw"] < current["raw"])
                )
                if better_raw:
                    per_engine_best[engine] = entry

        # Best aggregate = the engine whose best fold ranks highest by within-
        # engine percentile (consensus standing). Never averaged raw, never a
        # rescaled score. Engines without a distribution (percentile None) sort
        # below those with one; ties fall back to raw direction.
        best_aggregate: dict[str, Any] | None = None
        for engine, entry in per_engine_best.items():
            candidate_agg = {**entry, "engine": engine}
            if best_aggregate is None:
                best_aggregate = candidate_agg
                continue
            cur_pct = best_aggregate.get("percentile")
            new_pct = entry.get("percentile")
            if new_pct is not None and (cur_pct is None or new_pct > cur_pct):
                best_aggregate = candidate_agg
            elif new_pct is not None and cur_pct is not None and new_pct == cur_pct:
                tie = (
                    (higher_is_better and entry["raw"] > best_aggregate["raw"])
                    or (not higher_is_better and entry["raw"] < best_aggregate["raw"])
                )
                if tie:
                    best_aggregate = candidate_agg

        # Agreement per sequence; surface the sequence with the most engines so
        # the agreement panel is maximally informative.
        agreement_payload: dict[str, Any] | None = None
        best_coverage = 0
        for sequence in sequences:
            slot = records.get(sequence, {}).get(metric, {})
            if len(slot) < 2:
                continue
            per_engine_raw = {engine: entry["raw"] for engine, entry in slot.items()}
            agreement = engine_agreement(per_engine_raw, metric, distributions)
            if agreement is None:
                continue
            coverage = len(agreement.raw)
            if coverage > best_coverage:
                best_coverage = coverage
                agreement_payload = {
                    "sequence": sequence,
                    "metric": agreement.metric,
                    "raw": agreement.raw,
                    "percentile": agreement.percentile,
                    "labels": agreement.labels,
                    "n": agreement.n,
                    "mean_percentile": agreement.mean_percentile,
                    "percentile_spread": agreement.percentile_spread,
                    "raw_spread": agreement.raw_spread,
                    "agree": agreement.agree,
                    "best_engine": agreement.best_engine,
                    "worst_engine": agreement.worst_engine,
                    "consensus_label": agreement.consensus_label,
                    "note": agreement.note,
                }

        per_metric[metric] = {
            "label": meta.label,
            "higher_is_better": meta.higher_is_better,
            "best_per_engine": per_engine_best,
            "best_aggregate": best_aggregate,
            "agreement": agreement_payload,
        }

    return {
        "metrics": canonical_metrics,
        "per_metric": per_metric,
        "engines": sorted({normalize_engine(_candidate_engine(c)) for c in candidates}),
        "sequence_count": len(sequences),
        "standing_source": distributions.source,
        "min_distribution_samples": _min_distribution_samples(),
    }


def _min_distribution_samples() -> int:
    from ligandai.fold_calibration import MIN_DISTRIBUTION_SAMPLES

    return MIN_DISTRIBUTION_SAMPLES


def _candidate_engine(candidate: PeptideCandidate) -> str | None:
    """Resolve the structure-prediction engine that produced a candidate."""
    for key in ("engine", "method", "fold_method", "foldMethod", "model", "predictor"):
        value = candidate.scores.get(key) if isinstance(candidate.scores, dict) else None
        if value:
            return str(value)
        meta_value = candidate.metadata.get(key) if isinstance(candidate.metadata, dict) else None
        if meta_value:
            return str(meta_value)
    return None


def write_comparison_dashboard(
    candidates: list[PeptideCandidate],
    output_dir: str | Path,
    title: str = "LigandAI Fold Comparison",
) -> DashboardHandle:
    """Write a clickable multi-engine comparison dashboard — RAW is never adjusted.

    Summarizes ALL of a user's fold results on RAW scores in native units, with
    a within-engine percentile annotation beside each: best RAW fold per method,
    best aggregate across engines (ranked by mean within-engine percentile, not
    averaged raw), and a model agreement/disagreement panel (percentile spread).
    DeltaForge dG is shown per engine with its own within-engine standing. The
    per-candidate 3Dmol.js structure viewer is reused from the legacy dashboard.
    Everything is computed in Python here and embedded as ``summary.json`` +
    per-row ``standing``/``raw`` fields; the JavaScript only renders these
    precomputed fields and never recomputes a percentile.

    This is additive: it does NOT alter :func:`write_dashboard`'s behavior.
    """
    from ligandai.fold_calibration import (
        METRIC_META,
        build_distributions,
        normalize_engine,
        normalize_metric,
        standing,
    )

    out_dir = Path(output_dir).expanduser().resolve()
    structures_dir = out_dir / "structures"
    structures_dir.mkdir(parents=True, exist_ok=True)

    metric_keys = tuple(METRIC_META.keys())

    # Within-engine distributions from the user's own folds (raw-derived).
    distribution_records: list[dict[str, Any]] = []
    for candidate in candidates:
        engine = normalize_engine(_candidate_engine(candidate))
        record: dict[str, Any] = {"engine": engine}
        for metric in metric_keys:
            raw = candidate.score(metric)
            if raw is not None:
                record[normalize_metric(metric)] = raw
        distribution_records.append(record)
    distributions = build_distributions(distribution_records, metrics=metric_keys)

    rows: list[dict[str, Any]] = []
    for rank, candidate in enumerate(candidates, start=1):
        pdb_path = _materialize_candidate_pdb(candidate, structures_dir, rank)
        engine = normalize_engine(_candidate_engine(candidate))
        standings: dict[str, dict[str, Any]] = {}
        for metric in metric_keys:
            raw = candidate.score(metric)
            if raw is None:
                continue
            info = standing(distributions, engine, normalize_metric(metric), raw)
            standings[normalize_metric(metric)] = {
                "raw": raw,  # unchanged
                "percentile": info.percentile if info else None,
                "label": info.label if info else None,
                "n": info.n if info else 0,
            }
        rows.append(
            {
                "rank": rank,
                "id": candidate.id,
                "gene": candidate.gene,
                "target": candidate.target,
                "sequence": candidate.sequence,
                "conformation": candidate.conformation,
                "engine": engine,
                "scores": candidate.scores,
                "standing": standings,
                "pdb": pdb_path.relative_to(out_dir).as_posix() if pdb_path else None,
                "alignmentRmsd": candidate.alignment_rmsd,
                "alignmentAtoms": candidate.alignment_atoms,
            }
        )

    summary = build_comparison_summary(candidates, metrics=metric_keys)
    (out_dir / "candidates.json").write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    index_path = out_dir / "index.html"
    index_path.write_text(_comparison_dashboard_html(title), encoding="utf-8")
    return DashboardHandle(output_dir=out_dir, index_path=index_path)


def serve_comparison_dashboard(
    handle: DashboardHandle,
    host: str = "127.0.0.1",
    port: int = 8766,
    open_browser: bool = True,
) -> DashboardHandle:
    """Serve a generated comparison dashboard on localhost.

    Thin wrapper over :func:`serve_dashboard` with a distinct default port so a
    comparison dashboard and a legacy single-candidate dashboard can run side by
    side.
    """
    return serve_dashboard(handle, host=host, port=port, open_browser=open_browser)


def _dashboard_html(title: str) -> str:
    escaped_title = html.escape(title)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escaped_title}</title>
  <script src="https://3Dmol.org/build/3Dmol-min.js"></script>
  <style>
    body {{ margin: 0; font-family: Inter, system-ui, -apple-system, sans-serif; background: #0f1419; color: #e6edf3; }}
    main {{ display: grid; grid-template-columns: 390px 1fr; min-height: 100vh; }}
    aside {{ border-right: 1px solid #26313d; padding: 18px; overflow: auto; background: #111820; }}
    h1 {{ font-size: 18px; margin: 0 0 12px; }}
    .subtle {{ color: #93a4b7; font-size: 12px; }}
    .scorebar {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin: 12px 0; }}
    .metric {{ background: #17212b; border: 1px solid #26313d; border-radius: 6px; padding: 8px; }}
    .metric b {{ display: block; color: #7dd3fc; font-size: 13px; }}
    select, button {{ width: 100%; background: #17212b; color: #e6edf3; border: 1px solid #334155; border-radius: 6px; padding: 8px; }}
    button {{ cursor: pointer; margin-top: 8px; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 12px; }}
    tr {{ cursor: pointer; }}
    tr.active {{ background: #123047; }}
    td, th {{ border-bottom: 1px solid #26313d; padding: 7px 5px; text-align: left; }}
    #viewer {{ width: 100%; height: 100vh; }}
    .sequence {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; color: #c4b5fd; word-break: break-all; }}
    footer {{ margin-top: 14px; color: #718096; font-size: 11px; line-height: 1.4; }}
    @media (max-width: 900px) {{ main {{ grid-template-columns: 1fr; }} #viewer {{ height: 70vh; }} }}
  </style>
</head>
<body>
<main>
  <aside>
    <h1>{escaped_title}</h1>
    <div class="subtle">Ranked LigandForge/PTF fold results with iPSAE and DeltaForge-style scores.</div>
    <div style="margin: 14px 0">
      <select id="candidateSelect"></select>
      <button id="prevBtn">Previous</button>
      <button id="nextBtn">Next</button>
    </div>
    <div class="scorebar">
      <div class="metric"><span>iPSAE</span><b id="ipsae">-</b></div>
      <div class="metric"><span>DeltaG</span><b id="dg">-</b></div>
      <div class="metric"><span>ipTM</span><b id="iptm">-</b></div>
      <div class="metric"><span>pLDDT</span><b id="plddt">-</b></div>
    </div>
    <div id="details"></div>
    <table><thead><tr><th>#</th><th>Gene</th><th>Seq</th><th>iPSAE</th></tr></thead><tbody id="rows"></tbody></table>
    <footer>Terminal option: ProteinView by Tristan Farmer / 001TMF, MIT License. https://github.com/001TMF/ProteinView</footer>
  </aside>
  <section><div id="viewer"></div></section>
</main>
<script>
let candidates = [];
let selected = 0;
const fmt = value => value === null || value === undefined || Number.isNaN(Number(value)) ? '-' : Number(value).toFixed(3);
const score = (c, names) => {{
  const flat = c.scores || {{}};
  for (const name of names) if (flat[name] !== undefined && flat[name] !== null) return flat[name];
  return null;
}};
function renderRows() {{
  const select = document.getElementById('candidateSelect');
  const rows = document.getElementById('rows');
  select.innerHTML = '';
  rows.innerHTML = '';
  candidates.forEach((c, i) => {{
    const opt = document.createElement('option');
    opt.value = i;
    opt.textContent = `${{c.rank}}. ${{c.gene || c.target || 'peptide'}} ${{c.sequence || ''}}`;
    select.appendChild(opt);
    const tr = document.createElement('tr');
    tr.className = i === selected ? 'active' : '';
    tr.innerHTML = `<td>${{c.rank}}</td><td>${{c.gene || ''}}</td><td class="sequence">${{(c.sequence || '').slice(0, 18)}}</td><td>${{fmt(score(c, ['ipsae','overall_ipsae','peptide_ipsae']))}}</td>`;
    tr.onclick = () => setSelected(i);
    rows.appendChild(tr);
  }});
  select.value = selected;
}}
function setSelected(index) {{
  selected = Math.max(0, Math.min(candidates.length - 1, index));
  renderRows();
  renderCandidate();
}}
async function renderCandidate() {{
  const c = candidates[selected];
  if (!c) return;
  document.getElementById('ipsae').textContent = fmt(score(c, ['ipsae','overall_ipsae','peptide_ipsae']));
  document.getElementById('dg').textContent = fmt(score(c, ['delta_g','deltaG','bindingEnergy','v10_dg_best']));
  document.getElementById('iptm').textContent = fmt(score(c, ['iptm']));
  document.getElementById('plddt').textContent = fmt(score(c, ['plddt','mean_plddt','complex_plddt']));
  document.getElementById('details').innerHTML = `<div class="sequence">${{c.sequence || ''}}</div><p class="subtle">${{c.conformation || ''}}${{c.alignmentRmsd ? ' | receptor RMSD ' + fmt(c.alignmentRmsd) + ' A' : ''}}</p>`;
  const viewer = $3Dmol.createViewer('viewer', {{ backgroundColor: '#0f1419' }});
  if (!c.pdb) {{ viewer.render(); return; }}
  const pdb = await fetch(c.pdb).then(r => r.text());
  viewer.addModel(pdb, 'pdb');
  viewer.setStyle({{}}, {{ cartoon: {{ color: 'spectrum' }} }});
  viewer.addStyle({{chain: 'Z'}}, {{stick: {{colorscheme: 'greenCarbon', radius: 0.18}}}});
  viewer.zoomTo();
  viewer.render();
}}
document.getElementById('candidateSelect').onchange = e => setSelected(Number(e.target.value));
document.getElementById('prevBtn').onclick = () => setSelected(selected - 1);
document.getElementById('nextBtn').onclick = () => setSelected(selected + 1);
fetch('candidates.json').then(r => r.json()).then(data => {{ candidates = data; renderRows(); renderCandidate(); }});
</script>
</body>
</html>
"""


def _comparison_dashboard_html(title: str) -> str:
    """HTML for the multi-engine comparison dashboard — RAW shown, never adjusted.

    The browser renders ONLY precomputed fields from ``candidates.json`` (per row
    ``standing[metric].{{raw,percentile,label,n}}``) and ``summary.json``
    (``per_metric[metric].{{best_per_engine,best_aggregate,agreement}}``). The
    raw value is shown prominently and unchanged; the within-engine percentile is
    an annotation beside it. No per-engine band/calibration logic exists in the
    JavaScript — standings are computed solely in Python via
    :mod:`ligandai.fold_calibration` and the page never recomputes a percentile.
    """
    escaped_title = html.escape(title)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escaped_title}</title>
  <script src="https://3Dmol.org/build/3Dmol-min.js"></script>
  <style>
    body {{ margin: 0; font-family: Inter, system-ui, -apple-system, sans-serif; background: #0f1419; color: #e6edf3; }}
    header {{ padding: 16px 22px; border-bottom: 1px solid #26313d; background: #111820; }}
    header h1 {{ font-size: 18px; margin: 0; }}
    header .subtle {{ color: #93a4b7; font-size: 12px; margin-top: 4px; }}
    main {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; padding: 18px 22px; }}
    section.panel {{ background: #111820; border: 1px solid #26313d; border-radius: 8px; padding: 14px; }}
    section.panel h2 {{ font-size: 14px; margin: 0 0 10px; color: #7dd3fc; }}
    .full {{ grid-column: 1 / -1; }}
    .viewerwrap {{ display: grid; grid-template-columns: 360px 1fr; gap: 12px; min-height: 420px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
    td, th {{ border-bottom: 1px solid #26313d; padding: 6px 5px; text-align: left; }}
    tr.clickable {{ cursor: pointer; }}
    tr.active {{ background: #123047; }}
    .sequence {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; color: #c4b5fd; word-break: break-all; }}
    .raw {{ font-weight: 600; color: #e6edf3; }}
    .pct {{ color: #a5b4fc; font-size: 11px; }}
    .label-bottom {{ color: #f87171; }}
    .label-low {{ color: #fb923c; }}
    .label-mid {{ color: #facc15; }}
    .label-high {{ color: #4ade80; }}
    .label-top {{ color: #34d399; font-weight: 600; }}
    .pill {{ display: inline-block; border-radius: 10px; padding: 1px 8px; font-size: 11px; }}
    .agree {{ background: #14532d; color: #bbf7d0; }}
    .disagree {{ background: #7f1d1d; color: #fecaca; }}
    .tiles {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 8px; }}
    .tile {{ background: #17212b; border: 1px solid #26313d; border-radius: 6px; padding: 8px; }}
    .tile small {{ color: #93a4b7; display: block; }}
    .tile b {{ font-size: 14px; }}
    #viewer {{ width: 100%; height: 420px; border-radius: 6px; }}
    select, button {{ background: #17212b; color: #e6edf3; border: 1px solid #334155; border-radius: 6px; padding: 7px; }}
    button {{ cursor: pointer; }}
    footer {{ padding: 10px 22px; color: #718096; font-size: 11px; }}
  </style>
</head>
<body>
<header>
  <h1>{escaped_title}</h1>
  <div class="subtle" id="headSub">Raw scores in native units — each shown with its within-engine percentile (computed in Python). Raw values are never rescaled or calibrated across engines.</div>
</header>
<main>
  <section class="panel full">
    <h2>Best fold per method (best RAW per engine) &amp; best aggregate (consensus standing)</h2>
    <div id="bestPerMethod"></div>
  </section>
  <section class="panel">
    <h2>Model agreement / disagreement (within-engine percentile spread)</h2>
    <div id="agreement"></div>
  </section>
  <section class="panel">
    <h2>DeltaForge dG — per engine (own standing)</h2>
    <div id="deltaforge"></div>
  </section>
  <section class="panel full">
    <h2>Per-candidate structure</h2>
    <div class="viewerwrap">
      <div>
        <select id="candidateSelect"></select>
        <table><thead><tr><th>#</th><th>Engine</th><th>Seq</th><th>iPSAE (raw)</th><th>pct</th></tr></thead><tbody id="rows"></tbody></table>
      </div>
      <div id="viewer"></div>
    </div>
  </section>
</main>
<footer>Raw values, within-engine percentiles, and agreement verdicts are precomputed in Python (ligandai.fold_calibration) and embedded in summary.json / candidates.json. The browser shows the raw values and renders the precomputed percentiles; it does not recompute bands. ProteinView by Tristan Farmer / 001TMF, MIT License.</footer>
<script>
let candidates = [];
let summary = {{ per_metric: {{}}, metrics: [] }};
let selected = 0;
const fmt = v => (v === null || v === undefined || Number.isNaN(Number(v))) ? '-' : Number(v).toFixed(3);
const labelClass = l => l ? 'label-' + l : '';
const pctTxt = (p, l) => (p === null || p === undefined) ? '<span class="pct">no dist</span>' : `<span class="pct ${{labelClass(l)}}">p${{Math.round(p * 100)}} ${{l || ''}}</span>`;
function standingOf(c, metric) {{ return (c.standing && c.standing[metric]) ? c.standing[metric] : null; }}
function renderBestPerMethod() {{
  const host = document.getElementById('bestPerMethod');
  const metrics = (summary.metrics || []).filter(m => m !== 'deltaforge');
  let parts = [];
  for (const metric of metrics) {{
    const pm = summary.per_metric[metric];
    if (!pm) continue;
    const best = pm.best_per_engine || {{}};
    const rows = Object.keys(best).map(engine => {{
      const e = best[engine];
      return `<tr><td>${{engine}}</td><td class="raw">${{fmt(e.raw)}}</td><td>${{pctTxt(e.percentile, e.label)}}</td><td class="sequence">${{(e.sequence || '').slice(0,14)}}</td></tr>`;
    }}).join('');
    const agg = pm.best_aggregate;
    const aggTxt = agg ? `<div class="tile"><small>best aggregate (consensus standing)</small><b>${{agg.engine}}</b> raw ${{fmt(agg.raw)}} ${{pctTxt(agg.percentile, agg.label)}}</div>` : '';
    parts.push(`<div style="margin-bottom:12px"><b>${{pm.label}}</b>${{aggTxt}}<table><thead><tr><th>engine</th><th>raw</th><th>within-engine</th><th>seq</th></tr></thead><tbody>${{rows}}</tbody></table></div>`);
  }}
  host.innerHTML = parts.join('') || '<p class="subtle">No per-engine metrics present.</p>';
}}
function renderAgreement() {{
  const host = document.getElementById('agreement');
  const metrics = (summary.metrics || []).filter(m => m !== 'deltaforge');
  let parts = [];
  for (const metric of metrics) {{
    const pm = summary.per_metric[metric];
    if (!pm || !pm.agreement) continue;
    const a = pm.agreement;
    const pill = a.agree ? '<span class="pill agree">AGREE</span>' : '<span class="pill disagree">DISAGREE</span>';
    parts.push(`<div style="margin-bottom:10px"><b>${{pm.label}}</b> ${{pill}}<br>
      <span class="subtle">seq ${{(a.sequence||'').slice(0,14)}} · raw spread ${{fmt(a.raw_spread)}} · within-engine percentile spread ${{fmt(a.percentile_spread)}} · consensus <span class="${{labelClass(a.consensus_label)}}">${{a.consensus_label}}</span></span><br>
      <span class="subtle">${{a.note || ''}}</span></div>`);
  }}
  host.innerHTML = parts.join('') || '<p class="subtle">Need ≥2 engines on a shared sequence for agreement.</p>';
}}
function renderDeltaForge() {{
  const host = document.getElementById('deltaforge');
  const pm = summary.per_metric['deltaforge'];
  if (!pm) {{ host.innerHTML = '<p class="subtle">No DeltaForge dG reported.</p>'; return; }}
  const best = pm.best_per_engine || {{}};
  const tiles = Object.keys(best).map(engine => {{
    const e = best[engine];
    return `<div class="tile"><small>${{engine}}</small><b>${{fmt(e.raw)}} kcal/mol</b>${{pctTxt(e.percentile, e.label)}}</div>`;
  }}).join('');
  host.innerHTML = `<div class="tiles">${{tiles}}</div><p class="subtle">dG is shown per engine with its own within-engine standing — no shared band, no engine-unbiased rescale.</p>`;
}}
function renderRows() {{
  const select = document.getElementById('candidateSelect');
  const rows = document.getElementById('rows');
  select.innerHTML = '';
  rows.innerHTML = '';
  candidates.forEach((c, i) => {{
    const opt = document.createElement('option');
    opt.value = i;
    opt.textContent = `${{c.rank}}. ${{c.engine || 'engine'}} ${{c.gene || ''}} ${{(c.sequence||'').slice(0,16)}}`;
    select.appendChild(opt);
    const st = standingOf(c, 'ipsae');
    const tr = document.createElement('tr');
    tr.className = 'clickable' + (i === selected ? ' active' : '');
    tr.innerHTML = `<td>${{c.rank}}</td><td>${{c.engine || ''}}</td><td class="sequence">${{(c.sequence||'').slice(0,14)}}</td><td class="raw">${{st ? fmt(st.raw) : '-'}}</td><td>${{st ? pctTxt(st.percentile, st.label) : '-'}}</td>`;
    tr.onclick = () => setSelected(i);
    rows.appendChild(tr);
  }});
  select.value = selected;
}}
function setSelected(index) {{
  selected = Math.max(0, Math.min(candidates.length - 1, index));
  renderRows();
  renderCandidate();
}}
async function renderCandidate() {{
  const c = candidates[selected];
  if (!c) return;
  const viewer = $3Dmol.createViewer('viewer', {{ backgroundColor: '#0f1419' }});
  if (!c.pdb) {{ viewer.render(); return; }}
  const pdb = await fetch(c.pdb).then(r => r.text());
  viewer.addModel(pdb, 'pdb');
  viewer.setStyle({{}}, {{ cartoon: {{ color: 'spectrum' }} }});
  viewer.addStyle({{chain: 'Z'}}, {{stick: {{colorscheme: 'greenCarbon', radius: 0.18}}}});
  viewer.zoomTo();
  viewer.render();
}}
document.getElementById('candidateSelect').onchange = e => setSelected(Number(e.target.value));
Promise.all([
  fetch('candidates.json').then(r => r.json()),
  fetch('summary.json').then(r => r.json()),
]).then(([cands, summ]) => {{
  candidates = cands;
  summary = summ;
  renderBestPerMethod();
  renderAgreement();
  renderDeltaForge();
  renderRows();
  renderCandidate();
}});
</script>
</body>
</html>
"""
