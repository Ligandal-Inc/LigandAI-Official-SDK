# LIGANDAI Python SDK

Official Python client for the [LIGANDAI](https://ligandai.com) platform.

```python
from ligandai import LigandAI

client = LigandAI(api_key="lgai_pro_...")
print(f"Tier: {client.tier}, Credits: {client.credits}")

# Generate peptides
job = client.peptides.generate(gene="EGFR", num_peptides=300)
result = job.wait()
```

## Contents

```{toctree}
:maxdepth: 2

quickstart
authentication
resources
errors
jobs
api/ligandai/index
```

## Indices and tables

- {ref}`genindex`
- {ref}`modindex`
- {ref}`search`
