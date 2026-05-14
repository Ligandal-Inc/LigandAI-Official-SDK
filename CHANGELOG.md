# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.5.5] - 2026-05-11

### Added — `client.peptides.fold_batch()` and `BatchFoldJob`

- **`POST /api/v1/folding/predict-batch`** — submit N peptides against one
  fixed receptor for parallel Boltz-2 folding. Each peptide is folded as a
  2-chain complex (chain A = receptor, chain B = peptide).
- New methods on `Peptides` and `AsyncPeptides`: `fold_batch(peptides,
  target_gene= | receptor_pdb= | receptor_sequence=, ...)`.
- New top-level convenience: `client.fold_batch(peptides, target_gene=...)`.
- Receptor resolution accepts gene symbols, raw or path-based PDB content,
  or amino-acid sequences (server attempts UniProt match for attribution).
- Peptide input accepts bare AA strings AND FASTA records (multi-record
  FASTA blocks are parsed server-side; one fold job per record).
- New result types `BatchFoldJob` / `AsyncBatchFoldJob` expose `batch_id`,
  `jobs`, `total_cost_credits`, `peptide_count`, `trajectories_per_peptide`,
  `receptor`, `sub_jobs`, `results`, `folds`, `refunds_pending`, plus
  `wait(timeout=, poll_interval=, on_progress=)` and `cancel()`.
- **Billing**: 100 credits per fold per trajectory, with a
  `max(1.0, sampling_steps / 50)` multiplier (e.g. 100 sampling steps =
  2× credits). The full batch cost is charged upfront — HTTP 402 is
  returned when balance is insufficient.

### Examples
- New `examples/23_fold_batch.py` walks through gene / PDB / sequence /
  FASTA receptor modes against a realistic candidate library.

## [0.5.4] - 2026-05-11

### Fixed - production-safe top-level helpers

- `client.generate(...)` now routes to the mounted production
  `client.peptides.generate(...)` endpoint instead of the experimental
  `/api/workers/{method}/invoke` route, which is not mounted on production.
- `client.fold(...)` now routes to the mounted production
  `client.peptides.fold(...)` endpoint instead of
  `/api/workers/boltzgen/fold/invoke`.
- `client.generate(method=...)` raises a local `NotImplementedError` for
  non-LigandForge experimental worker methods rather than making a request
  that 404s.

### Added - DeltaForge binder readout fields

- DeltaForge score parsing exposes binder/non-binder classification fields
  separately from affinity `dg` / `kd_nm`, including structural-energy gate
  readouts and non-binder reasons when returned by the API.

### Changed — two-tier super-elite (structural + affinity)

Super-elite is now reported as TWO separate buckets, never collapsed:

- **Structural** (`super_elite=True`) — the Proteina-Complexa
  structural-confidence gate from LigandForge bioRxiv v27:
  `iPSAE ≥ 0.67 AND iPTM ≥ 0.80 AND pLDDT ≥ 88` (0–100 scale; null
  passes). The 3-metric structural gate. Use for the headline
  "super-elite" count.
- **Affinity** (`super_elite_affinity=True`, NEW) — structural gate AND
  predicted Kd < 100 nM (DeltaForge). The synthesis-priority subset
  for users who care about predicted affinity.

Fixes the prior `super_elite` gate which was effectively a no-op due
to two server-side bugs:

1. The Kd constraint compared `predicted_kd <= 100e-9` (Molar), but
   the column is stored in nanomolar — live values are 119.5, 127.2,
   100.0 nM, none of which would ever satisfy `≤ 1e-7 M`. Result:
   the structural gate never returned its true population.
2. The pLDDT constraint compared `plddt >= 0.88`, but the column is
   on the 0–100 scale (per-residue confidence × 100). Result: every
   non-null pLDDT row trivially passed `≥ 0.88`.

Both bugs are fixed server-side. The structural gate now uses
`pLDDT ≥ 88` (no Kd term); the affinity gate adds `predicted_kd ≤ 100`
in nM. Counts will increase substantially for sessions where pLDDT
was previously masking everything.

## [0.5.3] - 2026-05-07

### Changed — open `/v1/peptides/by-gene` + `/v1/peptides/:id` to ALL tiers (free included)

Previously gated to pro/enterprise/superadmin via `validatePaidApiKey`.
Per Andre 2026-05-08: every authenticated user should be able to read
their own peptides regardless of tier — free tier just sees the data
masked. Endpoints now use `validateFlexibleApiKey`:

- **free**: aggregate counts (by-gene/by-pdb) + per-peptide rows return
  sequences with **first 4 amino acids + `********`** (down from 10) +
  PDB content as polyalanine + REMARK header pointing to /pricing.
  `_tier_redacted: true`, `_upgrade_url`, `_upgrade_note` fields included.
- **basic / academia / pro / enterprise / discovery_partner**: full
  sequences + real PDB. `_tier_redacted: false`.

This restores the symmetry with `/v1/peptides/list` and `/v1/peptides/search`
which already used the flexible (mask-not-block) middleware.

### Added — `peptides.by_pdb()` for PDB-targeted aggregation

Mirror of `by_gene()` for users whose generation requests targeted a
specific PDB code (e.g. `9MIR` for the BMPR1A–RGMB heteromer) instead of
a gene symbol. Returns rows pivoted on `(pdb_code, gene)` — common when
users upload custom PDBs or design against multi-chain complexes.

```python
client.peptides.by_pdb("9MIR")
# [{"pdbCode": "9MIR", "gene": "BMPR1A", "sessions": 3, ...}, ...]
```

Tier-open like `by_gene()`. Backed by `GET /api/v1/peptides/by-pdb`.

### Fixed — `plddt_min` filter silently dropped on every search call

`peptides.search(plddt_min=...)` was sending the param as `pldd_min`
(missing the second `t`) since 0.5.1, so the server ignored it and
returned peptides below the requested pLDDT floor. Single-character
typo at `ligandai/resources/peptides.py:1404`. Verified by re-running
a known-fail query (BMPR1A `plddt_min=0.92` → previously returned 142
peps, several with pLDDT 0.78; now returns 38 peps, all ≥ 0.92).

### Added — async `peptides.search()` parity with sync

`AsyncPeptides.search()` was missing 20+ filters the sync version had
shipped in 0.5.1: `plddt_min`, `dg_max`, `binder_pct_min`, `length_min`,
`length_max`, `is_elite`, `super_elite`, `hotspot_residues`,
`pocket_residues`, `hotspot_hit`, `pocket_hit`, `contact_distance_a`,
`stability_grade`, `immuno_grade`, `conformation`, `session_id`,
`pdb_id`, `sort`, `order`. The async signature now mirrors the sync
signature exactly. No breaking change — existing async callers keep
working; new kwargs are all optional.

### Internal

- `__version__` and `pyproject.toml` bumped 0.5.2 → 0.5.3.
- All changes are bug-fix / additive; safe to upgrade in place.

---

## [0.5.2] - 2026-05-07

### Added — `pdb_url` on every peptide + `peptides.download_pdb()` helper

Server now returns `pdb_url` on every peptide row from `/v1/peptides/list`,
`/v1/peptides/search`, and `/v1/peptides/:id`. The SDK uses this to expose
a one-step download:

```python
peps = client.peptides.search(gene="BMPR1A", super_elite=True, limit=5)
for p in peps:
    print(p.pdb_url)                            # "/api/v1/structures/12345/pdb"
    pdb_bytes = client.peptides.download_pdb(   # raw bytes
        p.peptide_id, save_to=f"{p.peptide_id}.pdb"
    )
```

The new `peptides.download_pdb(peptide_id, save_to=None)` convenience
method resolves to the same endpoint as `client.structures.get_pdb()`
but is callable directly off the search result objects.

Tier behavior: free-tier keys get a side-chain-scrambled PDB; paid tiers
get the original. The peptide response includes `_pdb_masked: True` when
the next download will be scrambled.

### Fixed — academia tier mask leak (#13) + credits sentinel leak (#10)

- **#13** — `validateFlexibleApiKey` was bucketing academia paid users as
  free for masking because it relied on the API-key-prefix tier and
  ignored the user's DB `subscriptionTier`. Now takes the broader of
  (key tier, DB tier). Defense-in-depth in `_maskRow`. Server commit
  `def59779d`.
- **#10** — `client.account.credits()` was returning the superadmin
  sentinel for every user. Root cause: the `/api/user-credits` alias
  forwarded via `fetch('http://127.0.0.1:...')`, which triggered the
  VPN/localhost auto-login and rewrote `req.user` to superadmin BEFORE
  the API-key middleware ran. Fix: inlined the credit lookup in the
  alias handler and validated the inbound `X-API-Key` directly. Server
  commit `def59779d`.

### Internal

- `__version__` and `pyproject.toml` bumped 0.5.1 → 0.5.2.
- All new behaviors are additive — no breaking changes from 0.5.1.

---

## [0.5.1] - 2026-05-07

### Added — rich peptide search criteria + generate-loop planner + pocket lookup

The SDK can now express every workspace filter directly via `peptides.search(...)`,
plan a generate-and-fold loop until N peptides match arbitrary criteria, and
compute the pocket residues around one or more hotspots without leaving Python.
Pairs with platform commit `e516f36a9` on the server side.

#### `peptides.search(...)` — full criterion set

`peptides.search()` now accepts every score / coverage / scope filter the
ligandai.com workspace UI exposes. All criteria AND-combine.

```python
results = client.peptides.search(
    gene="BMPR1A",
    ipsae_min=0.80, iptm_min=0.85, plddt_min=0.85,
    kd_max=1e-7, dg_max=-8.0, binder_pct_min=0.7,
    length_min=20, length_max=40,
    super_elite=True,                          # combined gate
    hotspot_residues=["A:60", "A:62"],         # PDB numbering, chain:resi
    hotspot_hit=True,                          # require contact
    pocket_residues=["A:55","A:56","A:67"],
    pocket_hit=True,                           # hotspot OR pocket
    contact_distance_a=5.0,
    stability_grade=["A", "B"],
    immuno_grade=["A", "B"],
    conformation="monomer_C",
    pdb_id="9MIR",                             # PDB-scoped filter
    sort="ipsae", order="desc",
    limit=25,
)
```

Each returned peptide includes `hotspot_contacts` and/or `pocket_contacts`
arrays with per-residue heavy-atom distances when residue criteria were
specified. New optional kwargs (all backward-compatible):

- **Score thresholds**: `plddt_min`, `dg_max`, `binder_pct_min`
- **Length range**: `length_min`, `length_max`
- **Combined gates**: `is_elite`, `super_elite`
- **Hotspot/pocket coverage**: `hotspot_residues`, `pocket_residues`,
  `hotspot_hit`, `pocket_hit`, `contact_distance_a`
- **Categorical**: `stability_grade`, `immuno_grade`, `conformation`
- **Scope**: `session_id`, `pdb_id`
- **Sort**: `sort` (`ipsae|iptm|plddt|kd|dg|length|created_at`), `order`
  (`asc|desc`)

#### `peptides.fill_until(...)` — generate-and-fold loop planner

Plan or kick off a generate-and-fold loop until `target_count` peptides
match `criteria`. Two-phase contract avoids surprise spend:

```python
crit = {
    "super_elite": True,
    "hotspot_residues": ["A:60", "A:62"],
    "hotspot_hit": True,
}

# Phase 1 — see how many already match + estimated cost to fill
plan = client.peptides.fill_until(
    "BMPR1A", target_count=25, criteria=crit, mode="plan"
)
# plan["current_passing_count"], plan["remaining"],
# plan["plan"]["batches_recommended"], plan["plan"]["est_credits"]

# Phase 2 — client-side iteration so you can checkpoint progress
for _ in range(plan["plan"]["batches_recommended"]):
    client.peptides.generate(gene="BMPR1A",
                             num_peptides=plan["plan"]["batch_size"])
    # ... wait for fold ...
    next_plan = client.peptides.fill_until("BMPR1A", target_count=25,
                                           criteria=crit, mode="plan")
    if next_plan["remaining"] == 0:
        break

results = client.peptides.search(gene="BMPR1A", **crit, limit=25)
```

Empirical pass-rate guess (5% strict / 25% loose) sizes the plan; the
loop honors `budget_credits_max` and bails before exceeding it.

#### `peptides.pocket_for_hotspots(...)` — hotspot → pocket residue lookup

Given a PDB id (or fold session) and one or more hotspots, returns the
pocket residues within `radius_a` Å with per-residue heavy-atom distances.

```python
pocket = client.peptides.pocket_for_hotspots(
    pdb_id="9MIR",
    hotspots=["A:60", "A:62"],
    radius_a=8.0,
)
# pocket["pocket_residues"] -> [{chain, residue, resname, distance_a}, ...]
# Multi-hotspot input is unioned with closest-distance preference.
```

Wraps `GET /api/v1/structures/{pdb_id}/pocket`. Supports both canonical
PDB and fold-session sources (`session_id=`).

### Internal

- Bumped `__version__` and `pyproject.toml` to 0.5.1.
- All new methods are additive; no breaking changes from 0.5.0.
- `peptides.search()` legacy kwargs (`min_ipsae`) remain aliased.

---

## [0.5.0] - 2026-05-07

### Added — Andrew Keene SDK gaps (`peptides.list(program_id)` and friends)

The SDK now exposes the program-scoped peptide and structure listings that
SDK users have been asking for. Every new method handles 402 (paid-tier
required) by raising the new `LigandAIUpgradeRequired` exception.

- **`peptides.list()` accepts `program_id`** — fixes the long-standing
  `TypeError` when calling `client.peptides.list(42)`. The first positional
  arg now accepts either a gene symbol (str) or program DB id (int), and
  both are also exposed as keyword args. Passing both `gene` and
  `program_id` filters within the program. Backed by the new
  `GET /api/v1/peptides/list` endpoint, which returns a richer schema
  (peptide_id, fold_id, predicted_kd, isElite) than the legacy
  `/api/ptf/generated-peptides/by-gene/...` shape.
- **`peptides.list_by_program(program_id, ...)`** — convenience wrapper
  around the new endpoint with score thresholds (`min_ipsae`, `min_iptm`,
  `max_kd`).
- **`peptides.search(...)`** — now backed by `GET /api/v1/peptides/search`.
  Cross-program search by score thresholds (`ipsae_min`, `iptm_min`,
  `kd_max`); `gene` is now optional. The legacy `min_ipsae` kwarg is
  retained as an alias for `ipsae_min`.
- **`structures.list(program_id=...)`** — `GET /api/v1/structures/list`
  returns fold-structure metadata (gene, scores, `pdb_url`) for a program.
  Use `structures.get_pdb(structure_id)` to fetch the PDB content.
- **`structures.get_pdb(structure_id)`** — `GET /api/v1/structures/:id/pdb`.
  Returns the raw PDB text. Free-tier callers receive polyalanine
  (sidechains stripped, `REMARK   1` redaction header inserted at top);
  paid-tier callers receive full atomic detail.

### Added — `LigandAIUpgradeRequired` (alias for `LigandAIPaidTierRequired`)

`LigandAIUpgradeRequired` is the public-API name for the 402 case. Old
code catching `LigandAIPaidTierRequired` continues to work because the
new class inherits from it. The dispatcher now also surfaces the
server's `upgrade_url` field on the exception (defaults to
`https://ligandai.com/pricing`).

### Added — tier-redaction signaling on responses

All new endpoints include `_tier`, `_tier_redacted`, and `_upgrade_url`
in their JSON responses. The SDK `Peptide` model now has a `peptide_id`,
`length`, `predicted_kd`, `is_elite`, and `_masked` field so callers can
detect when free-tier sequence redaction has been applied.

### Fixed — `peptide_count` on `client.programs` was always 0

`GET /api/ptf/programs` (used by `client.programs.list()`) and
`GET /api/ptf/programs/:id` now compute `peptide_count`, `folded_count`,
and `elite_count` live via JOIN over `ptf_generated_peptides` and
`ptf_fold_results`. The legacy denormalized columns
`total_peptides_generated` / `total_peptides_folded` /
`elite_peptide_count` were never wired to the actual generation pipelines
and have been bypassed in the API response.

### Fixed — Free-tier API leaks closed (`/api/v1/*`)

The `/api/v1/peptides/list`, `/v1/peptides/search`, `/v1/structures/list`,
and `/v1/structures/:id/pdb` endpoints accept free-tier API keys but mask
sequences (first 10 AA + `********`) and scramble PDBs to polyalanine.
The CSV export endpoints (`/api/user/results/export`,
`/api/design-studio/download/csv`, `/api/design/projects/:id/export`)
have been updated to honor tier — free-tier users now see masked
sequences in CSV output and a `tier_visibility` column. The
`/api/ptf/generated-peptides/by-gene/:gene` endpoint, which was missing
its `hasPaymentMethod` check, now correctly masks sequences for
trial-without-card users.

## [0.4.1] - 2026-05-07

### Fixed — `proteins.upload_pdb` rejected partial server responses
- `UserProtein` and `ProteinVariant` pydantic models loosened: every
  non-`id` field is now Optional. Previously, an upload that succeeded
  server-side but came back with a degraded body (CIF parser returning
  `residueCount: 0`, `chainInfo: []`, `geneSymbol: null`) would raise a
  pydantic validation error and force users to bypass the SDK with raw
  multipart. Both models continue to inherit `extra="allow"` from
  `_LGModel` so additive server fields are preserved.
- Added explicit fields the server actually emits: `gene_symbol`,
  `user_id`, `chain_count`, `residue_count`, `chain_info`, `status`. They
  default to `None` so partial payloads validate cleanly.

### Fixed — `client.credits` returned superadmin sentinel for normal users
- Added sentinel detection: when the server returns a balance
  ≥ `1e10` (e.g. `Number.MAX_SAFE_INTEGER` 9_007_199_254_740_991, or the
  `1e16` superadmin marker), the SDK now sets
  `Credits.is_unlimited = True` and emits a one-shot stderr warning
  ("implausible credits balance — likely tier resolution bug, contact
  support@ligandai.com"). Use `client.account.credits().is_unlimited`
  to distinguish a true unlimited account from a server-side bug.
- `Credits` model now also accepts `credits` as an alias for `balance`
  (server has historically returned both shapes) and both attributes
  are populated on validation.

## [0.4.0] - 2026-05-07

### Added — chain / pocket / fold-partner control
- `peptides.generate(target_chains=["C"])` — restrict design AND folding to
  specific chain IDs of a multimer target. Maps to ``config.targetChains``.
  Use this for "design against chain C only" of a multi-chain PDB.
- `peptides.generate(fold_partners=...)` — three explicit modes for what
  receptor chains end up in the peptide co-fold:
  * ``"target_only"`` — peptide + only the listed target chain(s)
  * ``"native_complex"`` — peptide + target + its native interaction partners
    (e.g., BMPR1A + RGMB) so users can compare inhibitory effect against the
    native interface
  * ``"all_conformations"`` — full ensemble across all conformations
  * ``list[str]`` — explicit conformation names
- `peptides.generate(pocket_expansion_radius_a=6.0)` — when ``target_residues``
  are passed, the server now auto-includes every residue within this radius
  of any hotspot atom in the design pocket. Defaults to 6.0 Å. Pass 0 to use
  the literal residues only. Fixes "I gave you a hotspot but the peptide
  bound to the opposite face" (issue dre-2026-05-07-hotspots).
- Auto-strategy: when ``target_residues`` are non-empty and
  ``targeting_strategy`` is not explicitly set, the SDK now sends
  ``targeting_strategy="pocket_targeted"`` instead of the previous silent
  ``"full_surface"`` default that ignored the residues.
- Default ``fold_strategy="quality_ranked"`` — server pre-ranks generated
  peptides by composite (LigandIQ × predicted iPTM) and folds the top
  candidates first so credits go to the most promising designs.

### Added — local downloads
- `GenerationResult.save_to(directory)` — write ``peptides.csv`` (sequence +
  scores), ``folds/{rank}_{seq}.pdb`` (folded structures), and
  ``summary.json`` (full metadata) to a local directory. Parallel batched
  PDB fetch (8 concurrent requests).
- `Job.wait(save_to=...)` — auto-save run artifacts when the job completes.
  Pass empty string to use ``./ligandai_runs/{session_id}/`` default.
- `GenerationResult.view_url` — direct URL to the run on ligandai.com.
- `GenerationResult.csv_url` — authenticated CSV export endpoint.

### Added — custom PDB upload (now works for ALL tiers)
- `proteins.upload_pdb()` now hits the canonical ``/api/user/proteins/upload``
  endpoint (was 404'ing on the hyphenated path). Field name is ``files``
  (server tolerant of both ``file`` and ``files`` going forward), and the
  response is unwrapped to the first registered ``UserProtein``. CIF files
  send the correct ``chemical/x-mmcif`` MIME type.
- Upload is available to **all authenticated tiers** (free, basic, academia,
  pro, enterprise) — no tier gate. Uploads land in the user's "My PDBs"
  library at ``https://ligandai.com/account/billing?tab=my-pdbs``.

### Added — agent discoverability
- `AGENTS.md` and `CLAUDE.md` at the SDK package root so Claude Code, Codex,
  Cursor, and Aider auto-discover the four canonical workflows (gene,
  PDB-ID + chain, custom CIF/PDB upload, pocket-targeted) without grepping
  for method signatures.
- `examples/07_pdb_id_chain_design.py` — runnable demo of the PDB-ID +
  chain selection workflow.
- AGENTS.md now also documents tier GPU caps (academia=16, pro=25,
  enterprise=50) and instructs agents to pass ``fold_gpus=`` matching the
  user's tier so jobs finish in minutes instead of 30+.

### Server-side aliases (deployed alongside this release)
- ``/api/v1/user-proteins/*``, ``/api/user-proteins/*``, ``/api/v1/user/proteins/*``
  all rewrite to the canonical ``/api/user/proteins/*`` handlers.
- ``/api/v1/protein-variants*`` and ``/api/protein-variants*`` rewrite to
  ``/api/ptf/protein-variants*``.
- ``POST /api/user/proteins/upload`` now reads ``gene`` / ``customName``
  overrides from multipart form fields and accepts the file under either
  ``file`` (singular) or ``files`` (plural) field name.

## [0.3.9] - 2026-05-07

### Added
- `peptides.generate(target_chains=["C"])` — restrict design to specific chain
  IDs of a multimer target (e.g. design only against chain C of PDB ``9MIR``
  while keeping chains A/B/D as binding context). Maps to ``config.targetChains``
  on the server. Both sync and async clients support this.
- `AGENTS.md` and `CLAUDE.md` at the SDK package root so Claude Code, Codex,
  Cursor, and Aider auto-discover the four canonical workflows (gene,
  PDB-ID + chain, custom CIF/PDB upload, pocket-targeted) without grepping
  for method signatures. Includes API key URL, tier caps, error handling,
  job lifecycle, and platform URLs.
- `examples/07_pdb_id_chain_design.py` — runnable demo of the PDB-ID +
  chain selection workflow.

### Documentation
- `README.md` now includes "Designing against a specific PDB ID + chain" and
  "Designing against a custom CIF/PDB on disk" examples up-front, since these
  are the two flows agents most often need to reconstruct from scratch.

## [0.3.8] - 2026-05-07

### Fixed
- Docstring on `LigandAI(base_url=...)` now correctly documents the default
  as `https://ligandai.com`. The previous text claimed `https://api.ligandai.com`,
  which is **not** a published host (NXDOMAIN). Customers reading the docs and
  passing `base_url="https://api.ligandai.com"` got connection refused on every
  call; this lie is now removed and the docstring explicitly warns against
  pointing integrations at that subdomain.

### Added
- Startup INFO log on every `LigandAI()` / `AsyncLigandAI()` construction:
  `"LigandAI initialized: base_url=<url> tier=<tier> api_key=<first 8 chars>..."`.
  Suppressible via standard `logging` config. Designed for customers (and the
  AI agents they hand the SDK to) to confirm what host they're actually hitting
  and which tier their key resolved to without round-tripping the server.
- `LIGANDAI_DEBUG=1` environment variable enables per-request DEBUG logging on
  the `ligandai` logger. Format: `METHOD URL -> STATUS (Xms)`. Both sync and
  async transports honor it. Set `logging.getLogger("ligandai").setLevel(
  logging.DEBUG)` to surface the lines.
- Server now exposes `/api/v1/*` as a public versioned alias for the `/api/ptf/*`
  surface (programs, sessions, workstreams, projects, targets, settings,
  parallel/*). Resources still target `/api/ptf/*` internally for backwards
  compatibility, but customers and integrators following the documented "v1"
  convention can call either path.

## [0.3.7] - 2026-05-06

### Fixed
- `programs.list()`, `jobs.list()`, and `receptors.list()` no longer raise
  Pydantic validation errors against the live server. Required fields on
  `Program`, `JobInfo`, and `ReceptorComplex` are now optional and additional
  server-canonical fields (`programId`, `complexId`, enrichment metadata) are
  documented as aliases. Real-world server responses lack `id` (programs use
  `programId`, complexes use `complexId`); requiring `id` made every basic-tier
  caller fail.
- 403 responses are no longer blanket-classified as tier errors. The 403 mapper
  now inspects the response payload — if the server includes `requiredTier`,
  `tier_required`, `currentTier`, `upgrade_required`, or a `*_TIER_REQUIRED`
  code, the SDK still raises `LigandAITierError`. Otherwise (pilot allowlists,
  ownership checks) it raises the new `LigandAIForbidden` carrying the server's
  actual `error_code` (e.g. `pilot_restricted`) and message. This stops the
  SDK from telling Andrew Keene (basic tier) that "Pro tier required, you're on
  free" when he hits an internal-only AutoResearch pilot endpoint.

### Added
- `LigandAIForbidden` exception for honest 403 reporting (exposes `reason` from
  server `error_code`). Exported from the package root.

## [0.3.6] - 2026-05-06

- Adds `ResidueRange.from_residues()` so Studio-style pocket selections can be
  compressed into continuous chain-local ranges before peptide generation.
- Documents pocket-targeted generation payloads for agents and preserves
  multi-chain `targetResidues` wire-format coverage in SDK tests.

## [0.3.5] - 2026-05-06

- Publishes the corrected post-recovery SDK artifact after `0.3.4` was already
  uploaded to PyPI and could not be replaced.
- Adds basic-tier API-key awareness across local SDK entitlement checks,
  examples, and agent guidance while leaving generation, folding, GPU, and
  token enforcement to the authenticated LigandAI API.
- Clarifies agent billing and upgrade routing for free/basic/pro/academia users,
  including API-key creation from account settings and token/top-up prompts.
- Keeps startup PyPI version reminders on the real `ligandai-python-sdk`
  metadata path so Claude, Codex, and user shells can detect stale installs.
- Updates package notices for 2026 and ties SDK installation/import/use to the
  LigandAI Terms of Service and EULA.

## [0.3.4] - 2026-05-05

- Restores the public PyPI release line from the real `ligandai-python-sdk`
  package after the accidental `1.0.x` uploads from the wrong package root.
- Carries forward the SDK billing/session attribution, persistent goal-run,
  peptide viewer, direct fold controls, and ReceptorDB contribution updates
  intended for the `0.3.x` SDK line.
- Adds SDK startup version reminders that validate PyPI release metadata before
  recommending an agent or user run `python -m pip install --upgrade ligandai`.
- Documents agent API-key creation, billing/token routing, GPU guard handling,
  and Claude API Skill setup.
- Updates package copyright notices to 2026 and clarifies that SDK
  installation/import/use accepts the LigandAI Terms of Service and EULA.

## [0.3.3] - 2026-05-05

### Added

- Direct `Peptides.fold()` / `AsyncPeptides.fold()` accept
  `contribute_to_receptordb`. The SDK sends both the canonical
  `contributeToReceptordb` field and the legacy `submitToCommunity` alias so
  current servers can apply the documented ReceptorDB contribution discount to
  eligible direct human receptor or receptor-complex folds and persist the
  setting with fold outputs.

## [0.3.2] - 2026-05-03

### Added

- `Peptides.generate()` and `AsyncPeptides.generate()` now expose the PTF
  fold-side controls used by the server: `folding_mode`, `fold_strategy`,
  `folding_conformations`, `max_folds_per_target`, `enable_expansion`,
  `auto_conformation_expansion`, `clash_resolution_enabled`,
  `md_relaxation_enabled`, and `num_trajectories`.
- `Peptides.fold()` and `AsyncPeptides.fold()` now expose advanced Boltz fold
  controls: `sampling_steps`, `recycling_steps`, `num_trajectories`, and
  `step_scale`.
- Local peptide viewing helpers under `ligandai.peptide_viewer` can load
  LigandForge/PTF JSON, JSONL, PDB, and result directories; rank candidates by
  iPSAE or DeltaForge-style scores; align receptor+peptide folds to a base
  receptor; launch ProteinView; and write/serve a localhost 3Dmol dashboard.
- `client.goals` / `client.goals.start()` and async equivalents now expose
  persistent AutoResearch goal runs (`/api/autoresearch/*`). Starting a run
  requires `automatic_mode=True` and accepts `budget_cap_credits`,
  `program_id`, `project_id`, `program_db_id`, `project_db_id`, and
  `conversation_id`, plus `max_iterations` for evaluator follow-up loops.
  Server-side execution is currently limited to internal pilot accounts.
- New goal-run models: `GoalRunStart`, `GoalRun`, `GoalPlanStep`, and
  `GoalStepRecord`, with typed acceptance criteria and evaluator history on
  `GoalRun`. `GoalRun` also exposes the persisted Automatic Mode acknowledgement
  and acknowledgement timestamp when returned by the server.
- Goal runs now parse the server's derived project-management graph:
  `GoalProjectState`, checklist items, dependencies, evidence, blockers,
  next actions, progress, budget state, and completion audit. Use
  `client.goals.graph(run_id)` to fetch the graph directly.
- `client.goals.stream(run_id)` and the async equivalent now parse live
  AutoResearch SSE events into `GoalRunEvent` objects. The stream starts with
  the server's latest `hello` snapshot when available and then yields planning,
  step, evaluation, and terminal events.

### Changed

- Direct SDK folds now default to one diffusion sample unless the caller
  explicitly passes `diffusion_samples` or `num_trajectories`, matching the
  current platform fold policy.

### Notes

- Terminal viewing support cites ProteinView by Tristan Farmer / 001TMF under
  the MIT License: https://github.com/001TMF/ProteinView.

## [0.3.1] - 2026-04-30

### Added — billing surface (`client.account` + `client.peptides.estimate_cost`)

- **`Account.get_balance()`** and **`AsyncAccount.get_balance()`** — fetches the
  current credit balance, 30-day burn rate, days-of-runway, tier, and
  auto-topup status from `GET /api/billing/account-summary`. Returns a new
  `AccountBalance` model.
- **`Account.billing_usage(period="30d")`** and async equivalent — fetches the
  recent credit transaction history (period: `"7d"` | `"30d"` | `"90d"`) from
  the same summary endpoint. Returns `list[CreditTransaction]`.
- **`Account.top_up(amount_usd, save_card, payment_method_id)`** and async —
  posts to `POST /api/billing/topup`. When `payment_method_id` is provided (or
  a card is saved on file), charges immediately off-session and returns credits
  added + new balance. Otherwise returns a `checkout_url` for the browser Stripe
  flow. Returns `TopUpResult`.
- **`Account.configure_auto_topup(enabled, threshold_credits, amount_usd)`** and
  async — configures automatic top-ups via `POST /api/billing/auto-topup/configure`.
  Returns `AutoTopupConfig`.
- **`Peptides.estimate_cost(num_peptides, auto_fold, fold_top_n, fold_trajectories)`**
  and async — estimates credits and USD cost for a generation + folding run via
  `GET /api/billing/estimate`. Returns `CostEstimate` with `credits` (int),
  `cost_usd` (float), and a `breakdown` dict by phase (generation, folding, scoring).

### Added — new types (`ligandai.types`)

- **`AccountBalance`** — credits, burn_rate_30d, days_remaining, tier, auto_topup_enabled.
- **`TopUpResult`** — success, credits_added, new_balance, payment_intent_id, checkout_url.
- **`AutoTopupConfig`** — enabled, threshold_credits, amount_usd, last_charged_at, failure_count.
- **`CostEstimate`** — credits, cost_usd, breakdown dict by phase.

All four types are now exported at the package top level.

### Changed — `CreditTransaction` model

- Added `type` field (billing transaction type: `"topup"` / `"auto_topup"` /
  `"usage_gpu"` / `"refund"` / etc.) alongside the existing `operation` field.
- Added `balance_after` field (balance after this transaction applied).
- Added `created_at` alias alongside the existing `occurred_at`.
- `operation` is now optional (nullable) for forward compatibility with the
  billing system's new transaction schema.

### Fixed — publish blockers

- **`DEFAULT_BASE_URL`** corrected to `https://ligandai.com` (was
  `https://api.ligandai.com`, which is unreachable). Without this fix, fresh
  installs would fail their first request.
- **`jobs.TERMINAL_STATUSES`** now includes `generation_complete` and
  `fold_complete`. Previously, `Job.wait()` would hang forever on jobs whose
  Modal callbacks emit those terminal events.
- **`jobs.SUCCESS_STATUSES`** mirrors the same additions.
- **`Job` / `AsyncJob`** now accept an optional `result_loader` callback
  (sync or async) for deferred result hydration, plus improved `session_id`
  resolution from `model_extra` and `job_id` prefix when the result dict
  doesn't carry it.
- **`ligandai.resources.msa`** — new file backing the `MSAChain` and
  `MSAResult` types already exported by `ligandai.__init__`. Without it,
  `from ligandai import MSAResult` would raise on package init.

(0.2.0 was published to PyPI with only the parameter coverage in
"comprehensive generation parameter coverage"; this 0.3.0 layers the v1
peptide surface and the publish blockers on top.)

### Added — paid-only `/api/v1/peptides/*` surface (LIGANDAI_ALPHA_V2-afspr)

- **`Peptides.by_gene(...)`** and **`AsyncPeptides.by_gene(...)`** — gene-level
  peptide aggregation across all of the caller's sessions and programs. Wraps
  `GET /api/v1/peptides/by-gene`. Returns `list[GeneSummary]` with folded
  counts (total / great+ / elite), best iPSAE / best DeltaForge dG,
  session/program coverage, last activity timestamp. Filters: `genes=`,
  `min_ipsae=`, `program_id=`, `project_id=`, `since=`, paginated.
- **`Peptides.list(gene, ...)`** and **`AsyncPeptides.list(...)`** — list the
  actual peptide rows for a gene (peptides, not just counts). Wraps
  `GET /api/ptf/generated-peptides/by-gene/:gene`.
- **`Peptides.get(peptide_id, include=[...])`** and async equivalent — single-
  peptide detail keyed by `ptf_fold_results.id`. Default thin response;
  `include=["pocket_features"]` adds the per-residue 48-dim pocket feature
  matrix and metadata; `include=["interface"]` adds per-receptor-chain
  iPSAE/ipAE/pdockq2 + post-fold disulfide geometry; `include=["pdb"]` adds
  the full PDB content (5–50 KB). Unknown include values raise `ValueError`
  client-side and HTTP 400 server-side.

### Added — types

- **`GeneSummary`** Pydantic model in `ligandai.types` (mirrors server
  `AggregatePeptidesByGeneRow`).
- **`PeptideDetail`** Pydantic model in `ligandai.types` (mirrors the server
  `GET /api/v1/peptides/:id` response, with optional heavy-field properties).
- **`Peptide`** is now exported at the package top level (was previously only
  importable via `ligandai.types`).

### Added — paid-tier validation

- **`LigandAIPaidTierRequired`** exception (subclass of `LigandAIError`).
  Raised when the API key resolves to a tier (`free` / `academia`) that does
  not include API access.
  - Client-side fail-fast: `Peptides.by_gene/list/get` raise this immediately
    on free-tier keys (no network round-trip).
  - Server-side: the `/api/v1/peptides/*` middleware returns
    `HTTP 402 Payment Required` with `{"error":"upgrade_required",...}`. The
    SDK error mapper routes that response to `LigandAIPaidTierRequired`
    (instead of `LigandAICreditError`), so callers can `except` the right
    subclass.
  - This is per the policy in
    `/home/user/.claude/projects/-home-dre/memory/feedback_api_paid_only.md`:
    free users cannot use the SDK / `/api/v1/*` — those routes are
    monetized; the web UI is the free-tier acquisition channel.

### Changed — cysteine controls promoted from `extra` to typed kwargs (LIGANDAI_ALPHA_V2-lgxh7)

The previous SDK accepted cysteine / cyclic controls only via `extra={...}`
passthrough. They are now first-class typed kwargs on `Peptides.generate()`:

- `cysteine_mode` — `"allow_all"` / `"disulfide_only"` (default) / `"exclude_all"`
- `cyclic_mode` — `"none"` / `"lactam"` / `"disulfide"` / `"head_tail_contact"`
- `cyclic_strength`, `strict_recombinant`, `dual_fold_viz`

(See the existing v0.2.0 entries below for the full kwargs list — this entry
documents the typing migration policy.)

### Deprecated

- Passing `cys_mode`, `cysteine_mode`, `cys_gate`, `cyclic_mode`, `cyclic_strength`,
  `strict_recombinant`, `dual_fold_viz`, or `disulfide_constraints` via `extra={...}`
  emits `DeprecationWarning` as of v0.2.0. The `extra` path still works for
  backward compatibility but **will be hard-rejected in v0.3.0**. Migrate to the
  typed kwargs.

### Added — guidance kwargs (continued)

- **`immuno_modules`** parameter on `Peptides.generate()` and `AsyncPeptides.generate()`:
  dict of booleans enabling specific MHC-I/II, BCR, TAP, TCR, and humanness epitope
  modules (e.g. `{"mhc_i": True, "mhc_ii": True, "humanness": True}`). Forwarded as
  `immunoModules` in the request body. Requires pro+ tier.
- **`stability_modules`** parameter: dict enabling specific protease modules
  (trypsin, chymotrypsin, elastase, dppiv, plasmin, neprilysin). Forwarded as
  `stabilityModules`. Requires pro+ tier.
- **Charge / solubility filtering** — four new params: `charge_mode`
  (`"off"` / `"lt"` / `"gt"` / `"between"`), `charge_value`, `charge_min`,
  `charge_max`, `min_solubility`. Server activates the filtered Modal worker when
  any non-default constraint is present and the user is on a pro+ tier.
- **Cyclization** (`cyclic_mode`, `cyclic_strength`, `strict_recombinant`,
  `dual_fold_viz`): `"disulfide"` (primary recombinant-shippable, terminal Cys-Cys),
  `"lactam"` (head-to-tail amide, prediction/viz layer), or `"head_tail_contact"`
  (soft B-matrix bias). Tier-gated to academia / pro / pro_commercial / enterprise /
  discovery_partner; basic/free receive HTTP 403 from the server.
- **`StabilityScores`** and **`ImmunoScores`** structured output models in `types.py`,
  surfacing the full `stability_scores` and `immuno_scores` JSONB columns from the
  server schema (halflife, cleavage_risk, grades, epitope counts, TAP/BCR/TCR scores,
  population coverage).
- **`Peptide.stability_scores`** and **`Peptide.immuno_scores`** typed fields (map to
  the new structured models above).
- **`Peptide.cyclic_mode`** field: reflects `guidance_config.cyclicMode` from the
  server DB, present after generation.
- **`_CyclicMode`** and **`_ChargeMode`** Literal type aliases exposed at module level.
- CLI (`ligandai generate peptides`): all new flags mirroring SDK — `--cysteine-mode`,
  `--quality-guided`, `--immunogenicity`, `--immuno-strength`, `--serum-stability`,
  `--stability-strength`, `--stability-mode`, `--halflife`, `--halflife-strength`,
  `--charge-mode`, `--charge-value`, `--charge-min`, `--charge-max`,
  `--min-solubility`, `--cyclic-mode`, `--cyclic-strength`, `--strict-recombinant`,
  `--dual-fold-viz`.

### Fixed

- `cysteine_mode` was previously absent from server request body. Combined with the
  server-side dead-wire fix in `modal_workers/ligandforge_v6_5.py`, the SDK now
  correctly forwards the cysteine placement policy end-to-end. Requesting
  `num_peptides=N` returns exactly N peptides regardless of cysteine policy
  (rejection-sampling backpressure on the server side).

### Output fields

`Peptide` now exposes: `stability_scores` (halflife, cleavage risk, grade, protease
site counts), `immuno_scores` (risk score, grade, epitope counts by class,
population coverage), and `cyclic_mode` (which cyclization constraint was active).

## [0.1.8] - 2026-04-30

### Fixed

- Treat legacy LigandIQ `quality_scores.predicted_ptm` as `Peptide.predicted_iptm`
  only. The current LigandIQ quality head does not emit a distinct predicted pTM
  value, so `Peptide.predicted_ptm` now remains unset for that legacy alias.

## [0.1.7] - 2026-04-30

### Fixed

- Expose legacy LigandIQ `quality_scores.predicted_ptm` values as
  `Peptide.predicted_iptm` when explicit `predicted_iptm` is absent, matching
  production where Modal `pred_iptm` was normalized to `predicted_ptm`.

## [0.1.6] - 2026-04-30

### Fixed

- Treat generation-only PTF `generation_complete` jobs as successful terminal
  SDK jobs and hydrate sparse status payloads from `/api/ptf/sessions/{id}`.
- Parse generation-only session peptide dictionaries keyed by gene.
- Expose generation-time `predicted_ptm` and `predicted_plddt` separately from
  folded `iptm`/`plddt`; do not map `predicted_ptm` into `predicted_iptm`.

## [0.1.0] - 2025

### Added

- Initial public release.
- Sync (`LigandAI`) and async (`AsyncLigandAI`) clients.
- Tier detection from API key prefix (`lgai_free_*`, `lgai_edu_*`,
  `lgai_pro_*`, `lgai_ent_*`, `lgai_sa_*`). No network call required.
- Twelve resource namespaces:
  - `account`, `receptors`, `structures`, `proteins`, `discovery`,
    `diseases`, `peptides`, `bivalent`, `synthesis`, `memory`,
    `programs`, `charts`, `reports`, `jobs`.
- `Job` / `AsyncJob` polymorphic abstractions for long-running operations
  with `.wait()`, `.poll()`, `.cancel()`, `.stream()` (SSE).
- Typed exception hierarchy: `LigandAIError`, `LigandAIAuthError`,
  `LigandAITierError`, `LigandAIRateLimitError`, `LigandAICreditError`,
  `LigandAINotFoundError`, `LigandAIServerError`, `LigandAIValidationError`,
  `LigandAIJobError`, `LigandAITimeoutError`, `NotSupportedOnReceptorDB`.
- Pydantic v2 models for every documented request/response field.
- `httpx` transport with auto-retry on 429/5xx via `tenacity`,
  exponential backoff, and `Retry-After` / `X-RateLimit-Reset` parsing.
- `ReceptorDBClient` and `AsyncReceptorDBClient` with the read-mostly
  ReceptorDB subset; raises `NotSupportedOnReceptorDB` for endpoints
  outside the subset.
- Tier feature-gating raised client-side (no round-trip) for known features.
- Six example scripts in `examples/`.
- Sphinx docs scaffold (`docs/`).

### Server endpoint mapping

The SDK targets `/api/*` routes (NOT `/v1/*`). Express's `isAuthenticated`
middleware accepts either a session cookie OR `Authorization: Bearer lgai_*`
on every `/api/*` route, so the SDK uses Bearer auth uniformly.

The `/v1/*` enterprise routes have feature gaps (no bivalent, no synthesis
cart, no charts) and use an in-memory `apiKeyStore` map rather than the
canonical Drizzle `apiKeys` table. See `wiki/synthesis/api_endpoints_complete_catalog.md:498`
for details.
