# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Example 8 — list peptides by program, fetch full PDB structures.

Demonstrates the v0.5.0 SDK additions:
  - client.peptides.list(program_id=42) (a user's #1 ask)
  - client.peptides.search(kd_max=1e-8, ...)    (cross-program search)
  - client.structures.list(program_id=42)       (fold-structure listing)
  - client.structures.get_pdb(structure_id)     (raw PDB; polyalanine on free)

Run with::

    LIGANDAI_API_KEY=lgai_pro_... python examples/08_program_list_and_structures.py
"""

from __future__ import annotations

import os
from pathlib import Path

from ligandai import LigandAI, LigandAIUpgradeRequired


def main() -> None:
    api_key = os.environ.get("LIGANDAI_API_KEY")
    if not api_key:
        raise SystemExit("Set LIGANDAI_API_KEY in your environment.")

    base_url = os.environ.get("LIGANDAI_BASE_URL", "https://ligandai.com")
    client = LigandAI(api_key=api_key, base_url=base_url)
    print(f"Connected as {client.email or 'anonymous'} ({client.tier})")

    # --- 1. Pick a program ----------------------------------------------------
    programs = client.programs.list()
    if not programs:
        print("No programs yet — create one in the dashboard or via the SDK.")
        return
    program = programs[0]
    print(f"\nProgram: {program.name} (id={program.id})")
    # v0.5.0: live counts now correct (was always 0 before)
    print(f"  peptides generated: {getattr(program, 'peptide_count', '?')}")
    print(f"  peptides folded:    {getattr(program, 'folded_count', '?')}")
    print(f"  elites:             {getattr(program, 'elite_count', '?')}")

    # --- 2. List elite peptides via /v1/peptides/list -------------------------
    print("\nTop 10 peptides by iPSAE in this program:")
    elites = client.peptides.list_by_program(
        program_id=program.id,
        min_ipsae=0.7,
        limit=10,
    )
    for p in elites:
        print(f"  {p.gene:8s}  {p.sequence:35s}  iPSAE={p.ipsae or 0:.3f}")

    if not elites:
        return

    # --- 3. Fetch the PDB for the top hit (free tier = polyalanine) ----------
    top = elites[0]
    print(f"\nFetching PDB for peptide_id={top.peptide_id}...")
    pdb_text = client.structures.get_pdb(top.peptide_id)
    out = Path(f"./{top.gene}_{top.peptide_id}.pdb")
    out.write_text(pdb_text)
    print(f"  wrote {len(pdb_text):,} chars to {out}")

    if "REMARK   1 LIGANDAI FREE TIER" in pdb_text[:200]:
        print("  (free tier: sidechains stripped, sequence redacted in PDB)")

    # --- 4. Cross-program search by Kd threshold ------------------------------
    print("\nSearching for binders with Kd < 100 nM across all programs:")
    try:
        hits = client.peptides.search(kd_max=1e-7, ipsae_min=0.7, limit=20)
    except LigandAIUpgradeRequired as e:
        print(f"  upgrade needed: {e.message} (visit {e.upgrade_url})")
        return
    by_gene: dict[str, list] = {}
    for h in hits:
        by_gene.setdefault(h.target_gene or h.gene or "?", []).append(h)
    for gene, rows in sorted(by_gene.items(), key=lambda kv: -len(kv[1])):
        best = min(rows, key=lambda x: x.predicted_kd or float("inf"))
        print(f"  {gene}: {len(rows):3d} hits, best Kd={best.predicted_kd:.2e} M")


if __name__ == "__main__":
    main()
