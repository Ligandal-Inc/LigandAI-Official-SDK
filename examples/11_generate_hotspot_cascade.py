# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""
11 — Hotspot-steered generation cascade (Task H, NEW in 0.5.x).

Demonstrates the new must-contact / pocket-context residue pathway:
  1. Pick hotspot residues on the receptor (the ones the binder MUST touch).
  2. Auto-expand to the surrounding pocket via expand_hotspot_to_pocket.
  3. Submit generation with both lists; the the compute backend design worker filters its
     pocket_features tensor to (hotspots ∪ pocket) BEFORE the V6.5 generator
     runs, steering peptides to the named site.

This is the cleanest 'generate against this site, not the whole surface'
recipe. Use it instead of the legacy targetRegions ranges when the user
names ≤5 specific residues.

Run with:
    LIGANDAI_API_KEY=lgai_pro_... python 11_generate_hotspot_cascade.py
"""

from __future__ import annotations

import os
import sys

from ligandai import LigandAI
from ligandai.errors import LigandAIError


def main() -> int:
    key = os.environ.get("LIGANDAI_API_KEY")
    if not key:
        print("LIGANDAI_API_KEY env var is required", file=sys.stderr)
        return 1

    gene = os.environ.get("LIGANDAI_GENE", "BMPR1A")
    chain = os.environ.get("LIGANDAI_CHAIN", "A")
    # Two hotspots on the receptor — the binder MUST contact at least one.
    hotspots = [int(x) for x in os.environ.get("LIGANDAI_HOTSPOTS", "60,62").split(",")]

    client = LigandAI(api_key=key)

    try:
        # Optional pre-step: expand the hotspots to a pocket (≤8 Å).
        # If you skip this, the design worker will expand them in-process via CA-CA.
        # The expand endpoint requires a fold session; use it once you have folds.
        # For first-run generation, just pass hotspot_residues — the compute backend handles it.
        print(f"Generating peptides against {gene}/{chain} hotspots={hotspots}")
        job = client.peptides.generate(
            gene=gene,
            num_peptides=50,
            length_range=(15, 25),
            target_chains=[chain],
            # NEW in 0.5.x — preferred residue-list pathway:
            hotspot_residues=hotspots,
            # pocket_residues=[ ... ] # optional explicit pocket; omit to let the compute backend expand.
            # numbering="pdb",          # default — what the user sees in the viewer.
            auto_fold=False,            # keep this example fast; fold separately if needed.
        )

        print(f"  job_id={job.id}  status={job.status}")
        result = job.wait(timeout=600)
        print(f"  generated {len(result.peptides)} peptides")

        # The featurization block (echoed back from the compute backend) confirms the steering:
        feat = getattr(result, "featurization", None) or {}
        if feat:
            print(f"  featurization.mode  = {feat.get('mode')}")
            print(f"  L_residues_featured = {feat.get('n_residues_featurized')}")
            print(f"  hotspots_echoed     = {feat.get('hotspot_residues')}")
            print(f"  pocket_size_echoed  = "
                  f"{len(feat.get('pocket_residues') or [])}")
            if feat.get("fallback_reason"):
                print(f"  fallback            = {feat['fallback_reason']}")

        # Sample top 5 peptides
        print()
        print("Top 5 peptides by binding energy:")
        sorted_peps = sorted(result.peptides, key=lambda p: getattr(p, "binding_energy", 0))
        for p in sorted_peps[:5]:
            print(f"  {p.sequence:<30}  L={len(p.sequence):>2}  "
                  f"dG={getattr(p, 'binding_energy', 'n/a')}")

    except LigandAIError as e:
        print(f"API error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
