# Disease Atlas Guide (`client.diseases` and `client.map`)

The Disease Atlas surface maps human diseases, clinical abbreviations, causal genes, and compound disease-plus-tissue phrases directly to affected organs, tissues, single-cell populations, ranked surface receptors, public biological evidence, and binder campaign handoffs.

## Public Biological Workflow

Every public Disease Atlas call works with standard customer API keys and returns **only public biological data** (MONDO/Orphanet/OMIM ontology identifiers, GTEx v8 bulk expression, Human Protein Atlas single-cell expression, UBERON/Cell Ontology crosswalks, healthy reference GEO series, structure coverage, and ranked receptor targets). Commercial valuation, rNPV, comparator sales, patient population models, competition, and regulatory designations never appear on public responses.

### One-Call Disease Mapping (`client.map` / `client.diseases.map`)

Resolve a disease name, clinical abbreviation (`"hATTR"`, `"CF"`, `"HoFH"`), ontology identifier (`"ORPHA:271861"`, `"MONDO:0009061"`), or compound phrase (`"Fabry kidney disease"`) in one request:

```python
from ligandai import LigandAI

client = LigandAI(api_key="lgai_pro_...")

mapped = client.map("Fabry kidney disease", surface_only=True, top_k=10)
print(mapped.disease.name, mapped.matched_tissue_filter)
print("Tissues:", [t.label for t in mapped.tissues])
print("Cells:", [c.label for c in mapped.cells])
print("Ranked receptors:", [(r.gene, r.overall_score) for r in mapped.receptors])

if mapped.start_campaign:
    job = client.generate(
        gene=mapped.start_campaign.gene,
        num_peptides=50,
        auto_fold=True,
    )
```

### Step-by-Step Discovery (`search`, `get`, `tissues`, `targets`)

```python
# 1. Search by clinical abbreviation, name, gene, organ, or rarity
hits = client.diseases.search("hATTR", rarity="rare_genetic", limit=10)
disease_id = hits[0].identity_id  # "ORPHA:271861"

# 2. Fetch full public biological profile
detail = client.diseases.get(disease_id)

# 3. Inspect therapeutic and primary disease tissues + targetable cell types
tissues = client.diseases.tissues(disease_id, role="therapeutic", include_cells=True)

# 4. Rank surface-accessible targets and delivery receptors in a specific tissue
targets = client.diseases.targets(
    disease_id,
    tissue="liver",
    surface_only=True,
    top_k=10,
)
for target in targets:
    print(target.rank, target.gene, target.role_label, target.overall_score)
```

When a tissue filter has zero matching receptors, `client.diseases.targets_response(...)` reports `status == "no_targets"` with an explicit `empty_reason` rather than fabricating stand-in targets. Passing `strict_targets=True` raises `LigandAIValidationError` (`code="no_targets"`, HTTP 422). When a query matches multiple distinct diseases equally, the SDK raises `LigandAIAmbiguousDisease` (HTTP 409) carrying `.candidates` for disambiguation.

### Disease -> Tissue -> Cell -> Marker Graph & Ranking (`map_disease_to_targets` and `markers.ranking`)

We expose our indexed biological graph and final-stage marker-value ranking across fine `ligandai.com Studio dropdown` tissues (with the 12 coarse organ systems retained as a derived roll-up) and our peptide binder platform (over 6.5 million peptides screened in our wet lab and computational workflows):

```python
# Gate 29: resolve a disease to {tissues, cell_types, markers} with cited edge evidence
graph = client.diseases.map_disease_to_targets(
    "Fabry disease",
    tissue="kidney",
    top_k=10,
    seed=42,
)
for m in graph.markers:
    print(
        m.marker,
        m.score,
        m.selectivity,
        m.n_diseases,
        m.patient_pop,
        m.price_per_drug,
        m.evidence,
    )

# Gate 30: scored, explainable cross-disease marker ranking + per-organ top lists
ranking = client.markers.ranking(organ="kidney", platform_only=True, top_k=15, seed=42)
print("Top platform markers:", [(m.marker, m.score, m.n_diseases) for m in ranking.markers])
print("Per-tissue leaders:", [entry.fine_basket_id for entry in ranking.per_organ_top])
```

*Internal planning assumption (with sensitivity):* cross-disease platform reusability models roughly 50 to 100 indications per validated targeting capability with +/-40% composite-weight sensitivity across selectivity, indication count, patient population, and `v4` price-ladder anchors while keeping sourced GTEx, HPA, GEO, and `v4` ledger inputs unadjusted.

## Superadmin Intelligence Surface (`client.diseases.admin`)

Commercial decision matrices, organ basket rNPV models, US/EU5/World patient populations, rare-to-common expansion ladders, disease-tissue GEO cohorts, and US sales benchmarks live under `/api/admin/atlas/*` and require a superadmin API key (`lgai_sa_*` or `lgai_superadmin_*`).

The SDK enforces this role boundary client-side before sending any network request:
- Missing API key raises `LigandAIAuthError` (`status_code=401`, `code="superadmin_api_key_required"`).
- Non-superadmin API key raises `LigandAIForbidden` (`status_code=403`, `code="forbidden_role"`).

```python
from ligandai import LigandAI, MatrixWeights

admin_client = LigandAI(api_key="lgai_sa_...")

# Query or dynamically rerank the commercial decision matrix
matrix = admin_client.diseases.admin.matrix(
    organ="liver",
    exclude_imputed=True,
    weights=MatrixWeights(
        modality_ease=0.25,
        delivery_precedent=0.20,
        biology=0.20,
        us_market=0.15,
        competition_headroom=0.10,
        time_cost=0.04,
        expansion=0.03,
        regulatory=0.03,
    ),
)

# Organ basket rNPV summaries and per-organ cash flows
all_baskets = admin_client.diseases.admin.baskets()
liver_basket = admin_client.diseases.admin.baskets("liver")

# Patient population estimates (paginated list or single disease dossier)
populations = admin_client.diseases.admin.populations(organ="liver", exclude_imputed=True)
hattr_pop = admin_client.diseases.admin.populations("ORPHA:271861")

# Full disease intelligence dossier, expansion ladders, GEO cohorts, and benchmarks
intel = admin_client.diseases.admin.intelligence("ORPHA:271861")
ladders = admin_client.diseases.admin.ladders()
geo = admin_client.diseases.admin.geo("ORPHA:271861")
benchmarks = admin_client.diseases.admin.benchmarks()
```

## Dataset Version Switch (`v3` Default, Provisional Basket rNPV Until `v4` Gates Pass)

We default to dataset version `"v3"` (`DEFAULT_ATLAS_DATA_VERSION = "v3"`, activated after all 36 post-Addendum-7 `v3/validation/gates.json` checks passed). While the active dataset version is `"v2"` or `"v3"`, basket, lead-portfolio, and organ-stress rNPV responses from `admin_client.diseases.admin.baskets(...)` carry `rnpv_provisional=True` and `rnpv_provisional_label="provisional (pending v4)"` (standalone per-indication `commercial.rnpv_usd` and `pricing.rnpv_usd` are unaffected). Once `v4/validation/gates.json` passes all gates after `TASK_ADDENDUM8_LABEL_EXPANSION.md`, `resolve_atlas_data_version("v4", gates_path=...)` resolves to `"v4"` (failing closed to `"v3"` until all gates pass) and `rnpv_provisional` turns `False` automatically.
