# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""
10 — Receptor search and structure resolution.

Walks the full receptor → structure pathway:
  client.receptors.search() / .by_gene() / .get()
  client.structures.resolve() / .candidates() / .from_pdb() / .from_uniprot() / .from_alphafold()
  client.structures.get_pdb()
  client.structures.analyze() (quick + full)

All inputs are read-only; no GPU spend. Run with:

    LIGANDAI_API_KEY=lgai_pro_... python 10_receptors_search_resolve.py
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

    gene = os.environ.get("LIGANDAI_GENE", "EGFR")
    client = LigandAI(api_key=key)

    try:
        # 1) Receptor search by name
        print(f"== receptors.search('{gene}') ==")
        hits = client.receptors.search(gene, limit=3)
        for h in hits:
            print(f"  - {h.id}  {h.name}  oligomeric={h.oligomeric_state}  "
                  f"chains={h.chain_count}")

        # 2) Receptor by gene
        print(f"\n== receptors.by_gene('{gene}') ==")
        complexes = client.receptors.by_gene(gene)
        for c in complexes[:3]:
            print(f"  - {c.id}  {c.name}  ({c.oligomeric_state})")

        # 3) Resolve a structure
        print(f"\n== structures.resolve(gene='{gene}') ==")
        struct = client.structures.resolve(gene=gene)
        print(f"  Source: {struct.source}  PDB: {struct.pdb_id or '—'}  "
              f"UniProt: {struct.uniprot_id or '—'}")

        # 4) List candidate structures (different sources / conformations)
        print(f"\n== structures.candidates('{gene}') ==")
        candidates = client.structures.candidates(gene)
        for c in candidates[:5]:
            print(f"  - tier={c.tier:<10}  pdb={c.pdb_id or '—'}  "
                  f"score={c.score}  conf={c.conformation_label or '—'}")

        # 5) Quick + full structural analysis
        print(f"\n== structures.analyze('{gene}', analysis_depth='quick') ==")
        quick = client.structures.analyze(gene, analysis_depth="quick")
        print(f"  pocket_count={quick.pocket_count}  best_score={quick.best_pocket_score}")

        # 6) Specific PDB pull
        if struct.pdb_id:
            print(f"\n== structures.from_pdb('{struct.pdb_id}') ==")
            from_pdb = client.structures.from_pdb(struct.pdb_id)
            print(f"  Returned: {from_pdb.source} (pdb={from_pdb.pdb_id})")

        # 7) Resolve gene name (for ambiguous queries)
        print("\n== structures.resolve_gene_name('insulin receptor') ==")
        gene_resolution = client.structures.resolve_gene_name("insulin receptor")
        print(f"  resolved -> {gene_resolution.gene} (uniprot={gene_resolution.uniprot_id})")

    except LigandAIError as e:
        print(f"API error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
