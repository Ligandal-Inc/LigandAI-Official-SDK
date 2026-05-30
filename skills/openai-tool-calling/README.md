# LigandAI Tools — OpenAI Codex / GPT-5 / Responses API

Drop-in tool definitions for an OpenAI tool-calling agent.

## Files

- `tools.json` — function/tool definitions in OpenAI tool-call schema. Pass
  this verbatim to `chat.completions.create(..., tools=...)` or
  `responses.create(..., tools=...)`.
- `agent_example.py` — end-to-end runnable example: parses a user message,
  loops on `tool_calls`, dispatches each to a Python handler that wraps
  the `ligandai` SDK, and prints the final assistant response.

## Install

```bash
pip install openai ligandai>=0.6.0
export OPENAI_API_KEY=sk-...
export LIGANDAI_API_KEY=lgai_pro_...
python agent_example.py "Design 25 EGFR binders with iPSAE>0.7 and estimate cost first."
```

> **v0.6.0** — `client.structures.get(gene)` now accepts `pdb_code=`,
> `isoform=`, `species=`, `declared_gene_set=` kwargs. New methods
> `list_isoforms(gene)` and `list_species(gene)` enumerate UniProt-backed
> variants. Backwards-compatible — no kwargs = original human-default
> fast path. Tier handling is now `max(key_prefix_tier, account_tier)` so
> upgraded enterprise accounts on pro-prefix keys get enterprise privs.

## Tool catalogue (all 9 currently exposed)

| Tool | What it does |
|---|---|
| `ligandai_design_peptide` | Submit generation/fold job (gene OR PDB id, multimer-aware) |
| `ligandai_get_job` | Poll status of a session |
| `ligandai_estimate_cost` | Credit + USD preview |
| `ligandai_get_credits` | Tier, balance, 30-day burn |
| `ligandai_resolve_pdb` | Resolve PDB / gene / UniProt to chain layout |
| `ligandai_upload_pdb` | Upload custom .pdb / .cif → variant_id |
| `ligandai_search_peptides` | Cross-program threshold search (free tier sees masked output) |
| `ligandai_recommend_linker` | BLI biotin-linker recommendation (contact-map aware) |
| `ligandai_adaptyv_submit` | SPPS + BLI through Adaptyv Foundry |

## Adding more tools

Every method on `client.<resource>.*` in `ligandai/resources/` can be
wrapped the same way — see `agent_example.py`. The full resource list lives
in `../claude-code/ligandai/SKILL.md` and `../../AGENTS.md`.

## Tier policy reminders

- Free tier: hits 402 on all generation, sequences masked + polyalanine PDBs
  on read endpoints, 10 peptide / 3 unique target lifetime cap.
- Basic: 100 peptide cap, sequences masked unless CC on file, no transcriptomics.
- Academia: 300 cap, full sequences (mature trial path), transcriptomics + advanced guidance.
- Pro: 300 cap + commercial license, all features, 25 fold GPUs.
- Enterprise: 1000 cap, batch endpoints, priority queue, 50 fold GPUs.

The server returns `_tier`, `_tier_redacted`, and `_upgrade_url` on read
endpoints so the agent can route the user appropriately. Surface the
`_upgrade_url` value verbatim — never invent your own.
