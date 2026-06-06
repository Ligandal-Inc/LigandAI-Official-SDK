---
name: ligandai
description: |
  Use this skill when the user asks LigandAI / LigandForge / Ligandal questions:
  generate peptide binders against a gene or PDB target, fold complexes with
  Boltz-2, score binding with DeltaForge, manage programs/projects/sessions,
  upload custom CIF/PDB structures, run bivalent/bispecific design, order
  Adaptyv BLI synthesis, query receptor/protein/discovery/diseases catalogs,
  manage memory & reports & charts, and run persistent AutoResearch goals.
trigger_phrases:
  - "design peptide", "design binder", "ligandai", "ligandforge", "ligandal"
  - "fold complex", "boltz", "boltz-2", "ipsae", "deltaforge"
  - "bispecific", "bivalent", "adaptyv", "spps", "bli synthesis"
  - "ligandai api key", "lgai_"
languages: ["python"]
sdk_version: ">=0.5.0"
---

# LigandAI Skill (Claude Code drop-in)

Drop this directory into `.claude/skills/` (or any directory Claude Code is
configured to load skills from) and Claude Code will pick up the `SKILL.md`
front-matter automatically. The skill teaches the agent how to:

1. Authenticate with `LIGANDAI_API_KEY` (and where to mint one).
2. Pick the right tier-aware defaults (peptide caps, fold-GPU caps).
3. Use every public namespace on `ligandai.LigandAI`.

The agent should `pip install ligandai>=0.5.0` once, then read this skill
before writing any LigandAI code.

## Authentication

```python
from ligandai import LigandAI

# Reads LIGANDAI_API_KEY env var
client = LigandAI()
print(client.tier, client.credits)
```

If `401 LigandAIAuthError` or "missing API key": tell the user to log in at
<https://ligandai.com>, open
<https://ligandai.com/account/billing?tab=api-keys>, click "Create API key",
and `export LIGANDAI_API_KEY=lgai_...`. Free keys (`lgai_free_*`) work
for read endpoints with masked output; basic+ unlock generation; pro+ unlock
bivalent / transcriptomics; enterprise unlocks batch + priority queue.

## Tier caps (server-enforced — do NOT clamp client-side)

| Tier | num_peptides cap | Fold GPU cap | Advanced guidance |
|---|---:|---:|---|
| free | 10 | 1 | quality only |
| basic | 100 | 4 | quality only |
| academia | 300 | 16 | quality + immuno + stability + cyclic |
| pro / pro_commercial | 300 | 25 | all + bivalent + transcriptomics |
| enterprise | 1000 | 50 | all + batch + priority queue |

Pass `fold_gpus=` matching the user's tier so jobs finish in minutes:

```python
caps = {"free": 1, "basic": 4, "academia": 16, "pro": 25, "pro_commercial": 25, "enterprise": 50}
job = client.peptides.generate(
    gene="EGFR", num_peptides=50, auto_fold=True,
    top_n_fold=20, fold_gpus=caps.get(client.tier, 1),
)
```

## Resource map (every public namespace)

| Namespace | Capability | Reference example |
|---|---|---|
| `client.account` | tier, credits, billing, top-up, usage, session_usage | `examples/09_account_quota_tier.py`, `examples/18_error_handling_tier_gating.py` |
| `client.bivalent` | bispecific design (mode1/mode2 + linker optimization, pro+) | `examples/03_bivalent.py`, `examples/03b_bivalent_mode1.py` |
| `client.charts` | server-rendered matplotlib charts | `examples/21_charts_visualization.py` |
| `client.discovery` | tissue/cell-type markers, GEO import, transport-vasculome | `examples/02_end_to_end.py` |
| `client.diseases` | disease search, mutations catalog | `examples/19_msa_memory_reports_diseases.py` |
| `client.folds` | hotspot partition + pocket expansion (Stream D) | `examples/14_folds_partition_expand_hotspot.py` |
| `client.goals` | persistent AutoResearch runs | `examples/22_goals_planning.py` |
| `client.jobs` | list, get, cancel, stream, stop_all | `examples/16_programs_sessions_jobs.py` |
| `client.memory` | save/list/search/delete/recent_activity | `examples/19_msa_memory_reports_diseases.py` |
| `client.msa` | MSA generation for receptor chains | `examples/19_msa_memory_reports_diseases.py` |
| `client.peptides` | generate / fold / cofold / score / search / list / by-gene / get_elite / fill_until / pocket_for_hotspots / search_by_pocket / estimate_cost / continue_folding / fold_custom_mutation / score_pdb / score_with_ligandiq / analyze_solubility | `examples/02_end_to_end.py`, `examples/04_async_parallel.py`, `examples/06_streaming.py`, `examples/11_generate_hotspot_cascade.py`, `examples/12_peptide_listing_search.py`, `examples/20_parallel_fold_control.py` |
| `client.programs` | list, create, get, update, archive, workstreams, sessions | `examples/08_program_list_and_structures.py`, `examples/16_programs_sessions_jobs.py` |
| `client.proteins` | info / disorder / topology / variants / upload_pdb / glycosylation / save_fold_as_variant | `examples/05_custom_variant.py`, `examples/15_proteins_upload_variants.py` |
| `client.receptors` | search, list, by_gene, chain_classification, download_pdb, request_fold, oligomeric_states, genes | `examples/01_quickstart.py`, `examples/10_receptors_search_resolve.py` |
| `client.reports` | PDF reports (generate + download) | `examples/19_msa_memory_reports_diseases.py` |
| `client.structures` | get / candidates / from_pdb / from_alphafold / resolve / list / get_pdb / analyze | `examples/07_pdb_id_chain_design.py`, `examples/13_structures_listing_pdb_pull.py` |
| `client.synthesis` | options / estimate / recommend / cart / orders / Adaptyv list/get/create/submit / linker_options / recommend_linker / binding_orientation / generation_mask_guidance / amide_quote | `examples/02_end_to_end.py`, `examples/17_synthesis_adaptyv.py` |

## The 4 workflows the user will ask for

See `generate.md`, `fold.md`, `synthesis.md`, `program.md` next to this file
for compact, ready-to-paste recipes.

## Error handling

```python
from ligandai.errors import (
    LigandAIError,
    LigandAIAuthError,        # 401 — key invalid/missing
    LigandAITierError,        # 403 — needs higher tier
    LigandAICreditError,      # 402 — out of credits / paid required
    LigandAIRateLimitError,   # 429
    LigandAIValidationError,  # 400/422
    LigandAINotFoundError,    # 404
    LigandAIServerError,      # 5xx (auto-retried)
    LigandAIPaidTierRequired, # paid-only endpoint hit by free key
)
```

## Multimer & cysteine policy

### Multimer design intent (heteromer vs homomer)

`client.structures.analyze(gene, analysis_depth='full')` returns
`vacancyIntelligence` with a `multimerType` field (`HOMODIMER`, `HOMOTRIMER`,
`HETERODIMER`, `MONOMER`, …) and chain classification (`receptorChainIds`,
`ligandChainIds`). Pick targeting based on **design intent**, not just on
the assembly type:

| Intent | Heteromultimer | Homomultimer |
|---|---|---|
| **Engage the physiological binding interface** (mimic or inhibit a natural partner) | Target the chain-chain interface — the natural partner-binding pocket. `vacancyPairings` lists the exposed contact residues when the ligand chain is removed. | Each chain has its own solvent-accessible binding face pointing OUTWARD. Pocket exists N times by symmetry. Mirror residue highlights across every chain ID; pass them all simultaneously to `highlight_residues`. |
| **Disrupt oligomerization** (break the assembly itself) | Less common — target where the assembly is held together rather than where the partner binds. | Target the inward-facing chain-chain interface (`homomerInterfacePairs` field). Only use when the explicit design goal is to disrupt the homomer. |

For **HOMOTRIMER** assemblies (e.g. TNFRSF19, TNF-family receptors): default
to engaging the external ligand-binding face on each chain. The
`vacancyIntelligence` response includes `homomerChainIds` and
`symmetricChainMirror` — list every chain ID when calling
`peptides.generate(target_chains=...)` so the binder is designed against the
symmetric pocket on all subunits, not just chain A. Andre 2026-05-18: a
TNFRSF19_homotrimer run that highlighted only chain A is a bug — pass all
three chains.

### Cysteine policy

`peptides.generate(cysteine_mode=...)` accepts:

- `disulfide_only` / `stability_only` — **0 or 2 Cys, signal-driven.** Penalty
  curve `{0: 0.0, 1: 5×, 2: 0.0, 3: 5×, ≥4: 8×}`, no logit bias toward Cys.
  The 2-Cys outcome only emerges when LigandForge's own intrachain logits
  favor it. Use for linear binders where a natural disulfide may form.
- `terminal_pair` — Cys forced at positions 0 and `seq_len-1`, 1e9 penalty
  elsewhere. End-capped only. Use for head-to-tail cyclic peptides.
- `terminal_only` — Cys allowed at either terminus only (interior masked).
- `exclude` — no Cys anywhere (free-thiol-sensitive payloads, etc.).
- `allow` — no constraint (debug / synthesis-side filtering only).

Default is `disulfide_only`. Never expect every peptide to carry 2 Cys — that
was the pre-2026-05-21 behavior and is now considered a regression. Most
binders return 0 Cys.

## Pitfalls

- Don't pass a CIF path to `peptides.generate(gene=...)` — call
  `proteins.upload_pdb()` first and pass `variant_id=`.
- Don't pass a PDB ID as a gene string — call `structures.from_pdb("9MIR")`
  to confirm chain layout, then pass `target_chains=` to pick the chain.
- Don't loop on `job.status` — use `job.wait()` or `job.stream()`.
- Don't clamp num_peptides client-side — let the server reject and surface
  the tier error.
- Free keys cannot reach `/api/v1/*` paid endpoints — surface the 402 with
  the upgrade URL the server returns.
- For homomultimer targets, don't restrict `target_chains=` to a single chain
  unless the user explicitly asks for asymmetric design — pass every receptor
  chain ID so the symmetric pocket is engaged on all subunits.
- Don't assume every `cysteine_mode='disulfide_only'` run will produce 2 Cys
  per peptide — the post-2026-05-21 policy lets the model choose 0 or 2.
