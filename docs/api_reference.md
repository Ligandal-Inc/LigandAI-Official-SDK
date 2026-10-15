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
| `/api/ptf/parallel/generate` | POST | All (limits vary) | `client.peptides.generate(...)` |
| `/api/v1/peptides/by-gene` | GET | Paid | `client.peptides.by_gene(...)` |
| `/api/v1/peptides/list` | GET | All (free = masked) | `client.peptides.list(...)` |
| `/api/v1/peptides/list_by_program` (alias of list) | GET | All (free = masked) | `client.peptides.list_by_program(...)` |
| `/api/v1/peptides/search` | GET | All (free = masked) | `client.peptides.search(...)` |
| `/api/v1/peptides/auto-generate-until` | POST | All (free = masked) | `client.peptides.fill_until(...)` |
| `/api/v1/structures/:pdbId/pocket` | GET | All | `client.peptides.pocket_for_hotspots(...)` |
| `/api/v1/peptides/:id` | GET | Paid | `client.peptides.get(id, ...)` |

### `POST /api/ptf/parallel/generate` — residue targeting fields (`bd-LIGANDAI_ALPHA_V2-x4vt8`)

`target_residues`, `avoid_residues`, and `hotspot_residues` are **PDB residue
numbers as numbered in the uploaded/resolved structure** — never 0-based
indices into the pocket-feature array, and never sequence positions.
`chain` is part of a residue's identity. This is validated **client-side**
(`ligandai._targeting`, called from `Peptides.generate()` /
`AsyncPeptides.generate()` / `Goals.campaign_start()` / `Goals.drive()`)
before any HTTP call — a `start`/`end`/`residue` `< 1` raises
`LigandAIValidationError` immediately rather than reaching the server.

| SDK kwarg | Wire key(s) | Shape |
|---|---|---|
| `target_residues` | `targets[0].targetResidues` | `[{chain, start, end}, ...]` — canonical |
| `avoid_residues` | `avoidRegions` | `[{chain, start, end, startResidue, endResidue}, ...]` — both canonical and legacy Express-facing keys are sent |
| `hotspot_residues` | `hotspotResidues` / `hotspot_residues` | flat `[int, ...]` (PDB resnums; `{chain, residue}` dicts also accepted client-side, normalized to ints) |
| `avoid_shell_angstrom` | `avoidShellAngstrom` / `avoid_shell_angstrom` | `float`, range `[0, 50]`; server default `8.0` when omitted |

`avoid_residues` is applied **in every targeting mode** — `target_residues` /
`hotspot_residues` are not required for it to reach the featurization. By
default the server also removes the residues packing against each named one
(within `avoid_shell_angstrom`, Cα-Cα); pass `avoid_shell_angstrom=0` to
remove only the literally-named residues.

⚠ **These parameters do not place a peptide.** `avoid_residues` was measured to
have no effect on generation at all (`p = 1.00`, JSD `2.4e-5` between arms) and
does not keep designs off the named surface. `hotspot_residues` does change
which sequences are generated (`p ≈ 1e-4` across six receptor faces) but does
not aim at the named site. Determine where a design binds from the folded
complex — `client.evaluator.filter_folds(...)` with
`required_interface_residues`, or the `avoid_conformance_*` fold diagnostics.

The generation SSE stream (`job.stream()`, `sse_path=/api/ptf/parallel/{id}/stream`)
surfaces `target_residues_applied`, `avoid_residues_applied`, and
`targeting_warning` events with their real event names (previously these
collapsed into a generic `"message"` event — see CHANGELOG `[Unreleased]`). A
`targeting_warning` is also mirrored to `warnings.warn(...,
ligandai.LigandAITargetingWarning)` so it's visible even via `job.wait()`.

### `POST /api/ptf/parallel/generate` — contrastive (paralog-conditioned) guidance

See [contrastive](contrastive.md) for the full picture, including the measured
head-to-head result. This section is the wire-level reference.

| SDK kwarg | Wire key(s) | Shape |
|---|---|---|
| `avoid_paralogs` | `cfg_reference_genes` / `cfgReferenceGenes` | `list[str]` — HGNC gene symbols, uppercase-normalized, deduplicated, max 8 |
| `contrastive` (`ContrastiveGuidance`) | `reference_pocket_features` / `referencePocketFeatures` | `list[list[list[float]]]` — one 2-D `[n_residues][dim]` matrix per reference. **Entitled/internal**: `reference_pocket_features` exists as a request field, but the pocket widths it requires (107 for `v6.9.21`, 65 for `v6.9.22`) are not public — see `shared/design-surface-gate.ts` (`PUBLIC_POCKET_DIMS = {48}`), which 403s (`POCKET_DIM_INTERNAL`) anything else. `avoid_paralogs` is the supported way to reach the same capability without that entitlement. |

Only valid when the resolved generator's `inference_config.cfg_reference ==
"paralog_mean"` (the v6.9 line). Supplying either field to a model outside
that line is refused (`cfg_references_not_supported`). Supplying **both**
`avoid_paralogs` and `contrastive=` on the same call is refused
(`cfg_reference_genes_with_explicit_features`) — two different ways to name
one panel, and silently preferring one is how a caller ends up steered away
from something they did not choose.

**Provenance.** A run that executed `"paralog_mean"` emits an SSE event on the
generation stream, `cfg_references_resolved`:

```json
{
  "generator_model_id": "v6.9.21",
  "expected_dim": 107,
  "n_references": 1,
  "paralogs": [
    {"paralog_gene": "BRD2", "source_pdb_id": "2E3K", "source_chain": "A",
     "identity_to_target_domain": 0.71, "n_rows": 58}
  ],
  "rule_version": "...",
  "requested_reference_genes": ["BRD2", "BRD3", "BRDT"]
}
```

`requested_reference_genes` is `null` when the platform picked the panel
itself; it echoes the caller's `cfg_reference_genes` otherwise.

This is also persisted on the completed job record as
`cfg_reference_provenance`, so a finished run stays auditable after the SSE
stream closes. Absent provenance on a v6.9-line run means the server build did
not report it — never that the run was unconditioned; `"paralog_mean"`
executes unconditionally on that line.

**Errors:**

| Code | When |
|---|---|
| `missing_nonzero_cfg_references` | No usable reference could be produced — either every named gene in `cfg_reference_genes` failed the paralog criterion (`paralog_reference_reason: "named_references_all_rejected"`, with per-gene rejection reasons), or, with no genes named, the platform's own paralog discovery found nothing defensible for this target. Never falls back to a fabricated or zero-filled reference. |
| `cfg_references_not_supported` | A contrastive reference (named or explicit) was sent to a generator outside the v6.9 line. |
| `cfg_reference_genes_with_explicit_features` | Both `cfg_reference_genes` and `reference_pocket_features` were supplied on one request. |

### `GET /api/v1/peptides/list`

list peptides by program_id. v0.5.0+.

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

### DeltaForge scoring endpoints (`client.deltaforge`)

Auth: `Authorization: Bearer <api_key>`. Scorer selected server-side with
`scorer="auto"`; the model that ran is on `scorer_version`. Credits charged on
SUCCESS only. There is **no** `/api/binder-scoring/deltaforge` path (404) — use
these:

| Method | Endpoint | Body | Notes |
|---|---|---|---|
| `deltaforge.score_pdb` | `POST /api/v1/deltaforge/score-pdb` | `pdbContent`/`pdb_file`, `receptorChains`, `peptideChain`, `scorer`, `aggregateMethod`, `includeFeatures`, `includePae`, `fold*` | score arbitrary PDB |
| `deltaforge.score_fold` | `POST /api/v1/deltaforge/score-fold` | `foldJobId`, `scorer?`, `aggregateMethod?`, `includeFeatures?`, `includePae?`, `receptorChains?`, `peptideChain?` | score an existing fold; metrics auto-forwarded |
| `deltaforge.batch_score_fold` | `POST /api/v1/deltaforge/batch-score-fold` | `foldJobIds: [...]`, `scorer?`, `includePae?` | per-binder results + `errors[]`; `?format=csv` for CSV |

`score_fold` / `batch_score_fold` always return the fold confidence metrics
(`iptm`, `ptm`, `ipsae`, `peptide_ipsae`, `peptide_ipsae_status`, `plddt_mean`)
alongside affinity. For peptide binders **use `peptide_ipsae`** — the
peptide-receptor interface iPSAE — as the binder metric. The overall `ipsae` is
complex-level confidence dominated by receptor-self contacts (not a binder metric);
`iptm`/`ptm` are whole-complex confidence dominated by the receptor chain. When
`peptide_ipsae` is `None`, `peptide_ipsae_status` will be `'not_computed'`
(common for folds run via predict-batch). With `includePae=true`, the NxN PAE matrix
(Angstroms) is attached on `pae` when resolvable; otherwise `pae` is `null` and
`pae_status` is `pending` (a backend pull is in progress) or `unavailable`.

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
| `/api/structure/:gene` | GET | All | `client.structures.get(gene, **opts)` |
| `/api/structure/:gene/isoforms` | GET | All | `client.structures.list_isoforms(gene)` _(v0.6.0+)_ |
| `/api/structure/:gene/species` | GET | All | `client.structures.list_species(gene)` _(v0.6.0+)_ |
| `/api/structure/pdb/:pdbId` | GET | All | `client.structures.from_pdb(pdb_id)` |
| `/api/structure/alphafold/:uniprotId` | GET | All | `client.structures.from_alphafold(uniprot_id)` |

### `GET /api/structure/{gene}` — best structure with optional selection (v0.6.0+)

Resolves the best structure for a gene through the full 6-tier hierarchy
(PDB-with-EC-ligand → Boltz-2 high-quality → standard PDB multimer →
lower-quality Boltz-2/PDB monomer → experimental monomer → AlphaFold).
Backwards-compatible: no kwargs = human default fast path; any kwarg
routes through the full platform resolver.

**Query params (all optional):**

| Param | Type | Description |
|---|---|---|
| `pdb_code` | str | Specific PDB code to fetch (e.g. `6VG2`). Overrides automatic selection. |
| `isoform` | int | Isoform number (e.g. `2` for CLDN18.2). Routed through resolver's UniProt isoform lookup. |
| `species` | str | `human` (default), `mouse`, `rat`, `cyno`, `rhesus`, `pig`, `dog`, `rabbit`, `zebrafish`, `chimp`. Aliases accepted (e.g. `mus_musculus`, `mmu`). |
| `declared_gene_set` | csv | Comma-separated genes for explicit multimer/monomer disambiguation (e.g. `CD8A,CD8B` to force heterodimer; `CD8A` to force monomer). |

**SDK examples:**
```python
# Default (human, single best structure) — unchanged from v0.5.x
struct = client.structures.get("KRAS")

# Specific isoform — CLDN18.2
struct = client.structures.get("CLDN18", isoform=2)

# Specific PDB code
struct = client.structures.get("KRAS", pdb_code="6VG2")

# Cross-species — mouse KRAS (only when explicitly requested)
struct = client.structures.get("KRAS", species="mouse")

# Force monomer vs heterodimer
struct = client.structures.get("CD8A", declared_gene_set=["CD8A"])
struct = client.structures.get("CD8A", declared_gene_set=["CD8A", "CD8B"])
```

The response includes a `requested` block echoing the params honored.

### `GET /api/structure/{gene}/isoforms` (v0.6.0+)

Enumerate UniProt isoforms for a gene.

```python
isoforms = client.structures.list_isoforms("CLDN18")
# → [
# {"id": "P56856", "name": "CLD18_HUMAN", "sequence_length": 261, "is_canonical": True, "isoform_number": 1},
# {"id": "P56856-1", "name": "A1", "sequence_length": None, "is_canonical": False, "isoform_number": 1},
# {"id": "P56856-2", "name": "A2", "sequence_length": None, "is_canonical": False, "isoform_number": 2},
# ]
```

Returns `{gene, isoforms: [...], count}`. Empty list if the gene has no
reviewed UniProt entries.

### `GET /api/structure/{gene}/species` (v0.6.0+)

Enumerate species in which this gene has a reviewed UniProt entry.

```python
species = client.structures.list_species("KRAS")
# → [
# {"taxid": 9606, "species": "human", "organism_name": "Homo sapiens", "common_name": "Human", "accession": "P01116"},
# {"taxid": 10090, "species": "mouse", "organism_name": "Mus musculus", "common_name": "Mouse", "accession": "P32883"},
# {"taxid": 10116, "species": "rat", "organism_name": "Rattus norvegicus", "common_name": "Rat", "accession": "P08644"},
# ]
```

Use the returned `species` value as the `species=` kwarg on `get()`.

### Tier-upgrade-aware key authentication (v0.6.0+)

`validateAPIKey` now resolves the effective tier as
`max(key_prefix_tier, user.subscription_tier)`. An API key minted at a
lower tier prefix (e.g. `lgai_pro_*`) that belongs to an upgraded
enterprise account will now grant **enterprise** privileges — no more
401s after a tier upgrade. The key prefix is a *hint*, not a ceiling.


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
stripped, `REMARK 1` redaction header inserted at top); paid tier sees
the full atomic data.

Response headers expose the redaction state:
- `X-LigandAI-Tier: free|basic|pro|...`
- `X-LigandAI-Tier-Visibility: full|redacted`

Error codes: `E001`, `404 structure_not_found`.

## Folding

| Endpoint | Method | Tier | SDK |
|----------|--------|------|-----|
| `/api/folding/predict` | POST | Paid + credits | `client.peptides.fold(...)` |
| `/api/v1/folding/predict-batch` | POST | Paid | `client.peptides.fold_batch(...)` |
| `/api/folding/jobs/:jobId` | GET | Paid | `job.refresh()` / `job.wait()` |
| `/api/folding/jobs/:jobId` | DELETE | Paid | `job.cancel()` |
| `/api/folding/jobs/:jobId/logs/stream` | GET (SSE) | Paid | `job.stream()` |

Note the version prefixes: `predict-batch` is the only folding route under
`/api/v1`; submission, polling, cancellation and log streaming are all on the
unversioned `/api/folding` path. Earlier revisions of this table listed
`/api/v1/folding/predict` and `/api/v1/folding/batch` — neither is called by
this SDK.

Those two `/api/v1` routes do exist server-side as legacy enterprise endpoints,
but they accept a bare sequence with no receptor and therefore reject
`model="boltz2"` with `400 BOLTZ2_REQUIRES_RECEPTOR`. Boltz-2 predicts a
peptide:receptor complex, so it is only reachable through `fold_batch(...)`.

Error codes: `E001`, `E014`, `E429`, `402 upgrade_required`,
`402 insufficient credits` (returned with `error: "insufficient_credits"`,
not `upgrade_required`).

## Linker modifications + Mode B payload optimization (pro+ / academia+)

| Endpoint | Method | Tier | SDK |
|----------|--------|------|-----|
| `/api/v1/linker_modifications/uaa_palette` | GET | Authenticated | `client.linker_modifications.list_uaa_palette()` |
| `/api/v1/linker_modifications/fold` | POST | Pro+ | `client.linker_modifications.fold_with_linker_mods(...)` |
| `/api/v1/payload_optimization/libraries` | GET | Pro+ | `client.linker_modifications.list_payload_libraries()` |
| `/api/v1/payload_optimization/libraries/:key` | GET | Pro+ | `client.linker_modifications.get_payload_library(key)` |
| `/api/v1/payload_optimization/runs` | POST | Pro+ | `client.linker_modifications.optimize_payload(...)` |
| `/api/v1/payload_optimization/runs/:runId` | GET | Pro+ | `client.linker_modifications.get_payload_run(run_id)` / `wait(run)` |

Dataclasses exposed at the top level: `LinkerModification`, `CovalentAttachment`, `ReceptorChain`, `PayloadFilter`, `PayloadOptimizationRun`. Errors: `422 mw_verification_failed` (per-row rejection list in `.detail`), `403 pro tier required`.

## Transcriptomics

| Endpoint | Method | Tier | SDK |
|----------|--------|------|-----|
| `/api/v1/transcriptomics/tissues` | GET | Paid | `client.discovery.tissues()` |
| `/api/v1/transcriptomics/expression/:gene` | GET | Paid | `client.discovery.expression(gene)` |
| `/api/v1/transcriptomics/analyze` | POST | Paid + credits | `client.discovery.analyze(...)` |

## Account / Billing

| Endpoint | Method | Tier | SDK |
|----------|--------|------|-----|
| `/api/auth/user` | GET | All | `client.account.me()` — id, email, tier, organization |
| `/api/credits` | GET | All | `client.credits`, `client.account.credits()` |

`client.tier` makes **no HTTP call at all** — it is derived locally from the API-key
prefix at construction time (`_detect_tier()`, `ligandai/client.py:328`). Reading it
never reaches the server, so it cannot be used to check that a key is live.

> **`GET /api/v1/auth/verify` is not the way to get your identity, and this table
> used to say it was.** That endpoint does exist and does return
> `{valid, user{id, email, tier, organization}}`, but no SDK method calls it, and it
> is a dead end for discovering the rest of the API — it was the only `/api/v1` path
> this table named, so a reader who started there found no next step for folding or
> generation and stopped. To check a key is live, use `client.account.me()`
> (`GET /api/auth/user`). To discover the whole API-key surface, fetch the served
> OpenAPI 3 spec: `GET https://ligandai.com/api/v1/openapi.json` with your API key.

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

## Diagnostics (`job.diagnostics`) — bd-LIGANDAI_ALPHA_V2-x4vt8.3.2

A customer excluded surface residues on their target. The exclusion was
silently a no-op and 25/25 designed peptides bound the excluded region.
Nothing in the product told them. Diagnostics are the fix for the SILENCE,
and they are machine-readable so you can gate on them in CI instead of
eyeballing structures.

### The envelope

Every diagnostic — over SSE, over REST, or raised client-side — is the same
snake_case object:

```json
{
  "schema_version": 1,
  "code": "avoid_residues_none_resolved",
  "severity": "info | warning | error",
  "stage": "request | generate | fold | results | client",
  "title": "None of your excluded residues were found",
  "message": "None of the 11 residues you asked to avoid were found on BRD4.",
  "remediation": "Re-run with residue numbers taken from the structure itself.",
  "details": { "requested": 11, "unresolved_resnums": [36, 37, 38] },
  "doc_anchor": "sdk/python/allosteric-targeting#event-avoid",
  "occurred_at": "2026-08-04T18:22:01Z",
  "subject_id": "BRD4"
}
```

`code` is the join key across FastAPI, Express, both SDKs, and the website.
`doc_anchor` is relative — the full URL is
`https://ligandai.com/docs/<doc_anchor>`. In Python the envelope is
`ligandai.Diagnostic`; `d.to_dict()` returns exactly the JSON above.

**Unknown codes are never filtered.** A code this SDK build has never seen
reaches you in full, with `d.is_known_code == False` and any unrecognized
envelope keys preserved on `d.raw`. An SDK that dropped what it did not
recognize would reintroduce the exact defect this exists to close.

### Where they show up

| Transport | How the SDK surfaces it |
| --- | --- |
| SSE `event: diagnostic` | `job.stream()` yields a `JobEvent` with `event_type == "diagnostic"`, **and** the envelope lands on `job.diagnostics` |
| REST `diagnostics: [...]` on any job/results body | picked up by `job.refresh()` / `job.wait()` / `job.results`, so a `wait()`-only caller sees the same thing |
| Legacy `target_residues_applied` / `avoid_residues_applied` / `targeting_warning` | normalized locally into the same envelopes, so an older server still surfaces the problem |
| Client-side pre-flight (before any HTTP call) | `TargetingValidationResult.diagnostics` (`stage == "client"`) |
| `X-LigandAI-SDK-*` response headers | `sdk_outdated` / `sdk_unsupported` / `sdk_version_unknown` |

Severity `warning` and above additionally raises on the `warnings` channel,
so a caller who never reads `job.diagnostics` is still told. `info` never
warns — it is still recorded on `job.diagnostics`. Repeats do not re-warn:
a polling `wait()` loop warns once.

The category follows the code family. Residue-targeting codes (`avoid_*`,
`target_*`, `targeting_*`, `degenerate_*`) raise `LigandAITargetingWarning`;
everything else raises `LigandAIDiagnosticWarning`. The first subclasses the
second, so **one filter on `LigandAIDiagnosticWarning` catches every platform
diagnostic**, and anything catching `LigandAITargetingWarning` today keeps
working unchanged.

### Reading them

```python
job = client.peptides.generate(
    gene="BRD4",
    avoid_residues=[{"chain": "A", "start": 36, "end": 46}],
)
job.wait()

for d in job.diagnostics:
    print(d.severity, d.code, d.message, d.remediation, d.details)

# Gate a pipeline on it — this is the check the customer never had:
if job.diagnostics_at_least("error"):
    raise SystemExit("targeting did not do what was asked")

job.worst_diagnostic_severity   # "error" | "warning" | "info" | None
```

Escalate the warning channel to an exception instead:

```python
import warnings
from ligandai import LigandAIDiagnosticWarning

warnings.simplefilter("error", LigandAIDiagnosticWarning)
```

### Code registry

`ligandai.DIAGNOSTIC_CODES` is the taxonomy this build knows;
`ligandai.known_codes()` lists them. It is a byte-identical copy of the
platform registry (`shared/warnings.json`, also read by the FastAPI and
TypeScript layers) — copied, not imported, so the SDK carries no dependency on
the server repo, with a test that fails the build if the two drift. Adding a
code is additive; renaming or removing one is a breaking wire change.

| code | severity | stage |
| --- | --- | --- |
| `avoid_residues_unresolved` | warning | generate |
| `avoid_residues_none_resolved` | **error** | generate |
| `target_residues_unresolved` | warning | generate |
| `target_residues_none_resolved` | **error** | generate |
| `degenerate_pocket_after_avoid` | **error** | generate |
| `avoid_conformance_violation` | warning | fold |
| `avoid_conformance_clean` | info | fold |
| `avoid_conformance_unavailable` | info | fold |
| `param_ignored` | warning | request |
| `param_dropped_in_transport` | warning | request |
| `sdk_outdated` | warning | client |
| `sdk_unsupported` | **error** | client |
| `sdk_version_unknown` | info | client |
| `targeting_region_swapped` | info | client |
| `targeting_chain_ambiguous` | warning | client |
| `avoid_shell_disabled` | info | client |
| `degenerate_pocket_risk` | warning | client |
| `targeting_contradiction` | warning | client |

The last five (`stage: client`) are raised by the SDK itself before any HTTP
call — but they are registered centrally like every other code, so the website
renders them from the same taxonomy.

## SDK version compatibility

`GET /api/sdk/compatibility` — no tier required.

The platform advertises SDK compatibility on **every** response, so the
check costs no extra round-trip:

| Header | Values |
| --- | --- |
| `X-LigandAI-SDK-Status` | `current` \| `outdated` \| `unsupported` \| `unknown` |
| `X-LigandAI-SDK-Latest` | newest published SDK version |
| `X-LigandAI-SDK-Min-Supported` | oldest version the server still vouches for |

Anything other than `current` produces an `sdk_outdated` /
`sdk_unsupported` / `sdk_version_unknown` diagnostic, with the exact upgrade
command in `details["upgrade_command"]`, and (for `warning` and above) a
`LigandAIVersionWarning` — **once per process**. This matters because an SDK
build old enough to predate a wire change is exactly the client that
silently drops a request parameter.

The SDK **never upgrades itself**. Opt out with
`LIGANDAI_SKIP_VERSION_CHECK=1` (shared with the PyPI update nag) or
`LIGANDAI_SKIP_SERVER_VERSION_CHECK=1`. For an explicit on-demand check:

```python
for d in client.check_compatibility():
    print(d.code, d.message, d.details["upgrade_command"])
```

Returns `[]` when the client is current, and also `[]` against a server
that has no such endpoint.

## Receptor construct: the domain, not the whole protein

**The receptor is whatever sequence you put on the receptor entity.** Nothing infers a
domain for you, and a gene name does not select one.

If you submit a job with only a gene name and no explicit receptor sequence, the gene
resolves to the **canonical full-length** sequence. For a BET family target that is
947 residues (BRDT) or 1362 (BRD4) instead of the 67-residue ET domain you probably
meant. The fold succeeds and the structure is valid — the **interface scores become
uninterpretable**, because ipTM and ipSAE are computed over the whole complex and a
full-length receptor is dominated by inter-domain linker that has nothing to do with
the binding site.

Measured on BRD folds, stratified by user so construct is not confounded with design
quality:

| construct | mean ipTM | mean peptide ipSAE |
|---|---|---|
| isolated domain (67–69 aa) | 0.75–0.82 | **0.38–0.52** |
| full-length (726–1362 aa) | 0.18–0.32 | **0.002–0.006** |

peptide ipSAE at or near zero on full-length for every user who has tried it. A low
score there is evidence about the construct, not about the design.

### Submit both chains explicitly

```json
POST /api/folding/batch
{
  "jobs": [{
    "entities": [
      { "type": "protein", "chainId": "A",
        "sequence": "<isolated domain sequence>", "geneName": "Brd4ET" },
      { "type": "protein", "chainId": "B",
        "sequence": "<your peptide>", "isPeptide": true, "use_msa": false }
    ]
  }],
  "model": "boltz2", "numTrajectories": 4, "samplingSteps": 50
}
```

- **Chain A carries an explicit `sequence`.** `geneName` is metadata; the sequence is
  what gets folded. If you are reading the domain out of a PDB file, slice the domain
  residue range rather than taking all of chain A — a full-protein model has a
  full-length chain A even when the file is named for the domain.
- **Chain B is declared `isPeptide` with `use_msa: false`.** A de novo design has no
  homologs, so an MSA request on it fails several minutes in and reads like an MSA
  outage rather than a declaration problem.

### The platform now tells you

When a declared de novo peptide is paired with a receptor chain over 300 residues and
more than 5× the binder length, the job still runs and the response carries a
`warnings[]` entry:

```json
{
  "success": true,
  "jobs": [ ... ],
  "warnings": [{
    "code": "OVERSIZED_RECEPTOR_FOR_PEPTIDE_FOLD",
    "message": "This fold pairs a de novo peptide with a full-length receptor: chain(s) A (947 aa, 41.2x the 23 aa binder). ...",
    "findings": [{ "chainId": "A", "receptorLength": 947, "peptideLength": 23, "ratio": 41.2 }]
  }]
}
```

It warns rather than refuses, because folding against a full protein is sometimes
deliberate. Branch on `warnings?.length` and print them — the field is absent when
there is nothing wrong.

### Client-side assertion worth keeping

```python
assert len(receptor_seq) < 300, f"receptor is {len(receptor_seq)} aa — full-length?"
```

After a job returns, chain A length in the returned structure is the check that it
folded what you meant.
