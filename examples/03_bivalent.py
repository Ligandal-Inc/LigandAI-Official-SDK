# Copyright © 2025 Ligandal, Inc. All rights reserved.
"""Bivalent / bispecific peptide design.

Designs a PD-1 / PD-L1 bispecific binder using the bivalent workflow.

Tier: pro+ required.

Run:
    LIGANDAI_API_KEY=lgai_pro_... python examples/03_bivalent.py
"""

from __future__ import annotations

from ligandai import BivalentTarget, LigandAI, LinkerConfig


def main() -> None:
    client = LigandAI()
    if client.tier not in ("pro", "enterprise", "superadmin"):
        raise SystemExit(f"Bivalent design requires pro+. Current tier: {client.tier}")

    # Configure bivalent design.
    print("Step 1/3: Configure bivalent session")
    session = client.bivalent.start(
        target1=BivalentTarget(gene="PDCD1", chain="A"),
        target2=BivalentTarget(gene="CD274", chain="A"),
        linker=LinkerConfig(position="C", length_min=8, length_max=20, composition="GGS"),
        binder_length_min=15,
        binder_length_max=40,
        num_designs=200,
    )
    print(f"  Session: {session.id}")
    print(f"  Target1: {session.target1.gene}, Target2: {session.target2.gene}")

    # The .start() call returned segment_config + generation_params. The platform
    # would normally call generate_peptides next. For the SDK example we'll just
    # show the workflow shape — full integration requires the agent flow.
    print("\nStep 2/3: AI review of Run 1 (typically called after generation)")
    # Mock candidate sequences from Run 1
    run1_seqs = ["LSEKQLEKLEEEKKKGGGGS"] * 5
    review = client.bivalent.analyze_generation(
        session_id=session.id,
        stage="run1",
        sequences=run1_seqs,
        target_gene="PDCD1",
    )
    print(f"  Summary: {review.summary[:100]}...")

    # Run 2: hold Run 1 fixed, generate against Target 2.
    print("\nStep 3/3: Run 2 generation (Target 2)")
    selected_seeds = run1_seqs[:3]  # top 3 from Run 1 review
    session2 = client.bivalent.run2(
        session_id=session.id,
        selected_seeds=selected_seeds,
    )
    print(f"  Run 2 status: {session2.status}")


if __name__ == "__main__":
    main()
