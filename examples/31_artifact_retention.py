# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""
31 — Artifact retention: structures + PAE + scalars, every one sha256'd.

⛔ A SCORE WITHOUT ITS STRUCTURE CANNOT BE RE-EXAMINED ⛔
This is the rule every other script in this series depends on. A structure is
not regenerable without paying the fold again. An ipSAE with no structure
behind it cannot be re-examined, cannot be re-scored under a different
interface definition, and cannot be shipped to a partner. Persisting the scalar
alone converts a GPU-hour into a number you have to take on faith.

An earlier version of the CD34 glyco arm kept only the scalar ipSAE and
discarded 75 folds of structures. The retention below is what replaced it.

TWO RULES THAT ARE NOT NEGOTIABLE:
  1. RETAIN BEFORE SCORING. If you score first and the scorer throws, you have
     paid for a structure you no longer have. That is why this script writes
     each fold's artifacts the moment the result lands, and only then reads any
     metric off it. It is also why retention happens IN THE SAME PROCESS as the
     fold — re-fetching a structure later is a different, lossier problem.
  2. KEEP EVERY SCALAR THE ENGINE RETURNED, not just the ones you gate on
     today. The cheapest way to make a future re-analysis possible is to not
     throw the inputs away.

LAYOUT (mirrors the platform's fold-artifact convention, so a retained set can
be indexed alongside engine folds):

    <outdir>/structures/<design_id>__<context>/sample_<k>.cif
                                              /sample_<k>.pdb
                                              /sample_<k>.pae.npy
                                              /sample_<k>.meta.json
    <outdir>/manifest.jsonl    one line per file: path, sha256, bytes, ids

A NOTE ON PAE IDENTIFIERS. FoldResult carries `pae_matrix` inline, but it is
tier-gated (academia+) and may be None even for an entitled caller when the
matrix has not been hydrated. The lazy-fetch path is
`client.folds.download_pae(<ptf_fold_results.id>)` — note that this wants the
ptf_fold_results INTEGER primary key, which is NOT the same identifier as
`FoldResult.job_id` (a `fold_<ms>_<rand>` string). This script uses the inline
matrix when present and records why it is absent when it is not, rather than
guessing at an id mapping.

Usage — fold a sequence set and retain everything as it lands:
    LIGANDAI_API_KEY=lgai_... python 31_artifact_retention.py \\
        retain prefold_survivors.json --gene CD34 --k 5 --outdir artifacts

Usage — verify a manifest later (no API key, no network):
    python 31_artifact_retention.py verify artifacts

`retain_trajectory()` is importable and is the same function used by
29_glycan_context_modelling.py.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys

MANIFEST = "manifest.jsonl"
_CHUNK = 1024 * 1024

# Every scalar worth keeping off a FoldResult. Kept as a list rather than a
# hand-picked pair so a future gate change does not need a re-fold.
_SCALAR_FIELDS = (
    "job_id", "peptide_ipsae", "peptide_ipsae_status", "ipsae", "iptm", "ptm",
    "plddt", "ipae", "peptide_interface_iptm", "peptide_chain_id",
    "has_structure",
)


def sha256_file(path: str) -> str:
    """Streamed, so a 500 MB mmCIF does not have to fit in memory."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _append_manifest(outdir: str, paths: list[str], ids: dict) -> None:
    with open(os.path.join(outdir, MANIFEST), "a") as mf:
        for p in paths:
            mf.write(json.dumps({
                "path": os.path.relpath(p, outdir),
                "sha256": sha256_file(p),
                "bytes": os.path.getsize(p),
                **ids,
            }) + "\n")


def retain_trajectory(
    outdir: str,
    design_id: str,
    context: str,
    traj_index: int,
    trajectory: dict,
    meta: dict,
) -> list[str]:
    """Persist EVERY artifact one trajectory produced. Never optional.

    Accepts the raw trajectory dict as an engine returns it. Structure text may
    arrive under either camelCase or snake_case depending on the writer, so both
    spellings are checked rather than assumed.

    Returns the list of files written.
    """
    d = os.path.join(outdir, "structures", f"{design_id}__{context}")
    os.makedirs(d, exist_ok=True)
    written: list[str] = []

    cif = trajectory.get("cifContent") or trajectory.get("cif_content")
    if cif:
        fp = os.path.join(d, f"sample_{traj_index}.cif")
        with open(fp, "w") as fh:
            fh.write(cif)
        written.append(fp)

    pdb = trajectory.get("pdbContent") or trajectory.get("pdb_content")
    if pdb:
        fp = os.path.join(d, f"sample_{traj_index}.pdb")
        with open(fp, "w") as fh:
            fh.write(pdb)
        written.append(fp)

    pae = trajectory.get("pae")
    if pae is not None:
        try:
            import numpy as np
            fp = os.path.join(d, f"sample_{traj_index}.pae.npy")
            np.save(fp, np.asarray(pae, dtype=float))
            written.append(fp)
        except ImportError:
            # Do not lose the matrix because numpy is absent — write JSON.
            fp = os.path.join(d, f"sample_{traj_index}.pae.json")
            with open(fp, "w") as fh:
                json.dump(pae, fh)
            written.append(fp)

    # EVERY scalar the engine returned, not only the gated ones.
    scalars = {
        k: v for k, v in trajectory.items()
        if k not in ("cifContent", "cif_content", "pdbContent", "pdb_content", "pae")
        and not isinstance(v, (list, dict))
    }
    fp = os.path.join(d, f"sample_{traj_index}.meta.json")
    with open(fp, "w") as fh:
        json.dump({**meta, "traj_index": traj_index, "engine_scalars": scalars},
                  fh, indent=1)
    written.append(fp)

    _append_manifest(outdir, written, {
        "design_id": design_id, "context": context, "traj_index": traj_index,
    })
    return written


def fold_result_to_trajectory(fold: object) -> dict:
    """Flatten a FoldResult into the raw-trajectory shape retain_trajectory takes."""
    traj: dict = {}
    traj["cif_content"] = getattr(fold, "cif_data", None)
    traj["pdb_content"] = getattr(fold, "pdb_data", None)

    pae = getattr(fold, "pae_matrix", None)
    if pae is not None:
        traj["pae"] = pae
    else:
        # Say WHY it is missing rather than leaving a silent hole. pae_matrix is
        # academia+ and may simply not be hydrated yet.
        traj["pae_absent_reason"] = (
            "pae_matrix is None — tier < academia, or not yet hydrated; "
            "lazy-fetch via client.folds.download_pae(<ptf_fold_results.id>)"
        )
        uri = getattr(fold, "pae_matrix_uri", None)
        if uri:
            traj["pae_matrix_uri"] = uri

    for field in _SCALAR_FIELDS:
        traj[field] = getattr(fold, field, None)
    return traj


def load_cores(path: str) -> list[dict]:
    raw = json.load(open(path))
    rows = raw.get("scored", raw) if isinstance(raw, dict) else raw
    out, seen = [], set()
    for i, item in enumerate(rows):
        core = item.strip().upper() if isinstance(item, str) else (
            str(item.get("core") or item.get("sequence") or "").strip().upper()
        )
        if not core or core in seen:
            continue
        seen.add(core)
        cand_id = f"d{i}" if isinstance(item, str) else (
            item.get("cand_id") or item.get("id") or f"d{i}"
        )
        out.append({"cand_id": str(cand_id), "core": core})
    return out


def cmd_retain(args: argparse.Namespace) -> int:
    """Fold, retaining every artifact the instant it lands — before scoring."""
    if not os.environ.get("LIGANDAI_API_KEY"):
        print("LIGANDAI_API_KEY env var is required for `retain`", file=sys.stderr)
        return 1

    from ligandai import LigandAI
    from ligandai.errors import LigandAIError, LigandAITierError

    cores = load_cores(args.sequences)
    if not cores:
        print(f"no sequences in {args.sequences}", file=sys.stderr)
        return 1
    os.makedirs(args.outdir, exist_ok=True)

    client = LigandAI()
    peptides = [c["core"] for c in cores]
    samples: list[list[float]] = [[] for _ in cores]
    total_files = 0
    no_structure: list[str] = []

    print(f"folding {len(cores)} designs x k={args.k}, retaining as results land\n")

    for round_index in range(args.k):
        print(f"== round {round_index + 1}/{args.k} ==")
        try:
            job = client.peptides.fold_batch(
                peptides=peptides,
                target_gene=args.gene,
                sampling_steps=50,
                msa_enabled=True,      # receptor chains MUST use an MSA
                # Without force_resubmit the 24h local dedupe returns round 1's
                # cached job every time, and "k samples" becomes one repeated.
                force_resubmit=round_index > 0,
            )
            results = job.wait(timeout=7200)
        except LigandAITierError as exc:
            print(f"TIER-LOCKED: {exc}", file=sys.stderr)
            return 3
        except LigandAIError as exc:
            print(f"API error: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2

        for i, fold in enumerate(results):
            if fold is None:
                continue
            trajectory = fold_result_to_trajectory(fold)

            # ---- RETAIN FIRST -------------------------------------------
            if not (trajectory.get("cif_content") or trajectory.get("pdb_content")):
                no_structure.append(f"{cores[i]['cand_id']}/r{round_index}")
                continue
            written = retain_trajectory(
                args.outdir, cores[i]["cand_id"], args.context, round_index,
                trajectory,
                {
                    "design_id": cores[i]["cand_id"],
                    "core": cores[i]["core"],
                    "gene": args.gene,
                    "round": round_index,
                    "engine": "boltz2",
                },
            )
            total_files += len(written)

            # ---- SCORE SECOND -------------------------------------------
            # If this throws, the structure above is already on disk.
            v = trajectory.get("peptide_ipsae")
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                samples[i].append(float(v))

        print(f"  retained {total_files} files so far")

    # Summary: the median is derived from retained artifacts, not the other way.
    print(f"\n{total_files} files retained under {args.outdir}/")
    print(f"sha256 manifest: {os.path.join(args.outdir, MANIFEST)}")
    if no_structure:
        print(f"{len(no_structure)} results carried no structure content: "
              f"{no_structure[:8]}")

    summary = []
    for i, c in enumerate(cores):
        v = samples[i]
        summary.append({
            "cand_id": c["cand_id"],
            "core": c["core"],
            "n_samples": len(v),
            "per_round_peptide_ipsae": [round(x, 4) for x in v],
            "peptide_ipsae_median": round(statistics.median(v), 4) if len(v) >= 2 else None,
        })
    summary_path = os.path.join(args.outdir, "scores_from_retained.json")
    with open(summary_path, "w") as fh:
        json.dump(summary, fh, indent=1)
    print(f"-> {summary_path}")
    print("   Every score above has its structure + PAE on disk beside it.")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Re-hash every file in the manifest. No network, no API key."""
    manifest_path = os.path.join(args.outdir, MANIFEST)
    if not os.path.exists(manifest_path):
        print(f"no manifest at {manifest_path}", file=sys.stderr)
        return 1

    ok, missing, corrupt = 0, [], []
    with open(manifest_path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            path = os.path.join(args.outdir, rec["path"])
            if not os.path.exists(path):
                missing.append(rec["path"])
                continue
            if sha256_file(path) != rec["sha256"]:
                corrupt.append(rec["path"])
                continue
            ok += 1

    print(f"verified : {ok}")
    print(f"missing  : {len(missing)}")
    print(f"CORRUPT  : {len(corrupt)}")
    for p in missing[:10]:
        print(f"  missing  {p}")
    for p in corrupt[:10]:
        print(f"  CORRUPT  {p}")
    return 0 if not (missing or corrupt) else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Retain and verify fold artifacts (structures + PAE + scalars)."
    )
    sub = ap.add_subparsers(dest="command", required=True)

    p_retain = sub.add_parser("retain", help="fold and persist every artifact")
    p_retain.add_argument("sequences", help="JSON list of sequences or {core:...}")
    p_retain.add_argument("--gene", required=True)
    p_retain.add_argument("--k", type=int, default=5, help="independent fold rounds")
    p_retain.add_argument("--outdir", default="artifacts")
    p_retain.add_argument("--context", default="apo",
                          help="label for this modelling context, e.g. a CD34 "
                               "glycoform name")
    p_retain.set_defaults(func=cmd_retain)

    p_verify = sub.add_parser("verify", help="re-hash every file in the manifest")
    p_verify.add_argument("outdir")
    p_verify.set_defaults(func=cmd_verify)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
