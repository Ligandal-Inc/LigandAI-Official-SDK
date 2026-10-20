# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""
35 — Paralog-conditioned generation, and the honest measurement of it.

[bd-dre-siz9d] [bd-dre-atcmd]

⛔ READ docs/contrastive.md BEFORE reusing this pattern. In a controlled
head-to-head on BRD4-ET (330 resolved designs, three arms, one seed ladder),
paralog-conditioned generation did NOT produce more selective designs than
plain generate-and-filter: contrastive 0.437x vs baseline 0.414x vs an
unrelated-pocket control at 0.381x, all three sitting inside a measured
within-design min-of-3 permutation null of 0.423x [0.370, 0.486]
(contrastive-vs-baseline p=0.77). This example reproduces the MEASUREMENT
that finding rests on — the panel fold, the gate, and the null — not a
demonstration that conditioning "works". Do not lift the generate() call out
of this file and drop the rest.

What this script does
----------------------
  1. GENERATE against a target, steered away from named off-target pockets
     via `avoid_paralogs=` — the customer-reachable contrastive surface. No
     pocket-feature matrix is built or fetched; the server resolves the
     panel from the gene names alone.
  2. FOLD every design against the target AND every named paralog, in one
     panel — same designs, same settings, complete comparator set.
  3. GATE every leg on `peptide_ipsae >= 0.67` BEFORE any Kd is read. A
     computed 0.0 is a real (failing) measurement, never a blank; `None`
     means "not measured" and is tracked separately, never treated as zero.
  4. COMPUTE the selectivity distribution — `min(paralog Kd) / target Kd` —
     over designs that cleared the gate on EVERY leg, never a hand-picked
     best design.
  5. BUILD THE NULL BY PERMUTATION, on these exact rows. The null for a
     min-of-k ratio sits below 1x purely from taking a minimum over k draws,
     and it is population-specific: a null measured on a different panel, or
     a different k, does not transfer. So it is rebuilt here from this run's
     own gated legs, never assumed to be 1x and never copied from
     docs/contrastive.md's numbers (those came from a different, larger
     panel — bd-dre-atcmd).
  6. REPORT THE DISTRIBUTION AGAINST THE NULL — median, spread, and the
     fraction of designs that exceed 1x in BOTH the real labeling and the
     null. A large "percent selective" number that also shows up under
     random relabeling is the min-of-k artifact, not evidence.

Cost
----
`fold_panel` submits `len(designs) x (1 + len(paralogs)) x replicates` fold
jobs. The permutation null itself costs nothing — it is pure re-arithmetic
over already-measured rows, run thousands of times locally.

Run it
------
    export LIGANDAI_API_KEY=...
    python examples/35_paralog_conditioned_generation.py

Point `base_url` at a staging deployment to rehearse without touching
production capacity.
"""

from __future__ import annotations

import json
import os
import random
import statistics

from ligandai import LigandAI

# ── Run definition ───────────────────────────────────────────────────────────

TARGET = os.environ.get("EXAMPLE_TARGET", "BRD4")

# The named off-target panel. `avoid_paralogs=` sends exactly these gene
# symbols as `cfg_reference_genes`; each must independently clear the
# platform's paralog criterion (same species, Compara gene-tree paralogy,
# shared Pfam family containing the pocket, domain identity in [0.30, 0.95),
# pocket structural correspondence, an experimental structure, a different
# accession) or the run is refused rather than silently substituting a
# different panel. See docs/contrastive.md.
PARALOGS = [g for g in os.environ.get("EXAMPLE_PARALOGS", "BRD2,BRD3,BRDT").split(",") if g]

N_PEPTIDES = int(os.environ.get("EXAMPLE_N", "60"))

# peptide_ipsae gate, applied to EVERY leg (target and every paralog) before
# any design enters the selectivity computation. This is the platform's
# Great+ floor — never a number invented for this script.
IPSAE_GATE = 0.67

N_PERMUTATIONS = int(os.environ.get("EXAMPLE_N_PERM", "5000"))


def _gated_legs_per_design(rows: list[dict]) -> dict[str, dict[str, float]]:
    """``{sequence: {leg_name: kd_nm}}`` for designs gated on EVERY leg.

    A leg name is the target gene or a paralog gene. A design is included
    only if ALL of its legs (target + every named paralog) individually
    cleared ``peptide_ipsae >= IPSAE_GATE`` and carry a Kd. ``peptide_ipsae``
    is read as a MEASUREMENT — a returned 0.0 fails the gate exactly like any
    other sub-threshold value; ``None`` means "not measured" and that design
    is dropped here rather than silently scored on a subset of its legs.
    """
    expected_legs = {TARGET, *PARALOGS}
    by_seq: dict[str, dict[str, dict]] = {}
    for row in rows:
        by_seq.setdefault(row["sequence"], {})[row["receptor"]] = row

    n_incomplete = 0
    n_not_measured = 0
    n_failed_gate = 0
    n_missing_kd = 0
    out: dict[str, dict[str, float]] = {}
    for seq, legs in by_seq.items():
        if set(legs) != expected_legs:
            n_incomplete += 1  # a partial panel row — an upper bound, never a verdict
            continue
        kds: dict[str, float] = {}
        ok = True
        for name, row in legs.items():
            ipsae = row.get("peptide_ipsae")
            if ipsae is None:
                n_not_measured += 1
                ok = False
                break
            assert ipsae <= 1.0, (
                f"{seq[:12]}... vs {name}: peptide_ipsae={ipsae} > 1.0 — this is "
                f"the pre-fold predicted column or whole-complex ipsae, not the "
                f"binder-restricted interface value; fix the row source, do not "
                f"clamp it here"
            )
            if ipsae < IPSAE_GATE:
                n_failed_gate += 1
                ok = False
                break
            kd = row.get("kd_nm")
            if kd is None:
                n_missing_kd += 1
                ok = False
                break
            kds[name] = float(kd)
        if ok:
            out[seq] = kds

    print(
        f"    gate: {len(out)} design(s) cleared peptide_ipsae >= {IPSAE_GATE} "
        f"on every leg with a Kd; {n_incomplete} incomplete panel row(s), "
        f"{n_not_measured} leg(s) not measured, {n_failed_gate} leg(s) measured "
        f"below the gate (a real result, not a blank), {n_missing_kd} leg(s) "
        f"gated-in with no persisted Kd"
    )
    return out


def _null_draw(legs_per_design: dict[str, dict[str, float]], rng: random.Random) -> float:
    """One null draw: relabel a RANDOM leg per design as "on-target".

    Returns the population MEDIAN of min(remaining legs) / relabeled-on-target
    over every design in this draw. Call many times to build a null
    distribution for the SAME population the observed statistic came from —
    see ``main()``.
    """
    ratios: list[float] = []
    for kds in legs_per_design.values():
        names = list(kds)
        on = rng.choice(names)
        on_target_kd = kds[on]
        offs = [v for n, v in kds.items() if n != on]
        if on_target_kd > 0 and offs:
            ratios.append(min(offs) / on_target_kd)
    return statistics.median(ratios) if ratios else float("nan")


def main() -> None:
    # Rebound below when the PLATFORM picks the panel: the resolved provenance is
    # then the only source for which receptors every downstream leg must use.
    global PARALOGS

    client = LigandAI(
        api_key=os.environ["LIGANDAI_API_KEY"],
        base_url=os.environ.get("LIGANDAI_BASE_URL"),
    )

    # ── 1. Generate, steered away from the named panel ───────────────────────
    #
    # ⛔ NAMING A PANEL vs LETTING THE PLATFORM PICK. `avoid_paralogs=` has three
    # states, and only the first puts anything on the wire:
    #
    #   avoid_paralogs=PARALOGS  -> you choose the comparators. Sends
    #                               cfg_reference_genes. THIS is what the script
    #                               runs, because the selectivity panel below
    #                               has to fold against a panel you KNOW.
    #   avoid_paralogs=[]        -> the platform chooses the comparators.
    #                               Conditioning still happens; nothing extra is
    #                               sent; NOT an error.
    #   avoid_paralogs omitted   -> you did not choose comparators. Byte-identical
    #                               to [] above.
    #
    # On a capable checkpoint the model conditions in ALL THREE. "Off" means "I
    # did not choose the comparators", never "no conditioning" — there is no
    # conditioning-disable switch on this surface. Set EXAMPLE_PLATFORM_PICKS=1
    # to run the platform-picks variant instead; the script then reads the panel
    # back out of the provenance, because it no longer knows it up front.
    platform_picks = os.environ.get("EXAMPLE_PLATFORM_PICKS") == "1"
    panel: list[str] = [] if platform_picks else PARALOGS

    if platform_picks:
        print(f"[1] generating {N_PEPTIDES} design(s) against {TARGET}, "
              f"steered away from a panel the PLATFORM picks "
              f"(avoid_paralogs=[] — conditioning still happens) …")
    else:
        print(f"[1] generating {N_PEPTIDES} design(s) against {TARGET}, "
              f"steered away from {PARALOGS} …")

    job = client.peptides.generate(
        gene=TARGET,
        num_peptides=N_PEPTIDES,
        length_range=(12, 24),
        model="v6.9.21",              # CFG-capable — avoid_paralogs requires it
        avoid_paralogs=panel,
        auto_fold=False,              # the panel below does the folding
    )
    result = job.wait()
    sequences = [p.sequence for p in (result.peptides or [])]
    print(f"    generated: {len(sequences)}")

    # Read back the provenance rather than assuming the panel you named is
    # the panel that actually ran — a named gene that fails the paralog
    # criterion is rejected per-gene, never silently substituted.
    resolved: list[str] = []
    for event in getattr(job, "events", None) or []:
        if getattr(event, "event", None) == "cfg_references_resolved":
            for p in event.data.get("paralogs", []):
                resolved.append(p["paralog_gene"])
                print(f"    steered away from {p['paralog_gene']} "
                      f"({p.get('source_pdb_id')}:{p.get('source_chain')}, "
                      f"identity={p.get('identity_to_target_domain')})")

    # ⛔ IN THE PLATFORM-PICKS VARIANT THE PROVENANCE IS THE ONLY SOURCE for what
    # the panel was, so every downstream leg (folding, the selectivity minimum,
    # the permutation null) must use the RESOLVED genes. Folding against the
    # PARALOGS default here would measure selectivity over receptors the run was
    # never steered away from, and report it as a conditioned result.
    if platform_picks:
        if not resolved:
            raise SystemExit(
                "the platform picked the panel but this server build reported no "
                "cfg_references_resolved provenance — without it there is no "
                "defensible panel to fold against, and guessing one would "
                "measure selectivity over receptors nothing steered away from"
            )
        PARALOGS = resolved
        print(f"    platform-chosen panel: {PARALOGS}")

    if not sequences:
        raise SystemExit("no designs generated — nothing to fold")

    # ── 2. Fold every design against the target AND every paralog ───────────
    print(f"[2] folding {len(sequences)} design(s) against {TARGET} + {PARALOGS} …")
    panel = client.campaigns.fold_panel(
        sequences,
        target=TARGET,
        off_targets=PARALOGS,
        replicates=1,
        diffusion_samples=4,
    )
    rows = panel.to_rows()
    with open("paralog_panel_rows.json", "w") as fh:
        json.dump(rows, fh, indent=2, default=str)
    print("    wrote paralog_panel_rows.json")

    # ── 3+4. Gate on peptide_ipsae, THEN read the Kd-based selectivity ───────
    print(f"[3] gating on peptide_ipsae >= {IPSAE_GATE} before any Kd is read …")
    legs_per_design = _gated_legs_per_design(rows)
    if not legs_per_design:
        raise SystemExit(
            "no design cleared the gate on every leg — there is no selectivity "
            "distribution to compute. This is a real, reportable outcome, not "
            "a script failure."
        )

    observed = sorted(
        min(v for n, v in kds.items() if n != TARGET) / kds[TARGET]
        for kds in legs_per_design.values()
    )
    observed_median = statistics.median(observed)
    n_over_1x = sum(1 for r in observed if r > 1.0)

    # ── 5. Build the null BY PERMUTATION on these exact rows ─────────────────
    # ⛔ NOT 1.0x, and NOT the number in docs/contrastive.md — that number came
    # from a different, larger panel (bd-dre-atcmd) and does not transfer to
    # this run's own k, population, or draw. Rebuilding it here, every time,
    # is the point.
    print(f"[4] building the min-of-k null by permutation ({N_PERMUTATIONS} draws) …")
    rng = random.Random(0)

    # The free falsification check: with exactly 2 legs (target vs ONE
    # paralog at a time), a random relabeling has no way to prefer either
    # side, so the null median across many draws MUST land at ~1.0. If it
    # doesn't, the permutation itself is wrong and nothing below means
    # anything — see docs/contrastive.md's "k=1 method check".
    pairs = [
        {TARGET: kds[TARGET], paralog: kds[paralog]}
        for kds in legs_per_design.values()
        for paralog in PARALOGS
        if paralog in kds
    ]
    k1_null_draws = [
        _null_draw({str(i): pair for i, pair in enumerate(pairs)}, rng)
        for _ in range(min(2000, N_PERMUTATIONS))
    ]
    print(f"    k=1 method check: null median = {statistics.median(k1_null_draws):.4f} "
          f"(must be ~1.0000 or the permutation is broken, not the finding)")

    null_draws = sorted(
        _null_draw(legs_per_design, rng) for _ in range(N_PERMUTATIONS)
    )
    null_median = statistics.median(null_draws)
    null_lo = null_draws[int(0.05 * len(null_draws))]
    null_hi = null_draws[min(int(0.95 * len(null_draws)), len(null_draws) - 1)]
    n_null_over_1x = sum(1 for d in null_draws if d > 1.0)

    # ── 6. Report the DISTRIBUTION against the null — never a best design ────
    print("\n" + "=" * 72)
    print(f"n designs gated on every leg   : {len(legs_per_design)}")
    print(f"observed selectivity median    : {observed_median:.4f}x")
    print(f"observed selectivity range     : {observed[0]:.4f}x - {observed[-1]:.4f}x")
    print(f"observed designs > 1x          : {n_over_1x}/{len(observed)} "
          f"({100 * n_over_1x / len(observed):.1f}%)")
    print(f"min-of-k null (THIS run's rows) : {null_median:.4f}x "
          f"[{null_lo:.4f}, {null_hi:.4f}]")
    print(f"null draws w/ median > 1x       : {n_null_over_1x}/{len(null_draws)} "
          f"({100 * n_null_over_1x / len(null_draws):.1f}%)")
    print(
        "\n⛔ A population where many designs sit above 1x selectivity is NOT "
        "evidence of conditioning by itself — the null above is built from the "
        "SAME rows with a RANDOM leg playing \"on-target\", and if it also clears "
        "1x at a comparable rate, the effect is the min-of-k artifact, not a "
        "measured selectivity margin. Compare the OBSERVED median against the "
        "NULL BAND above; only a median that falls outside the band is even a "
        "candidate for a real effect, and even then it wants a baseline "
        "(unconditioned generate-and-filter) arm before it is reported as one — "
        "see docs/contrastive.md for what that comparison looked like on the "
        "panel this platform actually ran, and why it came back null there too."
    )

    with open("paralog_conditioned_report.json", "w") as fh:
        json.dump(
            {
                "target": TARGET,
                "paralogs": PARALOGS,
                "n_generated": len(sequences),
                "n_gated": len(legs_per_design),
                "observed_selectivity": observed,
                "observed_median": observed_median,
                "null_median": null_median,
                "null_band_90pct": [null_lo, null_hi],
                "null_draws_over_1x_fraction": n_null_over_1x / len(null_draws),
                "note": (
                    "This null is specific to this run's target, paralog panel, "
                    "k, and gated-design population. It is NOT the number in "
                    "docs/contrastive.md and must not be substituted for it, or "
                    "vice versa."
                ),
            },
            fh,
            indent=2,
            default=str,
        )
    print("\nwrote paralog_conditioned_report.json")


if __name__ == "__main__":
    main()
