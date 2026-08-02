# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""
28 — Batched folding + median-of-k validation, with the cost estimate FIRST.

Stage 4 of the target-to-order workflow. Quotes the fold campaign before
spending anything, folds the pre-fold-gate survivors k independent times, and
selects each design on the MEDIAN of its k peptide-ipSAE values.

HOW k IS ACTUALLY OBTAINED HERE. `fold_batch(..., num_trajectories=k)` sets the
diffusion-sample count inside one job, but a FoldResult carries ONE
`peptide_ipsae` — there is no per-trajectory breakdown on the returned object.
So to take a real median this script runs k INDEPENDENT fold rounds over the
same peptide list and takes the median across rounds. That is genuinely k
samples of the same quantity, and it uses only documented SDK surface. Do not
substitute one job's single value and call it median-of-k.

WHY THE MEDIAN, AND WHY NOT k=1 (all measured on the 133-design CD34 set folded
with all 5 trajectories):

  k = 1        11% FALSE-POSITIVE RATE — 6 bad designs against 56 true passes.
               Each would have shipped into a synthesis order on a single lucky
               draw. k=1 is never a supported mode.

  max of 5     Passes 63 designs where the median passes 56 — 7 designs riding
               one good sample. Max is also the statistic that made batched
               folding look unstable (marginal designs moved 0.5+ between runs)
               while genuinely strong binders reproduced to <0.015. Never
               select on the max.

  median of 5  The criterion that survives both. CONFIRMATION tier — every
               design entering a synthesis order gets this.

  median of 3  Disagrees with the k=5 reference on 3 designs at 60% of the
               cost. SCREENING tier.

COST. A trajectory costs 4.3 s batched against 20.8 s unbatched — a 4.8x
difference from warm-container residency, not from bigger batches. ~85% of an
unbatched call is fixed setup (import, model build, 2.6 GB checkpoint load,
490 MB components parse) paid once per peptide. Batch size is a QUALITY
decision too: chunks of 6-7 peptides sit at the run-to-run noise floor (ipSAE
delta 0.00249 vs a 0.00233 floor) while a mega-batch of 100 FAILS that gate
(ipSAE delta 0.00715, peptide RMSD 11.4 A vs a 3.6 A floor). Container
chunking is handled server-side and is not a client parameter.

THE METRIC IS peptide_ipsae, not ipsae. The complex-level `ipsae` is dominated
by the receptor's own well-predicted fold and stays high even when the peptide
is not engaging. When `peptide_ipsae` is None the value was NEVER COMPUTED (it
is nullable on the predict-batch path) — that is reported as unscored here, and
never silently backfilled from `ipsae`.

Run with:
    LIGANDAI_API_KEY=lgai_... python 28_median_of_k_validation.py \\
        prefold_survivors.json CD34 --tier confirmation
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys

from ligandai import LigandAI
from ligandai.errors import (
    LigandAIError,
    LigandAIInsufficientCredits,
    LigandAITierError,
)

# Trajectories (independent fold rounds) per design, by validation tier.
K_BY_TIER = {"screening": 3, "confirmation": 5}
IPSAE_GATE = 0.67


def load_cores(path: str) -> list[dict]:
    raw = json.load(open(path))
    out: list[dict] = []
    for i, item in enumerate(raw):
        if isinstance(item, str):
            out.append({"cand_id": f"d{i}", "core": item.strip().upper()})
        else:
            core = item.get("core") or item.get("sequence")
            if core:
                out.append({
                    "cand_id": item.get("cand_id") or item.get("id") or f"d{i}",
                    "core": str(core).strip().upper(),
                })
    # De-duplicate by sequence: folding the same core twice in one round would
    # corrupt the per-round alignment and quietly double-count it in the median.
    seen, deduped = set(), []
    for c in out:
        if c["core"] not in seen:
            seen.add(c["core"])
            deduped.append(c)
    return deduped


def binder_metric(fold: object) -> float | None:
    """The PEPTIDE ipSAE, or None when it was never computed.

    Deliberately does NOT fall back to `ipsae`: that is the complex-level value,
    dominated by the receptor's own fold, and substituting it would silently
    turn "not measured" into "looks great".
    """
    if fold is None:
        return None
    status = getattr(fold, "peptide_ipsae_status", None)
    if status == "not_computed":
        return None
    v = getattr(fold, "peptide_ipsae", None)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    return None


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Median-of-k fold validation with an up-front cost estimate."
    )
    ap.add_argument("survivors", help="JSON from 27_prefold_gating.py")
    ap.add_argument("gene", help="Target gene symbol, e.g. CD34")
    ap.add_argument("--tier", choices=sorted(K_BY_TIER), default="screening")
    ap.add_argument("--gate", type=float, default=IPSAE_GATE)
    ap.add_argument("--yes", action="store_true",
                    help="skip the interactive cost confirmation")
    ap.add_argument("--out", default="folds_median.json")
    args = ap.parse_args()

    if not os.environ.get("LIGANDAI_API_KEY"):
        print("LIGANDAI_API_KEY env var is required", file=sys.stderr)
        return 1

    cores = load_cores(args.survivors)
    if not cores:
        print(f"no sequences in {args.survivors}", file=sys.stderr)
        return 1

    k = K_BY_TIER[args.tier]
    client = LigandAI()

    try:
        # --- QUOTE FIRST -----------------------------------------------------
        # Nothing is spent by this call. Print it before folding so the cost of
        # the k choice is visible at the moment the choice is made.
        est = client.synthesis.estimate_cost(
            gene=args.gene,
            num_peptides=0,               # generation already paid for
            max_folds=len(cores) * k,     # k independent rounds
            fold_trajectories=1,
            sampling_steps=50,
            include_deltaforge=True,
        )
        credits = getattr(est, "credits", None)
        cost_usd = getattr(est, "cost_usd", None)

        print("== cost estimate (nothing spent yet) ==")
        print(f"  designs        : {len(cores)}")
        print(f"  tier           : {args.tier}  (k={k} independent fold rounds)")
        print(f"  total folds    : {len(cores) * k}")
        print(f"  credits        : {credits}")
        if cost_usd is not None:
            print(f"  approx USD     : ${cost_usd:,.2f}")
        other = "confirmation" if args.tier == "screening" else "screening"
        print(f"  (--tier {other} would use k={K_BY_TIER[other]}, "
              f"{len(cores) * K_BY_TIER[other]} folds)")

        if not args.yes:
            reply = input("\nproceed with the fold? [y/N] ").strip().lower()
            if reply not in ("y", "yes"):
                print("aborted — nothing was spent.")
                return 0

        # --- FOLD k INDEPENDENT ROUNDS --------------------------------------
        peptides = [c["core"] for c in cores]
        # values[i] accumulates the k peptide-ipSAE samples for cores[i].
        values: list[list[float]] = [[] for _ in cores]
        fold_ids: list[list[str]] = [[] for _ in cores]

        for round_index in range(k):
            print(f"\n== fold round {round_index + 1}/{k} "
                  f"({len(peptides)} designs) ==")
            job = client.peptides.fold_batch(
                peptides=peptides,
                target_gene=args.gene,
                sampling_steps=50,
                msa_enabled=True,        # receptor chains MUST use an MSA
                # force_resubmit: without it the 24h local dedupe would return
                # the FIRST round's cached job for every subsequent round, and
                # the "median of k" would be one number repeated k times.
                force_resubmit=round_index > 0,
            )
            print(f"  job {job.id} submitted; waiting…")
            # BatchFoldJob.wait() returns results ORDERED to match the input.
            results = job.wait(timeout=7200)
            for i, fold in enumerate(results):
                v = binder_metric(fold)
                if v is not None:
                    values[i].append(v)
                fid = getattr(fold, "job_id", None)
                if fid:
                    fold_ids[i].append(str(fid))
            scored = sum(1 for i in range(len(cores)) if len(values[i]) > round_index)
            print(f"  scored {scored}/{len(cores)} designs this round")

    except LigandAITierError as exc:
        print(f"TIER-LOCKED: {exc}", file=sys.stderr)
        return 3
    except LigandAIInsufficientCredits as exc:
        print(f"INSUFFICIENT CREDITS: {exc}", file=sys.stderr)
        return 4
    except LigandAIError as exc:
        print(f"API error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    # --- SELECT ON THE MEDIAN ------------------------------------------------
    rows, unscored = [], []
    for i, cand in enumerate(cores):
        v = values[i]
        if len(v) < 2:
            # One sample is not a median. Surface it; never gate on it.
            unscored.append({"cand_id": cand["cand_id"], "n_samples": len(v)})
            continue
        median = statistics.median(v)
        rows.append({
            "cand_id": cand["cand_id"],
            "core": cand["core"],
            "core_len": len(cand["core"]),
            "fold_ids": fold_ids[i],
            "n_samples": len(v),
            "peptide_ipsae_median": round(median, 4),   # <- THE gate value
            "peptide_ipsae_max": round(max(v), 4),
            "peptide_ipsae_min": round(min(v), 4),
            "peptide_ipsae_spread": round(max(v) - min(v), 4),
            "per_round_peptide_ipsae": [round(x, 4) for x in v],
            "passes_median_gate": median >= args.gate,
        })

    rows.sort(key=lambda r: -r["peptide_ipsae_median"])
    winners = [r for r in rows if r["passes_median_gate"]]
    # The comparison that justifies the rule, computed on your own data.
    by_max = sum(1 for r in rows if r["peptide_ipsae_max"] >= args.gate)

    print(f"\n== results (gate: peptide ipSAE >= {args.gate}, on the MEDIAN) ==")
    print(f"  scored          : {len(rows)} designs at k={k}")
    if unscored:
        print(f"  UNSCORED        : {len(unscored)} designs with <2 samples "
              "(peptide_ipsae was never computed — NOT treated as zero)")
    print(f"  pass on MEDIAN  : {len(winners)}   <- ship these")
    print(f"  pass on MAX     : {by_max}"
          + (f"   ({by_max - len(winners)} would ride one lucky sample)"
             if by_max > len(winners) else ""))

    if winners:
        print(f"\n  {'rank':<5}{'id':<14}{'median':>9}{'min':>8}{'max':>8}{'spread':>9}  k")
        print("  " + "-" * 62)
        for i, r in enumerate(winners[:25], 1):
            print(f"  {i:<5}{str(r['cand_id'])[:13]:<14}"
                  f"{r['peptide_ipsae_median']:>9.4f}{r['peptide_ipsae_min']:>8.4f}"
                  f"{r['peptide_ipsae_max']:>8.4f}{r['peptide_ipsae_spread']:>9.4f}"
                  f"  {r['n_samples']}")
        spreads = [r["peptide_ipsae_spread"] for r in winners]
        print(f"\n  spread across winners: median {statistics.median(spreads):.4f}  "
              f"max {max(spreads):.4f}")
        print("  (strong binders reproduce to <0.015 — a wide spread on a "
              "passing design is a warning, not a win)")

    # Retain EVERY sample, not just the median. A median with its samples
    # discarded cannot be re-examined under a different gate later.
    with open(args.out, "w") as fh:
        json.dump({"gate": args.gate, "k": k, "tier": args.tier,
                   "scored": rows, "unscored": unscored}, fh, indent=1)
    print(f"\n-> {args.out} ({len(rows)} designs, all {k} samples retained each)")
    print("   Structures + PAE: see 31_artifact_retention.py — a score without "
          "its structure cannot be re-examined.")

    if args.tier == "screening" and winners:
        print("   Screening only. Re-run the winners with --tier confirmation "
              "before any synthesis order.")
    else:
        print("   Next: 30_adaptyv_cassette_order.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
