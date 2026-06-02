# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""
23 — Batch fold N peptide candidates against one receptor.

Covers:
  client.peptides.fold_batch(peptides=[...], target_gene=...)
  client.peptides.fold_batch(peptides=[...], receptor_pdb=...)
  client.peptides.fold_batch(peptides=[...], receptor_sequence=...)
  FASTA peptide input parsing (multi-record blocks)

Use this when you have a candidate library (10-500 peptides) and want every
one folded against the same receptor — e.g. after a LigandForge generation
or a synthesized panel from Adaptyv where you want predicted structures to
inspect contacts before synthesis ordering.

Billing
-------
100 credits per fold per trajectory (= $1 per fold). Sampling steps >50
apply a `max(1.0, sampling_steps / 50)` multiplier. The full cost is
charged UPFRONT before any GPU work starts — be sure the credit balance
covers the whole batch.

Run with:
    LIGANDAI_API_KEY=lgai_pro_... python 23_fold_batch.py
Optional:
    LIGANDAI_BASE_URL=http://localhost:8000 python 23_fold_batch.py # dev
    LIGANDAI_BATCH_MODE=fasta python 23_fold_batch.py                  # FASTA input
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from ligandai import LigandAI
from ligandai.errors import LigandAIError


# A handful of real EGFR peptides from the v6.5_ET autoresearch
# elite set — short enough to fold fast and realistic for a smoke test.
EGFR_CANDIDATES = [
    "DPVQETICKAHGNRWQVDKLLNCEL",
    "KAFGNPWKVDELLNVCAEYTGLRDQ",
    "RGCDQAFGNHWKVDELLNVPETICK",
    "HGNRWQVDKLLNCELDPVQETICKA",
    "GETLNVDKLLAFGNHWPCRYQAKVE",
]

# Tiny CD47-targeting set (also real elite designs).
CD47_CANDIDATES = [
    "SKFLELLDDPNSCRYAEQVTNGRMD",
    "ELFLDDPNSKCRYAEQVMTNGRDIK",
    "DDPNSKCRYAEQVMTNGRDIKELFL",
]

# A small FASTA block (multi-record) — the SDK forwards the raw string and
# the server splits records server-side.
PCSK9_FASTA = """\
>PCSK9_candidate_1
MTRRDPNQAYNVCFGEKLISDPSLW
>PCSK9_candidate_2
DPSLWAYMTRRNVCFGEKLISDPNQ
>PCSK9_candidate_3
NQAYNVCFGEKLISDPSLWMTRRDP
"""


def _progress_callback(snapshot: dict) -> None:
    """Print a one-line progress update on each poll tick."""
    print(
        f"    [{snapshot['batch_id'][:18]}...] "
        f"{snapshot['done']}/{snapshot['total']} terminal "
        f"({snapshot['failed']} failed)",
        flush=True,
    )


def demo_gene(client: LigandAI) -> None:
    print("== client.peptides.fold_batch(target_gene='EGFR') ==")
    print(f"   Submitting {len(EGFR_CANDIDATES)} peptides × 1 trajectory @ 50 steps")
    print(f"   Estimated cost: {len(EGFR_CANDIDATES) * 100} credits")
    job = client.peptides.fold_batch(
        peptides=EGFR_CANDIDATES,
        target_gene="EGFR",
        diffusion_samples=1,
        sampling_steps=50,
    )
    print(f"   batch_id        = {job.batch_id}")
    print(f"   total_cost      = {job.total_cost_credits} credits")
    print(f"   peptide_count   = {job.peptide_count}")
    print(f"   trajectories    = {job.trajectories_per_peptide}/peptide")
    print(f"   receptor        = {job.receptor.get('gene')} "
          f"({job.receptor.get('source')}, length={job.receptor.get('length')})")
    print()


def demo_pdb(client: LigandAI) -> None:
    pdb_path = os.environ.get("LIGANDAI_RECEPTOR_PDB")
    if not pdb_path:
        print("== receptor_pdb demo skipped (set LIGANDAI_RECEPTOR_PDB=/path/to.pdb) ==\n")
        return
    if not Path(pdb_path).exists():
        print(f"== receptor_pdb demo skipped — file not found: {pdb_path} ==\n")
        return
    print("== client.peptides.fold_batch(receptor_pdb=<file>) ==")
    print(f"   PDB: {pdb_path}")
    print(f"   Peptides: {len(CD47_CANDIDATES)} × 4 trajectories @ 100 steps (2× multiplier)")
    print(f"   Estimated cost: {len(CD47_CANDIDATES) * 4 * 100 * 2} credits")
    job = client.peptides.fold_batch(
        peptides=CD47_CANDIDATES,
        receptor_pdb=pdb_path,                # SDK reads the file once
        receptor_name="CD47_VARIANT_X",
        diffusion_samples=4,
        sampling_steps=100,
    )
    print(f"   batch_id        = {job.batch_id}")
    print(f"   total_cost      = {job.total_cost_credits} credits")
    print(f"   receptor mode   = {job.receptor.get('mode')} "
          f"(length={job.receptor.get('length')})")
    print()


def demo_sequence(client: LigandAI) -> None:
    # A short CD47 EC-domain sequence (residues 19-141 of human CD47, UniProt
    # Q08722). Server will attempt a UniProt match to attach gene attribution
    # then fall back to the literal sequence if no hit.
    cd47_ec = (
        "QLLFNKTKSVEFTFCNDTVVIPCFVTNMEAQNTTEVYVKWKFKGRDIYTFDGALNKSTVPTDFSSAKIE"
        "VSQLLKGDASLKMDKSDAVSHTGNYTCEVTELTREGETIIELKYRVVSWFSP"
    )
    print("== client.peptides.fold_batch(receptor_sequence=<AA>) ==")
    print(f"   Receptor sequence length: {len(cd47_ec)}")
    print(f"   Peptides: {len(CD47_CANDIDATES)} × 1 trajectory @ 50 steps")
    print(f"   Estimated cost: {len(CD47_CANDIDATES) * 100} credits")
    job = client.peptides.fold_batch(
        peptides=CD47_CANDIDATES,
        receptor_sequence=cd47_ec,
        receptor_name="CD47_EC_DOMAIN",
        diffusion_samples=1,
        sampling_steps=50,
    )
    print(f"   batch_id          = {job.batch_id}")
    print(f"   total_cost        = {job.total_cost_credits} credits")
    print(f"   receptor.source   = {job.receptor.get('source')}")
    print(f"   receptor.gene     = {job.receptor.get('gene')}")
    print(f"   receptor.uniprot  = {job.receptor.get('uniprot_id')}")
    print()


def demo_fasta(client: LigandAI) -> None:
    print("== FASTA input — multi-record block via receptor_sequence ==")
    print(f"   FASTA block records: 3, target_gene: PCSK9")
    job = client.peptides.fold_batch(
        peptides=[PCSK9_FASTA],     # one list entry containing 3 FASTA records
        target_gene="PCSK9",
        diffusion_samples=1,
        sampling_steps=50,
    )
    print(f"   batch_id        = {job.batch_id}")
    print(f"   peptide_count   = {job.peptide_count}  "
          f"(server parsed {job.peptide_count} records from the FASTA block)")
    print(f"   total_cost      = {job.total_cost_credits} credits")
    for j in job.jobs[:5]:
        print(f"     [{j['peptide_index']}] {j['sequence'][:30]}... → "
              f"{j['job_id'] or '(submission failed)'}")
    print()


def demo_wait_for_results(client: LigandAI) -> None:
    """Optional: wait for one tiny batch and inspect parsed FoldResults."""
    if os.environ.get("LIGANDAI_WAIT_FOR_RESULTS") != "1":
        print("== wait-for-results demo skipped (set LIGANDAI_WAIT_FOR_RESULTS=1 to enable) ==")
        print("   Each fold takes ~2-5 min on B200; total wall-clock = max(fold_time) when parallel.")
        return
    print("== Waiting for a 3-peptide batch to complete ==")
    job = client.peptides.fold_batch(
        peptides=EGFR_CANDIDATES[:3],
        target_gene="EGFR",
        diffusion_samples=1,
    )
    print(f"   Polling batch {job.batch_id}...")
    try:
        results = job.wait(
            timeout=1800,           # 30 min cap
            poll_interval=10,
            on_progress=_progress_callback,
        )
    except LigandAIError as exc:
        print(f"   Wait raised: {exc}")
        return
    print(f"\n   Completed. {sum(1 for r in results if r is not None)}/{len(results)} succeeded.")
    for i, fold in enumerate(results):
        if fold is None:
            print(f"     [{i}] {job.jobs[i]['sequence'][:25]}... → FAILED")
            continue
        print(
            f"     [{i}] {job.jobs[i]['sequence'][:25]}... → "
            f"iPTM={fold.iptm}, iPSAE={fold.ipsae}, pLDDT={fold.plddt}"
        )


def main() -> int:
    key = os.environ.get("LIGANDAI_API_KEY")
    if not key:
        print("LIGANDAI_API_KEY env var is required", file=sys.stderr)
        return 1
    base_url = os.environ.get("LIGANDAI_BASE_URL", "https://ligandai.com")
    client = LigandAI(api_key=key, base_url=base_url)
    print(f"Connected: tier={client.tier}, base_url={client.base_url}")
    print(f"Current balance: {client.credits} credits\n")

    try:
        demo_gene(client)
        demo_pdb(client)
        demo_sequence(client)
        demo_fasta(client)
        demo_wait_for_results(client)
    except LigandAIError as exc:
        print(f"\nLigandAIError: {exc}", file=sys.stderr)
        return 2
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
