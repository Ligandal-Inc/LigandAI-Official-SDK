# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-05-01

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
