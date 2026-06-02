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

## Base URL

The default base URL is `https://ligandai.com`. The platform serves the
public versioned API surface under `/api/v1/*`, which the SDK targets via
typed resource methods (`client.programs.list()`, `client.peptides.generate()`,
etc.). Do **not** point integrations at `api.ligandai.com` — that subdomain is
not published and resolves to NXDOMAIN.

For dev / on-prem deployments override explicitly:

```python
client = LigandAI(api_key="...", base_url="http://localhost:8000")
```

## Debugging

Set `LIGANDAI_DEBUG=1` to see every HTTP call the SDK makes (one DEBUG line
per request, format `METHOD URL -> STATUS (Xms)`). Combine with stdlib
`logging`:

```python
import logging
logging.basicConfig(level=logging.DEBUG)
logging.getLogger("ligandai").setLevel(logging.DEBUG)
```

On construction the SDK also emits one INFO line confirming the resolved
`base_url`, tier, and an 8-character API-key prefix — useful for verifying
what host an AI agent is actually talking to.

## Contents

```{toctree}
:maxdepth: 2

quickstart
authentication
resources
api_reference
workflows
error_codes
errors
jobs
agents
api/ligandai/index
```

## Indices and tables

- {ref}`genindex`
- {ref}`modindex`
- {ref}`search`
