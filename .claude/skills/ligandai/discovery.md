# Target discovery (transcriptomics) — find/rank targets

## H-SWAE discovery and projection membership

The v2 comparison sorts `differential` by descending `delta_csi`. That order
shows how much higher CSI was in this cell type than in its highest-scoring
matched reference. A gene expressed in both populations can
have high expression and a small difference.

| Expression question | How to read the result |
|---|---|
| Which genes show the largest specificity difference in this comparison? | Read `delta_csi` in descending order. It equals `target_csi − max_reference_csi`. Check `reference_cell_types_matched` on the row to see which populations contributed. |
| Is expression recorded in each population being compared? | Read `target_mean_expression` for each resolved population alongside its units and `target_csi`. The position in one differential list cannot establish shared expression. |
| What does a positive difference establish? | `delta_csi > 0` means the target CSI exceeded the maximum among the matched references. Even a positive difference can occur at low RNA abundance. |

Keep missing values explicit. If `differential_truncated_at_top_n` is true,
an omitted gene's position and expression are unknown from that response.
RNA abundance alone cannot establish surface protein copy number or binding.

### Which field to rank by — the choice that changes the answer

`differential` arrives sorted by `delta_csi`. That order answers "which gene is
most *distinctive* here", which is the right question for ONE population and the
wrong question for a set of them. `delta_csi = target_csi − max_reference_csi`,
and every reference panel worth comparing against is made of cell types that
occur in many organs, so a gene is penalised for the very property a multi-organ
query is searching for.

| What the caller is asking for | Rank by | `delta_csi`'s role |
|---|---|---|
| A **selective** target: this population, not its neighbours | `delta_csi`, the returned order | the ranking field |
| A **shared / multi-organ** target: one construct engaging several populations | **worst-case `target_csi`** across the populations | a *filter* — require `delta_csi > 0` in EVERY population, do not sort by it |

Hold the reference panel identical across every population in the set.
`target_csi` is a property of the target population alone, so a panel that
differs between calls changes which genes appear without changing their CSI,
and the comparison stops being like-for-like.

Then apply an abundance floor, because a specificity margin says nothing about
how much material is there. Measured on a recorded brain + heart + eye capture
(17 cell types, `top_n=200`): `KCNJ4` ranks **3rd of 34** by `delta_csi` and
**11th** by worst-case `target_csi`, at 5.8 TPM in its weakest organ — a
selectivity win with too few transcripts to matter. The inverse trap is just as
real: in the same capture `PLPP3` (304.6 TPM) and `NRP1` (272.7 TPM) are
abundant in cardiomyocytes while scoring BELOW the ubiquitous comparator
(`delta_csi` −0.095 and −0.165), which makes them off-target load rather than
targets. Neither CSI nor TPM establishes surface protein copy number, avidity or
delivery; they rank candidates for an experiment.

Report the carrying cell type per organ alongside the numbers. A gene that
reaches a set only through fibroblasts, macrophages, vascular endothelium,
smooth muscle or pericytes is a marker of that ubiquitous compartment, not of
the organs — excluding those populations from the panels is what keeps the
result about the organs.

The executable form of this section, over recorded production rows, is
`tests/unit/test_hswae_multiorgan_shared_markers.py`; its live acceptance
counterpart is `tests/integration/test_hswae_multiorgan_live.py`.

**Choosing a reference.** V1 semantic search and surface markers use tissue contexts.
V2 resolves cell types and compares them against explicit reference cell types.
Read the canonical label, its matching candidate, confidence, resolution stage,
ambiguity and proxy flags. A high score for a broad label does not establish a
more specific subtype. In SDK 0.16.9, the typed result exposes
`canonical_cell_type`, `confidence` and `source` from the server's selection.
For an older receipt that omits those fields, the SDK reads the canonical label
and the corresponding candidate. Explicit nulls are preserved. Earlier
SDKs expose `canonical` and `candidates`. A search miss says which vocabulary
was queried. It gives no evidence of biological absence.

**November 18 production observations.** The tissue listing included `eye`
among 83 labels while the v1 semantic index reported 73 tissues and 85 cell labels.
V2 resolved retinal cell types, including exact matches. The published
0.16.5–0.16.8 SDKs reject five updated discovery definitions under the old pins.
Calling a known operation through `invoke_capability` still works for an entitled
account. The synchronous and asynchronous 0.16.9 validators recognize those
reviewed pre-digest definitions and verify each explicitly stated digest.

On November 20, 2026, a production consumer using unmodified SDK 0.16.8
again saw five digest errors. The caller reported `canonical_cell_type=None`
and `confidence=None` for cardiomyocytes, and a skin comparator panel for
the cardiomyocyte `topical` comparison. The `top_n=200` request failed after
15.2 seconds. The corrected registry and resolver had been exercised in preview.

**Comparison limits.** For a resolved target, an empty reference list with no
scope gives an empty differential and `no_references_resolved`. Use explicit
references for a reproducible contrast. Without explicit references, the observed
`topical` and `tissue_resident` scopes returned the same skin panel for
non-skin queries. `systemic` and `cluster_only` did not supply comparison
references in the reported calls. Read `reference_resolution`,
`differential_status`, `differential_status_detail` and
`differential_truncated_at_top_n` alongside the rows. The reported sweep had
502s near 15 seconds at both small and large depths. Retry transient failures
on **read-only** operations with bounded backoff. A 502 contains no scientific result.

**Projection writes in 0.16.9.** The coordinated server release advertises
`hswae.v2.add_projection_item` and `hswae.v2.remove_projection_item` to
authorized curators when its Ed25519 signer is configured. Both SDK methods take
a kind (`tissue` or `cell_type`) and an exact atlas label. The authenticated
principal needs an unexpired approval for that action and item. The SDK checks
the signed receipt and its requested membership. Use `tissues()` or
`projection_cell_types()` to read membership. A lost write response calls for
a membership readback before issuing another change. On this server version,
`surface_markers` can resolve an exact tissue while reporting
`tissue_ranking_unavailable` if its reference lacks tissue-level marker ranks.
In the integrated preview, `eye` resolves exactly and its `ranking_note`
identifies a single-cell fallback. The highest specificity and highest
expression can come from different clusters. Whole-eye expression remains
unmeasured in this reference.



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
