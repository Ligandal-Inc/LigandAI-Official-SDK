# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""
13 — Listing folded structures and pulling PDB content (a user gap closure).

Covers:
  client.structures.list(program_id=..., limit=..., offset=...)
  client.structures.get_pdb(structure_id)
  client.structures.from_uniprot / from_alphafold (single-chain pulls)

Run with:
    LIGANDAI_API_KEY=lgai_pro_... python 13_structures_listing_pdb_pull.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from ligandai import LigandAI
from ligandai.errors import LigandAIError


def main() -> int:
    key = os.environ.get("LIGANDAI_API_KEY")
    if not key:
        print("LIGANDAI_API_KEY env var is required", file=sys.stderr)
        return 1

    out_dir = Path(os.environ.get("LIGANDAI_OUT_DIR", "/tmp/ligandai_structures"))
    out_dir.mkdir(parents=True, exist_ok=True)

    client = LigandAI(api_key=key)

    try:
        # 1) List all folded structures (paged)
        print("== structures.list(limit=5) ==")
        listed = client.structures.list(limit=5)
        for s in listed:
            print(f"  id={s.get('id')}  gene={s.get('gene')}  "
                  f"plddt={s.get('plddt')}  iptm={s.get('iptm')}")

        # 2) Pull PDB content for the first one
        if listed:
            sid = listed[0].get("id")
            print(f"\n== structures.get_pdb({sid}) ==")
            pdb = client.structures.get_pdb(sid)
            outfile = out_dir / f"structure_{sid}.pdb"
            outfile.write_text(pdb)
            atom_count = pdb.count("\nATOM ")
            print(f"  wrote {outfile} ({len(pdb):,} bytes, {atom_count} ATOM records)")

        # 3) Per-program listing
        progs = client.programs.list()
        if progs:
            pid = progs[0].id
            print(f"\n== structures.list(program_id={pid}) ==")
            program_structures = client.structures.list(program_id=pid, limit=3)
            print(f"  {len(program_structures)} structures in program {pid}")

        # 4) Pull a fresh AlphaFold prediction by UniProt
        print("\n== structures.from_uniprot('P00533') (EGFR) ==")
        try:
            af = client.structures.from_uniprot("P00533")
            print(f"  source={af.source}  has_pdb_content={bool(af.pdb_content)}  "
                  f"len={len(af.pdb_content or '')} bytes")
        except LigandAIError as e:
            print(f"  (no AF cache yet: {e})")

    except LigandAIError as e:
        print(f"API error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
