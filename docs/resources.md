# Resource namespaces

| Namespace | Description |
|---|---|
| `client.account` | User profile, credits, tier limits |
| `client.receptors` | ReceptorDB browse, search, download |
| `client.structures` | Gene → PDB / AlphaFold resolution |
| `client.proteins` | UniProt info, variants, glycosylation |
| `client.discovery` | Tissue markers, scRNA, GEO import |
| `client.diseases` | Disease search, mutations |
| `client.peptides` | Generate, fold, score |
| `client.maturation` | **Server-side binder maturation** — improve sequences you already have, with same-cycle parent controls. ⛔ NOT `client.campaigns`; see [maturation](maturation.md) |
| `client.bivalent` | Bispecific design (pro+) |
| `client.synthesis` | Quote, cart, Adaptyv orders |
| `client.memory` | Episodic memory |
| `client.programs` | Programs, projects, sessions |
| `client.targets` | **Your own uploads, addressed by a name you chose.** `list()`, `resolve(name)`, `candidates(name)`, and `choose/choice/choices/clear_choice` when one name means several structures — see [named targets](named_targets.md) |
| `client.charts` | Chart generation |
| `client.reports` | PDF reports |
| `client.jobs` | List, cancel, stream |
| `client.campaigns` | **Local** panel-folding helper (`fold_panel`, `replicate`, `recursive`). ⛔ It never calls `/api/campaign/*` — the platform campaign/maturation loop is `client.maturation` |

Each namespace is documented in detail in the auto-generated API reference.
