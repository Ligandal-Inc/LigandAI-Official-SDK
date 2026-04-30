# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

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
