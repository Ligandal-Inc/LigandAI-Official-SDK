# LIGANDAI Python SDK

Official Python client for the [LIGANDAI](https://ligandai.com) platform.

```python
from ligandai import LigandAI

client = LigandAI(api_key="lgai_basic_...")
print(f"Tier: {client.tier}, Credits: {client.credits}")

# Generate peptides
job = client.peptides.generate(gene="EGFR", num_peptides=10)
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
agents
api/ligandai/index
```

## Indices and tables

- {ref}`genindex`
- {ref}`modindex`
- {ref}`search`
