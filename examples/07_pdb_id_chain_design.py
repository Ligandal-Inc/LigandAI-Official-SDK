# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""07 — Design against a specific PDB ID + chain (multimer-aware).

Use case: user has a multimer like PDB 9MIR (chains A/B/C/D) and wants
peptides designed against only chain C.

The flow is:
  1. Confirm the PDB resolves on the platform: client.structures.from_pdb()
  2. Submit generate() with target_chains=["C"] to restrict the binding surface

This is the right pattern when the user names a PDB ID. If they hand you a
.pdb / .cif file from disk instead, see 05_custom_variant.py for the
proteins.upload_pdb() flow.
"""

from __future__ import annotations

import os

from ligandai import LigandAI


def main() -> None:
    client = LigandAI()  # reads LIGANDAI_API_KEY
    print(f"Tier: {client.tier}, Credits: {client.credits}")

    pdb_id = os.getenv("LIGANDAI_PDB_ID", "9MIR")
    chain = os.getenv("LIGANDAI_TARGET_CHAIN", "C")

    struct = client.structures.from_pdb(pdb_id)
    chains = [c.id for c in (struct.chains or [])] if hasattr(struct, "chains") else []
    print(f"Resolved {pdb_id}: source={struct.source} chains={chains}")
    if chains and chain not in chains:
        print(f"WARNING: chain '{chain}' not in {chains} — server will reject")

    est = client.peptides.estimate_cost(
        num_peptides=50, auto_fold=True, fold_top_n=10
    )
    print(f"Estimated cost: {est.credits} cr (${est.cost_usd:.2f})")

    job = client.peptides.generate(
        gene=pdb_id,
        target_chains=[chain],
        num_peptides=50,
        length_range=(20, 40),
        auto_fold=True,
        top_n_fold=10,
        quality_guided=True,
    )
    print(f"Submitted job {job.id}")

    result = job.wait(timeout=1800)
    print(f"Got {len(result.peptides)} peptides")
    for p in result.peptides[:5]:
        print(
            f"  {p.sequence}"
            f"  ipsae={getattr(p, 'ipsae', None)}"
            f"  binding_energy={getattr(p, 'binding_energy', None)}"
        )


if __name__ == "__main__":
    main()
