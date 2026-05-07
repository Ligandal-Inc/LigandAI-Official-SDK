# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""
14 — Fold partitioning + hotspot expansion (Stream D + Task H1).

Covers:
  client.folds.partition_by_hotspot(session_id, hotspots, pocket_residues)
  client.folds.expand_hotspot(session_id, chain, residue, radius_a)

Use this AFTER you've folded a batch of peptides — it tells you which folds
actually contact the user's hotspots vs the wider pocket vs the wrong
interface (closes the loop on hotspot-steered design).

Run with:
    LIGANDAI_SESSION_ID=ptf_... LIGANDAI_API_KEY=lgai_pro_... python 14_folds_partition_expand_hotspot.py
"""

from __future__ import annotations

import os
import sys

from ligandai import LigandAI
from ligandai.errors import LigandAIError


def main() -> int:
    key = os.environ.get("LIGANDAI_API_KEY")
    sid = os.environ.get("LIGANDAI_SESSION_ID")
    if not key or not sid:
        print("LIGANDAI_API_KEY and LIGANDAI_SESSION_ID env vars are required",
              file=sys.stderr)
        return 1

    chain = os.environ.get("LIGANDAI_CHAIN", "A")
    hotspot_resi = int(os.environ.get("LIGANDAI_HOTSPOT_RESI", "148"))
    radius = float(os.environ.get("LIGANDAI_RADIUS", "8.0"))

    client = LigandAI(api_key=key)

    try:
        # 1) Expand a single hotspot to its pocket via heavy-atom distance.
        print(f"== folds.expand_hotspot(session_id, chain='{chain}', "
              f"residue={hotspot_resi}, radius_a={radius}) ==")
        expanded = client.folds.expand_hotspot(
            session_id=sid,
            chain=chain,
            residue=hotspot_resi,
            radius_a=radius,
        )
        if expanded.get("ok") is False:
            print(f"  expand failed: {expanded.get('error')}")
            return 2

        n_pocket = expanded.get("n_pocket_residues", 0)
        print(f"  pocket size: {n_pocket} residues")
        for r in expanded.get("pocket_residues", [])[:5]:
            print(f"    {r['chain']}{r['residue']:>4} ({r['resname']})  "
                  f"d={r['distance_a']} Å  closest_to={r.get('closest_to')}")
        if n_pocket > 5:
            print(f"    … and {n_pocket - 5} more")

        # 2) Partition session folds by hotspot contact.
        # passes_hotspot     : peptides that touch the hotspot residue(s)
        # passes_pocket      : peptides that touch the pocket but not the hotspot
        # wrong_interface    : peptides that bind elsewhere
        print(f"\n== folds.partition_by_hotspot(session_id, "
              f"hotspots=[{chain}{hotspot_resi}]) ==")
        result = client.folds.partition_by_hotspot(
            session_id=sid,
            hotspots=[{"chain": chain, "residue": hotspot_resi, "numbering": "pdb"}],
            distance_threshold_a=5.0,
        )
        stats = result.get("stats") or {}
        print(f"  total          : {stats.get('total', 0)}")
        print(f"  passes_hotspot : {stats.get('passes_hotspot', 0)}")
        print(f"  passes_pocket  : {stats.get('passes_pocket', 0)}")
        print(f"  wrong_interface: {stats.get('wrong_interface', 0)}")
        print(f"  unscored       : {stats.get('unscored', 0)}")

        # 3) Show top 3 hotspot-passing peptides
        passes = result.get("passes_hotspot") or []
        print(f"\nTop {min(3, len(passes))} hotspot binders:")
        for p in passes[:3]:
            print(f"  {p['sequence'][:30]:<32}  "
                  f"min_d={p.get('min_distance_a')} Å  iPSAE={p.get('ipsae')}")

    except LigandAIError as e:
        print(f"API error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
