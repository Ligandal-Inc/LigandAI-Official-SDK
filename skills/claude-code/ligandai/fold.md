# Fold a peptide-receptor complex (Boltz-2)

## A. Fold an arbitrary peptide against a receptor

```python
from ligandai import LigandAI

client = LigandAI()
fold_job = client.peptides.fold(
    sequences=["HHHHHHGGGGS" + "ACDEFGHIKLMNPQRS"],
    gene="EGFR",
    num_trajectories=1,         # 1 is enough for screening
    use_msa=True,               # never fold without MSA for the receptor
)
fold_result = fold_job.wait(timeout=1800)
print(fold_result.peptides[0].ipsae, fold_result.peptides[0].predicted_kd)
```

## B. Continue folding from a previous generation

```python
# Generate 50, fold the top 5, then later fold 20 more from the same session
job = client.peptides.generate(gene="EGFR", num_peptides=50, auto_fold=True, top_n_fold=5)
result = job.wait()

more = client.peptides.continue_folding(
    session_id=result.session_id,
    additional_top_n=20,
)
more.wait()
```

## C. Hotspot-aware partitioning post-fold

```python
parts = client.folds.partition_by_hotspot(
    session_id=result.session_id,
    hotspots=[{"chain": "A", "residue": 60, "numbering": "pdb"}],
    distance_threshold_a=5.0,
)
print(len(parts.hotspot_hit), len(parts.hotspot_miss))

# Expand a hotspot residue into the full surrounding pocket
expanded = client.folds.expand_hotspot(
    gene="EGFR", chain="A", residue=60, radius_a=8.0,
)
print(expanded.pocket_residues)
```

## D. Fold with a single-residue mutation in the receptor

```python
mut = client.peptides.fold_custom_mutation(
    fold_id=fold_result.peptides[0].fold_id,
    mutation="K745R",          # PDB numbering
)
mut_done = mut.wait()
```

## Score-only (skip Boltz, just compute DeltaForge / LigandIQ)

```python
score = client.peptides.score_complex(
    sequences=["RGDFKMEYHLA"], gene="EGFR",
)
print(score.peptides[0].ligandiq, score.peptides[0].deltaforge_dg)
```

## Always pass `fold_gpus=` matching tier

```python
caps = {"free": 1, "basic": 4, "academia": 16, "pro": 25, "enterprise": 50}
fold_gpus = caps.get(client.tier, 1)
```
