# Resource namespaces

| Namespace | Description |
|---|---|
| `client.account` | User profile, credits, tier limits |
| `client.receptors` | ReceptorDB browse, search, download |
| `client.structures` | Gene → PDB / AlphaFold resolution |
| `client.proteins` | UniProt info, variants, glycosylation |
| `client.discovery` | Tissue markers, scRNA, GEO import |
| `client.diseases` | Disease search, mutations |
| `client.peptides` | Generate, fold, score. `generate(...)`/`generate_multi(...)` take `avoid_paralogs=[...]` for paralog-conditioned (contrastive) generation — see [contrastive](contrastive.md) |
| `client.maturation` | **Server-side binder maturation** — improve sequences you already have, with same-cycle parent controls. ⛔ NOT `client.campaigns`; see [maturation](maturation.md) |
| `client.gut_stability` | **GI / gut-stability triage** — SGF & SIF protease-motif burden (`score_local`, `score`, `generation_settings`, `entitlement`, `read_sidecar`). ⛔ RULE COUNTS NORMALIZED BY LENGTH: not a measured degradation rate, not a probability of surviving the gut, not an oral bioavailability prediction, not a neural network. Enterprise module, default off — see [gut stability](gut_stability.md) |
| `client.bivalent` | Bispecific design (pro+) |
| `client.synthesis` | Quote, cart, Adaptyv orders |
| `client.memory` | Episodic memory |
| `client.programs` | Programs, projects, sessions |
| `client.targets` | **Your own uploads, addressed by a name you chose.** `list()`, `resolve(name)`, `candidates(name)`, and `choose/choice/choices/clear_choice` when one name means several structures — see [named targets](named_targets.md) |
| `client.charts` | Chart generation |
| `client.reports` | PDF reports |
| `client.jobs` | List, cancel, stream |
| `client.campaigns` | **Local** panel-folding + multi-round refinement loop (`pocket_features`, `contrastive_references`, `fold_panel`, `replicate`, `recursive` → `run_round`). ⛔ It never calls `/api/campaign/*` — the platform campaign/maturation loop is `client.maturation`. Full loop, lineage, and disambiguation: [campaigns](campaigns.md) |

Each namespace is documented in detail in the auto-generated API reference.
`client.campaigns` and `client.maturation` are two of *three* things called
"campaign" in this SDK — see the disambiguation at the top of
[campaigns](campaigns.md) for the third (the `ligandai campaign run` YAML CLI)
and which one to reach for.
