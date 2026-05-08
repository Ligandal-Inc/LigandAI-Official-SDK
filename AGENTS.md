# AGENTS.md — LigandAI Python SDK

> **For LLM coding assistants** (Claude Code, Codex, Cursor, Aider, etc.) using
> the `ligandai` package. This file is loaded automatically by Claude Code and
> Codex when present at the package root. Read this **before** writing any
> SDK code so you don't waste tokens grepping for method signatures.

## What this SDK does

`pip install ligandai` gives Python access to the LigandAI platform: peptide
binder generation (LigandForge), structure prediction (Boltz-2 via the
folding API), DeltaForge V10 scoring, ReceptorDB, programs/projects, and
synthesis ordering (Adaptyv BLI). Every call hits `https://ligandai.com/api/*`
under a tier-gated API key.

## Authentication (1 step)

```python
from ligandai import LigandAI

# Reads LIGANDAI_API_KEY env var by default
client = LigandAI()

# Or pass explicitly
client = LigandAI(api_key="lgai_pro_...")

print(client.tier, client.credits)
```

If the user gets `401 LigandAIAuthError` or "missing API key":

1. Tell them to log in at <https://ligandai.com>
2. Open <https://ligandai.com/account/billing?tab=api-keys>
3. Click "Create API key"
4. Set `export LIGANDAI_API_KEY=lgai_..._...` in their shell

API keys are available at **all tiers including free**. Free keys have prefix
`lgai_free_`, paid keys have `lgai_basic_`, `lgai_edu_`, `lgai_pro_`,
`lgai_ent_`.

## Tier caps you must respect

| Tier | Max peptides per job | Folding GPU cap | Advanced guidance |
|---|---:|---:|---|
| free | 10 | 1 | quality only |
| basic | 100 | 4 | quality only |
| academia | 300 | 16 | quality + immuno + stability + cyclic |
| pro | 300 | 25 | all |
| pro_commercial | 300 | 25 | all |
| enterprise | 1000 | 50 | all + batch + priority |

If a user asks for `num_peptides=500` on academia, **don't auto-clamp** —
report the cap and ask them to upgrade or reduce. The server returns 403
`LigandAITierError` with `required_tier` and `current_tier` fields.

### Use the user's full GPU allocation

`peptides.generate(fold_gpus=1)` is the SDK default but is **almost always wrong**
for paid tiers — academia gets 16, pro 25, enterprise 50. **Always pass**
`fold_gpus=` matching the user's tier so jobs finish in minutes instead of
30+. Read `client.max_folds_per_generation` or `client.tier` to pick.

```python
caps = {"free": 1, "basic": 4, "academia": 16, "pro": 25, "pro_commercial": 25, "enterprise": 50}
job = client.peptides.generate(
    gene="EGFR",
    num_peptides=50,
    fold_gpus=caps.get(client.tier, 1),   # use the full tier allocation
    auto_fold=True,
    top_n_fold=20,
)
```

### Default fold ranking (LigandIQ × iPTM)

The SDK defaults `fold_strategy="quality_ranked"` — the server pre-ranks all
generated peptides by composite (LigandIQ × predicted iPTM) and folds the top
candidates first. This means credits go to the most promising designs by
default. To override:

- ``fold_strategy="distributed"`` — round-robin across targets
- ``fold_strategy="consolidated"`` — sequential single-target
- ``fold_strategy=None`` — defer to server default

## ⛔ The 4 Workflows You Will Be Asked For

### Workflow 1 — Generate against a known gene (simplest)

```python
from ligandai import LigandAI

client = LigandAI()
job = client.peptides.generate(
    gene="EGFR",
    num_peptides=50,
    auto_fold=True,
    top_n_fold=10,
)
result = job.wait(timeout=1800)
for p in result.peptides[:5]:
    print(p.sequence, p.binding_energy, p.ipsae)
```

### Workflow 2 — Generate against a specific PDB ID and one chain

> User says: *"design against PDB 9MIR, chain C only"*

```python
from ligandai import LigandAI

client = LigandAI()

# 1) Resolve the PDB by ID (no gene name guessing — the server fetches the PDB
#    directly when given a PDB ID).
struct = client.structures.from_pdb("9MIR")
print(struct.gene, struct.source, [c.id for c in (struct.chains or [])])

# 2) Restrict generation to the chain the user picked.
#    target_chains is a multimer-aware filter that applies to BOTH stages:
#       - generation: peptides are designed against ONLY the listed chain(s)
#       - folding: only conformations matching the listed chain(s) are folded
#    so the user gets a single-chain receptor + peptide complex, not a
#    full multimer co-fold.
job = client.peptides.generate(
    gene="9MIR",                # PDB ID is accepted as the target identifier
    target_chains=["C"],        # design AND fold against chain C only
    num_peptides=50,
    fold_gpus=16,               # academia cap — fold runs in minutes
    auto_fold=True,
    top_n_fold=10,
)
result = job.wait(timeout=1800, save_to="./9mir_chainC")
```

#### "single-chain target" vs "fold against multimer" vs "conformer expansion"

When the user says "just chain C" the platform supports three different
intents — be explicit about which:

| Intent | What to set | What you get |
|---|---|---|
| Design *and* fold against ONE chain only (default of `target_chains=`) | `target_chains=["C"]` | Peptide + chain C, no other chains in the fold |
| Design against one chain but co-fold the full multimer | `target_chains=["C"], folding_conformations="all"` | Peptide + chains A/B/C/D in the fold |
| Design against one chain, also fold against alternate conformations of the same protein | `target_chains=["C"], auto_conformation_expansion=True` | Peptide co-folded against multiple receptor conformations of the chain (apo, bound, etc.) |
| Co-fold against the homo/hetero-dimer interface | omit `target_chains` (or pass all) | Peptide + the entire input complex |

If the user is unclear, **ask**: "do you want the peptide folded only with
chain C of the receptor, or with the full complex it sits in?"

### Workflow 3 — Generate against a custom CIF/PDB the user has on disk

> User says: *"I have an AlphaFold/relaxed CIF on my laptop, use that."*

```python
from pathlib import Path
from ligandai import LigandAI

client = LigandAI()

# 1) Upload the file. Server parses chains, registers a variant, returns the
#    variant_id you'll use in step 2.
up = client.proteins.upload_pdb(
    file=Path("/path/to/my_relaxed.cif"),
    gene="MY_TARGET",                   # symbolic name; can be anything
    custom_name="my_relaxed_2026_05_07",
)
print("variant_id:", up.id)

# 2) Generate against that uploaded structure. The server pulls the receptor
#    geometry and chain layout from the variant.
job = client.peptides.generate(
    gene="MY_TARGET",
    variant_id=up.id,
    target_chains=["A"],         # optional — restrict to one chain of the upload
    num_peptides=25,
    auto_fold=True,
    top_n_fold=5,
)
result = job.wait(timeout=1800)
```

`upload_pdb` accepts `.pdb` or `.cif` — the server detects the format from the
file extension. Any chains not listed in `target_chains` are kept as
binding-context but not designed against.

### Workflow 4 — Pocket-targeted generation (residue-level)

```python
from ligandai import LigandAI, ResidueRange

client = LigandAI()

# Compress arbitrary residue IDs into continuous ranges per chain.
target_residues = [
    *ResidueRange.from_residues([34, 35, 36, 41, 42], chain="A", label="EC pocket"),
    *ResidueRange.from_residues([102, 103, 104], chain="B", label="interface"),
]

job = client.peptides.generate(
    gene="EGFR",
    num_peptides=25,
    target_residues=target_residues,
    targeting_strategy="pocket_targeted",
    quality_guided=True,
    auto_fold=True,
)
```

## Resource Map (all `client.*` namespaces)

```python
client.account       # tier, credits, billing, top-ups, session_usage
client.structures    # gene → PDB, .from_pdb(pdb_id), .from_alphafold(uniprot_id)
client.proteins      # info, variants, .upload_pdb(file, gene, custom_name=)
client.discovery     # tissue markers, scRNA, GEO import
client.diseases      # disease search, mutations
client.goals         # autoresearch / persistent goal runs (pilot)
client.peptides      # generate, fold, score_complex, score_pdb, search
client.bivalent      # bispecific design (pro+)
client.synthesis     # quote / cart / Adaptyv BLI orders
client.programs      # programs, projects, sessions
client.memory        # episodic memory
client.charts        # matplotlib chart generation
client.reports       # PDF reports
client.jobs          # list, cancel, stream
```

## Error handling

All calls raise typed errors — handle them explicitly:

```python
from ligandai import (
    LigandAIError,
    LigandAIAuthError,        # 401 — bad/missing API key
    LigandAITierError,        # 403 — needs higher tier (read .required_tier)
    LigandAICreditError,      # 402 — out of credits (read .required, .available)
    LigandAIRateLimitError,   # 429 — back off
    LigandAIValidationError,  # 400/422 — bad request shape
    LigandAINotFoundError,    # 404
    LigandAIServerError,      # 5xx (auto-retried)
)

try:
    job = client.peptides.generate(gene="EGFR", num_peptides=10000)
except LigandAITierError as e:
    print(f"Need {e.required_tier}, you have {e.current_tier}")
except LigandAICreditError as e:
    print(f"Need {e.required} cr, have {e.available} — buy more at "
          f"https://ligandai.com/account/billing")
```

## Job lifecycle

`peptides.generate()`, `peptides.fold()`, `peptides.score_complex()`, and
`bivalent.start()` all return a `Job`. Don't poll yourself — use `.wait()`
or `.stream()`:

```python
job = client.peptides.generate(gene="EGFR", num_peptides=10)
job.id, job.status, job.progress

# Block until done
result = job.wait(timeout=1800, poll_interval=2.0)

# OR stream live SSE events
for ev in job.stream():
    print(ev.stage, ev.message, ev.progress)
    if ev.stage == "complete":
        break

# Cancel
job.cancel()
```

## Cost preview before submitting

Every generate/fold call charges credits. **Always** estimate before running
big jobs the user might not be expecting:

```python
est = client.peptides.estimate_cost(num_peptides=1000, auto_fold=True, fold_top_n=100)
print(f"Cost: ~{est.credits} credits (${est.cost_usd:.2f})")

bal = client.account.get_balance()
if bal.credits < est.credits:
    print(f"Insufficient — buy at https://ligandai.com/pricing/usage")
```

## Session attribution (so usage is traceable)

When running from a Claude Code / Codex / notebook session, tag every request
so the user can find your run on the billing dashboard:

```python
client = LigandAI(client_session_id="codex-egfr-screen-2026-05-07")

with client.session("codex-egfr-screen-2026-05-07") as run:
    job = client.peptides.generate(gene="EGFR", num_peptides=25, auto_fold=True)
    result = job.wait()

print(run.credits_used)
```

## Common pitfalls (don't do these)

| ❌ Don't | ✅ Do |
|---|---|
| Pass a CIF file path to `peptides.generate(gene=...)` | `proteins.upload_pdb()` first, then pass `variant_id` |
| Pass a PDB ID as a `gene` string and hope it resolves | `client.structures.from_pdb("9MIR")` to confirm, then `target_chains=` to pick the chain |
| Loop calling `job.status` yourself | Use `job.wait()` or `job.stream()` |
| Pass `num_peptides > tier cap` and clamp client-side | Let the server reject and surface the tier error to the user |
| Hard-code `cysteine_mode="exclude"` for cyclic designs | Cyclic mode requires Cys for `disulfide` — leave as default and set `cyclic_mode="disulfide"` |
| `mock_data = ...` for testing | Use real data — there is no mock mode. If user wants dry-run, use `estimate_cost` |

## Platform URLs (for telling the user where to go)

- API key creation: <https://ligandai.com/account/billing?tab=api-keys>
- Buy credits: <https://ligandai.com/pricing/usage>
- Subscribe / upgrade tier: <https://ligandai.com/pricing>
- Billing & invoices: <https://ligandai.com/account/billing>
- SDK docs (web): <https://ligandai.com/sdk>
- Platform UI (workspace): <https://ligandai.com>

## Version

This SDK auto-checks PyPI once per process and warns on stale installs.
Set `LIGANDAI_SKIP_VERSION_CHECK=1` in hermetic CI.

```bash
pip install --upgrade ligandai
```

## Async equivalents

Every method has an async twin under `AsyncLigandAI` with the same signature:

```python
import asyncio
from ligandai import AsyncLigandAI

async def screen(genes):
    async with AsyncLigandAI() as client:
        jobs = await asyncio.gather(*[
            client.peptides.generate(gene=g, num_peptides=10) for g in genes
        ])
        return await asyncio.gather(*[j.wait() for j in jobs])

asyncio.run(screen(["EGFR", "HER2", "KIT"]))
```

## Where to read more

- `README.md` — full method catalog with examples
- `docs/agents.md` — billing / tier routing for agent runs
- `docs/quickstart.md` — first 10 minutes
- `docs/resources.md` — every namespace, every method
- `docs/jobs.md` — Job class, streaming, cancellation
- `examples/` — runnable scripts (01_quickstart through 06_streaming)

## Support

If something looks broken, check the response status + body before assuming
SDK bug. The platform changelog is public at <https://ligandai.com/changelog>.
For account / billing issues, the user can email <support@ligandai.com>.
