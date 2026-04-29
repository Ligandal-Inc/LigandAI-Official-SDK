# Quickstart

```bash
pip install ligandai
```

```python
from ligandai import LigandAI

client = LigandAI(api_key="lgai_pro_...")

# Read-only — no API key required for these
hits = client.receptors.search("EGFR")

# Tier-gated — requires academia+ API key
markers = client.discovery.tissue_markers(target_tissues=["Liver"])

# GPU-bound — returns a Job
job = client.peptides.generate(gene="EGFR", num_peptides=300)
result = job.wait()
```

See the [examples directory](https://github.com/ligandal/ligandai-python-sdk/tree/main/examples)
for complete worked demos.
