# LigandAI Skill — Claude Agent SDK (`@anthropic-ai/agent-sdk`)

`ligandai-skill.ts` is a typed Claude Agent SDK skill exposing the LigandAI
peptide-design platform as agent tools. Drop it into any Claude Agent SDK
project that already has the `ligandai` Python SDK installed alongside it.

## Install

```bash
# In the agent project
npm install @anthropic-ai/agent-sdk
# In the same environment Claude can shell to:
pip install ligandai>=0.6.0
export LIGANDAI_API_KEY=lgai_pro_...     # any valid LigandAI key
```

### v0.6.0 — structure resolution with isoform / species / PDB selection

Agents can now pin a specific PDB, isoform, or cross-species variant
through `client.structures.get(gene, ...)`. Useful when the gene has
many candidate structures (KRAS, EGFR), an isoform-specific drug target
(CLDN18.2), or a non-human ortholog is needed:

```python
struct = client.structures.get("KRAS")                          # default — human, best
struct = client.structures.get("CLDN18", isoform=2)             # CLDN18.2
struct = client.structures.get("KRAS", pdb_code="6VG2")         # specific PDB
struct = client.structures.get("KRAS", species="mouse")         # cross-species
client.structures.list_isoforms("CLDN18")                       # enumerate
client.structures.list_species("KRAS")                          # enumerate
```

Tier handling: API key prefix is now a *hint*, not a privilege ceiling —
an enterprise account using a `lgai_pro_*` key gets enterprise privs.

## Wire it up

```ts
import { Agent } from "@anthropic-ai/agent-sdk";
import { ligandaiSkill } from "./ligandai-skill";

const agent = new Agent({ skills: [ligandaiSkill] });
```

## Tools exposed

| Tool | Purpose |
|---|---|
| `ligandai_design_peptide` | Submit a generation/fold job with full multi-segment scaffold + PDC + EC trimming options |
| `ligandai_get_job` | Poll a generation or fold job |
| `ligandai_recommend_linker` | Get a BLI biotin-linker recommendation (contact-map aware) |
| `ligandai_estimate_cost` | Cost preview before committing |
| `ligandai_get_credits` | Tier, balance, 30-day burn, days remaining |
| `ligandai_adaptyv_submit` | Submit top candidates to Adaptyv Foundry SPPS + BLI |

The handlers shell to `python3 -c "..."` against the `ligandai` package, so
the agent's environment must have `ligandai` importable AND a valid
`LIGANDAI_API_KEY` exported.

## Capability coverage

The skill covers the most common agent-driven workflows. For full SDK
coverage (charts, goals, programs, memory, etc.) see the example scripts
in `examples/` — Claude can `python3` them directly when needed.

## Notes

- Generation runs use the one-GPU server path. The GPU caps in the tier
  description are **folding** caps, not generation GPU caps.
- `cysteine_mode="exclude_all"` is incompatible with `cyclic_mode="disulfide"`
  — leave default for cyclic designs.
- The skill never displays API keys; tool error messages reference the
  cli-onboard URL when auth is missing.
