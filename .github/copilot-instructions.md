# GitHub Copilot Instructions — LigandAI Python SDK

> Loaded automatically by **GitHub Copilot Chat** (and **Copilot Workspace / Codex IDE**) when present at the repository root. For repos that consume `pip install ligandai`, copy this file (or symlink) into your own `.github/` so Copilot knows how to call the platform correctly.

This file complements the top-level [`AGENTS.md`](../AGENTS.md). AGENTS.md is the canonical, longer-form spec that **OpenAI Codex agent** and **Claude Code** read directly. This file is the Copilot-flavored quick-reference: short, code-block heavy, optimized for inline suggestions.

## What you (Copilot) are calling

- Package: **`ligandai`** (`pip install ligandai`, current version 0.5.1+)
- Platform: **`https://ligandai.com/api/*`**
- Auth: API key in env (`LIGANDAI_API_KEY`) or `LigandAI(api_key=...)`
- Tier-gated. Always read `client.tier` first; respect the cap table below.

## Tier capability table

| Tier | Max peptides / job | Fold GPU cap | Sequences | Synthesis API | Advanced guidance |
|---|---:|---:|---|---|---|
| free | 10 | 1 | masked | upgrade required | quality only |
| basic | 100 | 4 | full (CC required) | yes | quality only |
| academia | 300 | 16 | full | yes | + immuno + stability + cyclic |
| pro | 300 | 25 | full | yes | all |
| enterprise | 1000 | 50 | full | yes | all + batch + priority |

Check `client.account.me()` and `client.account.credits()` before suggesting expensive operations.

## The 4 most-suggested workflows

### 1. Generate against a gene name
```python
from ligandai import LigandAI
client = LigandAI()
job = client.peptides.generate(gene="EGFR", num_peptides=50, auto_fold=True, top_n_fold=5)
result = job.wait(timeout=1800)
for p in result.peptides[:5]:
    print(p.sequence, p.binding_energy, p.ipsae)
```

### 2. Generate against a specific PDB chain (multimer-aware)
```python
struct = client.structures.from_pdb("9MIR")
job = client.peptides.generate(
    gene="9MIR",
    target_chains=["C"],          # design AND fold against chain C only
    num_peptides=50,
    fold_gpus=16,                  # honors tier cap; SDK clamps if exceeded
    auto_fold=True, top_n_fold=10,
)
result = job.wait(timeout=1800, save_to="./9mir_chainC")
```

### 3. Hotspot-driven design (auto-pocket cascade)
```python
# Specify 1-3 hotspot residues — server auto-expands to surrounding pocket
# (8 Å radius), featurizes ONLY the pocket, conditions LigandForge on it.
job = client.peptides.generate(
    gene="BMPR1A",
    target_chains=["A"],
    hotspot_residues=[60, 62],       # PDB numbering by default
    numbering="pdb",                 # or "boltz" / "uniprot"
    num_peptides=100,
    auto_fold=True, top_n_fold=10,
)
result = job.wait(timeout=1800)

# After folds complete, partition by which peptides actually hit the hotspot
partition = client.folds.partition_by_hotspot(
    session_id=result.session_id,
    hotspots=[{"chain": "A", "residue": 60, "numbering": "pdb"},
              {"chain": "A", "residue": 62, "numbering": "pdb"}],
    distance_threshold_a=5.0,
)
print(partition.passes_hotspot, partition.wrong_interface)
```

### 4. Custom PDB upload → generate against uploaded structure
```python
upload = client.proteins.upload_pdb(
    file="my_target.pdb",
    gene="MYTARGET",
    custom_name="patient_variant_v3",
)
job = client.peptides.generate(
    gene="MYTARGET",
    variant_id=upload.variant_id,    # routes generation to uploaded PDB
    target_chains=["A"],
    num_peptides=200,
)
```

## Error handling Copilot should auto-suggest

```python
from ligandai import LigandAI
from ligandai.errors import (
    LigandAIAuthError,           # 401 — bad/missing key
    LigandAITierError,           # 403 — tier insufficient
    LigandAIUpgradeRequired,     # 402 — paid tier required
    LigandAIForbidden,           # 403 — feature gated for this tier
    LigandAIRateLimitError,      # 429 — slow down
    LigandAIInvalidRequest,      # 400 — bad params
)

try:
    job = client.peptides.generate(gene="EGFR", num_peptides=500)
except LigandAITierError as e:
    print(f"Tier {e.tier} cannot use this — current cap is {e.cap}, upgrade at {e.upgrade_url}")
except LigandAIUpgradeRequired as e:
    print(f"Paid endpoint — upgrade at {e.upgrade_url}")
```

## Resource map

```
client.account     → me, credits, usage
client.receptors   → search, get, candidates, analyze, list
client.structures  → from_pdb, get_pdb, list, partition_for_hotspots
client.proteins    → upload_pdb, list_variants
client.peptides    → generate, list, search, get, by_gene, fill_until
client.folds       → partition_by_hotspot, expand_hotspot
client.programs    → list, create, get
client.discovery   → tissues, generate (transcriptomics — academia+)
client.diseases    → search, categories
client.synthesis   → options, estimate, recommend (Adaptyv BLI — basic+)
client.charts      → visualization helpers (paid)
client.goals       → planning helpers
client.memory      → search, list, save (per-user)
client.msa         → cache helpers
client.reports     → exports
client.bivalent    → bivalent / multivalent design (pro+)
```

## Things Copilot should NOT suggest

- Synthesizing peptide sequences locally (no — use `client.peptides.generate`)
- Hardcoded API keys in source files (always env var)
- `time.sleep()` polling — use `job.wait(timeout=...)` (built-in async-friendly poll)
- More than `tier_cap` peptides — server clamps and returns 400; check `client.tier` first
- Real generation/folding when writing tests — use `LigandAIMock` from `ligandai.testing` if available, or skip with `@pytest.mark.modal`
- Mock data for end-user pipelines — generations are cheap; just run the real call

## Where to read more

- `examples/01_quickstart.py` … `examples/22_goals_planning.py` — runnable scripts, one per major capability
- `docs/api_reference.md` — endpoint catalog with required tier
- `docs/workflows.md` — guided multi-step recipes
- `docs/error_codes.md` — every error class + recovery path
- `AGENTS.md` (top-level) — long-form Codex/Claude-Code spec
- Platform docs: <https://ligandai.com/docs>
