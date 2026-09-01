# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""
34 — Reproduce an ENPP3 cyclic + linear binder campaign through the SDK.

[bd-LIGANDAI_ALPHA_V2-akqqj]

This is the "how do I re-run what you ran" script for a customer-facing
campaign against a 355-aa ENPP3 catalytic-domain construct: receptor + MSA,
pocket-conditioned generation of BOTH a cyclic (terminal-Cys disulfide) and a
linear arm, Boltz-2 folding at the production settings, the acceptance ladder,
DeltaForge v10.2 affinity, an off-target selectivity panel, and export.

    pip install --upgrade ligandai
    export LIGANDAI_API_KEY=...
    python examples/34_enpp3_cyclic_campaign.py

Every call below is signature-checked against the SDK source and against the
route that receives it. Where a setting does not survive that trip, it is
called out rather than shown working — a snippet that silently drops a fold
setting reproduces a DIFFERENT experiment and reports success.

⛔ WHAT THE SDK DOES NOT REACH, AND WHAT TO DO INSTEAD
------------------------------------------------------
  1. (RESOLVED — bd-LIGANDAI_ALPHA_V2-pybsy) EXPLICIT FOLD SEEDS now exist:
     `fold_batch(..., seed=N)`. `boltz predict --seed N` is a GLOBAL CLI option,
     so one invocation per seed — submit the same batch once per seed and treat
     each seed's median over its 4 diffusion samples as ONE observation. Never
     pool samples across seeds as independent replicates. The seed also rides
     the worker's retry path, so a retried fold is not silently unseeded while
     its receipt still names a seed.

  2. (RESOLVED — bd-LIGANDAI_ALPHA_V2-pybsy) COFACTOR ENTITIES now reach the
     BATCH surface through the grouped form:
         groups=[{"target": {"gene": "ENPP3"},
                  "partners": [{"sequence": pep, "type": "peptide"},
                               {"sequence": "ZN", "type": "ligand", "copies": 2}]}]
     `sequence` carries the CCD code for a ligand. The worker has materialised
     ligand/dna/rna entities since it was written; what was missing was the type
     in the SDK normaliser and the server's zod enum, so a cofactor was silently
     dropped — folding a zinc ion as a one-residue PROTEIN chain, a plausible
     structure for the wrong complex. Chain ids now advance by `copies`, so a
     binuclear site occupies two chains without colliding with the next partner.
     This matters here: removing the Zn drops ipSAE by ~0.38 on this target, so
     an apo fold is NOT the same measurement.

  3. AN EXPLICIT DISULFIDE BOND CONSTRAINT. The internal YAML carried
     `bond: [B,1,SG]-[B,L,SG]` and it is load-bearing (median SG-SG 1.79 A with
     it vs 17.35 A without, 224 poses). The platform emits that same bond
     AUTOMATICALLY: it detects intra-peptide Cys pairs at dispatch and pairs
     them sequentially, which for a `terminal_pair` design with exactly two Cys
     IS Cys1-CysL. So the constraint is applied. What has no SDK argument is
     OVERRIDING it — pinning a different (i, j), or running the no-bond arm.

  4. A CHOSEN CONTRASTIVE PANEL. The internal generation arms steered away from
     named paralog pockets supplied as feature matrices. `generate(contrastive=)`
     exists and REFUSES to send, because the public generation endpoint's
     forward payload drops the references — it would read as set and execute as
     unset. On a v6.9-line model the platform derives its OWN real paralog
     references and refuses to run without them, so the run is contrastive
     either way; you just do not choose the panel and are not told which
     paralogs were used.

  5. Generation `temperature` / `top_p` / `num_diffusion_steps`, and supplying
     your own MSA (`.a3m`) to a fold, have no SDK argument at all.
     `fold_shared_msa` accepts only 'auto' | 'none'.

  Also: on `peptides.fold()` the top-level `msa_enabled=` is inert (the
  receiving route never reads it). Control MSA PER CHAIN with `use_msa: False`
  on the de novo peptide. On `fold_batch()`, `msa_enabled=` is honoured.
"""

from __future__ import annotations

import json
import os
import warnings
from pathlib import Path

from ligandai import LigandAI, LigandAIDiagnosticWarning, ResidueRange

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

# ⛔ FRAME. The campaign folds a 355-aa catalytic-domain CONSTRUCT, not the
# 875-aa protein. Construct residue i corresponds to platform residue i + 162.
# Every residue number below is in the CONSTRUCT's own numbering, because that
# is the numbering of the coordinates being designed against. Mixing the two
# frames silently targets a different surface.
CONSTRUCT_PDB = os.environ.get("ENPP3_CONSTRUCT_PDB")   # path to your .pdb
RECEPTOR_SEQ = os.environ.get("ENPP3_RECEPTOR_SEQ", "")  # the 355-aa chain A

user_pdb_id = None
if CONSTRUCT_PDB:
    protein = client.proteins.upload_pdb(
        CONSTRUCT_PDB, gene="ENPP3", custom_name="ENPP3_6C02_355aa",
    )
    user_pdb_id = protein.id
    print(f"construct uploaded: user_pdb_id={user_pdb_id}")
    # ⛔ Pass this as `user_pdb_id=`, never as `variant_id=` — different id
    # space; a variant_id miss silently falls back to the canonical structure.

# The MSA the receptor is folded with. Cache hit <100 ms; a cold search ~60 s.
# Folding a receptor WITHOUT an MSA produces unusable interface scores. The de
# novo peptide chain is correctly folded MSA-free.
# ⛔ You can READ an MSA here; you cannot hand a specific .a3m back to a fold.
# The platform computes/looks up its own for the receptor chain.
if RECEPTOR_SEQ:
    msa = client.msa.generate(sequences={"A": RECEPTOR_SEQ}, gene="ENPP3")
    for cid, chain in msa.chains.items():
        print(f"  MSA chain {cid}: {chain.hits} homologs (cached={msa.cached})")

# ─────────────────────────────────────────────────────────────────────────────
# 2. GENERATION — pocket-conditioned, two arms
# ─────────────────────────────────────────────────────────────────────────────
# `target_residues` are PDB RESIDUE NUMBERS in the receptor being designed
# against — not indices, not sequence offsets. `from_residues` compresses a
# selection into contiguous chain-local ranges.
POCKET = [int(r) for r in os.environ.get("ENPP3_POCKET", "").split(",") if r.strip()]
target_residues = ResidueRange.from_residues(POCKET, chain="A") or None

# ── OFF-TARGET (paralog-contrastive) GUIDANCE ────────────────────────────────
# Selectivity is cheapest to buy at GENERATION time. `contrastive_references`
# builds real paralog-mean references for the named off-targets and the sampler
# steers away from them; the alternative is generating blind and discovering the
# cross-reactivity later in a 5-receptor fold panel, which costs ~960 folds.
#
# ⛔ These must be REAL references. The v6.9 line refuses a fabricated all-zero
# reference, which is the guard that stops a silently-unsteered run reporting
# itself as contrastive. Do not pass allow_unforwarded_contrastive=True to get
# around a reference that failed to build — that flag suppresses exactly the
# error you want to see.
OFF_TARGET_GENES = ["ENPP1", "ENPP6"]
contrastive_refs = client.campaigns.contrastive_references(
    target="ENPP3",
    paralogs=OFF_TARGET_GENES,
    # ⛔ v6.9.21, NOT v6.9.22. The point release is load-bearing: v6.9.21 carries
    # pocket_feature_dim=107, v6.9.22 carries 65, and v6.5.x carries 48. They are
    # different feature spaces, not truncations of one another (48 is NOT a prefix
    # of 107). Naming the wrong point release reproduces a different generator and
    # reports success. This campaign's 2,719 designs are v6.9.21 paralog-contrastive
    # with cfg_reference="paralog_mean" and real nonzero paralog references.
    model="v6.9.21",
)
print(f"contrastive references built for {OFF_TARGET_GENES}")

# ⛔ The references BUILD, but do not yet REACH the generator through the public
# endpoint: /api/ptf/parallel/generate forwards a fixed key list that omits
# reference_pocket_features, while the FastAPI request model and the Modal
# generation path both accept it. Passing `contrastive=` therefore raises
# LigandAIParameterNotForwarded rather than running unsteered and reporting
# success. Tracked as bd-LIGANDAI_ALPHA_V2-cjtxi; when it lands, add
#     contrastive=contrastive_refs,
# to `common` below. Until then a v6.9-line run is still paralog-contrastive on
# references the platform derives itself — you do not choose the panel.
# Do NOT reach for allow_unforwarded_contrastive=True to make the line above
# work: that flag suppresses the one error that tells you the panel was ignored.

common = dict(
    gene="ENPP3",
    user_pdb_id=user_pdb_id,
    target_residues=target_residues,
    pocket_expansion_radius_a=6.0,   # the packing shell, not just the named residues
    # v6.9 line = paralog-contrastive by construction. Passing the references
    # EXPLICITLY names which paralogs were steered against, so the run records
    # its own selectivity intent instead of relying on server-side defaults.
    # ⛔ v6.9.21, NOT v6.9.22. The point release is load-bearing: v6.9.21 carries
    # pocket_feature_dim=107, v6.9.22 carries 65, and v6.5.x carries 48. They are
    # different feature spaces, not truncations of one another (48 is NOT a prefix
    # of 107). Naming the wrong point release reproduces a different generator and
    # reports success. This campaign's 2,719 designs are v6.9.21 paralog-contrastive
    # with cfg_reference="paralog_mean" and real nonzero paralog references.
    model="v6.9.21",
    quality_guided=True,
    quality_guidance_scale=1.0,
    # ⛔ cellfree MUST be on for a terminal-Cys run. The cysteine policy rides
    # INSIDE the cell-free head's penalty loop: with cellfree off, a
    # cysteine_mode="terminal_pair" request produced 0/32 terminal-Cys designs;
    # with it on, 32/32. This is the single easiest way to think you ran a
    # cyclic campaign and not have.
    cellfree=True,
    cellfree_strength=2.0,
    immunogenicity=True,
    immuno_strength=1.5,
    serum_stability=True,
    stability_strength=2.0,
    stability_mode="resist",
    ec_domain_trimming=True,
    # Fold explicitly in step 3 so the fold settings are ours — `generate()`
    # has no `recycling_steps` argument.
    auto_fold=False,
)
if target_residues:
    common["targeting_strategy"] = "pocket_targeted"

# ── cyclic arm: a Cys at BOTH termini, scored as a disulfide bridge ──────────
# `cysteine_mode="terminal_pair"` hard-masks the terminal positions so the pair
# is PLACED, not hoped for (100% terminal-Cys pairs). The older
# `"disulfide_only"` is a PENALTY schedule that suppresses Cys to ~1% — it is
# not a cyclisation mode and will quietly give you a linear library.
# `strict_recombinant=True` forbids internal Cys, which is what a synthesis
# vendor needs for an unambiguous single bridge.
# ⛔ An unrecognised cysteine_mode is NOT rejected by the platform — it is
# silently rewritten to "disulfide_only". The SDK rejects unknown values
# client-side, which is the only place that mistake is visible.
cyclic_job = client.peptides.generate(
    num_peptides=300,
    length_range=(12, 24),
    cysteine_mode="terminal_pair",
    cyclic_mode="disulfide",
    cyclic_strength=2.0,
    strict_recombinant=True,
    **common,
)

# ── linear arm: same pocket, same guidance, no cyclisation ───────────────────
linear_job = client.peptides.generate(
    num_peptides=300,
    length_range=(12, 24),
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
# 3A. FOLD — batch, HOLO (both catalytic Zn), at named seeds
# ─────────────────────────────────────────────────────────────────────────────
# 4 diffusion samples, 3 recycling steps — the production pair.
# ⛔ The 4 samples are CONFIDENCE-RANKED, not exchangeable (mean ipSAE by sample
# index runs 0.62 / 0.51 / 0.37 / 0.22; model_0 is the max in 74% of stems).
# Summarise a fold by the MEDIAN of its 4 samples and treat that as ONE
# observation. The median is a fixed quantile, NOT a noise-averaged estimate —
# its spread is not sd/sqrt(4). Never pool samples across designs as replicates.
#
# ENPP3's site is binuclear zinc and the campaign folded HOLO throughout:
# removing the ions costs ~0.38 ipSAE, so an apo fold is a different experiment.
# The grouped form below carries the ions as ligand entities; `sequence` holds
# the CCD code and `copies` gives the second site its own chain.
FOLD = dict(
    diffusion_samples=4,
    recycling_steps=3,
    sampling_steps=50,
    use_potentials=True,
    n_parallel_gpus=16,
)

# ⛔ SEEDS. `boltz predict --seed N` is a GLOBAL CLI option, so one submission
# per seed — never a per-row field. The unit of analysis is (design, seed), and
# a design is only reproducible if it holds across independent seeds. The
# campaign's promotion rule: median over units >= 0.67 AND >= 2/3 of units
# clearing, across >= 2 seeds. A single fold ranks nothing.
SEEDS = [11223, 44556, 78901]

def _group(pep: str) -> dict:
    return {
        "target": (
            {"pdb": CONSTRUCT_PDB, "name": "ENPP3_6C02_355aa"}
            if CONSTRUCT_PDB else {"gene": "ENPP3"}
        ),
        "partners": [
            # chain B — the de novo binder. use_msa is forced False for a peptide:
            # MSA-ing an orphan de novo sequence is wasted work and the worker's
            # MSA_GUARD skips it anyway.
            {"sequence": pep, "type": "peptide"},
            # chains C+D — the two catalytic Zn(II). Declared AFTER both protein
            # chains: the interface scorer keys on the peptide chain id.
            {"sequence": "ZN", "type": "ligand", "copies": 2},
        ],
    }

fold_job_ids: list[str] = []
folds_by_seed = {}
for seed in SEEDS:
    batch = client.peptides.fold_batch(
        groups=[_group(p) for p in designs],
        seed=seed,
        **FOLD,
    )
    print(f"seed {seed}: batch {batch.batch_id}, {batch.total_cost_credits} credits")
    folds_by_seed[seed] = batch.wait(timeout=14400)
    fold_job_ids += [
        str(j.get("job_id") or j.get("jobId"))
        for j in batch.jobs
        if (j.get("job_id") or j.get("jobId"))
    ]
folds = folds_by_seed[SEEDS[0]]

# ─────────────────────────────────────────────────────────────────────────────
# 3B. FOLD — holo, with the 2x Zn cofactors as explicit ligand entities
# ─────────────────────────────────────────────────────────────────────────────
# A ligand entity carries its CCD code in `sequence` (ZN, MG, CA, MN, FE2, K,
# SO4, HEM, ...). Declare the ions LAST, after both protein chains — the
# peptide chain id is what the interface scorer keys on.
#
# ⛔ This is the SINGLE-COMPLEX form. Since bd-LIGANDAI_ALPHA_V2-pybsy the batch
#    grouped form also accepts entities (step 3A above uses it), so prefer the
#    batch surface for a sweep and keep this for a one-off or for options the
#    grouped form does not expose.
# ⛔ MSA is PER CHAIN here: the receptor gets one by default, and the de novo
#    peptide must carry `use_msa: False`. Top-level `msa_enabled=` is not read
#    on this route.
# ⛔ iPTM is ligand-inflated when ions are present. Read `protein_iptm` from the
#    confidence breakdown, not the headline `iptm`.
def fold_holo(peptide: str, receptor_seq: str):
    return client.peptides.fold(
        [
            {"sequence": receptor_seq, "chainId": "A", "geneName": "ENPP3"},
            {"sequence": peptide,      "chainId": "B", "use_msa": False},
            {"sequence": "ZN",         "chainId": "Y", "type": "ligand"},
            {"sequence": "ZN",         "chainId": "Z", "type": "ligand"},
        ],
        target_gene="ENPP3",
        diffusion_samples=4,
        recycling_steps=3,
        sampling_steps=50,
        use_potentials=True,
    )

holo = []
if RECEPTOR_SEQ and os.environ.get("ENPP3_HOLO_FOLD"):
    for seq in designs[:24]:
        fr = fold_holo(seq, RECEPTOR_SEQ).wait(timeout=7200)
        holo.append(fr)
        print(f"  ligands in fold: {[l.ccd for l in (fr.ligands or [])]} "
              f"peptide_ipsae={fr.peptide_ipsae}")

# ─────────────────────────────────────────────────────────────────────────────
# 4. SCORE + ACCEPTANCE TIERS
# ─────────────────────────────────────────────────────────────────────────────
# ⛔ peptide_ipsae — the peptide<->receptor interface score — is THE binder
# metric. Whole-complex `ipsae` is inflated by the receptor's confidence in
# itself. `peptide_ipsae=None` means NOT MEASURED, never zero: check
# `peptide_ipsae_status` and do not fall back to `ipsae`.
for fr in folds:
    if fr is None:
        continue
    print(f"  iptm={fr.iptm} peptide_ipsae={fr.peptide_ipsae} "
          f"({fr.peptide_ipsae_status}) plddt={(fr.metrics or {}).get('mean_plddt')}")

# The acceptance ladder is evaluated by ONE shared server-side evaluator
# (shared/quality-tiers.ts). Do not re-derive it client-side. The traps:
#   great_plus            peptide_ipsae >= 0.66
#   elite                 peptide_ipsae >= 0.85
#   super_elite           ipsae >= 0.67 AND iptm >= 0.80 AND plddt >= 88
#   super_elite_affinity  the above plus predicted Kd < 100 nM
# ⛔ super_elite is NOT a stricter elite — its iPSAE leg is LOWER. pLDDT is on
# the 0-100 scale (a 0.88 threshold is a silent no-op), and Kd is NANOMOLAR.
gate = client.evaluator.filter_folds(
    fold_job_ids,
    criteria={"min_peptide_ipsae": 0.66, "min_plddt": 70.0, "min_contacts": 5},
)
print(f"gate: {gate.get('passing')}/{gate.get('total')} pass, {gate.get('failing')} fail")
elite_ids = [r["fold_job_id"] for r in (gate.get("results") or []) if r.get("passes")]

# Or ask for a named tier. `quality_tier` on each row reports the STRONGEST
# criterion the design actually clears, and keeps 'indeterminate' distinct from
# 'fail' — "we did not measure this" is not "this design is bad".
shortlist_rows = client.peptides.search(
    gene="ENPP3",
    session_id=cyclic.session_id,
    acceptance_criterion="great_plus",
    sort="ipsae", order="desc", limit=100,
)
print(f"great_plus (cyclic arm): {len(shortlist_rows)}")

# ─────────────────────────────────────────────────────────────────────────────
# 5. DELTAFORGE v10.2 AFFINITY
# ─────────────────────────────────────────────────────────────────────────────
# scorer="v10_2" selects deltaforge_v10_2_unified_parallel. v9 and v10.2
# disagree substantially on the same poses (r(log Kd) ~ +0.16), so never mix
# numbers from the two heads in one table — state which scorer produced each.
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
# Binding ENPP3 is the easy half. NOT binding the family is the half that
# decides whether the molecule is useful, and a single-target run cannot see it.
#
# ⛔ REPLICATES ARE NOT OPTIONAL. Production folds are unseeded, so a single
# draw ranks partly on that draw's noise. The replicate spread IS the resolvable
# band; a delta below it is INDISTINGUISHABLE, not "slightly prefers".
shortlist = [p.sequence for p in shortlist_rows[:24]]

# (a) Named-gene comparators -> the panel helper, which does the delta / band /
#     hold-rate arithmetic for you.
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
(OUT / "selectivity_panel.json").write_text(
    json.dumps({"band": band, "rows": panel.to_rows(), "summary": summary},
               indent=2, default=str)
)

# ⛔ SURFACE THE SELECTIVITY DIAGNOSTICS. The platform emits five codes that say
# a leg was NOT MEASURED rather than measured-and-negative:
#   selectivity_on_target_unmeasured        the on-target fold is unusable
#   selectivity_panel_member_absent         a comparator has no fold result
#   selectivity_metric_missing              a gate metric was never computed
#   selectivity_frame_mismatch              legs folded in different construct frames
#   selectivity_metric_not_length_comparable  ipSAE is chain-length normalised, so a
#                                             cross-receptor delta on the raw value
#                                             is not a like-for-like comparison
# An unmeasured panel is NOT a clean bill of health. Collect them explicitly:
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always", LigandAIDiagnosticWarning)
    panel_again = client.campaigns.fold_panel(
        shortlist, target="ENPP3", off_targets=["ENPP1", "ENPP6"],
        replicates=2, diffusion_samples=4, recycling_steps=3, wait=True,
    )
for w in caught:
    if issubclass(w.category, LigandAIDiagnosticWarning):
        print(f"  DIAGNOSTIC: {w.message}")

# (b) Comparators that are NOT gene symbols — an unnamed family member, an
#     isoform pulled from a provided structure, or your own construct. The
#     panel helper resolves every leg BY GENE and cannot express these.
#     `fold_compare` can: off_targets accepts a raw amino-acid sequence, and
#     on_target_sequence pins the on-target to your construct.
OFFTARGET_SEQS = json.loads(os.environ.get("ENPP3_OFFTARGET_SEQS", "{}"))
for seq in shortlist[:5]:
    report = client.peptides.fold_compare(
        binder=seq,
        on_target="ENPP3",
        on_target_sequence=RECEPTOR_SEQ or None,
        off_targets=[*OFFTARGET_SEQS.values(), "ENPP6"],
        engine="boltz2",
        binder_id=seq[:12],
    )
    (OUT / f"foldcompare_{seq[:12]}.json").write_text(
        json.dumps(report, indent=2, default=str)
    )

# The whole-session shortcut (top-N elite by peptide_ipsae, then fold_compare
# on each) is:
#     client.peptides.validate_selectivity(
#         session_id=cyclic.session_id, on_target="ENPP3",
#         off_targets=["ENPP1", "ENPP6"], top_n=10)

# ─────────────────────────────────────────────────────────────────────────────
# 7. RETRIEVE + EXPORT
# ─────────────────────────────────────────────────────────────────────────────
# Whole-run bundle: peptides.csv + folds/*.pdb + summary.json.
cyclic.save_to(OUT / "cyclic")
linear.save_to(OUT / "linear")

# Individual structures by fold ROW id (ptf_fold_results.id, from search()).
for p in shortlist_rows[:10]:
    if p.fold_id:
        client.peptides.download_pdb(p.fold_id, save_to=str(OUT / f"{p.fold_id}.pdb"))
        # PAE for the same row (academia/pro/enterprise). Returns an ndarray
        # when decode=True. ⛔ It takes ptf_fold_results.id, NEVER a job id.
        # pae = client.folds.download_pae(p.fold_id)

print(f"CSV export: {cyclic.csv_url}")
print(f"done -> {OUT.resolve()}")
