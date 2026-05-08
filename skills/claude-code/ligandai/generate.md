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
