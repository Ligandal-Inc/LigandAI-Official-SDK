# Example Workflows

End-to-end recipes for the LigandAI Python SDK. Each workflow is a real,
runnable script — copy, paste, replace your API key, and run.

For a minimal "first call" example see [Quickstart](quickstart.md).

## 1. List peptides in a program, filter by iPSAE, fetch full detail

```python
from ligandai import LigandAI

client = LigandAI(api_key="lgai_pro_...")

# Step 1: find your program
programs = client.programs.list()
egfr_program = next(p for p in programs if p.name == "EGFR campaign")
print(f"Program {egfr_program.id} has {egfr_program.peptide_count} peptides "
      f"({egfr_program.elite_count} elites)")

# Step 2: list elite peptides in that program
elites = client.peptides.list_by_program(
    program_id=egfr_program.id,
    min_ipsae=0.85,
    limit=50,
)
print(f"Found {len(elites)} elite peptides for {egfr_program.name}")

# Step 3: fetch full detail (incl. pocket features and PDB) for the top hit
top_hit = elites[0]
detail = client.peptides.get(
    top_hit.peptide_id,
    include=["pocket_features", "interface", "pdb"],
)
print(f"Best binder: {detail.sequence}")
print(f"  iPSAE: {detail.ipsae}")
print(f"  Predicted Kd: {detail.predicted_kd:.3e} M")
print(f"  PDB length: {len(detail.pdb_content) if detail.pdb_content else 0} chars")

# Step 4: write the PDB to disk
if detail.pdb_content:
    with open(f"{detail.gene}_{detail.peptide_id}.pdb", "w") as f:
        f.write(detail.pdb_content)
```

Free-tier note: `peptides.list_by_program` works on free keys but returns
masked sequences (`ACDEFGHIKL********`). `peptides.get` requires a paid
tier; free keys raise `LigandAIUpgradeRequired`.

## 2. Search across all programs for high-affinity peptides

```python
from ligandai import LigandAI

client = LigandAI(api_key="lgai_pro_...")

# Find every binder under 10 nM Kd, regardless of program
strong_binders = client.peptides.search(
    kd_max=1e-8,    # 10 nM
    ipsae_min=0.8,  # high-confidence interface
    limit=200,
)

# Group by gene
by_gene: dict[str, list] = {}
for p in strong_binders:
    by_gene.setdefault(p.target_gene or "?", []).append(p)

print(f"Found {len(strong_binders)} hits across {len(by_gene)} targets:")
for gene, hits in sorted(by_gene.items(), key=lambda kv: -len(kv[1])):
    best = min(hits, key=lambda x: x.predicted_kd or float("inf"))
    print(f"  {gene}: {len(hits)} hits, best Kd = {best.predicted_kd:.2e} M")
```

## 3. Custom PDB upload → peptide generation → fold → fetch results

```python
from pathlib import Path
from ligandai import LigandAI

client = LigandAI(api_key="lgai_pro_...")

# Step 1: upload a custom receptor PDB
pdb_text = Path("./my_receptor.pdb").read_text()
upload = client.proteins.upload_pdb(
    pdb_content=pdb_text,
    name="MyTarget v1",
    metadata={"source": "internal cryo-EM"},
)
print(f"Uploaded {upload.id} (gene={upload.gene_symbol})")

# Step 2: launch generation (auto-fold the top 25 by predicted iPSAE)
job = client.peptides.generate(
    custom_target_id=upload.id,
    num_peptides=300,
    binder_length_min=20,
    binder_length_max=70,
    auto_fold=True,
    auto_fold_top_n=25,
)
print(f"Job {job.id} submitted; polling for completion...")

# Step 3: wait for completion (long-running — typical 5-15 min on B200)
result = job.wait(timeout=1800)  # 30 min ceiling
print(f"Job finished — folded {len(result.folded_peptides)} structures")

# Step 4: pull the folded structures with their scores
program_id = upload.program_id  # set by the generate() call
list_results = client.peptides.list_by_program(
    program_id=program_id,
    min_ipsae=0.7,
    limit=50,
)
for p in list_results:
    print(f"  {p.sequence}  iPSAE={p.ipsae:.3f}  Kd={p.predicted_kd:.2e}")

# Step 5: download every elite PDB
elites = [p for p in list_results if p.is_elite]
for e in elites:
    pdb = client.structures.get_pdb(e.peptide_id)
    Path(f"./elites/{e.gene}_{e.peptide_id}.pdb").write_text(pdb)
print(f"Wrote {len(elites)} elite PDBs to ./elites/")
```

## Bonus: Detect tier-redaction programmatically

```python
from ligandai import LigandAI, LigandAIUpgradeRequired

client = LigandAI(api_key="lgai_free_...")

try:
    detail = client.peptides.get(12345)
except LigandAIUpgradeRequired as e:
    print(f"Need to upgrade — current: {e.current_tier}, required: {e.required_tier}")
    print(f"Upgrade at: {e.upgrade_url}")
    # send the user to the upgrade URL or prompt for a paid key
else:
    # On free tier, list+search still work but return masked sequences.
    masked = [p for p in client.peptides.list("EGFR") if p.masked]
    if masked:
        print(f"{len(masked)} sequences are masked — upgrade for full data.")
```

## Async client

Every sync method has an async counterpart on `AsyncLigandAI`:

```python
import asyncio
from ligandai import AsyncLigandAI

async def main():
    async with AsyncLigandAI(api_key="lgai_pro_...") as client:
        peptides = await client.peptides.list_by_program(42, min_ipsae=0.85)
        # fetch top 5 PDBs concurrently
        pdbs = await asyncio.gather(*[
            client.structures.get_pdb(p.peptide_id) for p in peptides[:5]
        ])
        for p, pdb in zip(peptides[:5], pdbs):
            print(f"{p.gene} {p.peptide_id}: {len(pdb)} chars")

asyncio.run(main())
```
