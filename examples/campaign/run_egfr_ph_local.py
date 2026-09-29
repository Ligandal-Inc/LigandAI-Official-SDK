#!/usr/bin/env python3
# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Run the EGFR pH-selective campaign ENTIRELY on local hardware.

    python3 run_local_campaign.py [--max-iterations N] [--scale F] [--dry-run]

``CampaignEngine`` is used unchanged — its gates, Wilson certification, stop
metrics, ledger and planner all run exactly as they would against the hosted
platform.  Only the ``Platform`` is swapped, for one that generates with a local
LigandForge checkpoint, folds with a local Boltz-2 on a local GPU, and scores
in-process.  Nothing is submitted anywhere and no API key is read.

⛔ WHAT THIS DRIVER DELIBERATELY DOES NOT DO

It does not lower the 0.66 peptide-ipSAE gate or the 0.15 certification bar.
``--scale`` reduces the NUMBER of designs per round so a round finishes in
bounded GPU time; it never touches a threshold.  A campaign that reports
"certified" against a moved bar is worse than one that reports zero.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, "/mnt/backup/ligandai-python-sdk")

from ligandai.campaign import CampaignSpec, CampaignEngine            # noqa: E402
from ligandai.campaign.local_platform import (                        # noqa: E402
    LocalPlatform, LocalLigandForge, LocalBoltz, IPSAEScorer,
    MSARegistry, PocketSpec)

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC_PATH = os.path.join(HERE, "spec", "egfr_ph_selective.yaml")

# ---------------------------------------------------------------------------
# Receptor MSAs.  Every one is checked against the construct it claims to cover
# at registration; a mismatch refuses there rather than folding a shifted
# alignment.  Depths are what is on disk today.
# ---------------------------------------------------------------------------
MSA_FILES = {
    "EGFR_domIII_334_538": os.path.join(HERE, "fold", "EGFR_domIII.a3m"),
    "EGFR_ECD_25_645": ("/home/dre/research_work/beat_ipsae_20260822/"
                        "pairtrunk/msa/EGFR.a3m"),
    "ERBB2_ECD_23_652": ("/home/dre/research_work/peptune_ncaa_fold_20260824/"
                         "data/msa_cache/ERBB2_ECD_NP_33130c43af2d93be.a3m"),
}

# ---------------------------------------------------------------------------
# The pocket, in a STATED frame.
#
# The spec declares Patch A in UniProt P00533 numbering: 416 421 507 519 522 534.
# The structure we featurize from is 6ARU, whose author numbering is UniProt-24.
# Both numbers are written down here, the conversion is one line, and
# `expect_residues` makes a mistake in it impossible to ship: D416 E421 H507
# E519 D522 E534 must come back as "DEHEDE" or PocketSpec refuses.
# ---------------------------------------------------------------------------
UNIPROT_TO_6ARU = -24
PATCH_A_UNIPROT = [416, 421, 507, 519, 522, 534]
PATCH_A_6ARU = [n + UNIPROT_TO_6ARU for n in PATCH_A_UNIPROT]
PATCH_A_EXPECT = "DEHEDE"

# ---------------------------------------------------------------------------
# The CFG negative: ERBB2's version of the SAME epitope.
#
# v6.9.21 samples with classifier-free guidance and REFUSES the checkpoint's
# historical all-zero unconditional receptor (H21) — guidance against a zero
# receptor is arithmetically the same as no conditioning, and produces
# receptor-blind designs that look completely normal. It requires a real
# off-target/paralog pocket instead.
#
# The spec already names that paralog: ERBB2, with min_margin 0.15. So the
# negative is not a stand-in for the refused zero vector — it is the campaign's
# own selectivity objective expressed at generation time.
#
# The residues were derived by global pairwise alignment (BLOSUM62, open -11 /
# extend -1) of the two declared ECD constructs, then VERIFIED against the
# coordinates of 1N8Z chain C, which is ERBB2 ECD and whose author numbering
# equals the construct index (author 1 == P04626 residue 23).
#
# The mapping is also the scientific point of the campaign:
#     EGFR  D416 E421 H507 E519 D522 E534  ->  DEHEDE   (5 carboxylates)
#     ERBB2 D421 Q426 H512 G524 Q527 E539  ->  DQHGQE   (2 carboxylates)
# The acid-rich face is EGFR-specific; that difference is the selectivity
# handle, and guiding away from the ERBB2 form of it is the whole intent.
ERBB2_UNIPROT_TO_1N8Z = -22
ERBB2_PATCH_UNIPROT = [421, 426, 512, 524, 527, 539]
ERBB2_PATCH_1N8Z = [n + ERBB2_UNIPROT_TO_1N8Z for n in ERBB2_PATCH_UNIPROT]
ERBB2_PATCH_EXPECT = "DQHGQE"


def build_platform(work_dir: str, spec: CampaignSpec, log) -> LocalPlatform:
    msa = MSARegistry()
    by_name = {spec.target.construct.name: spec.target.construct.sequence}
    for c in (spec.target.comparison_constructs or []):
        by_name[c.name] = c.sequence
    for o in (spec.off_targets or []):
        if getattr(o, "construct", None):
            by_name[o.construct.name] = o.construct.sequence
    for name, path in MSA_FILES.items():
        if name not in by_name:
            continue
        entry = msa.register(name, by_name[name], path)
        log(f"  MSA {name}: {entry.n_rows} rows, {len(entry.query)} aa  {path}")
    unregistered = sorted(set(by_name) - set(MSA_FILES))
    if unregistered:
        log(f"  ⛔ no receptor MSA registered for {unregistered} — any arm "
            f"against those constructs will REFUSE rather than fold MSA-free.")

    pocket = PocketSpec(
        pdb_path=os.path.join(HERE, "target", "6ARU.pdb"),
        residue_numbers=PATCH_A_6ARU,
        frame=(f"6ARU author numbering (UniProt P00533 {UNIPROT_TO_6ARU}); "
               f"UniProt {PATCH_A_UNIPROT} -> 6ARU {PATCH_A_6ARU}"),
        chain_id="A", expect_residues=PATCH_A_EXPECT, gene=spec.target.gene)

    reference_pockets = [PocketSpec(
        pdb_path=os.path.join(HERE, "gonogo", "structures", "1N8Z.pdb"),
        residue_numbers=ERBB2_PATCH_1N8Z,
        frame=(f"1N8Z chain C author numbering (UniProt P04626 "
               f"{ERBB2_UNIPROT_TO_1N8Z}); UniProt {ERBB2_PATCH_UNIPROT} -> "
               f"1N8Z {ERBB2_PATCH_1N8Z}"),
        chain_id="C", expect_residues=ERBB2_PATCH_EXPECT, gene="ERBB2")]

    # The model comes from the SPEC, and its pocket-feature width and CFG
    # requirement come from the model registry — never from this file.
    generator = LocalLigandForge(model_id=spec.generation.model,
                                 device="cuda", progress=log)
    folder = LocalBoltz(
        device_index=0,
        # Measured headroom, not a guess: this host's A2000 is 12 GiB with ~6 GiB
        # already resident, and a 205 aa receptor + peptide (284 tokens) folds in
        # it today. The 621-630 aa ECD constructs need ~21-24 GiB and CANNOT run
        # here; the guard makes that arm refuse by name instead of OOMing
        # mid-batch, which the engine would read as "these designs did not score".
        max_receptor_residues=320,
        progress=log)
    return LocalPlatform(work_dir=work_dir, pocket=pocket, msa=msa,
                         reference_pockets=reference_pockets,
                         generator=generator, folder=folder,
                         ipsae=IPSAEScorer(), progress=log)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", default=SPEC_PATH)
    ap.add_argument("--out", default=os.path.join(HERE, "spec", "campaign_out_local"))
    ap.add_argument("--max-iterations", type=int, default=1)
    ap.add_argument("--scale", type=float, default=1.0,
                    help="scale n_per_round (design COUNT only; never a threshold)")
    ap.add_argument("--dry-run", action="store_true",
                    help="build the platform, run its self-tests, plan, submit nothing")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    log_path = os.path.join(args.out, "run.log")
    logf = open(log_path, "a", encoding="utf-8", buffering=1)

    def log(*parts) -> None:
        line = " ".join(str(p) for p in parts)
        stamp = time.strftime("%H:%M:%S")
        print(f"[{stamp}] {line}", flush=True)
        logf.write(f"[{time.strftime('%FT%T')}] {line}\n")

    log("=" * 78)
    log(f"LOCAL campaign run — spec {args.spec}")
    spec = CampaignSpec.load(args.spec)
    log(f"spec: {spec.name} | target {spec.target.gene} "
        f"{spec.target.construct.name} ({len(spec.target.construct.sequence)} aa)")
    log(f"gates: " + "; ".join(f"{g.metric} {g.op} {g.threshold}" for g in spec.gates))
    log(f"reliability: {spec.reliability.failure_metric} < "
        f"{spec.reliability.failure_threshold} is a failure; certification bar "
        f"{spec.reliability.certification_bar} at {spec.reliability.confidence}")

    if args.scale != 1.0:
        for pc in spec.peptide_classes:
            before = pc.n_per_round
            pc.n_per_round = max(2, int(round(pc.n_per_round * args.scale)))
            log(f"  scale: {pc.name} n_per_round {before} -> {pc.n_per_round} "
                f"(COUNT only — no threshold changed)")

    platform = build_platform(args.out, spec, log)
    log("platform manifest:")
    for line in json.dumps(platform.run_manifest, indent=2, default=str).splitlines():
        log("  " + line)
    json.dump(platform.run_manifest,
              open(os.path.join(args.out, "platform_manifest.json"), "w"),
              indent=2, default=str)

    if args.dry_run:
        log("dry run: platform built and self-tested; nothing submitted.")
        return 0

    # The iteration cap lives on the BUDGET, where the engine reads it — setting
    # it anywhere else would be a cap this driver believes in and the engine
    # ignores.
    if args.max_iterations:
        spec.budget.max_iterations = int(args.max_iterations)
        log(f"  iteration cap: {spec.budget.max_iterations}")

    engine = CampaignEngine(spec, platform, output_dir=args.out, log=log)
    t0 = time.time()
    result = engine.run()
    log(f"campaign finished in {(time.time() - t0) / 60:.1f} min")
    log(result.summary() if hasattr(result, "summary") else str(result))
    log(f"poses recorded: {len(engine.poses)} over "
        f"{len({p.design_id for p in engine.poses})} distinct designs, "
        f"{len({p.seed_unit for p in engine.poses})} seed units")
    log(f"artifacts in {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
