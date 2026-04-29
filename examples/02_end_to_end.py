# Copyright © 2025 Ligandal, Inc. All rights reserved.
"""End-to-end peptide design pipeline.

Discovery → structure → generation → fold → score → synthesis cart.

Run:
    LIGANDAI_API_KEY=lgai_pro_... python examples/02_end_to_end.py
"""

from __future__ import annotations

from ligandai import LigandAI


def main() -> None:
    client = LigandAI()
    print(f"Tier={client.tier}, credits={client.credits}\n")

    # 1. Discovery: find liver-specific surface markers (GTEx).
    print("Step 1/6: Liver-specific surface markers")
    markers = client.discovery.tissue_markers(
        target_tissues=["Liver"],
        receptor_only=True,
        top_n=20,
    )
    if not markers.top:
        print("  No markers returned.")
        return
    for m in markers.top[:5]:
        print(f"  {m.gene}: SI={m.si:.1f} rank={m.rank}")

    top_gene = markers.top[0].gene
    print(f"\nSelected: {top_gene}\n")

    # 2. Structure resolution + pocket extraction
    print("Step 2/6: Structure resolution & pocket analysis")
    structure = client.structures.get(top_gene)
    print(f"  Source: {structure.source}, PDB: {structure.pdb_code}")
    analysis = client.structures.analyze(top_gene, analysis_depth="full")
    if analysis.recommended_pocket:
        p = analysis.recommended_pocket
        print(f"  Recommended pocket: {p.range} ({p.label})")
    print()

    # 3. Generate peptides (free/basic: 100, pro: 1000, enterprise: 5000)
    print("Step 3/6: Peptide generation + auto-fold (this can take 10-30 min)")
    job = client.peptides.generate(
        gene=top_gene,
        num_peptides=300,
        target_residues=[analysis.recommended_pocket] if analysis.recommended_pocket else None,
        targeting_strategy=("pocket_targeted" if analysis.recommended_pocket else "full_surface"),
        auto_fold=True,
        top_n_fold=25,
    )
    print(f"  Submitted job {job.id}, status={job.status}")

    # 4. Wait for completion (or stream — see 06_streaming.py)
    result = job.wait(
        timeout=1800,
        on_progress=lambda info: print(f"  {info.status} {info.progress or ''}"),
    )
    print(f"\n  Got {len(result.peptides)} peptides")
    if result.peptides:
        top = result.peptides[0]
        print(f"  Top: {top.sequence[:30]}... iPSAE={top.ipsae}\n")

    # 5. Pre-synthesis solubility check
    print("Step 4/6: Solubility analysis")
    top10 = result.peptides[:10]
    solubility = client.peptides.analyze_solubility(top10)
    soluble = [p for p, s in zip(top10, solubility) if s.passes_filter]
    print(f"  {len(soluble)}/10 pass solubility filter\n")

    # 6. Synthesis quote
    if not soluble:
        print("Skipping synthesis — no soluble candidates.")
        return
    print("Step 5/6: Synthesis quote")
    from ligandai import SynthesisPeptide
    synth_peptides = [
        SynthesisPeptide(sequence=p.sequence, name=p.name or f"peptide_{i}")
        for i, p in enumerate(soluble[:5])
    ]
    quote = client.synthesis.estimate(synth_peptides, gene=top_gene, include_bli=True)
    print(f"  Estimated cost: ${quote.total_usd:.2f} ({len(quote.line_items)} line items)\n")

    # 7. Add to cart
    print("Step 6/6: Synthesis cart")
    if job.session_id:
        cart = client.synthesis.add_to_cart(
            session_id=job.session_id,
            gene=top_gene,
            peptide_names=[p.name for p in synth_peptides if p.name],
            include_bli=True,
        )
        print(f"  Cart created: {cart.cart_id}")
        if cart.deep_link:
            print(f"  Open: {cart.deep_link}")


if __name__ == "__main__":
    main()
