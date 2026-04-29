# Copyright © 2025 Ligandal, Inc. All rights reserved.
"""Async parallel peptide design across multiple genes.

Submits N generation jobs in parallel and waits for all to complete.

Run:
    LIGANDAI_API_KEY=lgai_pro_... python examples/04_async_parallel.py
"""

from __future__ import annotations

import asyncio

from ligandai import AsyncLigandAI


async def design_for_gene(client: AsyncLigandAI, gene: str) -> dict:
    print(f"  [{gene}] submitting...")
    job = await client.peptides.generate(
        gene=gene,
        num_peptides=100,
        auto_fold=True,
        top_n_fold=10,
    )
    print(f"  [{gene}] job_id={job.id}")
    result = await job.wait(timeout=1800)
    print(f"  [{gene}] done: {len(result.peptides)} peptides")
    return {
        "gene": gene,
        "job_id": job.id,
        "peptide_count": len(result.peptides),
        "top_ipsae": result.peptides[0].ipsae if result.peptides else None,
    }


async def main() -> None:
    genes = ["EGFR", "HER2", "KIT", "MET", "VEGFR2"]

    async with AsyncLigandAI() as client:
        print(f"Tier={client.tier}, designing for {len(genes)} genes in parallel\n")
        results = await asyncio.gather(
            *[design_for_gene(client, g) for g in genes],
            return_exceptions=True,
        )

    print("\n=== Results ===")
    for r in results:
        if isinstance(r, Exception):
            print(f"  FAILED: {r}")
        else:
            print(f"  {r['gene']}: {r['peptide_count']} peptides, top iPSAE={r['top_ipsae']}")


if __name__ == "__main__":
    asyncio.run(main())
