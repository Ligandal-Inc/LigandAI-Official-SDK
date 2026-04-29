# Authentication

```python
from ligandai import LigandAI

# 1. Pass explicitly
client = LigandAI(api_key="lgai_pro_...")

# 2. Read from env var
client = LigandAI()  # reads $LIGANDAI_API_KEY

# 3. Custom base URL
client = LigandAI(api_key="...", base_url="http://localhost:5050")
```

## Tier prefixes

| Prefix | Tier |
|---|---|
| `lgai_free_*` | free |
| `lgai_edu_*` | academia |
| `lgai_pro_*` | pro |
| `lgai_ent_*` | enterprise |
| `lgai_sa_*` | superadmin |

Tier is inferred from the prefix at construction — no network call.

## Anonymous access (ReceptorDB)

The `ReceptorDBClient` allows browse-only access without a key:

```python
from ligandai import ReceptorDBClient

client = ReceptorDBClient()  # no key — browse-only
hits = client.search("EGFR")
client.download_pdb(hits[0].complex_id, "egfr.pdb")
```

Fold/generate require an API key:

```python
client = ReceptorDBClient(api_key="lgai_basic_...")
job = client.fold(sequences=["MAEEPQSD"], target_gene="EGFR")
```
