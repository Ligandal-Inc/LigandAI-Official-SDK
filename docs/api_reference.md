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
| `/api/v1/peptides/auto-generate-until` | POST | All (free = masked) | `client.peptides.fill_until(...)` |
| `/api/v1/structures/:pdbId/pocket` | GET | All | `client.peptides.pocket_for_hotspots(...)` |
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

Cross-program peptide search with the full filter set the workspace UI
exposes. `program_id` is optional; omit to search all your programs.
All criteria AND-combine. v0.5.1+.

**Score thresholds** (any subset, optional):
- `ipsae_min`, `iptm_min`, `pldd_min` _(float, 0..1)_
- `kd_max` _(float, M, e.g. `1e-7` for ≤ 100 nM)_
- `dg_max` _(float, kcal/mol — negative is better; pass `-8.0` for ≤ -8)_
- `binder_pct_min` _(float, 0..1 — legacy DeltaForge binder probability when present)_
- `length_min`, `length_max` _(int, residues)_

**Combined gates**:
- `is_elite=true` — server-flagged elite (default iPSAE ≥ 0.80)
- `super_elite=true` — **STRUCTURAL** Proteina-Complexa gate
  (bioRxiv v27): iPSAE ≥ 0.67 AND iPTM ≥ 0.80 AND pLDDT ≥ 88
  (0–100 scale; null passes). The 3-metric structural-confidence gate.
- `super_elite_affinity=true` — **AFFINITY** super-elite: structural gate
  AND predicted Kd < 100 nM (DeltaForge). Synthesis-priority subset.
  Reported as a SEPARATE bucket from the structural gate.

DeltaForge scoring returns affinity (`dg`, `kd_nm`) separately from the
structure/energy binder call (`predicted_binder`, `predicted_binder_call`,
`predicted_non_binder_reasons`). A complex can therefore retain a predicted Kd
while still being called `not_binder` by the joint structural gate.

**Hotspot/pocket coverage** (uses migration 085 `peptide_residue_contacts`):
- `hotspot_residues=A:60,A:62` — chain:resi list (PDB numbering, comma-sep)
- `pocket_residues=A:55,A:56,A:67`
- `hotspot_hit=true` — require contact with ANY listed hotspot residue
- `pocket_hit=true` — require contact with hotspot OR pocket
- `contact_distance_a=5.0` — heavy-atom cutoff for "hit" (default 5.0 Å)

**Categorical**:
- `gene=KIT` — exact gene match
- `conformation=monomer_C` — exact conformation match
- `stability_grade=A,B` — pipe/comma list of acceptable grades
- `immuno_grade=A,B` — same

**Scope**:
- `program_id=42` — restrict to one program's sessions
- `session_id=session_parallel_…` — single session
- `pdb_id=9MIR` — pdbId-scoped (matches `pocket_metadata.pdb_id`)

**Pagination + sort**:
- `limit` _(int, max 200)_, `offset` _(int)_
- `sort=ipsae|iptm|plddt|kd|dg|length|created_at` — default `ipsae`
- `order=asc|desc` — default `desc`

Each returned peptide additionally exposes:
- `hotspot_contacts: [{chain, residue, distance_a}]` — when
  `hotspot_residues` was specified
- `pocket_contacts: [{chain, residue, distance_a}]` — when
  `pocket_residues` was specified
- `binder_pct`, `stability_grade`, `immuno_grade` — passthrough

The response also echoes back the resolved criteria in `criteria: {...}`
so the SDK can cache key the response.

### `POST /api/v1/peptides/auto-generate-until`

Plan (or kick off) a generate-and-fold loop until N peptides match
arbitrary criteria. v0.5.1+.

Body (JSON):
```json
{
  "gene": "BMPR1A",
  "target_count": 25,
  "criteria": {
    "super_elite": true,
    "hotspot_residues": ["A:60", "A:62"],
    "hotspot_hit": true
  },
  "batch_size": 100,
  "max_iterations": 5,
  "budget_credits_max": 50000,
  "mode": "plan"
}
```

`mode: "plan"` (default) returns:
```json
{
  "current_passing_count": 12,
  "target_count": 25,
  "remaining": 13,
  "plan": {
    "batches_recommended": 3,
    "batch_size": 100,
    "total_peptides_to_generate": 300,
    "est_credits": 30000,
    "est_minutes": 21,
    "pass_rate_assumed": 0.05,
    "strict_criteria": true,
    "budget_ok": true
  },
  "criteria": { ... },
  "gene": "BMPR1A"
}
```

`mode: "start"` validates against `budget_credits_max` and returns
`next_action` with the exact `/api/ptf/parallel/generate` calls to
make. The client iterates and re-checks via `peptides.search()`.
Empirical pass-rate guess: 5% for strict criteria (super_elite OR
hotspot_hit OR explicit residue list), 25% otherwise.

### `GET /api/v1/structures/:pdbId/pocket`

Compute the pocket residues within `radius_a` Å of one or more
hotspots. v0.5.1+.

Query params:
- `hotspots=A:60,A:62` _(required, chain:resi PDB numbering)_
- `radius_a=8.0` _(float, default 8.0, range 2.0–20.0)_
- `session_id=session_parallel_…` _(optional — compute against the
  session's fold structure instead of canonical PDB)_

Returns:
```json
{
  "pdb_id": "9MIR",
  "session_id": null,
  "hotspots": [{"chain":"A","residue":60}, {"chain":"A","residue":62}],
  "radius_a": 8.0,
  "pocket_residues": [
    {"chain":"A","residue":55,"resname":"VAL","distance_a":4.1},
    {"chain":"A","residue":56,"resname":"LYS","distance_a":5.7}
  ],
  "n_pocket_residues": 12
}
```

Multi-hotspot input is unioned with closest-distance preference per
residue.

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
