# API Reference

Every public REST endpoint exposed by the LigandAI platform that the SDK
calls into, organized by resource. Each endpoint table lists required tier,
parameters, return shape, and error codes. The SDK column shows the typed
Python method that wraps the call.

This page is the source of truth for SDK ↔ HTTP mapping. When you hit an
unexpected response in production, find the row and confirm tier + params.

> **Authentication.** All requests require an `lgai_*` API key in either
> the `Authorization: Bearer ...` header or `X-API-Key` header. Mint a
> key at <https://ligandai.com/account/billing?tab=api-keys>.
>
> **Tier names.** Tiers are normalized lowercase: `free`, `basic`,
> `academia`, `pro`, `pro_commercial`, `discovery_partner`, `enterprise`,
> `superadmin`. Free includes academia for read-only endpoints; for paid-
> only endpoints, academia is treated as free.

## Programs

| Endpoint | Method | Tier | SDK |
|----------|--------|------|-----|
| `/api/v1/programs` | GET | All | `client.programs.list()` |
| `/api/v1/programs/:id` | GET | All | `client.programs.get(id)` |
| `/api/v1/programs` | POST | Paid | `client.programs.create(...)` |

### `GET /api/v1/programs`

Returns the caller's PTF programs with **live peptide counts**. Computes
`peptide_count`, `folded_count`, and `elite_count` via JOIN over
`ptf_generated_peptides` and `ptf_fold_results` rather than the
denormalized columns (which are unreliable).

Query params:
- `status` _(optional, string)_ — filter by program status (`active`,
  `paused`, `archived`, `completed`).

Returns:
```json
{
  "programs": [
    {
      "id": 42,
      "programId": "uuid",
      "name": "EGFR campaign",
      "peptide_count": 287,
      "folded_count": 130,
      "elite_count": 18,
      "peptideCount": 287,
      "foldedCount": 130,
      "eliteCount": 18,
      "status": "active",
      "createdAt": "2026-04-01T...",
      ...
    }
  ]
}
```

Error codes: `E001` (auth), `E429` (rate limit).

### `GET /api/v1/programs/:id`

Returns one program plus its child projects, with the same live counts as
the list endpoint computed for the single program.

Error codes: `E001`, `404` (program not found).

## Peptides

| Endpoint | Method | Tier | SDK |
|----------|--------|------|-----|
| `/api/v1/peptides/by-gene` | GET | Paid | `client.peptides.by_gene(...)` |
| `/api/v1/peptides/list` | GET | All (free = masked) | `client.peptides.list(...)` |
| `/api/v1/peptides/list_by_program` (alias of list) | GET | All (free = masked) | `client.peptides.list_by_program(...)` |
| `/api/v1/peptides/search` | GET | All (free = masked) | `client.peptides.search(...)` |
| `/api/v1/peptides/:id` | GET | Paid | `client.peptides.get(id, ...)` |

### `GET /api/v1/peptides/list`

The endpoint Andrew Keene needed: list peptides by program_id. v0.5.0+.

Query params:
- `program_id` _(int, optional)_ — restrict to one program (Layer-4 db id).
- `gene` _(string, optional)_ — filter to one gene symbol.
- `min_ipsae` _(float)_ — folds with iPSAE ≥ value.
- `min_iptm` _(float)_ — folds with ipTM ≥ value.
- `max_kd` _(float, M)_ — folds with `predicted_kd` ≤ value.
- `limit` _(int, max 200)_, `offset` _(int)_ — pagination.

Returns:
```json
{
  "peptides": [
    {
      "peptide_id": 12345,
      "fold_id": 12345,
      "id": 12345,
      "gene": "EGFR",
      "sessionId": "...",
      "sequence": "ACDEFGHIKL...",
      "length": 25,
      "ipsae": 0.91, "iptm": 0.87, "ptm": 0.79, "plddt": 84.5,
      "deltaG": -32.4,
      "predictedKd": 1.8e-9,
      "isElite": true,
      "createdAt": "2026-04-01T...",
      "_masked": false
    }
  ],
  "total": 287,
  "limit": 50,
  "offset": 0,
  "_tier": "pro",
  "_tier_redacted": false,
  "_upgrade_url": null
}
```

When `_tier_redacted: true` (free tier), `sequence` is the first 10 amino
acids followed by `********`, and `_masked: true` is set per row.

Error codes: `E001` (auth), `E429` (rate limit).

### `GET /api/v1/peptides/search`

Same response shape as `/peptides/list` but tuned for cross-program
filtering. `program_id` is optional; omit to search all your programs.

Query params:
- `ipsae_min` _(float)_, `iptm_min` _(float)_, `kd_max` _(float, M)_.
- `gene` _(string, optional)_ — filter by gene.
- `program_id` _(int, optional)_ — scope to one program.
- `limit` _(int, max 200)_, `offset` _(int)_.

### `GET /api/v1/peptides/:id`

Single-peptide detail. Paid tier only — free keys receive 402 with
`error: "upgrade_required"`. Use `?include=` to load heavy fields:
- `?include=pocket_features` — adds 48-dim pocket features.
- `?include=interface` — adds peptide-per-receptor + disulfide analysis.
- `?include=pdb` — adds full PDB content.

Error codes: `E001`, `E014` (TOS not accepted), `402 upgrade_required`,
`404 peptide_not_found`.

## Structures

| Endpoint | Method | Tier | SDK |
|----------|--------|------|-----|
| `/api/v1/structures/list` | GET | All (free = limited) | `client.structures.list(...)` |
| `/api/v1/structures/:id/pdb` | GET | All (free = polyalanine) | `client.structures.get_pdb(id)` |
| `/api/v1/structures/fetch` | POST | Paid | `client.structures.get(gene)` |
| `/api/v1/structures/batch` | POST | Paid | _(bulk fetch)_ |

### `GET /api/v1/structures/list`

List folded structures (metadata only). Use `get_pdb(structure_id)` for
the actual atomic content.

Query params:
- `program_id` _(int, optional)_ — scope to one program.
- `limit` _(int, max 200)_, `offset` _(int)_.

Returns:
```json
{
  "structures": [
    {
      "structure_id": 9001,
      "fold_id": 9001,
      "id": 9001,
      "gene": "EGFR",
      "ipsae": 0.91, "iptm": 0.87, "ptm": 0.79, "plddt": 84.5,
      "isElite": true,
      "createdAt": "2026-04-01T...",
      "pdb_url": "/api/v1/structures/9001/pdb"
    }
  ],
  "total": 130,
  "_tier": "pro",
  "_tier_redacted": false
}
```

### `GET /api/v1/structures/:id/pdb`

Returns the PDB text body. Free tier sees polyalanine (sidechains
stripped, `REMARK   1` redaction header inserted at top); paid tier sees
the full atomic data.

Response headers expose the redaction state:
- `X-LigandAI-Tier: free|basic|pro|...`
- `X-LigandAI-Tier-Visibility: full|redacted`

Error codes: `E001`, `404 structure_not_found`.

## Folding

| Endpoint | Method | Tier | SDK |
|----------|--------|------|-----|
| `/api/v1/folding/predict` | POST | Paid + credits | `client.peptides.fold(...)` |
| `/api/v1/folding/jobs/:jobId` | GET | Paid | `job.refresh()` / `job.wait()` |
| `/api/v1/folding/batch` | POST | Paid | `client.peptides.fold_batch(...)` |
| `/api/v1/folding/jobs/:jobId` | DELETE | Paid | `job.cancel()` |

Error codes: `E001`, `E014`, `E429`, `402 upgrade_required`,
`402 insufficient credits` (returned with `error: "insufficient_credits"`,
not `upgrade_required`).

## Transcriptomics

| Endpoint | Method | Tier | SDK |
|----------|--------|------|-----|
| `/api/v1/transcriptomics/tissues` | GET | Paid | `client.discovery.tissues()` |
| `/api/v1/transcriptomics/expression/:gene` | GET | Paid | `client.discovery.expression(gene)` |
| `/api/v1/transcriptomics/analyze` | POST | Paid + credits | `client.discovery.analyze(...)` |

## Account / Billing

| Endpoint | Method | Tier | SDK |
|----------|--------|------|-----|
| `/api/v1/auth/verify` | GET | All | `client.tier`, `client.email` |
| `/api/v1/credits` | GET | Paid | `client.credits`, `client.account.credits()` |

## CSV Exports (UI / browser auth — not API key)

These endpoints serve the dashboard CSV download buttons. They require a
session cookie, not an API key, but they honor tier-based redaction in
the same way as the SDK endpoints:

- `GET /api/user/results/export?format={csv,fasta,pdb}` — bulk fold results.
- `POST /api/design-studio/download/csv` — design studio peptide table.
- `GET /api/design/projects/:projectId/export` — full project ZIP
  (includes `_TIER_INFO.json` describing redaction state).

Free tier responses: sequences truncated to first 10 amino acids +
`********`; PDB structures redacted to polyalanine; `tier_visibility`
column appended; `X-LigandAI-Tier`/`X-LigandAI-Tier-Visibility` headers
set.

## Tier-Redaction Markers (machine-readable)

When the server returns potentially redacted data, the JSON response
includes:

```json
{
  "...": "...",
  "_tier": "free",
  "_tier_redacted": true,
  "_upgrade_url": "https://ligandai.com/pricing"
}
```

`_tier_redacted: true` means at least one field in the response was
truncated or scrambled due to the caller's tier. Programmatic SDK clients
should check this flag; free-tier callers should treat sequences and PDBs
as previews and prompt the user to upgrade for full data.

CSV/PDB exports also set the response headers:
- `X-LigandAI-Tier`
- `X-LigandAI-Tier-Visibility: full | redacted`
