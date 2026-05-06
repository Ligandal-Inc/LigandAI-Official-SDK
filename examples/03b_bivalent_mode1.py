# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Bivalent design (Beta) — Mode 1: Generate-Then-Link.

This is the biologically-conservative paradigm: design two binders
INDEPENDENTLY against their respective targets, then run a third generation
pass that fixes those two as `premade` segments and varies only the linker
between them. Validate by folding the full bivalent peptide against each
target alone — bivalent_score = min(iPTM_T1, iPTM_T2).

When to use this — and when NOT to.

  Use bivalent design when you want a single peptide to physically bridge
  two SEPARATE proteins that don't naturally interact (PROTAC-style induced
  proximity, molecular glue, forced heterodimerisation). The therapeutic
  mechanism IS the bridging itself.

  Do NOT use bivalent design for natively-multimeric targets (HER2 dimer,
  MHC class-II, antibody Fab+Fc, CD8αβ heterodimer). Multi-chain receptors
  are first-class via `client.peptides.generate` + `client.peptides.fold`
  with multi-chain target input. That path is the documented default.

Tier: pro+ required.

Run:
    LIGANDAI_API_KEY=lgai_pro_... python examples/03b_bivalent_mode1.py
"""

from __future__ import annotations

from ligandai import BivalentTarget, LigandAI, LinkerConfig


def main() -> None:
    client = LigandAI()
    if client.tier not in ("pro", "enterprise", "superadmin"):
        raise SystemExit(f"Bivalent design requires pro+. Current tier: {client.tier}")

    target1 = BivalentTarget(gene="PDCD1", chain="A")
    target2 = BivalentTarget(gene="CD274", chain="A")
    linker_cfg = LinkerConfig(
        position="C", length_min=8, length_max=20, composition="GGS",
    )

    # ----------------------------------------------------------------------
    # Step 1: plan two independent binder generations (no linker context).
    # ----------------------------------------------------------------------
    print("Step 1/4: run1_parallel — plan two independent binder runs")
    session = client.bivalent.run1_parallel(
        target1=target1,
        target2=target2,
        linker=linker_cfg,
        binder_length_min=15,
        binder_length_max=40,
        num_designs=200,
    )
    print(f"  Session: {session.id}")
    print(f"  Status:  {session.status}")
    # The server returned target1_plan + target2_plan in the raw response.
    # The SDK wraps them in BivalentSession; use client.bivalent.get_session
    # to retrieve the plans for piping into client.peptides.generate.

    # ----------------------------------------------------------------------
    # Step 2: dispatch the two generations in parallel and pick top-K binders.
    # In a real workflow you'd:
    #   plans = client.bivalent.get_session(session.id).target_plans
    #   t1_run = client.peptides.generate(receptor_pdb=...,
    #                                     segment_config=plans.t1.segment_config)
    #   t2_run = client.peptides.generate(receptor_pdb=...,
    #                                     segment_config=plans.t2.segment_config)
    # then score, AI-review, and pick top-K from each. For this example we
    # use stand-in sequences.
    # ----------------------------------------------------------------------
    top_t1 = ["LSEKQLEKLEEEKKK", "AERLAERLAERLAER"]
    top_t2 = ["DKKGSEKQLEEELKE", "MQLLEAARRREEAQK"]

    print("\nStep 2/4: record_mode1_binders — persist top-K from each run")
    recorded = client.bivalent.record_mode1_binders(
        session_id=session.id,
        top_t1=top_t1,
        top_t2=top_t2,
    )
    print(f"  Recorded: t1={recorded.get('recorded', {}).get('t1')}, "
          f"t2={recorded.get('recorded', {}).get('t2')}")
    print(f"  Status:   {recorded.get('status')}")

    # ----------------------------------------------------------------------
    # Step 3: optimise ONLY the linker between the two best binders.
    #   segment_config = [premade(top_t1[0]) + linker(varied) + premade(top_t2[0])]
    # ----------------------------------------------------------------------
    print("\nStep 3/4: optimize_linker — linker-only generation between fixed termini")
    plan = client.bivalent.optimize_linker(
        session_id=session.id,
        t1_index=0,
        t2_index=0,
        num_designs=100,
    )
    print(f"  Anchor T1: {plan.get('binder_t1')}")
    print(f"  Anchor T2: {plan.get('binder_t2')}")
    seg = plan.get("segment_config", {}).get("segments", [])
    print(f"  Segments:  {[(s.get('type'), s.get('label')) for s in seg]}")
    gp = plan.get("generation_params", {})
    print(f"  Length:    {gp.get('binder_length_min')}–{gp.get('binder_length_max')} aa")

    # Pipe `plan.segment_config` to client.peptides.generate(...) to produce
    # linker variants. Score by validation fold (Step 4) and pick the best.

    # ----------------------------------------------------------------------
    # Step 4: validation folds (peptide × T1 + peptide × T2 separately).
    # bivalent_score = min(iPTM_T1, iPTM_T2). Both interfaces must clear
    # threshold; the weakest one drives the composite score.
    # ----------------------------------------------------------------------
    bivalent_seq = top_t1[0] + "GGGGSGGS" + top_t2[0]  # placeholder
    print(f"\nStep 4/4: dispatch_folds — peptide × T1 + peptide × T2")
    print(f"  Candidate: {bivalent_seq}  ({len(bivalent_seq)} aa)")
    descriptors = [
        {
            "candidate_sequence": bivalent_seq,
            "fold_mode": "target1",
            "entities": [
                {"type": "protein", "chainId": "A", "sequence": bivalent_seq,
                 "bivalent_role": "peptide", "use_msa": False},
                {"type": "protein", "chainId": "B", "gene": target1.gene,
                 "bivalent_role": "target1"},
            ],
        },
        {
            "candidate_sequence": bivalent_seq,
            "fold_mode": "target2",
            "entities": [
                {"type": "protein", "chainId": "A", "sequence": bivalent_seq,
                 "bivalent_role": "peptide", "use_msa": False},
                {"type": "protein", "chainId": "B", "gene": target2.gene,
                 "bivalent_role": "target2"},
            ],
        },
    ]
    dispatched = client.bivalent.dispatch_folds(
        session_id=session.id,
        descriptors=descriptors,
        num_trajectories=1,
        model="boltz2",
    )
    ids = dispatched.get("fold_job_ids", {})
    print(f"  T1 fold:   {ids.get('t1', [None])[0]}")
    print(f"  T2 fold:   {ids.get('t2', [None])[0]}")
    print(f"  Failures:  {len(dispatched.get('failures', []))}")
    print()
    print("  Poll client.bivalent.get_session(session.id) until both folds "
          "land in fold_results, then read bivalent_score = min(iPTM_T1, iPTM_T2).")


if __name__ == "__main__":
    main()
