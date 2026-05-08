# Order synthesis (Adaptyv BLI + LigandAI amide / SPPS)

## A. Recommend a biotinylation linker before submitting

`recommend_linker` analyzes the contact map (when a fold ID is given) to
identify which terminus contacts the receptor — biotin should go on the
OPPOSITE end so the binding interface stays exposed on the BLI sensor.

```python
rec = client.synthesis.recommend_linker(
    sequence="HHHHHHGGGGSRRRGDFKMEYHLA",
    gene="EGFR",
    pdb_job_id=fold_id,             # enables contact-map orientation
    intended_application="bli_validation",
)
print(rec.recommended_linker, rec.binding_orientation)
```

## B. Adaptyv: search target → create experiment → submit

```python
targets = client.synthesis.adaptyv_search_targets("EGFR")
target_id = targets[0].id

from ligandai.types import AdaptyvSequence
seqs = [
    AdaptyvSequence(name="EGFR_001", sequence="HHHHHHGGGGS..."),
    AdaptyvSequence(name="EGFR_002", sequence="HHHHHHGGGGS..."),
]
exp = client.synthesis.adaptyv_create(
    name="EGFR_screen_2026_05_07",
    target_id=target_id,
    sequences=seqs,
    include_bli=True,
)
submitted = client.synthesis.adaptyv_submit(exp.id)
print(submitted.status)            # 'submitted' / 'in_progress' / 'complete'
```

## C. Get a quick estimate

```python
est = client.synthesis.estimate_cost(
    gene="EGFR", num_peptides=10, max_folds=5, include_bli=True,
)
print(est.credits, est.cost_usd)
```

## D. Generation mask guidance — feed back into next design

```python
guidance = client.synthesis.generation_mask_guidance(
    sequence="...", pdb_job_id=fold_id,
)
# guidance.generation_constraints can be passed straight into peptides.generate
```

## E. Amide / LigandAI in-house quote

```python
quote = client.synthesis.amide_quote([
    {"name": "EGFR_001", "sequence": "HHHHHHGGGGS..."},
])
```

## Tier reminder

Synthesis endpoints are open at **basic** and above (academia treated at pro
level). Free tier hits 402 with the upgrade URL.
