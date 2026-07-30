# Target discovery (transcriptomics) — find/rank targets

**Reach for `client.discovery` FIRST** when the user asks "find targets",
"which receptors are enriched in tissue / cell-type X", "discover binders for
disease Y", or "what should I design against". The platform already runs the
specificity-index (SI) funnel server-side over GTEx and single-cell atlases —
**don't hand-stitch GTEx + CellGuide / CellxGene yourself.**

Every ranking call returns a `MarkerResponse`; read `.top` — a list of
`TissueMarker` rows with `.gene`, `.si` (specificity index, higher = more
selective), `.csi`, `.receptor`, and `.rank`.

## A. Resolve identifiers FIRST

The markers endpoints expect exact GTEx strings — don't guess the spelling.

```python
from ligandai import LigandAI

client = LigandAI()
tissues = client.discovery.tissues()         # ['Kidney - Cortex', 'Liver', ...]
systems = client.discovery.organ_systems()   # ['Nervous', 'Digestive', ...]
```

## B. SI-ranked surface receptors in a tissue (GTEx — the workhorse)

```python
markers = client.discovery.tissue_markers(
    target_tissues=["Kidney - Cortex"],
    exclude_tissues=["Liver", "Lung"],   # demote broadly-shared (off-target) genes
    receptor_only=True,                  # THE surface filter — plasma-membrane only
    top_n=200,
)
for m in markers.top[:10]:
    print(m.rank, m.gene, m.si, m.receptor)

best_gene = markers.top[0].gene          # → feed straight into design (section F)
```

`specificity_index` (surfaced per row as `.si`) measures how selective a gene's
expression is for `target_tissues` vs the rest of the body — high SI = a cleaner
target. `receptor_only=True` (the default) keeps only cell-surface receptors,
the only druggable surface for a peptide binder; set `False` only to inspect the
full (incl. intracellular) ranking.

## C. Single-cell resolution (Academia+)

When "enriched in the tissue" is too coarse and you need a specific cell type:

```python
markers = client.discovery.cell_type_markers(
    scrna_tissue="kidney",
    target_cell_types=["proximal_tubule", "podocyte"],
    receptor_only=True,
    top_n=200,
)
```

## D. Differential / selectivity ranking

`compare_targets` surfaces SHARED vs DIFFERENTIAL genes across groups. Each
group is a dict (or `TargetGroup` / `ReferenceGroup`) with a `type`:
`gtex` (+`tissue`), `geneset` (+`genes`), or `custom` (+`datasetId`/`cellTypes`).
`groups[0]` is the target; the rest are references.

```python
resp = client.discovery.compare_targets([
    {"name": "tumor",  "type": "gtex", "tissue": "Kidney - Cortex"},
    {"name": "normal", "type": "gtex", "tissue": "Liver"},
], receptor_only=True)
print(resp.differential_genes)   # target-enriched / target-exclusive
print(resp.shared_genes)         # expressed in target AND reference
```

### BBB transcytosis shuttles (Enterprise)

```python
shuttles = client.discovery.transport_vasculome(
    modality="monovalent",
    specificity_weight=0.5,   # >0 demotes broadly-shared (off-target) shuttles
    limit=25,
)
for r in shuttles:
    print(r.gene, r.score, r.specificity_index, r.broadly_shared)
```

## E. Custom transcriptomics (your own counts) — quickstart

Upload counts, then run the SAME SI ranking on them. Passing
`custom_dataset_targets` routes `tissue_markers` to the `analyze-fast` endpoint.

```python
# dataset_type: "bulk" | "scrna" | "scRNA-seq" | "microarray"
ds = client.discovery.upload_dataset("counts.h5ad", dataset_type="bulk")

markers = client.discovery.tissue_markers(
    # CustomDatasetTarget shape (aliases): datasetId (required), cellTypes, samples.
    # CustomDatasetTarget lives in ligandai.types; the dict form is equivalent.
    custom_dataset_targets=[{"datasetId": ds.id, "cellTypes": ["proximal_tubule"]}],
    receptor_only=True,
    top_n=200,
)
top_gene = markers.top[0].gene
```

`client.discovery.list_datasets()` finds earlier uploads;
`client.discovery.import_geo("GSE12345")` ingests a public GEO series instead.

## F. Chain discovery into design

Discovery picks the gene — design, fold, and score take it from there:

```python
gene = markers.top[0].gene
job = client.peptides.generate(gene=gene, num_peptides=50,
                               auto_fold=True, top_n_fold=10)
result = job.wait(timeout=1800)
```

See `generate.md` for the full design workflow and `fold.md` for folding-only
recipes. **Don't hand-stitch GTEx/CellGuide — the SI ranking is already done
server-side; just call `client.discovery`.**
