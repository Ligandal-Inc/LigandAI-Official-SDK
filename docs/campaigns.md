# Design campaigns — `client.campaigns`

A local loop that draws new sequences, folds them against a target and its
off-targets, and lets you turn a shortlist into a declared, multi-round
refinement lineage.

> **Anchor beads:** `bd-dre-if562`, `bd-dre-umd3l`, `bd-dre-rp11j.9`,
> `bd-dre-rp11j.9.1`. See also [contrastive](contrastive.md) for
> `avoid_paralogs=` / `ContrastiveGuidance`, which this module conditions
> generation with.

---

## ⛔ Three things are called "campaign". Read this first.

| | what it is | what it operates on |
|---|---|---|
| **`client.campaigns`** (this page) | A **local** loop: you call SDK methods that compose fold/score primitives *in your process* — `pocket_features`, `contrastive_references`, `fold_panel`, `replicate`, `recursive` (then `run_round`). It **never calls `/api/campaign/*`**. | Sequences you generate as you go — new draws. |
| [`client.maturation`](maturation.md) | A **server-side** loop (`POST /api/campaign/mature`). You hand it sequences and it folds, gates, mutates, and re-folds them itself, cycle after cycle. | Sequences you **already have**. |
| `ligandai campaign run <spec.yaml>` (the CLI; `ligandai.campaign` package) | A **declarative spec runner** — one YAML/JSON file names the target, construct, and stop metrics, and `CampaignEngine` generates/folds/scores/queues against that spec until the metrics are hit or the budget runs out. | A whole campaign, unattended, from one file. |

Want to **draw new sequences and select among them yourself, with full control
over the loop in Python**? `client.campaigns`.
Want to **improve sequences you already have**? `client.maturation`.
Want to **hand off a whole campaign to run unattended from one spec file**?
`ligandai campaign run`.

`client.campaigns` has no `.mature()` — reaching for it raises
`LigandAISurfaceNotInThisPackage`, naming `client.maturation.start()` as the
place that capability actually lives, because a bare `AttributeError` here
would read like a typo and leave "this package cannot do maturation"
indistinguishable from "I called it wrong."

---

## The local loop

```python
from ligandai import LigandAI
from ligandai.contrastive import AvoidancePolicy

client = LigandAI(api_key="lgai_...")

campaign = client.campaigns.recursive(
    target="ENPP3",
    off_targets=["ENPP1", "ENPP6"],
    band=0.05,                    # measured cross-draw band — see below
    target_leg_min=0.66,
    model="v6.9.21",
    num_peptides=60,
    avoid_paralogs=["ENPP1", "ENPP6"],
)

rnd = campaign.run_round(replicates=1, confirm=True)
```

Five methods, all local, none of them touching `/api/campaign/*`:

| method | what it does |
|---|---|
| `pocket_features(gene, dim=, model=)` | Fetch a gene's featurized pocket at the width a generator declares. `GET /api/pocket-features/{gene}` — the only network call in this group that isn't a fold or a generate. |
| `contrastive_references(target=, paralogs=, model=)` | Build a `ContrastiveGuidance` from real off-target pockets you name — the internal/entitled path. Most callers want [`avoid_paralogs=`](contrastive.md) instead, which needs no pocket-feature matrix at all. |
| `fold_panel(sequences, target=, off_targets=, replicates=, ...)` | Fold the same designs against the target **and** every off-target, in one panel. Returns a `PanelResult`. |
| `replicate(sequences, ...)` | Re-fold a shortlist at independent draws, so "top of the list" survives contact with fold-to-fold noise before it's called a candidate. |
| `recursive(target=, off_targets=, band=, target_leg_min=, avoid_paralogs=, ...)` | Build a `RecursiveCampaign` bound to this client — the object `run_round()` lives on. |

`band` has **no default anywhere in this module** — it must be measured from
your own replicates (`PanelResult.cross_draw_band()`) before the first round,
because a band measured on one campaign is not a band on yours. See
`examples/33_recursive_contrastive_campaign.py` for the calibration step done
in full, before the loop starts.

---

## The multi-round refinement API

`recursive()` returns a `RecursiveCampaign`; `run_round()` executes one round
and appends it to `campaign.rounds`:

```python
rnd = campaign.run_round(
    sequences=None,            # None -> generate this round's pool
    derived_from=None,         # or: {child_seq: parent_seq, ...}
    steered_by=None,           # or: [lead_seq, ...]
    refold_parents=True,       # refold every declared parent IN this round's panel
    replicates=1,
    confirm=True,
    confirm_replicates=1,
)
```

- **`sequences=`** skips generation and folds a set you already produced —
  hand-edited variants, an external optimizer's output, or a re-run.
- **`derived_from={child: parent}`** states a real, per-child parent link. It
  is the *only* mechanism that produces a named, individual parent.
- **`steered_by=[lead, ...]`** states a pool-level steer: you generated this
  round's pool while conditioning on what an earlier round measured (a hotspot
  list read off a lead's fold, say). The parentage is **cohort-level** — which
  individual lead a given child came from is unknown, so no per-child parent
  is invented, and each child is compared against the **best of the declared
  cohort** instead.
- **`refold_parents=True`** (the default) appends every declared parent to
  this round's fold list, so each child is measured beside its parent **in the
  same panel**, at the same draw count, under the same fold conditions.

### ⛔ The generator takes no parent sequence. Round numbering means nothing on its own.

The served generator has no seed-sequence input. A round `run_round()`
generates with no `derived_from=`/`steered_by=` is a set of **independent,
de novo draws** — running second does not make it a refinement of round 1, and
nothing about "round 2" implies descent from "round 1" unless you declare a
link. An **undeclared round 2 is an independent draw, not a refinement of
round 1**, and this module never reports it as one.

### ⛔ A parent's Kd from an earlier round is not a control.

Fold conditions differ between rounds, and the pose-to-pose spread inside one
fold cell is larger than the difference a refinement round is trying to
detect. So the comparison is **same-cycle refolding, or no comparison at
all** — never a cross-round difference presented as a result:

- With `refold_parents=True` (default), the declared parent is refolded in
  *this* round's panel, and the child is compared against that same-panel
  measurement.
- With `refold_parents=False`, or when a declared parent turns out to be a
  hard negative (measured to prefer an off-target) and is therefore not
  refolded, **nothing is compared** — every field on the resulting
  `ParentComparison` is `None`, the verdict is `NOT_COMPARABLE`, and
  `not_comparable_reason` says why.

A same-panel parent control is also never counted as a survivor of the round
that measured it — it's the baseline, not a new hit, and the round that first
produced it already carries it as one.

---

## The lineage contract

Every design folded by a `RecursiveCampaign` gets exactly one `Lineage` record
per round, keyed by sequence:

```python
@dataclass(frozen=True)
class Lineage:
    sequence: str
    round_index: int          # the round this record describes
    origin: LineageOrigin
    parent: str | None = None
    parent_round: int | None = None
    parent_cohort: tuple[str, ...] = ()
    note: str | None = None
```

`origin` is one of exactly four values (`LineageOrigin`):

| value | meaning |
|---|---|
| `de_novo` | Drawn with no parent. This is what a round produces when `run_round()` is called with no `derived_from=`/`steered_by=` — **including round 2 and later**. |
| `derived` | The caller stated this design's parent (`derived_from={child: parent}`) — a point mutation, a hand-edited variant, an external optimizer's output. The **only** mechanism that produces a per-child parent link anywhere in this module. |
| `reseeded_pool` | The caller generated this round's pool while steering on an earlier round's measurement (`steered_by=[leads]`). Cohort-level parentage: `parent` stays `None` because which individual lead a given child came from is unknown, and none is invented. |
| `parent_control` | An earlier round's design, re-folded in *this* round's panel so its children have a same-panel control. Not a new design, and never counted as a survivor. |

`Lineage.has_parent` is `True` **only** for `origin="derived"` with a named
`parent` — never for a cohort. `campaign.ancestry(sequence)` walks the chain
back to the earliest **named** ancestor and stops there: a `de_novo` or
`reseeded_pool` design has no individual parent to walk to, so the chain ends
where it honestly ends, not at a fabricated root.

### `ParentComparison` — is this variant better than what it came from?

One `ParentComparison` per design with declared lineage, on
`CampaignRound.vs_parent` (and `campaign.comparisons()` across every round):

```python
d = design_comparison
d.same_panel              # False -> every numeric field below is None
d.verdict                 # BETTER | WORSE | MIXED | INDISTINGUISHABLE | NOT_COMPARABLE
d.not_comparable_reason   # set only when verdict == NOT_COMPARABLE
d.delta_improvement       # child Δ - baseline Δ; None unless resolvable
d.delta_improvement_resolvable  # against THIS panel's own measured band
d.kd_fold_change          # parent Kd / child Kd; >1 = child tighter
d.kd_improvement_resolvable
```

`verdict` is one of exactly five values (`ParentVerdict`):

| value | meaning |
|---|---|
| `BETTER` | A resolvable improvement, on the interface Δ, with nothing lost on Kd. |
| `WORSE` | A resolvable regression. |
| `MIXED` | The interface Δ and the Kd disagree — one improved, the other did not. A real outcome, not a fudge: they are two objectives, and a variant that wins one while losing the other has not answered "is it better" with a yes. |
| `INDISTINGUISHABLE` | Measured, in the same panel, and the difference sits inside the measured band on both axes. **"We looked and saw no difference."** |
| `NOT_COMPARABLE` | No fair comparison exists — the parent wasn't refolded in this round's panel (`refold_parents=False`), a declared parent is a hard negative and wasn't refolded, or this design has no complete-panel row this round. **"We could not look."** |

### Why `NOT_COMPARABLE` is reported beside the rest, never folded into `INDISTINGUISHABLE`

"We could not look" and "we looked and saw no difference" are different facts,
and collapsing them would silently turn every unmeasured comparison into a
reported null result. `_comparison_counts()` keeps `NOT_COMPARABLE` as its own
bucket for exactly this reason, and `CampaignRound.not_comparable_to_parent`
exists as its own accessor — "N designs improved" is not the headline if it
hides how many designs were never fairly compared at all.

### Reading it as rows

```python
campaign.ancestry(sequence)          # -> list[Lineage] back to the earliest named ancestor
campaign.comparisons()               # -> list[ParentComparison], every round
rnd.lineage_rows()                   # -> flat per-design rows for a DataFrame/CSV
rnd.improved_over_parent             # -> sequences with verdict == BETTER, resolvably
rnd.not_comparable_to_parent         # -> the ParentComparisons this round could not judge
campaign.report()["lineage"]         # -> the whole campaign's lineage, in one block
```

---

## The min-of-k warning

Any time this module reports a ranked or "best design" view — `PanelResult.deltas()`,
a `SelectivityRatio`, a shortlist sorted by Δ or by Kd fold-selectivity — the
top of that ranking is **not evidence of selective conditioning by itself**.
`min(off-target Kd) / on-target Kd` over k off-targets has a null that sits
**below 1x purely from taking a minimum over k draws**, and that null is
**population-specific**: it must be measured by permutation on the same rows
the claim is made from, never assumed to be 1x and never carried over from a
different panel or a different k. See the "honest result" section of
[contrastive](contrastive.md) for the measured numbers this produced on a
real head-to-head, and
`_global/wiki/concepts/the_minofk_selectivity_null_below_1x_and_populationspecific`
for the full methodology. Never present a hand-picked top design from this
loop as the result — the per-arm distribution against its own measured null is
the result.
