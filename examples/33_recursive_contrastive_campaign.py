# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""
33 — A recursive, contrastive, off-target-aware design campaign, two rounds.

[bd-dre-if562]

The scenario: design a peptide binder against a target that has close paralogs.
Binding the target is the easy half. NOT binding the paralogs is the half that
decides whether the molecule is useful, and the half a single-target run cannot
see at all.

What this example runs
----------------------

Per round:

  1. GENERATE against the target, steered away from real off-target receptor
     pockets. On a v6.9-line model the platform's guidance reference is the mean
     of REAL paralog pockets — it refuses to run against a fabricated all-zero
     reference — so the run is contrastive whether or not you choose the panel.
     `contrastive=` is how you choose it (see the note at the bottom).

  2. FOLD every design against the target AND every off-target, in one panel.
     Same designs, same settings, complete comparator set.

  3. SCORE on `peptide_ipsae` — the binder-restricted interface score. NEVER
     whole-complex `ipsae`, which a multimeric receptor inflates with its own
     self-confidence.

  4. SELECT on Δ = ipSAE(target) − ipSAE(worst off-target). Ranking on the
     target score alone selects designs that bind everything.

  5. REPLICATE the shortlist at independent draws. This is the step that decides
     what is real: the top of a single-draw list is chosen partly by that draw's
     noise, so re-rolling moves it down. Expect to lose some — that is the
     measurement working, not the pipeline failing.

  6. FEED FORWARD — designs measured to prefer an off-target become hard
     negatives and are not re-proposed.

Cost
----

`fold_panel` submits `len(designs) x (1 + len(off_targets)) x replicates` fold
jobs. That multiplies fast. Start with the small numbers below, read
`client.account`, and scale once the shape is right.

Run it
------
    export LIGANDAI_API_KEY=...
    python examples/33_recursive_contrastive_campaign.py

Point `base_url` at a staging deployment to rehearse without touching
production capacity.
"""

from __future__ import annotations

import json
import os

from ligandai import LigandAI
from ligandai.contrastive import AvoidancePolicy

# ── Campaign definition ──────────────────────────────────────────────────────

TARGET = os.environ.get("CAMPAIGN_TARGET", "ENPP3")

# The comparator panel. Choose it for MEASURED discriminating power, not for
# taxonomic tidiness: a paralog that scores far below everything else gives a
# margin that is close to free and grades nothing. `panel.deltas()` returns
# per-comparator counts so you can see which ones are actually doing work.
OFF_TARGETS = [g for g in os.environ.get("CAMPAIGN_OFF_TARGETS", "ENPP1,ENPP6").split(",") if g]

N_PEPTIDES = int(os.environ.get("CAMPAIGN_N", "60"))
ROUNDS = int(os.environ.get("CAMPAIGN_ROUNDS", "2"))

# The on-target floor. Take it from the platform's tier ladder rather than
# inventing one — 0.66 is the Great+ floor, the level hit rates are quoted on.
# `client.evaluator.filter_folds(..., criteria=...)` enforces the named tiers
# server-side if you would rather not carry a number here at all.
TARGET_LEG_MIN = float(os.environ.get("CAMPAIGN_TARGET_MIN", "0.66"))


def main() -> None:
    client = LigandAI(
        api_key=os.environ["LIGANDAI_API_KEY"],
        base_url=os.environ.get("LIGANDAI_BASE_URL"),
    )

    # ── Step 0: measure the noise band BEFORE it is needed ───────────────────
    #
    # ⛔ A Δ is only readable against the draw-to-draw spread of the metric on
    # THIS target. A band copied from another campaign is a guess wearing a
    # number. So: fold a handful of designs twice and read the spread.
    #
    # Production folds are unseeded, so two submissions of the same design are
    # two independent draws. (They are therefore not reproducible either — you
    # cannot re-request draw #2. Keep the fold job ids.)
    print(f"[0] calibrating the noise band on {TARGET} …")
    probe_designs = client.peptides.generate(
        gene=TARGET,
        num_peptides=8,
        length_range=(12, 24),
        model="v6.9.21",
        auto_fold=False,
    ).wait()
    probe_seqs = [p.sequence for p in (probe_designs.peptides or [])][:8]

    probe_panel = client.campaigns.fold_panel(
        probe_seqs,
        target=TARGET,
        off_targets=OFF_TARGETS,
        replicates=2,          # two independent draws per leg — this IS the band
        diffusion_samples=4,
    )
    spread = probe_panel.observed_spread()
    band = probe_panel.cross_draw_band()
    print(f"    measured per-leg range (median): {spread['median_range']}")
    print(f"    band on a DIFFERENCE of two independent legs: {band}")
    if band is None:
        raise SystemExit(
            "no spread measured — every leg came back with a single draw. "
            "Without a band, no Δ in this campaign is interpretable."
        )

    # ── The loop ─────────────────────────────────────────────────────────────
    campaign = client.campaigns.recursive(
        target=TARGET,
        off_targets=OFF_TARGETS,
        band=band,
        target_leg_min=TARGET_LEG_MIN,
        model="v6.9.21",
        num_peptides=N_PEPTIDES,
        length_range=(12, 24),
        # Head-to-tail cyclisable designs: a cysteine at BOTH termini, placed by
        # the kernel rather than hoped for.
        cysteine_mode="terminal_pair",
        cyclic_mode="disulfide",
        auto_fold=False,        # the panel does the folding, not the generator
        avoidance=AvoidancePolicy(off_target_genes=OFF_TARGETS),
    )

    for _ in range(ROUNDS):
        rnd = campaign.run_round(
            replicates=1,           # per-design draws inside the screening panel
            confirm=True,           # re-fold the survivors at a fresh draw
            confirm_replicates=1,
            panel_kwargs={"diffusion_samples": 4},
        )
        print(f"\n── round {rnd.index} " + "─" * 50)
        print(f"    generated              : {len(rnd.generated)}")
        print(f"    dropped (hard negative): {len(rnd.filtered_as_hard_negative)}")
        print(f"    complete panel rows    : {rnd.summary.get('n_complete')}")
        print(f"    upper bounds (partial) : {rnd.summary.get('n_partial_upper_bounds_not_verdicts')}")
        print(f"    scaffold-mediated      : {rnd.summary.get('n_excluded_scaffold_mediated')}")
        print(f"    Δ below the band       : {rnd.summary.get('n_delta_below_band')}")
        print(f"    survived one panel     : {len(rnd.survivors)}")
        print(f"    HELD at a fresh draw   : {len(rnd.confirmed)}")
        for note in rnd.notes:
            print(f"    · {note}")

        # The top of the table, with its comparators visible. Ranked on Δ.
        for row in rnd.complete[:5]:
            print(
                f"      {row.sequence[:24]:<24} Δ={row.delta:+.4f} "
                f"target={row.target_ipsae:.3f} worst={row.worst_off_target}"
                f"({row.worst_off_target_ipsae:.3f}) {row.verdict}"
                f"{'' if row.resolvable else '  ⚠ Δ inside the band — not a measurement'}"
            )

    # ── What survived ────────────────────────────────────────────────────────
    print("\n" + "=" * 66)
    print("CONFIRMED — passed a complete panel AND held at an independent draw")
    for seq in campaign.confirmed:
        print(f"  {seq}")
    print(f"\nhard negatives carried forward: {len(campaign.hard_negatives)}")

    with open("campaign_report.json", "w") as fh:
        json.dump(campaign.report(), fh, indent=2)
    print("wrote campaign_report.json")

    # Per-(design, receptor) rows for a DataFrame / CSV, with the draws intact.
    last = campaign.rounds[-1].panel
    if last is not None:
        with open("campaign_panel_rows.json", "w") as fh:
            json.dump(last.to_rows(), fh, indent=2)
        print("wrote campaign_panel_rows.json")


# ─────────────────────────────────────────────────────────────────────────────
# Choosing the contrastive panel yourself
# ─────────────────────────────────────────────────────────────────────────────
#
# Everything above lets the platform derive its own reference panel. To CHOOSE
# it — which is what closes the recursion, because last round's survivors become
# next round's references — build the panel explicitly:
#
#     refs = client.campaigns.contrastive_references(
#         target=TARGET, paralogs=OFF_TARGETS, model="v6.9.21",
#     )
#     job = client.peptides.generate(
#         gene=TARGET, num_peptides=200, model="v6.9.21", contrastive=refs,
#     )
#
# ⛔ On the current public API both of those calls stop with a named error
# (LigandAIParameterNotForwarded) rather than doing nothing quietly:
#
#   * the pocket featurizer that produces a reference at the model's declared
#     width is not mounted on the public API, and
#   * the generation endpoint's forward payload is built from a fixed key list
#     that does not include the references — so they would be accepted by HTTP,
#     dropped in transit, and the run would report the panel as set while
#     executing without it.
#
# The client surface is complete and the errors name the exact hop, so this
# example starts working unchanged the moment the plumbing lands. Until then,
# `contrastive=` is reachable against a deployment where the passthrough exists
# by passing `allow_unforwarded_contrastive=True`.


if __name__ == "__main__":
    main()
