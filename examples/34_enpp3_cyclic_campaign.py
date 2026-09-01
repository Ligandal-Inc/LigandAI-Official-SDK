# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""
34 — Reproduce an ENPP3 cyclic + linear binder campaign entirely through the SDK.

[bd-LIGANDAI_ALPHA_V2-akqqj]

This is the "how do I re-run what you ran" script for a customer-facing
campaign: receptor + MSA, pocket-conditioned generation of BOTH a cyclic
(terminal-Cys disulfide) and a linear arm, Boltz-2 folding at the production
settings, the acceptance ladder, DeltaForge v10.2 affinity, an off-target
selectivity panel, and export.

    pip install --upgrade ligandai
    export LIGANDAI_API_KEY=...
    python examples/34_enpp3_cyclic_campaign.py

Every call below was checked against the SDK source and against the route that
receives it. Where a setting does not survive that trip, it is called out
rather than shown working — a snippet that silently drops a fold setting
reproduces a DIFFERENT experiment and reports success.

⛔ THE THREE THINGS TO KNOW BEFORE YOU TRUST A REPRODUCTION
-----------------------------------------------------------
  1. COFACTORS (the 2x Zn in the ENPP3 catalytic site) DO have an SDK path,
     but only on the single-complex `peptides.fold()` surface, and only as
     explicit entity dicts — `{"type": "ligand", "sequence": "ZN"}`, where
     `sequence` carries the CCD code. The BATCH surface
     (`peptides.fold_batch()`) is polymer-only: its request schema has no
     entity/ligand field at all and silently strips one. So a Zn-aware
     campaign is `fold()` in a loop, not `fold_batch()`. Step 3B below.

  2. THERE ARE NO EXPLICIT PER-FOLD SEEDS on the Boltz-2 path. `--seed` is
     never passed to the folding worker; `num_seeds` / `esmfold_seed` are
     ESMFold knobs. `diffusion_samples=4` gives four trajectories inside ONE
     call; it does not name seeds and cannot re-request a specific draw.
     Repeat measurements are independent but NOT reproducible — record the
     returned fold job ids, they are the only handle on a given draw.

  3. THE DISULFIDE BOND CONSTRAINT IS AUTOMATIC, NOT AN ARGUMENT. The platform
     detects intra-peptide Cys pairs at dispatch and emits the SG-SG
     `constraints[].bond` into the Boltz-2 input by default. A terminal-Cys
     cyclic design gets its bridge without you asking. What has no SDK
     argument is OVERRIDING that — pinning a specific (i, j) pair, or turning
     it off.

  Also: on `peptides.fold()` the top-level `msa_enabled=` is inert (the
  receiving route never reads it). Control MSA PER CHAIN instead, with
  `use_msa: False` on the de novo peptide entity. On `fold_batch()`,
  `msa_enabled=` is honoured.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ligandai import LigandAI, ResidueRange

OUT = Path(os.environ.get("ENPP3_OUT", "./enpp3_campaign"))
OUT.mkdir(parents=True, exist_ok=True)

client = LigandAI()  # reads LIGANDAI_API_KEY

# ─────────────────────────────────────────────────────────────────────────────
# 1. RECEPTOR + MSA
# ─────────────────────────────────────────────────────────────────────────────
# Resolve the exact crystal structure rather than letting the platform pick.
structure = client.structures.get("ENPP3", pdb_code="6C02")
print(f"receptor: {structure.gene} {structure.pdb_code} "
      f"source={structure.source} chains={structure.chain_count}")

# Designing against YOUR construct (e.g. the 355-aa ENPP3 fragment, Zn kept as
# HETATM): upload it and design against those coordinates. `user_pdb_id` is
# what makes the server load your file instead of the canonical structure for
# the gene. ⛔ Do NOT pass this id as `variant_id=` — different id space.
CONSTRUCT_PDB = os.environ.get("ENPP3_CONSTRUCT_PDB")  # path to your .pdb
user_pdb_id = None
if CONSTRUCT_PDB:
    protein = client.proteins.upload_pdb(
        CONSTRUCT_PDB, gene="ENPP3", custom_name="ENPP3_6C02_355aa",
    )
    user_pdb_id = protein.id
    print(f"construct uploaded: user_pdb_id={user_pdb_id}")

# The MSA the receptor is folded with. Cache hit <100 ms; a cold search ~60 s.
# Folding a receptor WITHOUT an MSA produces unusable interface scores. The de
# novo peptide chain is correctly folded MSA-free (see `use_msa` below).
RECEPTOR_SEQ = os.environ.get("ENPP3_RECEPTOR_SEQ", "")
if RECEPTOR_SEQ:
    msa = client.msa.generate(sequences={"A": RECEPTOR_SEQ}, gene="ENPP3")
    for cid, chain in msa.chains.items():
        print(f"  MSA chain {cid}: {chain.hits} homologs (cached={msa.cached})")

# ─────────────────────────────────────────────────────────────────────────────
# 2. GENERATION — pocket-conditioned, two arms
# ─────────────────────────────────────────────────────────────────────────────
# `target_residues` are PDB RESIDUE NUMBERS in the receptor being designed
# against — not indices, not sequence offsets. If your construct is renumbered
# 1..N, use the construct's numbering. `from_residues` compresses a selection
# into contiguous chain-local ranges.
POCKET = [int(r) for r in os.environ.get("ENPP3_POCKET", "").split(",") if r.strip()]
target_residues = ResidueRange.from_residues(POCKET, chain="A") or None

common = dict(
    gene="ENPP3",
    user_pdb_id=user_pdb_id,
    target_residues=target_residues,
    pocket_expansion_radius_a=6.0,   # include the packing shell, not just the named residues
    # Guidance heads. Each is a separately-measured kernel; the measured optima
    # behind these numbers are in examples/26_gated_generation.py.
    quality_guided=True,
    quality_guidance_scale=1.0,
    immunogenicity=True,
    immuno_strength=1.5,
    serum_stability=True,
    stability_strength=2.0,
    stability_mode="resist",
    cellfree=True,
    cellfree_strength=2.0,
    ec_domain_trimming=True,
    # Fold explicitly in step 3 so the fold settings are ours — `generate()`
    # has no `recycling_steps` argument.
    auto_fold=False,
)
if target_residues:
    common["targeting_strategy"] = "pocket_targeted"

# ── cyclic arm: a Cys at BOTH termini, scored as a disulfide bridge ──────────
# `cysteine_mode="terminal_pair"` hard-masks the terminal positions so the pair
# is PLACED, not hoped for. `cyclic_mode="disulfide"` makes the closure part of
# the objective. `strict_recombinant=True` forbids internal Cys, which is what
# a peptide-synthesis vendor needs for an unambiguous single bridge.
# ⛔ An unrecognised cysteine_mode is NOT rejected by the platform — it is
# silently rewritten to "disulfide_only", i.e. a typo runs a different
# experiment and reports success. The SDK rejects unknown values client-side.
cyclic_job = client.peptides.generate(
    num_peptides=300,
    length_range=(12, 22),
    cysteine_mode="terminal_pair",
    cyclic_mode="disulfide",
    cyclic_strength=2.0,
    strict_recombinant=True,
    **common,
)

# ── linear arm: same pocket, same guidance, no cyclisation ───────────────────
linear_job = client.peptides.generate(
    num_peptides=300,
    length_range=(20, 40),
    cysteine_mode="exclude_all",
    **common,
)

cyclic = cyclic_job.wait(timeout=7200)
linear = linear_job.wait(timeout=7200)
print(f"generated: cyclic={len(cyclic.peptides)} linear={len(linear.peptides)}")
print(f"  cyclic run: {cyclic.view_url}")
print(f"  linear run: {linear.view_url}")

designs = [p.sequence for p in (*cyclic.peptides, *linear.peptides)]

# ─────────────────────────────────────────────────────────────────────────────
# 3A. FOLD — batch, production Boltz-2 settings (NO cofactor entities)
# ─────────────────────────────────────────────────────────────────────────────
# 4 diffusion samples, 3 recycling steps. `use_potentials` is ON platform-wide;
# it is passed explicitly so the run does not move when the default changes.
#
# Receptor: `receptor_pdb=` with your construct keeps the fold in the SAME
# coordinate frame the designs were generated against. `target_gene="ENPP3"`
# instead folds against the platform's canonical structure — a different frame,
# whose residue numbers will not line up with your pocket selection.
FOLD = dict(
    diffusion_samples=4,
    recycling_steps=3,
    sampling_steps=50,
    msa_enabled=True,       # honoured on this (batch) surface
    use_potentials=True,
    n_parallel_gpus=16,
)
receptor_arg = (
    dict(receptor_pdb=CONSTRUCT_PDB, receptor_name="ENPP3_6C02_355aa")
    if CONSTRUCT_PDB else dict(target_gene="ENPP3")
)

batch = client.peptides.fold_batch(peptides=designs, **receptor_arg, **FOLD)
print(f"fold batch {batch.batch_id}: {batch.peptide_count} designs x "
      f"{batch.trajectories_per_peptide} trajectories = "
      f"{batch.total_cost_credits} credits")
folds = batch.wait(timeout=14400)

fold_job_ids = [
    str(j.get("job_id") or j.get("jobId"))
    for j in batch.jobs
    if (j.get("job_id") or j.get("jobId"))
]

# ─────────────────────────────────────────────────────────────────────────────
# 3B. FOLD — with the 2x Zn cofactors as explicit ligand entities
# ─────────────────────────────────────────────────────────────────────────────
# The single-complex surface takes an entity list. A ligand entity carries its
# CCD code in `sequence` (ZN, MG, CA, MN, FE2, SO4, HEM, ... ). Chain ids are
# assigned A, B, C, ... in order when you omit them.
#
# ⛔ ONE COMPLEX PER CALL. There is no batch form that accepts entities, so this
#    is a loop, and it is the reason the main run above is the batch surface.
# ⛔ MSA is per chain here: give the receptor MSA (default) and set
#    `use_msa: False` on the de novo peptide. The top-level `msa_enabled=` is
#    NOT read on this route.
def fold_with_cofactors(peptide: str, receptor_seq: str):
    return client.peptides.fold(
        [
            {"sequence": receptor_seq, "chainId": "A", "geneName": "ENPP3"},
            {"sequence": "ZN", "chainId": "B", "type": "ligand"},
            {"sequence": "ZN", "chainId": "C", "type": "ligand"},
            {"sequence": peptide, "chainId": "D", "use_msa": False},
        ],
        target_gene="ENPP3",
        diffusion_samples=4,
        recycling_steps=3,
        sampling_steps=50,
        use_potentials=True,
    )

holo_folds = []
if RECEPTOR_SEQ and os.environ.get("ENPP3_HOLO_FOLD"):
    for seq in designs[:24]:
        holo_folds.append(fold_with_cofactors(seq, RECEPTOR_SEQ).wait(timeout=7200))
    # Cofactors that travelled with the receptor come back annotated.
    for fr in holo_folds:
        print(f"  ligands in fold: {[l.ccd for l in (fr.ligands or [])]}")

# ─────────────────────────────────────────────────────────────────────────────
# 4. SCORE + ACCEPTANCE TIERS
# ─────────────────────────────────────────────────────────────────────────────
# ⛔ peptide_ipsae — the peptide<->receptor interface score — is THE binder
# metric. Whole-complex `ipsae` is inflated by the receptor's confidence in
# itself and admits false positives. `peptide_ipsae=None` means NOT MEASURED,
# never zero: check `peptide_ipsae_status` and do not fall back to `ipsae`.
for fr in folds:
    if fr is None:
        continue
    print(f"  iptm={fr.iptm} peptide_ipsae={fr.peptide_ipsae} "
          f"({fr.peptide_ipsae_status}) plddt={(fr.metrics or {}).get('mean_plddt')}")

# The acceptance ladder is evaluated by ONE shared server-side evaluator. Do
# not re-derive the thresholds client-side: pLDDT is on the 0-100 scale and
# predicted Kd is in NANOMOLAR, and both have been got wrong by hand before.
gate = client.evaluator.filter_folds(
    fold_job_ids,
    criteria={"min_peptide_ipsae": 0.60, "min_plddt": 70.0, "min_contacts": 5},
)
print(f"gate: {gate.get('passing')}/{gate.get('total')} pass, {gate.get('failing')} fail")
elite_ids = [r["fold_job_id"] for r in (gate.get("results") or []) if r.get("passes")]

# Or ask for a named tier. `quality_tier` on each row reports the STRONGEST
# criterion the design actually clears, and keeps 'indeterminate' distinct from
# 'fail' — "we did not measure this" is not "this design is bad".
super_elite = client.peptides.search(
    gene="ENPP3",
    session_id=cyclic.session_id,
    acceptance_criterion="super_elite",
    sort="ipsae", order="desc", limit=100,
)
print(f"super-elite (cyclic arm): {len(super_elite)}")

# ─────────────────────────────────────────────────────────────────────────────
# 5. DELTAFORGE v10.2 AFFINITY
# ─────────────────────────────────────────────────────────────────────────────
df = client.deltaforge.batch_score_fold(
    fold_job_ids,
    scorer="v10_2",
    aggregate_method="boltzmann_parallel",
    include_pae=True,
)
print(f"DeltaForge v10.2: scored={df.get('scored')} failed={df.get('failed')}")
(OUT / "deltaforge_v10_2.csv").write_text(
    client.deltaforge.batch_score_fold_csv(fold_job_ids, scorer="v10_2")
)

# ─────────────────────────────────────────────────────────────────────────────
# 6. OFF-TARGET SELECTIVITY PANEL
# ─────────────────────────────────────────────────────────────────────────────
# Binding ENPP3 is the easy half. NOT binding ENPP1 / ENPP6 is the half that
# decides whether the molecule is useful, and a single-target run cannot see it.
#
# `replicates` is not optional: production folds are UNSEEDED, so a single draw
# ranks partly on that draw's noise. The replicate spread IS the resolvable
# band, and without it a small delta is unreadable.
shortlist = [p.sequence for p in super_elite[:24]]
panel = client.campaigns.fold_panel(
    shortlist,
    target="ENPP3",
    off_targets=["ENPP1", "ENPP6"],
    replicates=2,
    diffusion_samples=4,
    recycling_steps=3,
    msa_enabled=True,
)
band = panel.cross_draw_band()                      # measured, never assumed
rows, partial, summary = panel.deltas(band=band, target_leg_min=0.66)
print(f"selectivity band={band:.3f}; resolvable={len(rows)}, partial={len(partial)}")
for d in rows[:10]:
    print(f"  {d}")
(OUT / "selectivity_panel.json").write_text(
    json.dumps({"band": band, "rows": panel.to_rows(), "summary": summary},
               indent=2, default=str)
)

# ⛔ `campaigns.fold_panel` folds each comparator BY GENE. If a comparator must
# be a specific construct or a raw sequence, drive `peptides.fold_batch` once
# per receptor with `receptor_sequence=` / `receptor_pdb=` and compare the legs
# yourself — the panel helper has no per-receptor construct argument.
#
# The single-binder equivalent, isoform-aware, is:
#     client.peptides.fold_compare(binder=seq, on_target="ENPP3",
#                                  isoforms="all", off_targets=["ENPP1", "ENPP6"])
#     client.peptides.validate_selectivity(session_id=cyclic.session_id,
#                                          on_target="ENPP3", top_n=10)

# ─────────────────────────────────────────────────────────────────────────────
# 7. RETRIEVE + EXPORT
# ─────────────────────────────────────────────────────────────────────────────
# Whole-run bundle: peptides.csv + folds/*.pdb + summary.json.
cyclic.save_to(OUT / "cyclic")
linear.save_to(OUT / "linear")

# Individual structures by fold ROW id (ptf_fold_results.id, from search()).
for p in super_elite[:10]:
    if p.fold_id:
        client.peptides.download_pdb(p.fold_id, save_to=str(OUT / f"{p.fold_id}.pdb"))
        # PAE for the same row (academia/pro/enterprise). Returns an ndarray
        # when decode=True. ⛔ It takes ptf_fold_results.id, NEVER a job id.
        # pae = client.folds.download_pae(p.fold_id)

print(f"CSV export: {cyclic.csv_url}")
print(f"done -> {OUT.resolve()}")
