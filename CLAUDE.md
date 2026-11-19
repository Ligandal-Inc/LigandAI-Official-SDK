# CLAUDE.md

This Python package is the official **LigandAI SDK** (`pip install ligandai`).

**Read [AGENTS.md](./AGENTS.md) before writing any code that uses this SDK.**
It covers authentication, tier caps, the four canonical workflows
(generate by gene / by PDB-ID + chain / from custom CIF upload / pocket-targeted),
error handling, job lifecycle, and the URLs to send the user to when they
need an API key or more credits.

Quick start:

```python
from ligandai import LigandAI
client = LigandAI()                     # reads LIGANDAI_API_KEY
print(client.tier, client.credits)
```

API key page: <https://ligandai.com/account/billing?tab=api-keys>

For LigandAI **platform development** (not SDK consumption), platform
developers should refer to the internal LigandAI platform repository.

## H-SWAE transcriptomics

Use `client.discovery` for tissue vocabulary and v2 cell-type resolution. An exact
vocabulary match establishes a label. Measured expression and differential ranks
have separate coverage. The bundled discovery guide covers the registry, signed
projection edits, reference selection, truncation and production limitations.
See `agent_assets/claude_skills/ligandai/discovery.md`.
