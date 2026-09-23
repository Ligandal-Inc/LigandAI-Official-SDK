# Paralog-conditioned generation — `avoid_paralogs`

Steer a design run away from named off-target receptor pockets, at generation
time, instead of drawing freely and filtering the result afterward.

> **Anchor beads:** `bd-dre-siz9d`, `bd-dre-if562`, `bd-dre-f4mc9`, `bd-dre-atcmd`,
> `bd-dre-lknrx`. Null methodology:
> `_global/wiki/concepts/the_minofk_selectivity_null_below_1x_and_populationspecific`.

---

## What this does mechanically

On the v6.9 generator line (`v6.9.21`, `v6.9.22`), design generation is a
classifier-free-guidance (CFG) sample. The only reference operator the
platform executes is `"paralog_mean"`: the guidance direction is

```
guidance = target_pocket − mean(reference_pockets)
```

— the target pocket you are designing against, minus the mean of whatever
off-target pockets are supplied as references. **The sign is the whole
mechanism.** Guidance pushes samples *away from* the mean of the reference
panel. That means the reference panel must be the thing you want to avoid,
never a second thing you want to bind.

⛔ **A positive co-optimization target named as a reference steers designs
away from it.** If you are co-optimizing a binder against two receptors —
human `BRD4` *and* mouse `Brd4`, say — and you (or a helper) place `Brd4` in
the reference panel for the `BRD4` run, the request is well-formed, costs full
GPU spend, and optimizes for the *opposite* of what you asked: it steers the
generator away from a receptor you are designing a binder *for*. This is why
`client.multitarget.CoOptimization.check_reference_roles()` refuses that
combination before any request is sent — see `docs/api_reference.md` and
`ligandai/multitarget.py` for the co-optimization surface. When you build a
panel yourself, keep the mental model literal: **the reference panel is what
you are being steered away from, full stop.**

---

## The honest result

> **What this does.** Paralog-conditioned generation steers the generator away
> from the off-target pockets you name, instead of drawing freely and filtering
> afterwards.
>
> **What it did not do.** In a controlled head-to-head on BRD4-ET — three arms,
> one seed ladder, 330 resolved designs — it did not produce more selective
> designs than generate-and-filter. Contrastive 0.437x, baseline 0.414x,
> unrelated-pocket control 0.381x, against a within-design min-of-3 permutation
> null of 0.423x [0.370, 0.486]. All three arms sit inside the null band, and
> the contrastive-vs-baseline difference was p=0.77. Choose it because it is a
> different experiment, not because it is a better mode.

24-26% of designs exceed 1x selectivity in every arm, including the
unrelated-pocket control. That is what taking the minimum of k off-target
measurements produces, not evidence of conditioning. The null for this metric
is not 1x and is not transferable between populations — it is recomputed by
permutation within design, on these rows.

| Arm | What it is | n | Max selectivity observed |
|---|---|---:|---:|
| A | contrastive (paralog-conditioned) | 115 | 37.24x |
| B | baseline (generate-and-filter, no CFG reference) | 105 | 16.92x |
| A0 | unrelated-pocket control (a CFG reference that is not a real paralog of the target) | 110 | 6.16x |

Source of every number above: bead `bd-dre-atcmd`, panel complete 2026-09-23,
7200/7200 structures folded, 0 fold failures, 4217/7200 poses cleared
`peptide_ipsae >= 0.67`. The k=1 method check (no minimum taken — symmetry
forces this to exactly 1.0 or the permutation machinery is wrong) landed at
1.0003 at n=330. Full null methodology, including why the null sits below 1x
and why a pooled cross-target null does not transfer to a within-design one:
`_global/wiki/concepts/the_minofk_selectivity_null_below_1x_and_populationspecific`.

Do not ship copy, in code comments, docstrings, PR descriptions, or customer
email, that implies paralog-conditioned generation improves selectivity. It is
a different way to sample, measured against generate-and-filter exactly once,
and that measurement came back null.

---

## The one call

```python
from ligandai import LigandAI

client = LigandAI(api_key="lgai_...")

job = client.peptides.generate(
    gene="BRD4",
    num_peptides=200,
    model="v6.9.21",
    avoid_paralogs=["BRD2", "BRD3", "BRDT"],
)
result = job.wait()
```

`avoid_paralogs` sends HGNC gene symbols on the wire as `cfg_reference_genes`
(uppercase-normalized, deduplicated, capped at 8). It does **not** require you
to fetch or build any pocket-feature matrix — the server resolves the panel:
it restricts its own paralog-reference producer to exactly the genes you
named, and each one must still clear the full paralog criterion below. A named
gene that fails is recorded with its rejection reason and is not served.

`avoid_paralogs` requires a CFG-capable model. Passing it with a model on the
v6.5 line raises `LigandAIValidationError` client-side, naming the models that
take it (`v6.9.21`, `v6.9.22`) rather than silently dropping the argument —
sending a knob that reads as set and executes as unset is the exact defect
class this SDK refuses to ship.

The same kwarg is available anywhere a generation request is built:

```python
client.peptides.generate_multi(..., avoid_paralogs=["BRD2", "BRD3", "BRDT"])
client.campaigns.recursive(..., avoid_paralogs=["BRD2", "BRD3", "BRDT"])
```

`avoid_paralogs=None` on a v6.9-line model does not mean "no conditioning" —
see the next section.

---

## What happens when you name nothing

`v6.9.21` / `v6.9.22` execute `cfg_reference == "paralog_mean"` unconditionally.
There is no "uncontrasted" mode on those models: `"paralog_mean"` needs a
reference to average, and an all-zero reference is refused rather than
fabricated (see the errors below). So when you call `generate(gene="BRD4",
model="v6.9.21")` with no `avoid_paralogs` and no `contrastive=`, the platform
derives its own real paralog panel for you and runs contrastive whether or not
you chose the panel.

The panel the platform derives is not a similarity search. A candidate off-
target `R` is served as a reference for target `T` only if it clears every one
of:

1. **Same species.** `R` and `T` are both human. A within-genome duplicate is a
   paralog; a cross-species match is an ortholog and is a different, unrelated
   capability.
2. **Compara gene-tree paralogy.** Ensembl Compara calls `R` a within-species
   paralogue of `T` — the canonical genomic definition (inferred gene
   duplication), not a sequence-similarity hit.
3. **Shared Pfam family containing the pocket.** The Pfam family that contains
   `T`'s pocket residues must also be present in `R`. Without a shared domain
   there is no structural frame in which "the same pocket" means anything.
4. **Domain identity in [0.30, 0.95).** Computed by global alignment over the
   domain, not asserted. Below 0.30 a pairwise alignment cannot be trusted to
   transfer residue-level correspondence; at or above 0.95 the "paralog" is an
   isoform or near-duplicate close enough that the CFG delta would be
   numerically degenerate.
5. **Pocket structural correspondence is establishable.** Failing this returns
   nothing for that candidate — never "use it anyway."
6. **Experimental structure only.** `R` must have a real deposited structure
   covering the domain. A predicted model is not accepted as a reference: the
   entire point of a reference is that it is a real other receptor.
7. **Different accession.** `R` is a different UniProt primary accession *and*
   a different gene symbol than `T`, with a non-identical chain sequence.

A target with no defensible paralog under these seven criteria gets a refusal
carrying a reason (`missing_nonzero_cfg_references`, see below) — that is the
**expected outcome** for such a target, not a bug to work around. The platform
will never zero-fill or fabricate a reference to make the run proceed.

---

## Reading the provenance

A run that executed `"paralog_mean"` emits an SSE event, `cfg_references_resolved`,
naming exactly what it used:

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
itself (no `avoid_paralogs=`); it echoes the genes you named otherwise.

Read it off the job stream:

```python
for event in job.stream():
    if event.event == "cfg_references_resolved":
        for p in event.data["paralogs"]:
            print(f"steered away from {p['paralog_gene']} "
                  f"({p['source_pdb_id']}:{p['source_chain']}, "
                  f"identity={p['identity_to_target_domain']:.2f})")
```

This is also persisted on the completed job record as `cfg_reference_provenance`,
so a finished run stays auditable after the stream closes — which paralogs, from
which PDB structure and chain, at what domain identity to the target.

⛔ **Absent provenance means "this server build did not report it," never "no
conditioning happened."** `v6.9.21`/`v6.9.22` execute `"paralog_mean"`
unconditionally, so a run against one of those models was contrastive whether
or not the provenance event reached you. Treat a missing
`cfg_references_resolved` event as an audit gap on your build, not as evidence
the run was unconditioned.

---

## The two errors

| Code | When | What to do |
|---|---|---|
| `missing_nonzero_cfg_references` | The model needs a `"paralog_mean"` reference and none could be produced — either you named genes and every one failed the paralog criterion (`paralog_reference_reason: "named_references_all_rejected"`, with per-gene rejection reasons), or you named nothing and the platform's own discovery found no defensible paralog for this target. | Read the per-gene reasons. This is not a bug to route around — some targets genuinely have no defensible paralog reference. If you believe a specific gene should have cleared the criterion, check which of C1-C7 above it failed; there is no override. |
| `cfg_references_not_supported` | `avoid_paralogs` (or `contrastive=`) was sent to a model that does not execute a CFG reference — the v6.5 line. | Switch to a CFG-capable model (`v6.9.21`, `v6.9.22`). This SDK raises the client-side equivalent (`LigandAIValidationError`) before the request goes out whenever it recognizes the model id, so you should rarely see the server-side code directly. |

There is also a refusal for supplying both a named panel and raw features at
once — `cfg_reference_genes_with_explicit_features`, raised if you pass
`avoid_paralogs` *and* `contrastive=ContrastiveGuidance` on the same call.
These are two different ways to name one panel, and silently preferring one
over the other is exactly how a caller ends up steered away from something
they did not choose. Pick one.

---

## Why you cannot supply your own matrices

`contrastive=ContrastiveGuidance` (see `ligandai/contrastive.py`) exists and
still works unchanged, but it requires a raw pocket-feature matrix per
reference — 107 columns for `v6.9.21`, 65 for `v6.9.22`. Those widths are
deliberately **not public**: the geometric and all-atom feature blocks are
Ligandal-internal, and a caller without that entitlement gets a `403` naming
the alternative rather than a reshaped matrix.

Do not treat the internal widths, or fetching raw pocket features to hand-build
a `ContrastiveGuidance`, as a documented customer workflow. `avoid_paralogs=`
is the supported way to choose a panel: it takes gene symbols, not matrices,
and the server does the featurization, the paralog qualification, and the
width resolution for you. If you need `client.campaigns.pocket_features()` or
`client.campaigns.contrastive_references()` directly, you already hold the
internal entitlement that route requires — most callers should reach for
`avoid_paralogs` instead and never touch a pocket-feature matrix at all.

See also: [campaigns](campaigns.md) for the recursive, multi-round loop that
`avoid_paralogs` and `contrastive=` both plug into.
