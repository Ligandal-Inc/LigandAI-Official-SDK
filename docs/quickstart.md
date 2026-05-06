# Quickstart

```bash
pip install ligandai
python -m pip install --upgrade ligandai
```

```python
from ligandai import LigandAI

# Reads LIGANDAI_API_KEY. Free+ authenticated accounts can create keys.
client = LigandAI()

# Read-only — no API key required for these
hits = client.receptors.search("EGFR")

# Tier-gated — requires the tier returned by the API response
markers = client.discovery.tissue_markers(target_tissues=["Liver"])

# GPU-bound — returns a Job
job = client.peptides.generate(gene="EGFR", num_peptides=10)
result = job.wait()
```

On client startup the SDK checks PyPI once per process. If a newer valid SDK
release exists, it warns with the upgrade command agents should ask before
running.

See the [examples directory](https://github.com/ligandal/ligandai-python-sdk/tree/main/examples)
for complete worked demos.
