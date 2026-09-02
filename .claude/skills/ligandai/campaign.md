# Run a whole campaign from one spec file

Use this when the user wants a RESULT (`"25 certified selective binders against
X"`) rather than a job (`"fold these 40 peptides"`). One declarative file states
the target and the definition of done; the runner generates, folds, scores,
checks itself, and queues whatever would close whatever is still open, until the
stop metrics are met or the budget is spent.

## The one command

```bash
pip install ligandai
export LIGANDAI_API_KEY=lgai_...

ligandai campaign validate my_campaign.yaml   # is it runnable? spends nothing
ligandai campaign explain  my_campaign.yaml   # what would round 1 do, and cost?
ligandai campaign run      my_campaign.yaml   # go
```

A worked example on a real, non-trivial target ships with the package:
`examples/campaign/ca12_selective_binders.yaml` (carbonic anhydrase XII,
selective over CA9 and CA2, zinc cofactor control) plus its `README.md`.

## From Python

```python
from ligandai.campaign import CampaignSpec, CampaignEngine, SDKPlatform

spec = CampaignSpec.load("my_campaign.yaml")
result = CampaignEngine(spec, SDKPlatform()).run()
print(result.summary())
```

## Writing the spec

Minimum runnable spec:

```yaml
campaign_spec_version: 1
name: my_campaign
target:
  gene: MYGENE
  construct:
    name: MYGENE_ECD
    sequence_file: constructs/mygene.fasta
    numbering_frame: "UniProt P12345 numbering"
peptide_classes:
  - { name: linear, topology: linear, length_range: [12, 24], n_per_round: 200 }
gates:
  - { metric: peptide_ipsae_median, op: ">=", threshold: 0.67 }
stop_metrics:
  - { id: certified_25, kind: certified_designs, target: 25, of_population: gate_passing }
budget:
  max_credits: 200000
  max_iterations: 20
  no_progress: { patience_iterations: 3, min_absolute_progress: 1.0 }
ranking:
  score: peptide_ipsae_median
  higher_is_better: true
  population: gate_passing
  length_baseline: required
```

Add these when they apply:

- `off_targets:` — paralogs, each with `min_margin`. Enables `margin_coverage`
  stop metrics and the paralog panel. Both legs are folded on the same construct
  set, so a margin is never assembled from whatever happened to finish.
- `target.cofactors:` — metals or ligands the site needs. This is what makes the
  PAIRED cofactor-free control runnable (`control_present` with
  `control: cofactor_free`).
- `target.comparison_constructs:` — the full-length chain the customer actually
  expresses, so the fragment you designed against gets compared to it.

## Stop metric kinds

`certified_designs` (count) · `margin_coverage` (count, needs `off_targets` +
`min_margin`) · `control_present` (count, needs `control`) · `block_coverage`
(FRACTION 0-1, needs `block`) · `interval_width` (count, needs
`max_interval_width`).

`of_population` is `all`, `gate_passing`, or `certified`.

## Budget and the stop conditions

`budget.max_credits` is required — there is no uncapped mode. The loop also
stops on `max_iterations`, `max_wall_clock_hours`, and a no-progress rule: if the
total distance to the stop metrics has closed by less than
`min_absolute_progress` over the last `patience_iterations` iterations, it stops
and says so. `per_iteration_credit_cap` bounds a single round;
`reserve_fraction` (default 5%) is never spent.

Every exit carries a reason: `metrics_met`, `credit_cap`, `iteration_cap`,
`wall_clock`, `no_progress`, or `nothing_to_queue`.

## Reliability — read this before choosing a certification bar

A single fold is not a result. The runner folds each design from INDEPENDENT
seeds and reports the fold-failure rate with a Wilson 95% interval. CERTIFIED
means the interval's upper bound sits at or below `reliability.certification_bar`.

With ZERO failures: 8 runs bound the rate no tighter than 0.32, 16 at 0.19, 22 at
0.149. So a bar of 0.15 needs **22 clean runs per design**, and 25 certified
designs is ~275,000 credits at 500 credits a fold before anything fails. The spec
validator computes this and refuses a budget that cannot buy one certified design.

Never count the diffusion samples inside one seed as independent runs. The runner
does not, and `reliability.count_unit` will not accept anything else.

## What it refuses, and why

Each of these is a real defect, not a preference:

1. a gate whose threshold removes (almost) nothing — every gate reports the
   metric's observed range, and a bar that removes <1% is `DECORATIVE` and
   refused without a written reason
2. a ranking computed on rows outside the population it names, or covering only
   part of it
3. a score term that is constant across the cohort, or that cannot apply to the
   molecule (which contributes `not_applicable`, never a constant)
4. a scorer with no evidence for its direction, or whose evidence contradicts it
5. a learned scorer whose only performance evidence is a random split
6. a ranking with no length baseline
7. receptor identity taken from a folder, arm or file name instead of hashing the
   structure's own receptor chain
8. an aggregate that passes on zero rows

## Outputs

`designs_all_scores.csv` / `.jsonl` (every design, every field),
`designs_certified.csv`, `campaign_result.json`, `campaign_spec.resolved.json`,
and `iterations/iteration_NNN.json`.

Each metric is three columns: `metric`, `metric_status`, `metric_n`. Status is
`measured` / `absent` / `failed` / `not_applicable` / `block_absent`. A blank
cell is never a zero.

## What not to say about the output

`deltaforge_predicted_kd_nm` is a PREDICTION. No design in a computational
campaign has a measured Kd until it is synthesised and run on BLI. The ranking
orders candidates; it does not measure affinity.
