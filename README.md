# LIGANDAI Python SDK

Official Python SDK for the [LIGANDAI](https://ligandai.com) platform — peptide
design, structure prediction, scoring, and discovery.

> **v0.6.0** — `client.structures.get(gene)` now accepts `pdb_code=`,
> `isoform=`, `species=`, and `declared_gene_set=` kwargs so SDK callers
> can pin a specific PDB, an isoform (e.g. CLDN18.2), a non-human species
> (mouse / rat / cyno / rhesus / pig / dog / rabbit / zebrafish / chimp), or
> force a monomer-vs-multimer disambiguation. New: `list_isoforms(gene)`
> and `list_species(gene)` to enumerate. Backwards-compatible — no kwargs
> = unchanged human-default fast path. Also: tier resolution is now
> `max(key_prefix_tier, account_tier)` so an upgraded enterprise account
> with a pro-prefixed key gets enterprise privs (no more post-upgrade
> 401s). Backend resolver parallelized: high-PDB-count genes (KRAS,
> EGFR, TP53) resolve in ~2 s instead of the prior 90 s timeout, with
> a 6 h in-process LRU cache for repeats. See [CHANGELOG.md](CHANGELOG.md)
> for details.

> **v0.5.6** — SDK hardening: `gpu="b200_plus"` is the only accepted GPU type
> (2x/4x/8x B200 / H100 / A100 raise `LigandAIInvalidConfig` before any
> network call); identical `fold` / `fold_batch` calls within 24h return the
> cached `Job` handle from a local sqlite at `~/.ligandai/submitted.db`;
> client-side credit pre-flight raises `LigandAIInsufficientCredits` if your
> balance can't cover the batch. See
> [Concurrency & Quota](#concurrency--quota) and
> [Credit Tracking](#credit-tracking) below, or
> [CHANGELOG.md](CHANGELOG.md).

> **v0.5.0** — `peptides.list(program_id)` now works (no more `TypeError`),
> plus new `peptides.search(...)`, `structures.list(program_id)`, and
> `structures.get_pdb(id)`. See
> [`docs/api_reference.md`](docs/api_reference.md),
> [`docs/workflows.md`](docs/workflows.md), and
> [`docs/error_codes.md`](docs/error_codes.md), or [CHANGELOG.md](CHANGELOG.md).

## Concurrency & Quota

The SDK enforces tier limits **client-side** before submitting GPU work. Your
tier (inferred from the API-key prefix) caps the number of in-flight GPU jobs:

| Tier        | Concurrent GPU jobs |
|-------------|---------------------|
| free        | 1                   |
| basic       | 4                   |
| academia    | 16                  |
| pro         | 25                  |
| enterprise  | 50                  |
| superadmin  | 50                  |

When you exceed your cap, the SDK raises `LigandAIConcurrencyLimit`
immediately — no network round-trip, no wasted GPU minutes. Inspect
in-flight submissions via:

```python
for row in client.submitted_set.list_in_flight(client.api_key_hash):
    print(row["submission_hash"][:12], row["kind"], row["job_id"])
```

### GPU type policy

The SDK targets a single GPU class: **B200+** (`"b200_plus"`). Multi-GPU
folds (2x/4x/8x B200) are **not exposed** through the public SDK. Passing
`gpu="b200_2x"`, `gpu="b200_4x"`, `gpu="b200_8x"`, or any non-`b200_plus`
value (including bare `b200`, `h100`, `a100`, etc.) raises
`LigandAIInvalidConfig` before the network call.

### Local dedupe

Identical `client.fold(...)` / `client.fold_batch(...)` calls within a
24-hour window return the cached `Job` handle instead of re-submitting —
saving credits and Modal GPU slots. Submission identity is
`sha256(peptide_set + receptor_seq + gpu + params)` so re-ordering a peptide
list does not produce a "different" submission. To force a fresh submission,
pass `force_resubmit=True`:

```python
# First call: POSTs and records in ~/.ligandai/submitted.db.
job = client.fold_batch(["ACDE", "PQRS"], target_gene="EGFR", diffusion_samples=4)

# Second identical call: returns the cached Job, no HTTP.
same_job = client.fold_batch(["PQRS", "ACDE"], target_gene="EGFR", diffusion_samples=4)
assert same_job.batch_id == job.batch_id

# Force a real re-submission (e.g. after a server-side cache invalidation).
new_job = client.fold_batch(["ACDE", "PQRS"], target_gene="EGFR",
                            diffusion_samples=4, force_resubmit=True)
```

The dedupe DB lives at `~/.ligandai/submitted.db` (mode 0600). Failed
submissions are eligible for retry — only `submitted` and `completed` rows
within the window block re-submit.

## Credit Tracking

Every `fold` / `fold_batch` call pre-checks your credit balance against the
estimated cost; if you can't afford the batch, the SDK raises
`LigandAIInsufficientCredits` BEFORE any HTTP work. Catch it like:

```python
from ligandai import LigandAI
from ligandai.errors import LigandAIInsufficientCredits

client = LigandAI()
try:
    job = client.fold_batch(
        peptides=my_500_peps,
        target_gene="EGFR",
        diffusion_samples=4,
        sampling_steps=100,
    )
except LigandAIInsufficientCredits as e:
    print(f"Need {e.required:,} cr; have {e.available:,} (shortfall {e.shortfall:,}).")
    print("Top up at https://ligandai.com/account/billing.")
```

Superadmin / unlimited accounts (`is_unlimited=True`) skip the pre-flight
and rely on the server's authoritative gate.

The SDK also keeps a local credit-consumption ledger at
`~/.ligandai/credit_ledger.db` for offline audit:

```python
events = client.credit_ledger.recent_events(client.api_key_hash, limit=20)
for e in events:
    print(e["ts"], e["kind"], e["job_id"], e["estimated"], "→", e["actual"])
```

Both local databases are mode 0600 under a 0700 parent directory.

> **License & Terms** — By installing or using this SDK you agree to the
> [LigandAI Terms of Service](https://ligandai.com/terms) and
> [End User License Agreement](https://ligandai.com/eula). API usage is logged
> for billing and abuse prevention. Submitted sequences and job artifacts may
> be retained under those terms. See `LICENSE` for the full agreement.

```bash
pip install ligandai
```

The SDK checks PyPI once per process when a client is created. If a newer valid
`ligandal/ligandai-python-sdk` release exists, it emits:

```bash
python -m pip install --upgrade ligandai
```

Set `LIGANDAI_SKIP_VERSION_CHECK=1` in hermetic CI if network checks are not
allowed.

```python
from ligandai import LigandAI, ResidueRange

client = LigandAI(api_key="lgai_pro_...")
print(f"Tier: {client.tier}, Credits: {client.credits}")

# Find tissue-specific surface markers
markers = client.discovery.tissue_markers(target_tissues=["Liver"], top_n=2000)

# Resolve a structure for the top marker
gene = markers.top[0].gene
structure = client.structures.get(gene)
analysis = client.structures.analyze(gene, analysis_depth="full")

# Generate peptides targeting the recommended pocket
pocket_ranges = []
if analysis.recommended_pocket:
    pocket_ranges = [analysis.recommended_pocket]

job = client.peptides.generate(
    gene=gene,
    num_peptides=50,
    target_residues=pocket_ranges,
    targeting_strategy="pocket_targeted",
    auto_fold=True,
    top_n_fold=25,
)

# Wait for completion (generation + auto-fold)
result = job.wait(timeout=1800)
print(f"Got {len(result.peptides)} peptides, top iPSAE: {result.peptides[0].ipsae}")
```

### Designing against a specific PDB ID + chain

For a multimer like PDB ``9MIR`` (chains A/B/C/D) where you only want to
design against chain C, fetch the structure by PDB ID and pass
``target_chains``:

```python
client = LigandAI()
struct = client.structures.from_pdb("9MIR")     # confirms the PDB resolves
job = client.peptides.generate(
    gene="9MIR",                                 # PDB ID accepted as identifier
    target_chains=["C"],                         # restrict design to chain C
    num_peptides=50,
    auto_fold=True,
    top_n_fold=10,
)
```

### Designing against a custom CIF/PDB on disk

```python
from pathlib import Path

client = LigandAI()
up = client.proteins.upload_pdb(
    file=Path("/path/to/relaxed.cif"),
    gene="MY_TARGET",
    custom_name="my_relaxed_2026_05_07",
)
job = client.peptides.generate(
    gene="MY_TARGET",
    variant_id=up.id,
    target_chains=["A"],          # optional chain restriction on the upload
    num_peptides=25,
    auto_fold=True,
)
```

### Pocket-targeted generation

Selected pockets can span one or more chains. The helper below compresses
arbitrary selected residues into the continuous chain ranges expected by the
generation API:

```python
from ligandai import LigandAI, ResidueRange

client = LigandAI(api_key="lgai_basic_...")

target_residues = [
    *ResidueRange.from_residues([34, 35, 36, 41, 42], chain="A", label="EC pocket 1"),
    *ResidueRange.from_residues([102, 103, 104], chain="B", label="interface pocket"),
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

## Authentication

```python
# 1. Pass explicitly
client = LigandAI(api_key="lgai_basic_...")

# 2. Read from env var (preferred for prod)
# $ export LIGANDAI_API_KEY=lgai_basic_...
client = LigandAI()

# 3. Custom base URL (dev / on-prem / enterprise)
client = LigandAI(api_key="...", base_url="http://localhost:5050")
```

Any authenticated LIGANDAI account, including free accounts, can create API
keys from account settings in the Developer/API Keys area. API keys identify
the account; the API still gates feature access by tier, credits/tokens, and
GPU limits.

API keys carry a **tier prefix**:

| Prefix | Tier | What it can do |
|---|---|---|
| `lgai_free_*` | free | quality-guided generation up to 10 peptides, 10 folds, 3 targets, 1 folding GPU |
| `lgai_basic_*` | basic | up to 100 peptides, paid folding allowance, 4 folding GPUs |
| `lgai_edu_*`  | academia | up to 300 peptides, academia guidance modules, 16 folding GPUs |
| `lgai_pro_*`  | pro | up to 300 peptides, transcriptomics analysis, bivalent, 25 folding GPUs |
| `lgai_ent_*`  | enterprise | everything + batch operations + priority queue |
| `lgai_sa_*`   | superadmin | all features (internal) |

The client detects the tier from the prefix at construction — no network call.

```python
client.tier                     # "pro"
client.credits                  # int
client.feature_allowed("...")   # bool
client.max_peptides_per_generation
client.max_folds_per_generation
client.max_targets_per_generation
client.max_concurrent_gpu_slots
client.rate_limit_per_minute
```

When a method requires a higher tier than the key carries, it raises
`LigandAITierError` **client-side**, before sending the request.

For agent-specific billing, token, API-key, and Claude Skill routing, see
[`docs/agents.md`](docs/agents.md).

## Resource Namespaces

| Namespace | Endpoints | What it does |
|---|---|---|
| `client.account`       | `/api/auth/user`, `/api/user-credits`, ... | profile, credits, tier limits |
| `client.receptors`     | `/api/receptordb/*` | search, browse, download PDBs |
| `client.structures`    | `/api/structure/*`, `/api/gene-resolver/*` | gene → PDB / AlphaFold |
| `client.proteins`      | `/api/protein-info/*`, `/api/protein-variants/*` | UniProt info, variants, custom PDBs |
| `client.discovery`     | `/api/transcriptomics/*`, `/api/scrna/*`, `/api/geo-import/*` | tissue markers, scRNA, GEO import |
| `client.diseases`      | `/api/disease-viewer/*` | disease search, mutations |
| `client.goals`         | `/api/autoresearch/*` | persistent goal-directed AutoResearch runs |
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

### Track credits for a local agent or notebook run

Pass a stable `client_session_id` to tag every API request from a Claude Code,
Codex, notebook, or pipeline run. The dashboard exposes the same run ID under
Account Billing -> API Activity.

```python
client = LigandAI(client_session_id="codex-il31-screen-20260505")

with client.session("codex-il31-screen-20260505") as run:
    job = client.peptides.generate(gene="IL31", num_peptides=25, auto_fold=True)
    result = job.wait()

print(run.credits_used)

usage = client.account.session_usage("codex-il31-screen-20260505")
print(usage.summary.total_calls, usage.summary.credits_used)
```

## Persistent Goal Runs (v0.3.2+)

Goal runs are Automatic Mode jobs: they can keep running on the server and
spending credits after your Python process exits until stopped or capped. The
SDK requires `automatic_mode=True` as an explicit acknowledgement. This server
capability is currently limited to internal pilot accounts.

```python
run = client.goals.start(
    "Generate and fold IL31 peptides until at least five candidates exceed the iPSAE threshold",
    automatic_mode=True,
    budget_cap_credits=5000,
    max_iterations=3,
    conversation_id="optional-conversation-id",
    program_id="optional-ptf-program-id",
)

status = client.goals.get(run.run_id)
print(status.status, status.credits_consumed, status.budget_cap_credits)
print(status.automatic_mode_acknowledged, status.automatic_mode_acknowledged_at)
print(status.satisfaction_status, status.acceptance_criteria, status.evaluation_history[-1:])

graph = client.goals.graph(run.run_id)
print(graph.progress.percent, graph.next_actions[:1], graph.blockers)
for item in graph.checklist:
    print(item.status, item.type, item.label)

for event in client.goals.stream(run.run_id):
    print(event.type, event.run_id)
    if event.type in {"completed", "failed"}:
        break

client.goals.pause(run.run_id)
client.goals.resume(run.run_id)
client.goals.stop(run.run_id)
```

## DeltaForge Scoring

```python
# Fold a target/binder pair, then score with DeltaForge auto mode.
job = client.peptides.score_complex(
    binder_sequence="ACDEFGHIK",
    target_sequence="MNPQRSTVWY",
    scorer="auto",  # auto | current | v10 | v10_2 | unified
)
score = job.wait().results
print(score.dg, score.kd_nm, score.predicted_binder_call, score.scorer_version)

# Score your own folded PDB directly, with multivalent per-chain decomposition.
# The fold_* values are optional Boltz-2 confidence metrics used for the
# separate binder/non-binder call. The affinity values still return separately.
score = client.peptides.score_pdb(
    pdb_file="complex.pdb",
    receptor_chains=["A", "C"],
    peptide_chain="B",
    scorer="auto",
    fold_ipsae=0.72,
    fold_iptm=0.84,
    fold_complex_plddt=91.2,
)
print(score.dg, score.kd_nm)                       # affinity readout
print(score.predicted_binder_call, score.predicted_non_binder_reasons)
for pair in score.pair_scores or []:
    print(pair.receptor_chain, pair.peptide_chain, pair.dg, pair.contacts)
```

## Folding Controls and Peptide Viewing (v0.3.3+)

Direct SDK folds default to one diffusion sample. Increase
`num_trajectories`, `sampling_steps`, `recycling_steps`, or `step_scale` only
when you need ensemble-validation depth. On eligible direct human receptor or
receptor-complex folds, set `contribute_to_receptordb=True` to request
ReceptorDB contribution and the documented discount.

```python
job = client.peptides.generate(
    gene="IL31",
    num_peptides=25,
    auto_fold=True,
    top_n_fold=5,
    num_trajectories=4,
    folding_mode="parallel",
    fold_strategy="top_ranked",
    sampling_steps=50,
)

fold_job = client.peptides.fold(
    ["ACDEFGHIK", "MNPQRSTVWY"],
    sampling_steps=1000,
    recycling_steps=5,
    num_trajectories=10,
    step_scale=1.2,
    contribute_to_receptordb=True,
)
```

## Batch fold against a fixed receptor (v0.5.5+)

`client.peptides.fold_batch(peptides, target_gene=...)` submits N peptides
against one fixed receptor in parallel — each peptide is folded as a 2-chain
complex (chain A = receptor, chain B = peptide). Pass exactly one of
`target_gene`, `receptor_pdb`, or `receptor_sequence`. Peptide input accepts
bare AA strings **or** FASTA blocks (multi-record supported).

Billing is **100 credits per fold per trajectory**, with a
`max(1.0, sampling_steps / 50)` multiplier — the full batch cost is charged
upfront. HTTP 402 if balance is short.

```python
# Gene-based receptor (canonical PDB or human-proteome lookup)
job = client.peptides.fold_batch(
    peptides=["ACDEFGHIK", "MNPQRSTVWY", "DPVQETICK"],
    target_gene="EGFR",
    diffusion_samples=4,
)
print(job.batch_id, job.total_cost_credits)
results = job.wait(on_progress=lambda s: print(s["done"], "/", s["total"]))
for fold in results:
    if fold is not None:
        print(fold.iptm, fold.ipsae)

# Custom PDB receptor
job = client.peptides.fold_batch(
    peptides=peptide_library,
    receptor_pdb="receptors/my_target.pdb",  # path OR raw PDB content
    receptor_name="MY_TARGET_v2",
    sampling_steps=100,                      # 2× billing multiplier
)

# FASTA input (server parses multi-record blocks)
with open("candidates.fasta") as fh:
    job = client.peptides.fold_batch(
        peptides=[fh.read()],
        target_gene="CD47",
    )
```

`BatchFoldJob` exposes `batch_id`, `jobs` (per-peptide submission metadata),
`total_cost_credits`, `peptide_count`, `trajectories_per_peptide`, `receptor`,
`sub_jobs`, `results`/`folds`, plus `wait(timeout=, poll_interval=, on_progress=)`
and `cancel()`. See `examples/23_fold_batch.py` for the full workflow.

```python
from ligandai import (
    align_candidates_to_receptor,
    load_peptide_results,
    rank_peptides,
    serve_dashboard,
    write_dashboard,
)

candidates = load_peptide_results(["fold_results.jsonl"])
ranked = rank_peptides(candidates, score="ipsae", limit=10)
aligned = align_candidates_to_receptor(ranked, "base_receptor.pdb", "aligned")
handle = write_dashboard(aligned, "peptide_dashboard")
serve_dashboard(handle, open_browser=True)
```

Terminal rendering can also launch ProteinView by Tristan Farmer / 001TMF,
MIT License: https://github.com/001TMF/ProteinView.

## Guidance Modules (v0.2.0+)

Quality-guided generation is available to all authenticated tiers, including
free. Academia, pro, and enterprise keys unlock immunogenicity guidance, serum
stability guidance, and logits-style advanced outputs:

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

# Charge / solubility filtering (server-tier gated)
# Keep only peptides with net charge < -1.0
job = client.peptides.generate(
    gene="EGFR",
    num_peptides=200,
    charge_mode="lt",
    charge_value=-1.0,
)

# Cyclic peptides — terminal Cys-Cys disulfide (primary Adaptyv synthesis route)
# Requires academia/pro/enterprise tier.
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
job = client.peptides.generate(gene="EGFR", num_peptides=10)
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
            client.peptides.generate(gene=g, num_peptides=10) for g in genes
        ])
        results = await asyncio.gather(*[j.wait() for j in jobs])
        return results

results = asyncio.run(design_for_genes(["EGFR", "HER2", "KIT"]))
```

## Errors

```python
from ligandai import (
    LigandAIError,             # base
    LigandAIAuthError,         # 401 — invalid/expired/revoked key
    LigandAIUpgradeRequired,   # 402 — caller's tier doesn't include the surface
    LigandAICreditError,       # 402 — insufficient credits for operation
    LigandAITierError,         # 403 — tier escalation needed
    LigandAIForbidden,         # 403 — pilot allowlist, EULA, ownership
    LigandAINotFoundError,     # 404
    LigandAIRateLimitError,    # 429 — rate limit
    LigandAIServerError,       # 5xx (auto-retried)
    LigandAIValidationError,   # 400/422
)

try:
    detail = client.peptides.get(12345)
except LigandAIUpgradeRequired as e:
    print(f"Upgrade: {e.current_tier} → {e.required_tier} ({e.upgrade_url})")
except LigandAICreditError as e:
    print(f"Need {e.required} credits, have {e.available}")
```

See [docs/error_codes.md](docs/error_codes.md) for the full table of
HTTP status codes, server `code` strings, and matching SDK exceptions.

## Retry & Rate Limiting

The SDK automatically retries on `429`, `5xx`, and transient network errors
with exponential backoff (configurable via `max_retries=`). It also respects
`Retry-After` and `X-RateLimit-Reset` headers.

Per-tier rate limits:

| Tier | req/min |
|---|---|
| free | 10 |
| basic | 20 |
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

Proprietary. By installing or using this SDK you agree to the
[LigandAI Terms of Service](https://ligandai.com/terms), the
[LigandAI End User License Agreement](https://ligandai.com/eula), and the
license terms in [LICENSE](LICENSE).
