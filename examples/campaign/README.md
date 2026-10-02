# Run a campaign

A campaign is one file. You describe the target, what you want, and what you are
willing to spend; the runner generates, folds, scores, checks itself against your
stop metrics, and queues whatever would close the ones that are still open. It
stops when your metrics are met, when the credit cap is reached, or when it stops
making progress — and it always says which.

## 1. Install

```bash
pip install ligandai
export LIGANDAI_API_KEY=lgai_...        # your key, from ligandai.com
ligandai credits                        # confirms the key works and shows your balance
```

That is the whole setup. Nothing else to configure.

## 2. Look at the example

`ca12_selective_binders.yaml` in this directory designs peptide binders against
the extracellular domain of carbonic anhydrase XII, selective over CA9 and CA2.
Every sequence it uses is real, pulled from the UniProt entry named beside it.

Check it without spending anything:

```bash
ligandai campaign validate ca12_selective_binders.yaml
```

See what the first round would actually do, and what it would cost:

```bash
ligandai campaign explain ca12_selective_binders.yaml
```

## 3. Run it

```bash
ligandai campaign run ca12_selective_binders.yaml
```

It prints, every iteration, which stop metric is unmet and what it queued to
close it:

```
iteration 3: 412 designs, 9,880 poses, 118 pass the gates
  gate interface: ACTIVE — 118/412 pass, 294 removed; observed [0.02, 0.87] median 0.31
  stop metrics:
  [UNMET] certified_25: observed 11 / target 25  (n=118 of population 118)
  [MET]   zinc_control: observed 17 / target 15  (n=118 of population 118)
  QUEUE  replicate n_designs=14 runs_each=22 ~154,000cr  [certified_25]
         11 of 25 certified; 63 designs are short of RUNS rather than quality
```

Stop it at any time with Ctrl-C — everything written so far stays.

## 4. What you get

In `output_dir`:

| file | what it is |
|---|---|
| `designs_all_scores.csv` | every design, every score, one row each |
| `designs_all_scores.jsonl` | the same, nested, with full provenance |
| `designs_certified.csv` | only the designs that met the reliability bar |
| `campaign_result.json` | why it stopped, what it spent, every iteration |
| `campaign_spec.resolved.json` | the spec as the runner understood it |
| `iterations/iteration_NNN.json` | per-iteration gates, ranking, plan |

Every metric appears as three columns — `metric`, `metric_status`, `metric_n` —
so a blank cell is never mistaken for a zero. `metric_status` is one of
`measured`, `absent` (never run), `failed` (run, errored), `not_applicable` (the
term cannot apply to this molecule), or `block_absent`.

## 5. Writing your own spec

Copy `ca12_selective_binders.yaml` and change the target. The required parts:

```yaml
campaign_spec_version: 1
name: my_campaign

target:
  gene: MYGENE
  construct:
    name: MYGENE_ECD
    sequence_file: constructs/mygene.fasta     # or: sequence: MKTAYIAK...
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

Optional but usually worth it: `off_targets` (paralogs you need to be selective
against), `target.cofactors` (metals or ligands the site needs — this is what
makes the paired cofactor-free control runnable), and
`target.comparison_constructs` (the full-length chain you will actually express,
so the fragment you designed against is compared to it rather than assumed
equivalent).

### The five stop-metric kinds

| kind | target means | closed by |
|---|---|---|
| `certified_designs` | a COUNT of designs whose fold-failure rate has a 95% upper bound at or below `reliability.certification_bar` | more independent seeds, then more designs |
| `margin_coverage` | a COUNT of designs with a measured margin ≥ `min_margin` against EVERY named off-target | folding the paralog panel |
| `control_present` | a COUNT of designs carrying a named paired control | folding the control arm |
| `block_coverage` | a FRACTION of the population carrying a whole record block | scoring that block |
| `interval_width` | a COUNT of designs whose reliability interval is narrower than `max_interval_width` | more independent seeds |

## 6. Things it will refuse to do, and why

These are not style preferences. Each one is a defect that cost a real campaign
real time.

- **A gate that removes nothing.** Every gate reports the metric's observed range
  when it runs. A threshold that removes less than 1% of the rows reaching it is
  reported as `DECORATIVE` and refused, unless you write down why you are keeping
  it. A quality bar that excludes 1 design of 2,997 is decoration.
- **Ranking the wrong rows.** A ranking is computed on a named population and
  must cover it. Scoring designs a gate rejected, while skipping designs it
  passed, is an error, not a warning.
- **A score column that is the same for everybody.** A term with no variance
  across the cohort cannot order anything; it is refused entry to a composite. A
  term that cannot apply to your molecule contributes "not applicable", never a
  filled-in constant.
- **A metric nobody checked the direction of.** A scorer must declare whether
  higher or lower is better and carry evidence. If the evidence's sign
  contradicts the declaration, that is an error at construction.
- **A learned model reported on a random split.** Supply a clustered, grouped, or
  temporal split. A random split on peptides is not held-out performance.
- **A ranking with no length baseline.** Every ranking ships
  `Spearman(score, peptide_length)` with its interval, plus the overlap with a
  length-only ranker. There is no way to get an ordering without it.
- **Trusting a name.** Which receptor a structure contains is decided by hashing
  its receptor chain and reading its peptide chain. Folder names, arm names and
  file names are metadata.
- **Passing on an empty set.** `all([])` is `True` in Python. Nothing here
  returns a bare boolean from an aggregate: every verdict carries the row count
  that produced it, and zero rows is `INDETERMINATE`.

## 7. Reliability, in one paragraph

A single fold is not a result. The runner folds each design from independent
seeds and reports the **fold-failure rate** — the fraction of independent runs
that land below the interface threshold — with a Wilson 95% interval. A design is
CERTIFIED when the upper bound of that interval is at or below your
`certification_bar`. The arithmetic is unforgiving and worth knowing before you
set a target: with **zero** failures, 8 runs bound the rate no tighter than 0.32,
16 runs at 0.19, and certifying below 0.15 takes **22 clean runs**. An 8-run read
can rule a design out; it cannot certify one. When a design is not yet certified
the runner tells you how many more runs would do it, or that its rate is too high
for any number of runs to help.

Budgeting follows from that. The default rates are 10 credits per fold ($0.10 at
the default 4 trajectories — `ligandai.pricing`), plus the ledger medians for
generation (100 credits per peptide) and scoring (25 per design). One certified
design at a 0.15 bar is 100 + 22 x 10 + 25 = 345 credits, which puts 25 certified
designs at roughly 8,625 credits if nothing fails. `ligandai campaign validate`
does this arithmetic for your spec and refuses a budget that cannot buy one
certified design. A spec that raises `diffusion_samples` or `sampling_steps`
without stating `credits_per_fold` is priced at that setting (8 trajectories is
20 credits a fold). If your account prices differently, override
`credits_per_fold`, `credits_per_generated_peptide` and
`credits_per_scored_design` under `fold:`.

## 8. From Python

```python
from ligandai.campaign import CampaignSpec, CampaignEngine, SDKPlatform

spec = CampaignSpec.load("ca12_selective_binders.yaml")
result = CampaignEngine(spec, SDKPlatform()).run()

print(result.stop_reason, result.stop_detail)
for r in result.certified:
    print(r.sequence, r.reliability.interval.upper, r.margin_against("CA9"))
```
