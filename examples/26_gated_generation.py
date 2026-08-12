# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""
26 — Target → pocket features → gated generation (all four guidance heads).

Stage 1+2 of the target-to-order workflow. Resolves a gene to its best
structure, extracts the pocket, then generates with every guidance head at its
MEASURED optimum.

Guidance values used here and why (all measured, CD8AB / CD34 campaign).
Every knob below is shown with DEFAULT / MIN / CEILING, because turning a knob
on should never require knowing a magic number:

  immuno_strength=1.5      default 2.0 | min 0.0 | CEILING 1.5
                           Frequency-weighted epitope burden per 100 residues,
                           class I and class II NEVER summed:
                             off  5.31 / 12.67
                             1.0  3.25 /  6.73
                             1.5  2.59 /  4.59   <- ceiling, all 5 seeds clean
                             1.75 2.31 /  3.85   <- L1 guard trips (2/5 seeds)
                           The ceiling is the composition guard
                           (L1-distance-to-natural, MAX_L1 = 45), not a reversal:
                           the objective is STILL IMPROVING at 1.5. That is where
                           the damage budget runs out. The worker clamps here.

  stability_strength=2.0   default 2.0 | min 0.0 | CEILING 2.0
                           The protease knob (called proteolytic_strength in the
                           worker). Cleavage sites per residue: 2.033 (off) ->
                           1.933 @0.5 -> 1.733 @1.0 -> 1.535 @2.0 -> 1.625 @4.0.
                           This one genuinely REVERSES past 2.0, and L1-to-natural
                           runs 40 -> 70 -> 95 at 2.0/4.0/8.0 while YIELD RISES to
                           100% — a yield-based guard never sees it (bd-qmhck).
                           It ALSO lowers MHC-I burden, so the two objectives are
                           synergistic rather than competing.

  cellfree_strength=2.0    SDK default 1.5 | min 0.0 | ceiling 4.0 (JUDGEMENT)
                           ⛔ The SDK/website/FastAPI default is 1.5 but the
                           WORKER signature's default is 2.0. Every client sends
                           the key explicitly so the worker default never
                           applies; the two disagree and one is wrong. Set here
                           explicitly for exactly that reason.
                           E. coli cell-free (CFPS) expressability — Adaptyv's
                           platform. The 0.5-8.0 sweep was clean throughout
                           (L1-to-natural 14.6-19.5, no collapse). The 4.0 ceiling
                           rests on measured kernel SATURATION above s=4
                           (bd-b8k08) — higher buys nothing — NOT on measured
                           harm. Flagged as a judgement so it is not later cited
                           as a measured limit.

  residue_composition=     default target None | strength 2.0 | min 0.0 |
  "extended"               ceiling 4.0 | unsupported above 2.0
                           ⛔ RENAMED from `halflife` (bd-LIGANDAI_ALPHA_V2-b4f34).
                           It does NOT measure or predict a half-life. It shapes
                           SEQUENCE-LEVEL COMPOSITION only: tryptophan load, basic
                           fraction, tyrosine content and terminal residues. No
                           N-end-rule term, no PEST term. The old name still works
                           and warns.
                           "extended" = full strength; "moderate" = 0.4x.
                           The ceiling is where the dose effect on between-receptor
                           JSD stops being indistinguishable from zero: -11.5% at
                           strength 4 (95% CI [-27.2%, +4.7%], NOT significant),
                           significant only at strength 8 (-22.3%, CI [-38.3%,
                           -6.4%]).

  cysteine_mode=           default "disulfide_only"
  "disulfide_only"         0 or exactly 2 cysteines. Adaptyv expresses in a
  cysteine_enabled=True    REDUCING E. coli cell-free lysate, where unpaired
                           thiols scramble, cross-link between molecules and
                           aggregate — and >2 Cys cannot form one defined
                           disulfide. This is a soft bias, NOT a constraint: a
                           prior CD34 deliverable came back with 8/25 designs at
                           4, 6 and 8 cysteines because nothing counted them on
                           the finished sequence. Example 27 does the counting.
                           `cysteine_enabled` is the tri-state switch for the
                           kernel itself: True fires it (and is a guidance request
                           on its own), False suppresses it whatever the mode says,
                           None keeps the legacy "fires iff guidance is effectively
                           active" resolution. Asking for it explicitly matters
                           because the kernel costs -17.7% between-receptor JSD
                           (95% CI [-32.5%, -3.7%]) and used to fire unasked.

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
IMMUNO_STRENGTH = 1.5        # worker ceiling — composition guard, not a reversal
PROTEOLYTIC_STRENGTH = 2.0   # worker ceiling — its own metric REVERSES above it
CELLFREE_STRENGTH = 2.0      # SDK default 1.5, worker default 2.0 — set explicitly
RESIDUE_COMPOSITION_STRENGTH = 1.0   # default 2.0; ceiling 4.0; unsupported >2.0

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

            # De-immunisation. 1.5 is the clamp — the objective is still
            # improving there; that is where the composition budget runs out.
            immunogenicity=True,
            immuno_strength=IMMUNO_STRENGTH,

            # Protease-site avoidance ("resist"; flip to "target" for prodrugs).
            serum_stability=True,
            stability_strength=PROTEOLYTIC_STRENGTH,
            stability_mode="resist",

            # Residue & terminal composition — Trp down, basic fraction down,
            # Tyr up, terminal P/Q/D rewarded. NOT a half-life measurement.
            residue_composition="extended",
            residue_composition_strength=RESIDUE_COMPOSITION_STRENGTH,

            # 0 or exactly 2 Cys, for the reducing cell-free lysate. Asked for
            # explicitly rather than inherited: the kernel costs -17.7%
            # between-receptor JSD, so it should be a decision, not a side
            # effect of some other head being on.
            cysteine_mode="disulfide_only",
            cysteine_enabled=True,

            # Fold the survivors at k=5 and gate on the MEDIAN, never the max:
            # max-of-5 passed 63 designs where the median passed 56 — 7 designs
            # riding a single lucky sample into a synthesis order.
            auto_fold=True,
            top_n_fold=50,
            num_trajectories=K_CONFIRMATION,

            # Cell-free expressability. Typed kwarg as of
            # bd-LIGANDAI_ALPHA_V2-sxc8j (previously **extra-only). Default 2.0,
            # ceiling 4.0.
            cellfree=True,
            cellfree_strength=CELLFREE_STRENGTH,
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
                "gen_residue_composition_strength": RESIDUE_COMPOSITION_STRENGTH,
                "gen_cysteine_mode": "disulfide_only",
                "gen_cysteine_enabled": True,
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
