# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""
12 — Peptide listing, search, and retrieval (closes Andrew Keene's gaps).

Covers:
  client.peptides.list(program_id=..., gene=..., min_iptm=..., max_kd=...)
  client.peptides.list_by_program(program_id, ...)
  client.peptides.search(...)
  client.peptides.search_by_pocket(gene, chain, start_residue, end_residue)
  client.peptides.by_gene([genes])
  client.peptides.get(peptide_id)
  client.peptides.get_elite(...)

Run with:
    LIGANDAI_API_KEY=lgai_pro_... python 12_peptide_listing_search.py
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

    client = LigandAI(api_key=key)

    try:
        # 1) Cross-program search by gene + iPSAE/ipTM filter
        print("== peptides.search(gene='EGFR', ipsae_min=0.7, limit=5) ==")
        hits = client.peptides.search(gene="EGFR", ipsae_min=0.7, limit=5)
        for p in hits:
            seq_display = p.sequence if not getattr(p, "_tier_redacted", False) else \
                f"{p.sequence[:10]}…"
            print(f"  {seq_display:<40}  iPSAE={p.ipsae:.3f}  ipTM={p.iptm or 'n/a'}")

        # 2) Per-program listing — enumerate programs first
        print("\n== programs.list() ==")
        programs = client.programs.list()
        for prog in programs[:3]:
            print(f"  - id={prog.id}  name={prog.name}  status={prog.status}")

        if programs:
            pid = programs[0].id
            print(f"\n== peptides.list(program_id={pid}, limit=5) ==")
            in_program = client.peptides.list(program_id=pid, limit=5)
            print(f"  {len(in_program)} peptides in program {pid}")
            for p in in_program:
                print(f"    {p.sequence[:30]:<32}  L={p.length}  iPSAE={p.ipsae or 'n/a'}")

            # 3) Pocket-scoped search (residues 100-130 on chain A)
            print(f"\n== peptides.search_by_pocket(gene='{programs[0].lead_gene or 'EGFR'}', chain='A', 100, 130) ==")
            try:
                pocket_hits = client.peptides.search_by_pocket(
                    gene=programs[0].lead_gene or "EGFR",
                    chain="A",
                    start_residue=100,
                    end_residue=130,
                    limit=5,
                )
                print(f"  {len(pocket_hits)} peptides contacting that pocket")
            except LigandAIError as e:
                print(f"  (no pocket-scoped results: {e})")

            # 4) Elite peptides for a session (top scorers)
            sessions = client.programs.list_sessions(limit=1)
            if sessions:
                sid = sessions[0].session_id
                print(f"\n== peptides.get_elite(session_id='{sid}') ==")
                elites = client.peptides.get_elite(session_id=sid)
                print(f"  {len(elites)} elites")

        # 5) by_gene cross-program rollup
        print("\n== peptides.by_gene(genes=['EGFR'], min_ipsae=0.8) ==")
        rollup = client.peptides.by_gene(genes=["EGFR"], min_ipsae=0.8)
        for r in rollup[:3]:
            print(f"  - {r.gene}: {r.peptide_count} peptides, best iPSAE={r.best_ipsae}")

        # 6) Detail fetch on a specific peptide
        if programs and in_program:
            pep_id = in_program[0].id
            print(f"\n== peptides.get({pep_id}) with detailed include ==")
            detail = client.peptides.get(pep_id, include=["scores", "fold_metadata"])
            print(f"  sequence={detail.sequence[:30]}…")
            print(f"  scores  ={getattr(detail, 'scores', {})}")

    except LigandAIError as e:
        print(f"API error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
