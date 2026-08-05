# Generate peptide binders (the most common request)

`pip install ligandai>=0.5.0` and ensure `LIGANDAI_API_KEY` is set.

## A. Against a known gene (simplest)

```python
from ligandai import LigandAI

client = LigandAI()
job = client.peptides.generate(
    gene="EGFR",
    num_peptides=50,
    auto_fold=True,
    top_n_fold=10,
)
result = job.wait(timeout=1800)
for p in result.peptides[:5]:
    print(p.sequence, p.binding_energy, p.ipsae)
```

## B. Against a specific PDB ID + chain

```python
struct = client.structures.from_pdb("9MIR")
print(struct.gene, [c.id for c in (struct.chains or [])])

job = client.peptides.generate(
    gene="9MIR",
    target_chains=["C"],   # design AND fold against chain C only
    num_peptides=50,
    fold_gpus=16,          # pick caps per tier
    auto_fold=True,
    top_n_fold=10,
)
result = job.wait(timeout=1800, save_to="./9mir_chainC")
```

## C. Against a custom CIF on disk

```python
from pathlib import Path

up = client.proteins.upload_pdb(
    file=Path("/path/to/relaxed.cif"),
    gene="MY_TARGET",
    custom_name="relaxed_2026_05_07",
)
job = client.peptides.generate(
    gene="MY_TARGET",
    variant_id=up.id,
    target_chains=["A"],
    num_peptides=25,
    auto_fold=True,
)
```

## D. Pocket-targeted (residue-level)

`target_residues`, `avoid_residues` and `hotspot_residues` are **PDB residue
numbers as numbered in the receptor structure** — not indices into any internal
array, not sequence offsets. `chain` is honoured.

```python
from ligandai import ResidueRange

target_residues = [
    *ResidueRange.from_residues([34, 35, 36, 41, 42], chain="A"),
]
job = client.peptides.generate(
    gene="EGFR",
    num_peptides=25,
    target_residues=target_residues,
    targeting_strategy="pocket_targeted",
    quality_guided=True,
    auto_fold=True,
)
```

## E. Excluding a surface (`avoid_residues`)

```python
from ligandai import ResidueRange

job = client.peptides.generate(
    gene="EGFR",
    num_peptides=25,
    hotspot_residues=["A:792", "A:855"],                     # where to bind
    avoid_residues=[ResidueRange(chain="A", start=719, end=836)],  # keep clear
    avoid_shell_angstrom=8.0,   # default; 0 = named residues only
    auto_fold=True,
)
```

- Enforced **unconditionally, in every targeting mode** — `target_residues` /
  `hotspot_residues` / `pocket_config` are all optional for it to apply.
- The server removes the named residues **and** the residues packing against
  them within 8 Å (Cα-Cα). Tune with `avoid_shell_angstrom`: omit for the 8.0 Å
  server default, `0` for named residues only. Range `[0, 50]`, validated
  client-side before any HTTP call.
- ⚠ Targeting steers pocket **featurization**. It is a strong prior, **not** a
  hard geometric constraint — the fold step can still place a peptide on an
  excluded face. Validate against the folded complex, not the submitted request.
- Residue numbers absent from the receptor's featurized residue set come back as
  `unresolved_resnums` on the generation stream's targeting events instead of
  failing the job. Check that before assuming an exclusion applied.
- `targeting_strategy` is a run label; it does not itself move the design
  surface.

## F. Choosing the generator model (`model=`)

**Omit `model` unless you have a reason.** Sending nothing lets the server apply
the platform default, so a platform-side change to the default reaches you
without an SDK upgrade. The SDK holds no copy of the default.

```python
job = client.peptides.generate(
    gene="EGFR",
    num_peptides=50,
    model="v6.9.21",   # omit this line to use the platform default
    auto_fold=True,
)
```

- **`"v6.5.21"` (default)** — Receptor-conditioned. Designs respond to the
  receptor you select. Replaces v6.5, which was trained with all-zero receptor
  features and produced the same peptides regardless of target.
- **`"v6.9.21"`** — Experimental. A different, larger receptor feature space
  (107 per-residue channels vs 48), with classifier-free guidance applied at
  inference. Validated for distinguishing sequence composition between
  receptors. Not validated for where on the structure the peptide binds.
  Guidance options are not available on this model.
- **`"v6.5"`** — Legacy, deprecated. Trained with all-zero receptor features, so
  its output does not depend on the receptor you select. Retained only to
  reproduce runs made before August 2026.

⚠ `model="v6.9.21"` combined with `serum_stability`, `immunogenicity`,
`halflife`, `immuno_modules`, `stability_modules`, or the cell-free guidance keys
raises `LigandAIInvalidConfig` before any HTTP call, naming the conflict. Those
guidance heads are not available on that model and are never dropped silently.
`quality_guided` is a different mechanism and works on every model.

## Cost preview before submitting

```python
est = client.peptides.estimate_cost(
    gene="EGFR", num_peptides=1000, auto_fold=True, fold_top_n=100,
)
print(f"Cost: ~{est.credits} credits (${est.cost_usd:.2f})")

bal = client.account.get_balance()
if bal.credits < est.credits:
    print("Insufficient — buy at https://ligandai.com/pricing/usage")
```

See also: `fold.md` (folding-only workflows), `synthesis.md` (Adaptyv BLI).
