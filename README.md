# LIGANDAI Python SDK

Official Python SDK for the [LIGANDAI](https://ligandai.com) platform — peptide
design, structure prediction, scoring, and discovery.

<!--
  ANTI-DRIFT CONTRACT — read before editing the banner below.

  This README is the PyPI long description. Whatever version it names is what
  https://pypi.org/project/ligandai/#description advertises to the world, and
  it only changes when a new release is published.

  RULES:
    1. Exactly ONE version banner lives here. Do NOT append a new banner above
       the existing one — replace it. (This file previously accumulated a stack
       of v0.6.0 / v0.5.6 / v0.5.0 banners; the top one went stale for five
       minor releases and shipped "v0.6.0" to PyPI while 0.11.0 was current.)
    2. The version named below MUST equal [project].version in pyproject.toml.
    3. Full release history belongs in CHANGELOG.md — the single source of
       truth for per-release detail. Never duplicate it here.

  ENFORCEMENT: node scripts/check-docs-drift.mjs (in the LIGANDAI_ALPHA_V2
  repo) parses the banner version out of this file and fails when it disagrees
  with pyproject.toml or with the version PyPI actually serves.
-->

> **v0.15.0** — binder maturation reaches customers. `client.maturation` drives the
> platform's server-side maturation loop: hand it sequences you already have and it
> folds them, gates on the peptide-interface ipSAE, scores the in-domain poses,
> mutates the interface, and re-folds the variants **and their parents in the same
> cycle**, so a variant-vs-parent delta means something. Every withheld number says
> which of six reasons withheld it — and only one of the six is a verdict about the
> design. ⛔ `client.campaigns` is a DIFFERENT capability that merely shares the
> word: a local panel-folding helper that never calls `/api/campaign/*`. Reaching
> for `client.campaigns.mature` now names the real surface instead of raising
> something that reads like a typo.
>
> Upgrading from 0.14.0 keeps everything it introduced: the declarative campaign
> runner with its stop metrics and hard credit cap, `client.ensemble` co-folding N
> peptides x M engines priced first, contrastive / avoidance design as a first-class
> argument, and round-to-round lineage on the recursive campaign.
>
> See [CHANGELOG.md](CHANGELOG.md) for the full history.

## Concurrency & Quota

The SDK enforces tier limits **client-side** before submitting GPU work. Your
tier (inferred from the API-key prefix) caps the number of in-flight GPU jobs:

| Tier | Concurrent GPU jobs |
|-------------|---------------------|
| free | 1 |
| basic | 4 |
| academia | 16 |
| pro | 25 |
| enterprise | 50 |
| superadmin | 50 |

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
saving credits and GPU slots. Submission identity is
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

**Behind a SOCKS proxy?** httpx needs the `socksio` backend to route through a
`socks5://` / `socks5h://` proxy (`HTTPS_PROXY` / `ALL_PROXY`); without it the
first request raises an `ImportError`. Install the extra:

```bash
pip install ligandai[socks]
```

Optional extras: `ligandai[socks]` (SOCKS proxy support),
`ligandai[viz]` (matplotlib charts).

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

**Resuming after a disconnect.** A generation run keeps going server-side even
if your process exits. Persist `job.id` and reattach later to resume polling:

```python
session_id = job.id          # save this somewhere durable

# ... later, in a fresh process / after a disconnect ...
result = client.peptides.reattach(session_id).wait()
```

`client.jobs.get(session_id)` returns a one-shot `JobInfo` status snapshot for
the same parallel-generation id; `reattach()` returns a *waitable* `Job`.

### Target discovery (transcriptomics)

Don't know which gene to design against yet? Use the built-in `client.discovery`
funnel — the platform already computes the specificity-index (SI) ranking over
GTEx and single-cell atlases server-side, so **you never hand-stitch GTEx +
CellGuide / CellxGene yourself.** Every ranking call returns a `MarkerResponse`;
read `.top` (a list of `TissueMarker` with `.gene`, `.si`, `.receptor`, `.rank`).

```python
client = LigandAI()

# 1) Resolve identifiers first — exact GTEx strings (don't guess the spelling).
client.discovery.tissues()         # ['Kidney - Cortex', 'Liver', ...]
client.discovery.organ_systems()   # ['Nervous', 'Digestive', ...]

# 2) SI-ranked surface receptors enriched in the tissue (GTEx bulk).
#    receptor_only=True is THE cell-surface filter; exclude_tissues demotes
#    broadly-shared, off-target-prone genes.
markers = client.discovery.tissue_markers(
    target_tissues=["Kidney - Cortex"],
    exclude_tissues=["Liver", "Lung"],
    receptor_only=True,
    top_n=200,
)
gene = markers.top[0].gene
print(markers.top[0].si, markers.top[0].receptor)   # specificity index, surface flag

# 3) Hand the winning gene straight into design.
job = client.peptides.generate(gene=gene, num_peptides=50,
                               auto_fold=True, top_n_fold=10)
result = job.wait(timeout=1800)
```

**Refine the ranking** when a whole-tissue cut is too coarse:

```python
# Single-cell resolution (Academia+):
client.discovery.cell_type_markers(
    scrna_tissue="kidney", target_cell_types=["proximal_tubule"], receptor_only=True,
)

# SHARED vs DIFFERENTIAL selectivity across groups (gtex / geneset / custom):
resp = client.discovery.compare_targets([
    {"name": "target", "type": "gtex", "tissue": "Kidney - Cortex"},
    {"name": "ref",    "type": "gtex", "tissue": "Liver"},
], receptor_only=True)
print(resp.differential_genes, resp.shared_genes)

# BBB transcytosis shuttles for CNS delivery (Enterprise):
client.discovery.transport_vasculome(modality="monovalent", specificity_weight=0.5)
```

**Custom transcriptomics quickstart** — upload your own counts, then run the
SAME SI ranking on them. Passing `custom_dataset_targets` routes
`tissue_markers` to the `analyze-fast` endpoint:

```python
# dataset_type: "bulk" | "scrna" | "scRNA-seq" | "microarray"
ds = client.discovery.upload_dataset("counts.h5ad", dataset_type="bulk")

markers = client.discovery.tissue_markers(
    # CustomDatasetTarget aliases (in ligandai.types): datasetId (required),
    # cellTypes, samples. The dict form below is equivalent.
    custom_dataset_targets=[{"datasetId": ds.id, "cellTypes": ["proximal_tubule"]}],
    receptor_only=True,
    top_n=200,
)
top_gene = markers.top[0].gene
```

`client.discovery.list_datasets()` finds earlier uploads;
`client.discovery.import_geo("GSE12345")` ingests a public GEO series instead.

### Designing against a specific PDB ID + chain

For a multimer like PDB ``9MIR`` (chains A/B/C/D) where you only want to
design against chain C, fetch the structure by PDB ID and pass
``target_chains``:

```python
client = LigandAI()
struct = client.structures.from_pdb("9MIR") # confirms the PDB resolves
job = client.peptides.generate(
    gene="9MIR", # PDB ID accepted as identifier
    target_chains=["C"], # restrict design to chain C
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
    target_chains=["A"], # optional chain restriction on the upload
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

#### `target_residues` / `avoid_residues` / `hotspot_residues` are PDB residue numbers

**Not 0-based array indices, and not sequence positions.** `start`/`end` (or
a single `residue`) are the residue numbers as numbered in the
uploaded/resolved structure — the same numbers you'd read off the PDB file or
a structure viewer. `chain` is part of a residue's identity: pass it whenever
your request could span more than one chain.

The SDK validates this **client-side, before any HTTP call** — passing `0`
(the classic "I meant array index 0" mistake) raises
`ligandai.LigandAIValidationError` immediately, rather than the server
silently no-oping the request:

```python
>>> client.peptides.generate(gene="EGFR", avoid_residues=[{"chain": "A", "start": 0, "end": 10}])
ligandai.errors.LigandAIValidationError: avoid_residues[0]: residue numbers are
PDB numbering and start at 1; got start=0. If you meant an array index, that is
not supported — pass the residue number shown in the structure.
```

`avoid_residues` removes a receptor surface from the residue set the generator
is featurized on. It is applied in every targeting mode (`target_residues` /
`hotspot_residues` are not required for it to apply), and by default the server
also removes the residues packing directly against each named one (within
`avoid_shell_angstrom`, default 8.0 Å Cα-Cα):

```python
from ligandai import LigandAI, ResidueRange

client = LigandAI(api_key="lgai_basic_...")

job = client.peptides.generate(
    gene="EGFR",
    num_peptides=25,
    hotspot_residues=[792, 855],  # bind here (flat PDB resnums)
    avoid_residues=[ResidueRange(chain="A", start=719, end=724)],  # keep this clear
    avoid_shell_angstrom=8.0,     # default; pass 0 to exclude ONLY the named residues
    auto_fold=True,
)
```

#### ⚠ Targeting does not place a peptide — measure placement on the fold

Both parameters act on which receptor rows enter the pocket featurization.
Neither has been shown to put a design on a chosen site, and they fail in
different ways:

- **`avoid_residues` did not change generation at all.** Running the same
  request with and against the exclusion produced statistically
  indistinguishable sequence distributions (two-sample test `p = 1.00`,
  Jensen-Shannon divergence `2.4e-5`). Downstream, designs that cleared the
  quality gates still contacted the excluded patch — at roughly twice the rate
  expected by chance. Treat it as a record of your intent on the request and in
  run provenance, not as a constraint on the output.
- **`hotspot_residues` does change which sequences are generated** — it selects
  the featurized rows, and the shift is real and reproducible (six receptor
  faces, `p ≈ 1e-4`, 64 designs per arm). It does **not** aim: contact at the
  named face was not enriched on either receptor tested, and a randomly
  scattered residue set moved the distribution as much as a genuine contiguous
  face did. Use it to vary a run, not to choose a binding site.

**The workflow that holds is fold, then measure, then filter.** Fold the
candidates (`auto_fold=True` or `client.peptides.fold_batch()`), then select on
measured interface contacts:

```python
kept = client.evaluator.filter_folds(
    fold_job_ids,
    criteria={
        "min_peptide_ipsae": 0.67,
        "required_interface_residues": [{"chain": "A", "residue": 792}],
    },
)
```

### A fold job submitted on ligandai.com is a BATCH — address the designs, not the job

A `ligandforge_folding` job (what the website's fold funnel submits) hands every
peptide of a run to **one** smartfold job. Measured on production 2026-09-23,
271 of 291 such jobs span more than one distinct sequence; the largest holds 200
designs. That job id therefore has **no single `peptide_ipsae`**, and the metric
surfaces refuse it rather than report one arbitrary design's number as the job's:

```python
r = client.evaluator.filter_folds([batch_job_id], criteria={})["results"][0]
r["metrics"]["peptide_ipsae_reason"]   # 'job_is_a_batch_of_designs'
r["metrics"]["peptide_ipsae"]          # None — NOT a failed gate, and not zero
r["metrics"]["fold_handle"]            # None — no single design to address
```

`client.folds.list_designs()` enumerates what that job actually produced, each
design with its own handle:

```python
batch = client.folds.list_designs(batch_job_id)
print(batch["design_count"], "designs;", "batch" if batch["is_batch"] else "single fold")

for d in batch["designs"]:
    print(d["fold_handle"],        # 'foldrow_72210' — pass this anywhere a fold_job_id goes
          d["peptide_ipsae"],      # the A-B peptide-receptor interface metric
          d["peptide_length"],     # travels beside every Kd
          d["kd_nm"], d["kd_status"])
```

A `fold_handle` is accepted by `client.evaluator.filter_folds`,
`client.deltaforge.batch_score_fold` and `client.folds.download_pae`, so one
design's metrics and structure are one call away:

```python
best = max(
    (d for d in batch["designs"] if d["peptide_ipsae"] is not None),
    key=lambda d: d["peptide_ipsae"],
)
detail = client.evaluator.filter_folds([best["fold_handle"]], criteria={})
pae    = client.folds.download_pae(best["fold_result_id"])
```

Reading the numbers (real output, AQP2 batch of 66 designs, 2026-09-23):

| field | meaning |
|---|---|
| `peptide_ipsae` | **The binder metric** — the A-B peptide-receptor interface iPSAE, bounded at 1.0. Overall `ipsae` is complex-level and is deliberately not returned. **`0.0` is a measurement** ("no interface"), never a gap. |
| `kd_status: "ok"` | A Kd is quoted: a persisted DeltaForge V10 row above the interface gate. 27 of the 66 designs. |
| `kd_status: "below_ipsae_gate"` | **A refusal to quote, not a missing or zero affinity.** DeltaForge scores an interface; below `kd_ipsae_gate` (0.67) there is none to score. 39 of the 66. `kd_nm` is `None` — do not read it as a weak binder. |
| `peptide_length` | Carried beside every Kd. The deployed scorer has **no measured-affinity evaluation**, so a Kd ordering is *not* a validated affinity ordering, and length is its most obvious confound. |
| `coord_degenerate: true` | The structure parsed with no geometry. Its scalars are misleading, not missing — such a complex reads to DeltaForge as a systematically excellent binder. Never rank one. |

Use `client.folds.iter_designs(job_id)` to page through a batch larger than one
response.


The `avoid_conformance_violation` / `avoid_conformance_clean` fold-stage
diagnostics report the same check for an exclusion, on the finished complex.

Targeting events remain observable on the request side:
`target_residues_applied` / `avoid_residues_applied` on `job.stream()` echo
what the server accepted, and a `targeting_warning` event (e.g. a near-empty
pocket after `avoid_residues` was applied) is surfaced both on `job.stream()`
and via `warnings.warn(..., ligandai.LigandAITargetingWarning)`, so it's
visible even if you only call `job.wait()`. These confirm the request was
received — they are not evidence that a design landed where you asked.

## Authentication

```python
# 1. Pass explicitly
client = LigandAI(api_key="lgai_basic_...")

# 2. Read from env var (preferred for prod)
# $ export LIGANDAI_API_KEY=lgai_basic_...
client = LigandAI()

# 3. Custom base URL (dev / on-prem / enterprise)
client = LigandAI(api_key="...", base_url="http://localhost:8000")
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
| `lgai_edu_*` | academia | up to 300 peptides, academia guidance modules, 16 folding GPUs |
| `lgai_pro_*` | pro | up to 300 peptides, transcriptomics analysis, bivalent, 25 folding GPUs |
| `lgai_ent_*` | enterprise | everything + batch operations + priority queue |
| `lgai_sa_*` | superadmin | all features (internal) |

The client detects the tier from the prefix at construction — no network call.

```python
client.tier # "pro"
client.credits # int
client.feature_allowed("...") # bool
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
| `client.account` | `/api/auth/user`, `/api/user-credits`, ... | profile, credits, tier limits |
| `client.receptors` | `/api/receptordb/*` | search, browse, download PDBs |
| `client.structures` | `/api/structure/*`, `/api/gene-resolver/*` | gene → PDB / AlphaFold |
| `client.proteins` | `/api/protein-info/*`, `/api/protein-variants/*` | UniProt info, variants, custom PDBs |
| `client.discovery` | `/api/transcriptomics/*`, `/api/scrna/*`, `/api/geo-import/*`, `/api/transport-vasculome/*` | target-discovery funnel: SI-ranked tissue/cell-type surface markers, compare_targets, BBB shuttles, custom-dataset/GEO upload (see "Target discovery") |
| `client.diseases` | `/api/disease-viewer/*` | disease search, mutations |
| `client.goals` | `/api/autoresearch/*` | persistent goal-directed AutoResearch runs |
| `client.peptides` | `/api/ptf/parallel/*`, `/api/folding/*`, `/api/v1/deltaforge/score-pdb` | generate, fold, score |
| `client.deltaforge` | `/api/v1/deltaforge/score-pdb`, `/api/v1/deltaforge/score-fold`, `/api/v1/deltaforge/batch-score-fold` | thermodynamic scoring (ΔG/Kd) |
| `client.ligands` | `/api/v1/score/ligand` | small-molecule Kd scoring (free-tier) |
| `client.bivalent` | `/api/ligandforge/bivalent/*` | bispecific design (pro+) |
| `client.synthesis` | `/api/synthesis-checkout/*`, `/api/adaptyv/*` | quote, cart, order |
| `client.memory` | `/api/episodic-memory/*` | memory search & save |
| `client.programs` | `/api/ptf/programs/*`, `/api/ptf/sessions/*` | programs, projects, sessions |
| `client.charts` | `/api/charts/*` | matplotlib chart generation |
| `client.reports` | `/api/reports/*` | PDF report generation |
| `client.jobs` | `/api/jobs/*` | list, cancel, stream |

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

DeltaForge predicts binding thermodynamics (ΔG / Kd) plus a binder/non-binder
call. Use the `client.deltaforge` namespace. Auth is `Authorization: Bearer
<api_key>` (handled by the client). The production scorer is selected
server-side with `scorer="auto"`; the exact model that ran is returned on
`score.scorer_version`.

**Canonical endpoints (these are the ONLY valid paths):**

| Method | HTTP endpoint |
|---|---|
| `client.deltaforge.score_pdb(...)` | `POST /api/v1/deltaforge/score-pdb` |
| `client.deltaforge.score_fold(fold_job_id, ...)` | `POST /api/v1/deltaforge/score-fold` |
| `client.deltaforge.batch_score_fold([...])` | `POST /api/v1/deltaforge/batch-score-fold` |

> There is **no** `/api/binder-scoring/deltaforge` endpoint — that path returns
> 404. Use the `/api/v1/deltaforge/*` paths above.

```python
# 1) Score your own folded PDB. fold_* are optional Boltz-2 confidence metrics
# used for the binder/non-binder call; the affinity (dg/kd_nm) returns
# independently. Credits are charged only on a successful score.
score = client.deltaforge.score_pdb(
    pdb_file="complex.pdb",
    receptor_chains=["A", "C"],
    peptide_chain="B",
    scorer="auto", # auto | current | v10 | v10_2 | unified
    fold_ipsae=0.72,
    fold_iptm=0.84,
    fold_complex_plddt=91.2,
    include_pae=False, # attach NxN PAE matrix when available
)
print(score.dg, score.kd_nm, score.scorer_version)
print(score.predicted_binder_call, score.predicted_non_binder_reasons)
for pair in score.pair_scores or []:
    print(pair.receptor_chain, pair.peptide_chain, pair.dg, pair.contacts)

# 2) Score a fold you ALREADY ran (no re-fold). Pass the fold job id; the stored
# PDB + fold metrics (iptm/ptm/ipsae/plddt) are pulled and forwarded for you.
score = client.deltaforge.score_fold(
    "fold_1780410230005_wzgke1p5f",
    include_pae=True, # NxN PAE on score.pae when resolvable
)
print(score.dg, score.kd_nm, score.iptm, score.ptm, score.ipsae, score.plddt_mean)
if score.pae is not None:
    print("PAE", len(score.pae), "x", len(score.pae[0]))
else:
    print("PAE status:", score.pae_status) # 'pending' | 'unavailable'

# 3) Batch-score many existing folds; per-binder dg/kd_nm + all fold metrics.
out = client.deltaforge.batch_score_fold(
    ["fold_a", "fold_b", "fold_c"],
    include_pae=False,
)
for r in out["results"]:
    print(r["foldJobId"], r["sequence"], r["delta_g"], r["kd_nm"], r["classification"])

# CSV export (PAE summarized as pae_shapeN / pae_status columns when requested):
csv_text = client.deltaforge.batch_score_fold_csv(["fold_a", "fold_b"], include_pae=True)
open("scores.csv", "w").write(csv_text)
```

For peptide binders the metric to use is `peptide_ipsae` — the peptide-receptor
interface iPSAE. The overall `ipsae` is complex-level confidence dominated by
receptor-self contacts and stays high even when the peptide is not engaging; it
is **not** a binder metric. `iptm`/`ptm` are whole-complex confidence dominated
by the (large) receptor and are similarly not reliable as binder-quality signals.
When `peptide_ipsae` is `None`, check `peptide_ipsae_status`: `'not_computed'`
means the fold was run via the batch path (which skips peptide iPSAE). The legacy
`client.peptides.score_pdb(...)` remains available and now also accepts `include_pae=`.

## Folding Controls and Peptide Viewing (v0.3.3+)

Direct SDK folds default to one diffusion sample (`num_trajectories`/
`diffusion_samples` parameter default is still `1` in this SDK version). As of
2026-08-11 the **platform's own tier default** rose to 4 trajectories for
academia/pro/pro_beta/pro_commercial/enterprise/discovery_partner tiers (free
and basic stay capped at 1) — the SDK parameter default did not change, so
callers on a paid tier who want to match the platform's UI/LigandForge default
should pass `num_trajectories=4` explicitly. Increase `num_trajectories`,
`sampling_steps`, `recycling_steps`, or `step_scale` only when you need
ensemble-validation depth. On eligible direct human receptor or
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
    [
        # Receptor: leave it unflagged so it KEEPS its MSA.
        {"chainId": "A", "sequence": receptor_seq, "geneName": "EGFR"},
        # ⛔ Flag the designed chain. An unflagged protein chain is folded as a
        # RECEPTOR, and a receptor is required to have an MSA — a de novo binder
        # has no homologs, so the fold can be refused outright. See
        # "Folding a designed binder" below.
        {"chainId": "B", "sequence": designed_binder, "is_peptide": True},
    ],
    target_gene="EGFR",
    sampling_steps=1000,
    recycling_steps=5,
    num_trajectories=10,
    step_scale=1.2,
    contribute_to_receptordb=True,
)
```

## Folding a designed binder

Every `protein` chain you submit is treated as a **receptor** unless you say
otherwise, and a receptor is entitled to a multiple-sequence alignment. A
LigandForge design — or any de novo binder — has **no homologs**; that is the
entire point of designing it. So the platform runs a full mmseqs search that
can only come back degenerate, and when the MSA service cannot answer at all
**the fold is refused** for an alignment that cannot exist.

Flag the designed chain. Any **one** of these is sufficient — the SDK
normalizes whichever you set into every spelling the platform reads:

```python
job = client.peptides.fold(
    sequences=[
        # Receptor: unflagged, and geneName is what sources its MSA.
        {"chainId": "A", "sequence": BRD4_ET_DOMAIN, "geneName": "BRD4"},

        # The design. Pick any one:
        {"chainId": "B", "sequence": binder, "is_peptide": True},
        # {"chainId": "B", "sequence": binder, "isPeptide": True},
        # {"chainId": "B", "sequence": binder, "use_msa": False},
        # {"chainId": "B", "sequence": binder, "role": "peptide"},   # or "binder"
    ],
    target_gene="BRD4",
    diffusion_samples=4,
)

result = job.wait()
print(result.peptide_ipsae)      # peptide<->receptor interface confidence
```

The typed form works too:

```python
from ligandai.types import Sequence

job = client.peptides.fold(sequences=[
    Sequence(sequence=BRD4_ET_DOMAIN, target_chain="A", target_gene="BRD4"),
    Sequence(sequence=binder, target_chain="B", is_peptide=True),
])
```

And for the plain two-chain case, the shorthand flags chain B for you:

```python
job = client.fold(BRD4_ET_DOMAIN, binder)
```

Flagging is also **faster**, not merely safer: a flagged chain is folded with an
empty MSA, which is what the model wants for a design anyway. Measured on an
identical two-chain fold (306 aa receptor + 10-mer peptide, same settings, warm
container): **34.2 s flagged vs 77.3 s unflagged** — about 43 s of extra GPU
time per fold, spent searching for homologs that do not exist.

Do **not** flag the receptor. Folding a receptor without its MSA measurably
degrades the predicted complex, and because the result still scores, a
wrongly-flagged receptor yields a plausible number for the wrong complex rather
than an error. Ligand, DNA and RNA entities are never MSA'd and never need the
flag.

You only need this when you build the `sequences` list by hand.
`generate(auto_fold=True)` folds its own output,
`fold_batch(peptides=[...], target_gene=...)` takes bare peptide strings against
one receptor and treats them all as de novo, the grouped
`fold_batch(groups=[...])` form flags any partner typed `"peptide"`, and
`cofold()` derives it from each chain's `kind`.
`ligandai.resources.peptides.is_peptide_chain(entity)` is exported if you want
to test an entity yourself.

## Batch fold against a fixed receptor (v0.5.5+)

`client.peptides.fold_batch(peptides, target_gene=...)` submits N peptides
against one fixed receptor in parallel — each peptide is folded as a 2-chain
complex (chain A = receptor, chain B = peptide). Pass exactly one of
`target_gene`, `receptor_pdb`, or `receptor_sequence`. Peptide input accepts
bare AA strings **or** FASTA blocks (multi-record supported).

Billing is **25 credits per fold per trajectory**, with a
`max(1.0, sampling_steps / 50)` multiplier, floored at a **100-credit minimum
per fold** (so a single-trajectory fold still costs 100, not 25) — the full
batch cost is charged upfront. HTTP 402 if balance is short.

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
        # peptide_ipsae is the binder metric. It is None on the fold_batch path
        # (which skips the peptide iPSAE step) — check peptide_ipsae_status
        # rather than falling back to fold.ipsae, which is complex-level.
        if fold.peptide_ipsae_status == "ok":
            print(fold.peptide_ipsae)
        else:
            print("peptide iPSAE not computed; complex ipsae:", fold.ipsae)

# Custom PDB receptor
job = client.peptides.fold_batch(
    peptides=peptide_library,
    receptor_pdb="receptors/my_target.pdb", # path OR raw PDB content
    receptor_name="MY_TARGET_v2",
    sampling_steps=100, # 2× billing multiplier
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

## Ensemble co-fold across engines (v0.6.7+)

`client.peptides.cofold(chains, engines=[...])` folds the **same** complex on
1–4 structure-prediction engines in parallel — each on its own single-B200
container — and returns a per-engine record (best-of canonical scores plus
ranges) with an optional DeltaForge score on each engine's best structure.
Available engines: `esmfold2`, `boltz2` (default), `protenix`, `openfold3`.

Each engine reports its confidences (iPSAE / iPTM / pLDDT) in its **own native
scale** — raw scores are never rescaled or normalized across engines, and
DeltaForge ΔG likewise varies by engine. Compare engines on within-engine
percentile, not by transforming one engine's raw numbers onto another's.

MSA is a single **shared** control (`shared_msa="auto"` computes OUR-OWN MSA
once on the receptor chains and hands the same alignment to every engine;
`"none"` runs single-sequence). MSA keys are rejected in `per_engine` — it is
never per-engine, and never an external/public MSA server.

```python
job = client.peptides.cofold(
    chains=[
        {"chain": "A", "sequence": "RECEPTOR_SEQ_HERE", "kind": "receptor"},
        {"chain": "B", "sequence": "BINDER_SEQ_HERE", "kind": "peptide"},
    ],
    engines=["boltz2", "protenix", "openfold3"],
    shared_msa="auto",          # OUR-OWN MSA, computed once, shared across engines
    run_deltaforge=True,
    gene="EGFR",
)
result = job.wait(timeout=1800)
for name, rec in result["engines"].items():
    print(name, rec["best"]["iptm"], rec["best"]["ipsae"])
print(result["billing"]["total_credits"])
```

Ensemble billing is **per engine per trajectory** (summed): esmfold2 16,
boltz2 50, protenix 20, openfold3 130 credits/trajectory, with a 5% / 10% / 15%
bundle discount for 2 / 3 / 4 engines and a 50-credit minimum per job. Any tier
(including free) may submit as long as the balance is positive; a zero-balance
caller gets HTTP 402. `GET /api/v1/cofold/engines` lists engines and defaults.

## Guidance Modules (v0.2.0+)

Quality-guided generation is available to all authenticated tiers, including
free. Academia, pro, and enterprise keys unlock immunogenicity guidance, serum
stability guidance, and logits-style advanced outputs:

```python
# Immunogenicity guidance — reduce MHC-I/II epitopes and improve humanness
# default 2.0 | min 0.0 | CEILING 1.5 (the worker clamps above it)
job = client.peptides.generate(
    gene="EGFR",
    num_peptides=200,
    immunogenicity=True,
    immuno_strength=1.5,
    immuno_modules={"mhc_i": True, "mhc_ii": True, "humanness": True},
)

# Serum stability — resist trypsin/DPP-IV/chymotrypsin cleavage
# default 2.0 | min 0.0 | CEILING 2.0 (its own metric REVERSES above it)
job = client.peptides.generate(
    gene="EGFR",
    num_peptides=200,
    serum_stability=True,
    stability_strength=2.0,
    stability_mode="resist",
    stability_modules={"trypsin": True, "chymotrypsin": True, "dppiv": True},
)

# Residue & terminal composition (renamed from `halflife` — it does NOT measure
# or predict a half-life; it shapes Trp load, basic fraction, Tyr content and
# terminal residues, with no N-end rule and no PEST term).
# target default None | strength default 2.0 | min 0.0 | ceiling 4.0
# The old `halflife=` / `halflife_strength=` spellings still work and warn.
job = client.peptides.generate(
    gene="EGFR",
    num_peptides=200,
    residue_composition="extended",   # "extended" = full strength, "moderate" = 0.4x
    residue_composition_strength=2.0,
)

# Cysteine policy as a first-class, explicitly-requested knob. The kernel costs
# -17.7% between-receptor JSD (95% CI [-32.5%, -3.7%]), so it is worth asking
# for deliberately rather than inheriting it from another head being on.
job = client.peptides.generate(
    gene="EGFR",
    num_peptides=200,
    cysteine_mode="disulfide_only",   # default; "allow" is the OFF position
    cysteine_enabled=True,            # True | False | None (legacy resolution)
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
    length_range=(12, 22), # cyclic-friendly length range
    cyclic_mode="disulfide",
    strict_recombinant=True, # forbid internal Cys (required for Adaptyv path)
)

result = job.wait(timeout=1800)
for p in result.peptides:
    if p.stability_scores:
        print(f"{p.sequence}: grade={p.stability_scores.stability_grade}, "
              f"halflife={p.stability_scores.predicted_halflife_hours:.1f}h")
    if p.immuno_scores:
        print(f" immuno_grade={p.immuno_scores.immuno_grade}, "
              f"pop_coverage={p.immuno_scores.population_coverage_pct:.0f}%")
    if p.cyclic_mode and p.cyclic_mode != "none":
        print(f" cyclic={p.cyclic_mode}")
```

## Long-Running Jobs

Generation, folding, and scoring submit GPU work and return a `Job`:

```python
job = client.peptides.generate(gene="EGFR", num_peptides=10)
job.id # str
job.status # "queued" | "running" | "complete" | "failed"
job.progress # 0-100 or None
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

## Diagnostics — did the run actually do what you asked?

A high-confidence result is not the same thing as the result you asked for.
A customer excluded a surface loop via `avoid_residues`, the exclusion was
silently a no-op, and 25/25 designed peptides bound that exact loop. Nothing
told them; they found out by eyeballing structures.

Every job now carries structured diagnostics you can branch on:

```python
job = client.peptides.generate(
    gene="BRD4",
    avoid_residues=[{"chain": "A", "start": 36, "end": 46}],
)
job.wait()

for d in job.diagnostics:
    print(f"[{d.severity}] {d.code}: {d.message}")
    if d.remediation:
        print(f"  → {d.remediation}")
    print(f"  details: {d.details}")

# The check the customer never had — fail the pipeline instead of shipping
# designs that ignored the constraint:
if job.diagnostics_at_least("error"):
    raise SystemExit("targeting did not do what was asked")
```

Each entry is a `Diagnostic` with a stable `code`, a `severity`
(`info` / `warning` / `error`), the pipeline `stage` it came from, a human
`message`, an actionable `remediation`, and a machine-readable `details`
payload. `job.worst_diagnostic_severity` is the one-line summary.

They arrive the same way whichever call style you use — the canonical
`diagnostic` SSE event during `job.stream()`, a `diagnostics` array on the
REST job/results payload for `job.wait()`, and the SDK's own pre-flight
validation before any HTTP call. Severity `warning` and above **also** raises
on the `warnings` channel, so a caller who never inspects `job.diagnostics`
is still told (targeting codes as `LigandAITargetingWarning`, everything else
as its `LigandAIDiagnosticWarning` base — so one filter catches all of them):

```python
import warnings
from ligandai import LigandAIDiagnosticWarning

# Turn every platform diagnostic into an exception (good for CI):
warnings.simplefilter("error", LigandAIDiagnosticWarning)
```

Client-side validation returns its diagnostics too, so you can inspect what
the SDK objected to instead of catching warnings:

```python
from ligandai._targeting import validate_and_normalize_targeting

result = validate_and_normalize_targeting(
    target_residues=[{"chain": "A", "start": 1, "end": 10}],
    avoid_residues=[{"chain": "A", "start": 1, "end": 10}],
)
[d.code for d in result.diagnostics]   # ['degenerate_pocket_risk']
```

**A code the SDK does not recognize is never filtered out.** Newer server
codes reach you in full (`d.is_known_code is False`, unknown envelope keys
preserved on `d.raw`) — dropping what it did not recognize is how the SDK
went silent in the first place. `ligandai.known_codes()` lists the taxonomy
this build ships; see [`docs/api_reference.md`](docs/api_reference.md) for
the full table.

### SDK version advisory

The platform reports on every response whether your SDK build is still
current (`X-LigandAI-SDK-Status` / `-Latest` / `-Min-Supported`). An outdated
client is not cosmetic — it is the one that silently drops a request
parameter a newer server expects. When the server flags it you get an
`sdk_outdated` (or `sdk_unsupported`) diagnostic and a
`LigandAIVersionWarning`, **once per process**, with the exact upgrade
command. The SDK never upgrades itself.

```python
for d in client.check_compatibility():   # explicit, on demand; [] when current
    print(d.message)
```

Opt out with `LIGANDAI_SKIP_VERSION_CHECK=1` (also silences the PyPI update
nag) or `LIGANDAI_SKIP_SERVER_VERSION_CHECK=1`.

## Errors

```python
from ligandai import (
    LigandAIError, # base
    LigandAIAuthError, # 401 — invalid/expired/revoked key
    LigandAIUpgradeRequired, # 402 — caller's tier doesn't include the surface
    LigandAICreditError, # 402 — insufficient credits for operation
    LigandAITierError, # 403 — tier escalation needed
    LigandAIForbidden, # 403 — pilot allowlist, EULA, ownership
    LigandAINotFoundError, # 404
    LigandAIRateLimitError, # 429 — rate limit
    LigandAIServerError, # 5xx (auto-retried)
    LigandAIValidationError, # 400/422 — also raised CLIENT-SIDE for pre-flight checks
    LigandAIConnectionError, # DNS/refused/reset/TLS/timeout — surfaced once retries are exhausted
)

try:
    detail = client.peptides.get(12345)
except LigandAIUpgradeRequired as e:
    print(f"Upgrade: {e.current_tier} → {e.required_tier} ({e.upgrade_url})")
except LigandAICreditError as e:
    print(f"Need {e.required} credits, have {e.available}")
except LigandAIConnectionError as e:
    print(f"Couldn't reach the server: {e.original_error}")
```

`LigandAITargetingWarning` (a `UserWarning` subclass, not an exception) is
emitted via `warnings.warn(...)` for non-fatal issues with
`target_residues`/`avoid_residues`/`hotspot_residues`/`avoid_shell_angstrom`
— catch it explicitly with `warnings.catch_warnings()` /
`warnings.simplefilter("error", LigandAITargetingWarning)` if you want CI to
fail on a degenerate targeting config instead of just printing.

See [docs/error_codes.md](docs/error_codes.md) for the full table of
HTTP status codes, server `code` strings, and matching SDK exceptions.

## Retry & Rate Limiting

The SDK automatically retries `GET`/`HEAD`/`PUT`/`DELETE`/`OPTIONS` on `429`,
`5xx`, and transient connection errors, with exponential backoff **+ jitter**
(configurable via `max_retries=`). It honours `Retry-After` and
`X-RateLimit-Reset` headers as the actual wait duration, not just a value on
the exception.

**`POST`/`PATCH` are sent at most once** unless you pass
`idempotency_key=` — retrying a POST whose *response* was merely lost (not
its effect) risks a duplicate submission (double credit charge, double GPU
job). Resource methods that wrap `transport.request(...)` internally accept
this by threading it through to the underlying call; at the transport level:

```python
client.transport.request(
    "POST", "/api/some/endpoint", json={"...": "..."},
    idempotency_key="my-caller-generated-uuid",
)
```

Connect-phase timeout is explicit and separate from the overall
read/write/pool timeout (`connect_timeout=` on `LigandAI(...)`, default
`min(10, timeout)`) — a dead TCP handshake fails fast independent of how long
you're willing to wait for a slow-but-alive server.

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
