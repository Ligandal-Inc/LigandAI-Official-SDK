# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Custom variant — fold a mutation, save as variant, then generate against it.

Useful for designing peptides against disease-causing mutants (e.g., L858R EGFR).

Run:
    LIGANDAI_API_KEY=lgai_pro_... python examples/05_custom_variant.py
"""

from __future__ import annotations

from ligandai import LigandAI


def main() -> None:
    client = LigandAI()
    print(f"Tier={client.tier}\n")

    # 1. Fold a mutation
    print("Step 1/3: Fold EGFR L858R mutation")
    fold_job = client.peptides.fold_custom_mutation(
        gene="EGFR",
        mutations=["L858R"],
        alias="EGFR-L858R",
    )
    fold_result = fold_job.wait(timeout=900)
    print(f"  iPTM={fold_result.iptm}, pLDDT={fold_result.plddt}")

    # 2. Save the fold as a named variant
    print("\nStep 2/3: Save fold as protein variant")
    variant = client.proteins.save_fold_as_variant(
        fold_job_id=fold_job.id,
        gene="EGFR",
        alias="EGFR-L858R",
    )
    print(f"  Variant id: {variant.id}, alias: {variant.alias}")

    # 3. Generate peptides against the mutated structure
    print("\nStep 3/3: Generate peptides targeting the variant")
    gen_job = client.peptides.generate(
        gene="EGFR",
        variant_id=variant.id,
        num_peptides=100,
        auto_fold=True,
        top_n_fold=10,
    )
    result = gen_job.wait(timeout=1800)
    print(f"  Got {len(result.peptides)} peptides")
    for p in result.peptides[:3]:
        print(f"    {p.sequence[:30]}... iPSAE={p.ipsae}")


if __name__ == "__main__":
    main()
