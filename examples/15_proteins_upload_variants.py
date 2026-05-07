# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""
15 — Custom PDB uploads, protein variants, and receptor topology.

Covers:
  client.proteins.upload_pdb(file, gene, custom_name)
  client.proteins.variants(gene)
  client.proteins.get_variant(variant_id)
  client.proteins.delete_variant(variant_id)
  client.proteins.save_fold_as_variant(fold_job_id, gene, alias)
  client.proteins.info(gene, include_sequence, include_ptms, include_domains)
  client.proteins.receptor_topology(gene)
  client.proteins.disorder_profile(gene)
  client.proteins.check_glycosylation(gene, tissue, site_type)

Run with:
    LIGANDAI_API_KEY=lgai_pro_... LIGANDAI_PDB_FILE=/path/to/your.pdb python 15_proteins_upload_variants.py
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

    pdb_file = os.environ.get("LIGANDAI_PDB_FILE")
    gene = os.environ.get("LIGANDAI_GENE", "EGFR")
    client = LigandAI(api_key=key)

    try:
        # 1) Protein info
        print(f"== proteins.info('{gene}') ==")
        info = client.proteins.info(gene, include_sequence=False, include_ptms=True)
        print(f"  uniprot={info.uniprot_id}  length={info.sequence_length} aa")
        print(f"  ptms   ={len(info.ptms or [])} known PTMs")
        if info.domains:
            print(f"  domains: {', '.join(d.name for d in info.domains[:3])}…")

        # 2) Topology classification (membrane vs soluble, EC trimming hints)
        print(f"\n== proteins.receptor_topology('{gene}') ==")
        topo = client.proteins.receptor_topology(gene)
        print(f"  topology_type={topo.topology_type}  "
              f"ec_ranges={topo.ec_ranges or '—'}  "
              f"tm_ranges={topo.tm_ranges or '—'}")

        # 3) Disorder profile
        print(f"\n== proteins.disorder_profile('{gene}') ==")
        try:
            disorder = client.proteins.disorder_profile(gene)
            n_disordered = sum(1 for x in (disorder.scores or []) if x > 0.5)
            print(f"  n_disordered_residues>0.5: {n_disordered}/{len(disorder.scores or [])}")
        except LigandAIError as e:
            print(f"  (no disorder profile: {e})")

        # 4) Existing variants
        print(f"\n== proteins.variants(gene='{gene}') ==")
        variants = client.proteins.variants(gene=gene)
        for v in variants[:5]:
            print(f"  - id={v.id}  alias={v.alias}  status={v.status}  pdb={bool(v.pdb_content)}")

        # 5) Optional: upload a custom PDB
        if pdb_file and Path(pdb_file).exists():
            print(f"\n== proteins.upload_pdb('{pdb_file}', gene='{gene}', "
                  f"custom_name='example_upload') ==")
            uploaded = client.proteins.upload_pdb(
                file=pdb_file,
                gene=gene,
                custom_name="example_upload_15",
            )
            print(f"  variant_id={uploaded.id}  status={uploaded.status}")

            # 6) Get the freshly uploaded variant
            fetched = client.proteins.get_variant(uploaded.id)
            print(f"  fetched alias={fetched.alias}  has_pdb={bool(fetched.pdb_content)}")

            # NOTE: We do NOT delete here — leaving the variant in the user's
            # workspace. To clean up, call client.proteins.delete_variant(uploaded.id).
        else:
            print("\n(set LIGANDAI_PDB_FILE to demonstrate upload_pdb)")

    except LigandAIError as e:
        print(f"API error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
