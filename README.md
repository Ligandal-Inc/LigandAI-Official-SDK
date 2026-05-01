# LIGANDAI Python SDK

Official Python SDK for the [LIGANDAI](https://ligandai.com) platform — peptide
design, structure prediction, scoring, and discovery.

> **License & Terms** — By installing or using this SDK you agree to the
> [LigandAI Terms of Service](https://ligandai.com/terms) and
> [End User License Agreement](https://ligandai.com/eula). API usage is logged
> for billing and abuse prevention. See `LICENSE` for the full agreement.

```bash
pip install ligandai
```

```python
from ligandai import LigandAI

client = LigandAI(api_key="lgai_pro_...")
print(f"Tier: {client.tier}, Credits: {client.credits}")

# Find tissue-specific surface markers
markers = client.discovery.tissue_markers(target_tissues=["Liver"], top_n=2000)

# Resolve a structure for the top marker
gene = markers.top[0].gene
structure = client.structures.get(gene)
analysis = client.structures.analyze(gene, analysis_depth="full")

# Generate peptides targeting the recommended pocket
job = client.peptides.generate(
    gene=gene,
    num_peptides=300,
    target_residues=[analysis.recommended_pocket] if analysis.recommended_pocket else None,
    targeting_strategy="pocket_targeted",
    auto_fold=True,
    top_n_fold=25,
)

# Wait for completion (generation + auto-fold)
result = job.wait(timeout=1800)
print(f"Got {len(result.peptides)} peptides, top iPSAE: {result.peptides[0].ipsae}")
```

## Authentication

```python
# 1. Pass explicitly
client = LigandAI(api_key="lgai_pro_...")

# 2. Read from env var (preferred for prod)
# $ export LIGANDAI_API_KEY=lgai_pro_...
client = LigandAI()

# 3. Custom base URL (dev / on-prem / enterprise)
client = LigandAI(api_key="...", base_url="http://localhost:5050")
```

API keys carry a **tier prefix**:

| Prefix | Tier | What it can do |
|---|---|---|
| `lgai_free_*` | free | search, view structures, get job status |
| `lgai_edu_*`  | academia | + generate, fold, score, glycosylation |
| `lgai_pro_*`  | pro | + bivalent, transport vasculome (no batch ops) |
| `lgai_ent_*`  | enterprise | everything + batch operations + priority queue |
| `lgai_sa_*`   | superadmin | all features (internal) |

The client detects the tier from the prefix at construction — no network call.

```python
client.tier                     # "pro"
client.credits                  # int
client.feature_allowed("...")   # bool
client.max_peptides_per_generation
client.rate_limit_per_minute
```

When a method requires a higher tier than the key carries, it raises
`LigandAITierError` **client-side**, before sending the request.

## Resource Namespaces

| Namespace | Endpoints | What it does |
|---|---|---|
| `client.account`       | `/api/auth/user`, `/api/user-credits`, ... | profile, credits, tier limits |
| `client.receptors`     | `/api/receptordb/*` | search, browse, download PDBs |
| `client.structures`    | `/api/structure/*`, `/api/gene-resolver/*` | gene → PDB / AlphaFold |
| `client.proteins`      | `/api/protein-info/*`, `/api/protein-variants/*` | UniProt info, variants, custom PDBs |
| `client.discovery`     | `/api/transcriptomics/*`, `/api/scrna/*`, `/api/geo-import/*` | tissue markers, scRNA, GEO import |
| `client.diseases`      | `/api/disease-viewer/*` | disease search, mutations |
| `client.peptides`      | `/api/ptf/parallel/*`, `/api/folding/*`, `/api/binder-scoring/*`, `/api/v1/deltaforge/score-pdb` | generate, fold, score |
| `client.bivalent`      | `/api/ligandforge/bivalent/*` | bispecific design (pro+) |
| `client.synthesis`     | `/api/synthesis-checkout/*`, `/api/adaptyv/*` | quote, cart, order |
| `client.memory`        | `/api/episodic-memory/*` | memory search & save |
| `client.programs`      | `/api/ptf/programs/*`, `/api/ptf/sessions/*` | programs, projects, sessions |
| `client.charts`        | `/api/charts/*` | matplotlib chart generation |
| `client.reports`       | `/api/reports/*` | PDF report generation |
| `client.jobs`          | `/api/jobs/*` | list, cancel, stream |

## Billing & Account Management (v0.3.0+)

```python
# Check balance and runway
bal = client.account.get_balance()
print(f"{bal.credits} credits, {bal.days_remaining:.1f} days runway")

# Auto top-up when low
if bal.credits < 10000:
    client.account.top_up(amount_usd=200)

# Estimate cost before running a big job
est = client.peptides.estimate_cost(num_peptides=1000, auto_fold=True, fold_top_n=100)
print(f"This run will cost ~{est.credits} credits (${est.cost_usd:.2f})")

# Configure automatic top-ups
client.account.configure_auto_topup(
    enabled=True,
    threshold_credits=5000,
    amount_usd=200,
)

# Inspect recent transactions
txns = client.account.billing_usage(period="30d")
for t in txns[:5]:
    print(f"[{t.type}] {t.amount:+d} credits — {t.description}")
```

## DeltaForge V10 Scoring

```python
# Fold a target/binder pair, then score with DeltaForge auto mode.
job = client.peptides.score_complex(
    binder_sequence="ACDEFGHIK",
    target_sequence="MNPQRSTVWY",
    scorer="auto",  # auto | current | v10
)
score = job.wait().results
print(score.dg, score.kd_nm, score.scorer_version)

# Score your own PDB directly, with multivalent per-chain decomposition.
score = client.peptides.score_pdb(
    pdb_file="complex.pdb",
    receptor_chains=["A", "C"],
    peptide_chain="B",
    scorer="v10",
)
for pair in score.pair_scores or []:
    print(pair.receptor_chain, pair.peptide_chain, pair.dg, pair.contacts)
```

## Guidance Modules (v0.2.0+)

Pro+ tier keys unlock guidance modules that steer LigandForge during generation:

```python
# Immunogenicity guidance — reduce MHC-I/II epitopes and improve humanness
job = client.peptides.generate(
    gene="EGFR",
    num_peptides=200,
    immunogenicity=True,
    immuno_strength=2.5,
    immuno_modules={"mhc_i": True, "mhc_ii": True, "humanness": True},
)

# Serum stability — resist trypsin/DPP-IV/chymotrypsin cleavage
job = client.peptides.generate(
    gene="EGFR",
    num_peptides=200,
    serum_stability=True,
    stability_strength=2.0,
    stability_mode="resist",
    stability_modules={"trypsin": True, "chymotrypsin": True, "dppiv": True},
)

# Extended plasma half-life
job = client.peptides.generate(
    gene="EGFR",
    num_peptides=200,
    halflife="extended",
    halflife_strength=2.5,
)

# Charge / solubility filtering (pro+ tier)
# Keep only peptides with net charge < -1.0
job = client.peptides.generate(
    gene="EGFR",
    num_peptides=200,
    charge_mode="lt",
    charge_value=-1.0,
)

# Cyclic peptides — terminal Cys-Cys disulfide (primary Adaptyv synthesis route)
# Requires academia / pro / enterprise / discovery_partner tier.
job = client.peptides.generate(
    gene="EGFR",
    num_peptides=100,
    length_range=(12, 22),      # cyclic-friendly length range
    cyclic_mode="disulfide",
    strict_recombinant=True,    # forbid internal Cys (required for Adaptyv path)
)

result = job.wait(timeout=1800)
for p in result.peptides:
    if p.stability_scores:
        print(f"{p.sequence}: grade={p.stability_scores.stability_grade}, "
              f"halflife={p.stability_scores.predicted_halflife_hours:.1f}h")
    if p.immuno_scores:
        print(f"  immuno_grade={p.immuno_scores.immuno_grade}, "
              f"pop_coverage={p.immuno_scores.population_coverage_pct:.0f}%")
    if p.cyclic_mode and p.cyclic_mode != "none":
        print(f"  cyclic={p.cyclic_mode}")
```

## Long-Running Jobs

Generation, folding, and scoring submit GPU work and return a `Job`:

```python
job = client.peptides.generate(gene="EGFR", num_peptides=300)
job.id              # str
job.status          # "queued" | "running" | "complete" | "failed"
job.progress        # 0-100 or None
job.estimated_credits

# Block until done
result = job.wait(timeout=1800, poll_interval=2.0)

# Or stream live progress events (SSE)
for event in job.stream():
    print(f"{event.stage}: {event.message} ({event.progress})")

# Cancel
job.cancel()
```

Async equivalents:

```python
import asyncio
from ligandai import AsyncLigandAI

async def design_for_genes(genes):
    async with AsyncLigandAI() as client:
        jobs = await asyncio.gather(*[
            client.peptides.generate(gene=g, num_peptides=300) for g in genes
        ])
        results = await asyncio.gather(*[j.wait() for j in jobs])
        return results

results = asyncio.run(design_for_genes(["EGFR", "HER2", "KIT"]))
```

## Errors

```python
from ligandai import (
    LigandAIError,           # base
    LigandAIAuthError,       # 401 — invalid/expired/revoked key
    LigandAITierError,       # 403 — feature requires higher tier
    LigandAIRateLimitError,  # 429 — rate limit
    LigandAICreditError,     # 402 — insufficient credits
    LigandAINotFoundError,   # 404
    LigandAIServerError,     # 5xx (auto-retried)
    LigandAIValidationError, # 400/422
)

try:
    job = client.peptides.generate(gene="EGFR", num_peptides=10000)
except LigandAITierError as e:
    print(f"Need {e.required_tier}, you have {e.current_tier}")
except LigandAICreditError as e:
    print(f"Need {e.required} credits, have {e.available}")
```

## Retry & Rate Limiting

The SDK automatically retries on `429`, `5xx`, and transient network errors
with exponential backoff (configurable via `max_retries=`). It also respects
`Retry-After` and `X-RateLimit-Reset` headers.

Per-tier rate limits:

| Tier | req/min |
|---|---|
| free | 10 |
| academia | 30 |
| pro | 60 |
| enterprise | 300 |

## ReceptorDB-restricted Client

For receptordb.com users, a thinner client exposes only browse / search /
download (no API key required for read endpoints):

```python
from ligandai import ReceptorDBClient

client = ReceptorDBClient()
hits = client.search("EGFR")
client.download_pdb(hits[0].complex_id, "egfr.pdb")

# With API key — fold/generate
client = ReceptorDBClient(api_key="lgai_basic_...")
job = client.fold(sequences=["MAEEPQSD..."], target_gene="EGFR")
```

## Typed Models

All request/response shapes are pydantic models. IDE autocompletion works
out of the box, including for nested fields:

```python
from ligandai import BivalentTarget, LinkerConfig

session = client.bivalent.start(
    target1=BivalentTarget(gene="PDCD1", chain="A"),
    target2=BivalentTarget(gene="CD274", chain="A"),
    linker=LinkerConfig(position="C", length_min=8, length_max=20),
    binder_length_min=15,
    binder_length_max=40,
    num_designs=200,
)
print(session.id, session.status)
```

## Examples

See `examples/` for complete worked demos:

- `examples/01_quickstart.py` — auth, tier check, simple search
- `examples/02_end_to_end.py` — discovery → structure → generate → fold → score → cart
- `examples/03_bivalent.py` — PD-1 / PD-L1 bispecific design
- `examples/04_async_parallel.py` — design for many genes concurrently
- `examples/05_custom_variant.py` — fold a mutation, save as variant, regenerate
- `examples/06_streaming.py` — live SSE progress

## Development

```bash
git clone https://github.com/ligandal/ligandai-python-sdk
cd ligandai-python-sdk
pip install -e ".[dev]"
pytest
mypy ligandai/
ruff check ligandai/
```

## License

MIT — see [LICENSE](LICENSE).
