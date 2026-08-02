# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""
26 — Target → pocket features → gated generation (all four guidance heads).

Stage 1+2 of the target-to-order workflow. Resolves a gene to its best
structure, extracts the pocket, then generates with every guidance head at its
MEASURED optimum.

Guidance values used here and why (all measured, CD8AB / CD34 campaign):

  immuno_strength=3.0      MHC-I epitope burden per 100 residues falls
                           5.23 (off) -> 4.48 -> 4.17 -> 3.47 (@3.0), then
                           REVERSES: 3.45 @4.0, 3.59 @5.0, 4.00 @6.0. Above ~3
                           the sampler collapses toward poly-alanine and alanine
                           is itself a P2 anchor for much of the allele panel, so
                           the collapse rebuilds class-I epitopes. Class-II keeps
                           falling through that range, which is exactly how the
                           damage hides from a class-II-only check. The worker
                           CLAMPS this at 3.0. Cost: ~2.6x more generated
                           material to reach the same yield.

  stability_strength=2.0   The protease knob (called proteolytic_strength in the
                           worker). Cleavage sites per residue: 2.033 (off) ->
                           1.933 @0.5 -> 1.733 @1.0 -> 1.535 @2.0 -> 1.625 @4.0.
                           Optimum 2.0; the worker clamps there. It ALSO lowers
                           MHC-I burden (3.47 -> 2.59), so the two objectives are
                           synergistic rather than competing.

  cellfreeStrength=2.0     E. coli cell-free (CFPS) expressability — Adaptyv's
                           platform. Safe across the full 0.5-8.0 sweep
                           (L1-to-natural 14.6-19.5), so it is deliberately NOT
                           clamped. No named SDK argument yet: passed through
                           **extra. Server default is enabled at 1.5.

  halflife="extended"      Cavaco 2021 regression
                           ln(t1/2) = 2.226 + 0.053*NP% - 1.515*W + 1.290*Y
                                      - 1.052*pI
                           Trp SHORTENS half-life (metabolic oxidation) despite
                           being nonpolar; Tyr extends it; high pI shortens it
                           (renal clearance). REPORTED and ranked, never gated —
                           the paper's (0,1) normalisation is unrecorded, so the
                           absolute t1/2 is indicative only.

  cysteine_mode=           0 or exactly 2 cysteines. Adaptyv expresses in a
  "disulfide_only"         REDUCING E. coli cell-free lysate, where unpaired
                           thiols scramble, cross-link between molecules and
                           aggregate — and >2 Cys cannot form one defined
                           disulfide. This is a soft bias, NOT a constraint: a
                           prior CD34 deliverable came back with 8/25 designs at
                           4, 6 and 8 cysteines because nothing counted them on
                           the finished sequence. Example 27 does the counting.

Tier: academia / pro / enterprise (the immuno + stability heads are gated).

Run with:
    LIGANDAI_API_KEY=lgai_... python 26_gated_generation.py CD34
"""

from __future__ import annotations

import json
import os
import sys

from ligandai import LigandAI
from ligandai.errors import (
    LigandAIError,
    LigandAIInsufficientCredits,
    LigandAITierError,
    LigandAIUpgradeRequired,
)

# Measured optima. Named as constants so a call site can never silently drift
# off them, and so the clamp values are visible at the top of the file.
IMMUNO_STRENGTH = 3.0        # worker ceiling — higher REVERSES class-I burden
PROTEOLYTIC_STRENGTH = 2.0   # worker ceiling — higher degrades composition
CELLFREE_STRENGTH = 2.0      # unclamped: clean across 0.5-8.0
HALFLIFE_STRENGTH = 1.0      # reported + ranked, never gated

# Gate on the median of k trajectories. k=1 measured an 11% false-positive rate.
K_SCREENING = 3
K_CONFIRMATION = 5


def main() -> int:
    if not os.environ.get("LIGANDAI_API_KEY"):
        print("LIGANDAI_API_KEY env var is required", file=sys.stderr)
        return 1

    gene = sys.argv[1] if len(sys.argv) > 1 else "CD34"
    out_path = f"gated_pool_{gene.lower()}.json"
    client = LigandAI()

    try:
        # --- Stage 1: target -> structure -> pocket ------------------------
        print(f"== structures.get({gene!r}) ==")
        struct = client.structures.get(gene)
        print(f"  tier={struct.tier}  source={struct.source}")

        print(f"\n== structures.analyze({gene!r}) ==")
        analysis = client.structures.analyze(gene, analysis_depth="full")
        # recommended_pocket is a ResidueRange OBJECT. target_residues wants a
        # SEQUENCE of them — passing the `.range` STRING ("A:12-31") instead is
        # silently dropped by the serializer, and the run then looks successful
        # while having designed against the full surface.
        pocket = getattr(analysis, "recommended_pocket", None)
        target_residues = [pocket] if pocket is not None else None
        if pocket is not None:
            print(f"  recommended pocket: {pocket.range}")
        else:
            print("  no recommended pocket — designing against the full surface")

        # --- Stage 2: guided over-generation -------------------------------
        # Over-generate deliberately. The pre-fold gate in example 27 keeps
        # 10-35% of designs (measured), so ask for several times what you need.
        print(f"\n== peptides.generate({gene!r}) with all four guidance heads ==")
        job = client.peptides.generate(
            gene=gene,
            num_peptides=300,
            length_range=(20, 40),
            target_residues=target_residues,

            # De-immunisation. 3.0 is the optimum AND the clamp.
            immunogenicity=True,
            immuno_strength=IMMUNO_STRENGTH,

            # Protease-site avoidance ("resist"; flip to "target" for prodrugs).
            serum_stability=True,
            stability_strength=PROTEOLYTIC_STRENGTH,
            stability_mode="resist",

            # Plasma half-life bias. Reported, not gated.
            halflife="extended",
            halflife_strength=HALFLIFE_STRENGTH,

            # 0 or exactly 2 Cys, for the reducing cell-free lysate.
            cysteine_mode="disulfide_only",

            # Fold the survivors at k=5 and gate on the MEDIAN, never the max:
            # max-of-5 passed 63 designs where the median passed 56 — 7 designs
            # riding a single lucky sample into a synthesis order.
            auto_fold=True,
            top_n_fold=50,
            num_trajectories=K_CONFIRMATION,

            # Cell-free expressability has no named SDK argument yet. **extra
            # keys are merged into the request body verbatim (camelCase).
            cellfreeEnabled=True,
            cellfreeStrength=CELLFREE_STRENGTH,
        )
        print(f"  job {job.id} submitted; waiting…")

        result = job.wait(timeout=3600)
        peptides = getattr(result, "peptides", None) or []
        print(f"  {len(peptides)} peptides returned")

        # Persist the pool with the guidance settings that produced it. A design
        # whose provenance is lost cannot be reproduced or audited later.
        pool = [
            {
                "sequence": getattr(p, "sequence", None),
                "peptide_id": getattr(p, "id", None),
                "gene": gene,
                "gen_immuno_strength": IMMUNO_STRENGTH,
                "gen_proteolytic_strength": PROTEOLYTIC_STRENGTH,
                "gen_cellfree_strength": CELLFREE_STRENGTH,
                "gen_halflife_strength": HALFLIFE_STRENGTH,
                "gen_cysteine_mode": "disulfide_only",
                "gen_job_id": job.id,
            }
            for p in peptides
            if getattr(p, "sequence", None)
        ]
        with open(out_path, "w") as fh:
            json.dump(pool, fh, indent=1)
        print(f"\n-> {out_path} ({len(pool)} designs)")
        print("   Next: 27_prefold_gating.py — gate on CPU before spending GPU.")

    except LigandAIUpgradeRequired as exc:
        print(f"UPGRADE REQUIRED: {exc}", file=sys.stderr)
        print(f"  your tier={exc.current_tier} required={exc.required_tier}",
              file=sys.stderr)
        print("  The immuno + stability heads need academia / pro / enterprise.", file=sys.stderr)
        return 3
    except LigandAITierError as exc:
        print(f"TIER-LOCKED: {exc}", file=sys.stderr)
        return 3
    except LigandAIInsufficientCredits as exc:
        print(f"INSUFFICIENT CREDITS: {exc}", file=sys.stderr)
        return 4
    except LigandAIError as exc:
        print(f"API error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
