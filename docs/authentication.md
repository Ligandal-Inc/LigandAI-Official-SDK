# Authentication

```python
from ligandai import LigandAI

# 1. Pass explicitly
client = LigandAI(api_key="lgai_basic_...")

# 2. Read from env var
client = LigandAI() # reads $LIGANDAI_API_KEY

# 3. Custom base URL
client = LigandAI(api_key="...", base_url="http://localhost:8000")
```

## Tier prefixes

| Prefix | Tier |
|---|---|
| `lgai_free_*` | free |
| `lgai_basic_*` | basic |
| `lgai_edu_*` | academia |
| `lgai_pro_*` | pro |
| `lgai_ent_*` | enterprise |
| `lgai_sa_*` | superadmin |

Tier is inferred from the prefix at construction — no network call.

## Creating API keys

Any authenticated LIGANDAI account, including free accounts, can create API
keys from account settings in the Developer/API Keys area. Set the key as:

```bash
export LIGANDAI_API_KEY=lgai_free_...
```

Free and basic users can authenticate with the SDK. The API then gates each
operation by tier, credits/tokens, and GPU limits. Generation, folding, scoring,
and synthesis calls may return an upgrade or buy-credits response even when the
key itself is valid.

Free users can use quality-guided generation up to 10 peptides, 10 folds, 3
targets, and 1 folding GPU. Basic users can generate up to 1000 peptides and use
up to 4 folding GPUs. Academia and pro users can generate up to 5000 peptides;
enterprise users can generate up to 25000. Immunogenicity guidance, serum
stability guidance, and logits-style outputs require academia, pro, or
enterprise access.

All authenticated users, including free users, accept the LIGANDAI Terms of
Service and EULA; submitted sequences and job artifacts may be retained under
those terms.

Agents should never assume enterprise access just because a key exists. They
should surface `currentTier`, `requiredTier`, credit, token, and GPU-limit
fields from API responses and route users to billing or subscription pages.

## Anonymous access (ReceptorDB)

The `ReceptorDBClient` allows browse-only access without a key:

```python
from ligandai import ReceptorDBClient

client = ReceptorDBClient() # no key — browse-only
hits = client.search("EGFR")
client.download_pdb(hits[0].complex_id, "egfr.pdb")
```

Fold/generate require an API key:

```python
client = ReceptorDBClient(api_key="lgai_basic_...")
job = client.fold(sequences=["MAEEPQSD"], target_gene="EGFR")
```
