# LigandAI Skill — Claude Agent SDK (`@anthropic-ai/agent-sdk`)

`ligandai-skill.ts` is a typed Claude Agent SDK skill exposing the LigandAI
peptide-design platform as agent tools. Drop it into any Claude Agent SDK
project that already has the `ligandai` Python SDK installed alongside it.

## Install

```bash
# In the agent project
npm install @anthropic-ai/agent-sdk
# In the same environment Claude can shell to:
pip install ligandai>=0.5.0
export LIGANDAI_API_KEY=lgai_pro_...     # any valid LigandAI key
```

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
