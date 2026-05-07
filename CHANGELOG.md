# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

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
    `/home/dre/.claude/projects/-home-dre/memory/feedback_api_paid_only.md`:
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
