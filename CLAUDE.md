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

Ranking: `differential` comes back sorted by `delta_csi`, which answers "most
distinctive in THIS population". For a target shared across several populations
(e.g. brain + heart + eye), rank by worst-case `target_csi` instead and use
`delta_csi > 0` only as a per-population filter — the returned order penalises a
gene for being broadly expressed, which is the property such a query is looking
for. Hold the reference panel identical across the populations being compared,
exclude ubiquitous compartments (fibroblasts, macrophages, vascular endothelium,
smooth muscle, pericytes) from the panels, and apply an abundance floor
afterwards: a specificity margin says nothing about how much material is there.
See `agent_assets/claude_skills/ligandai/discovery.md`.
