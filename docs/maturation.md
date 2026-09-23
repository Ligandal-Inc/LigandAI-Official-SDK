# Binder maturation — `client.maturation`

Take binder sequences you **already have** and iteratively improve them — against
one target, or against a panel of close isoforms.

> **Anchor beads:** `bd-dre-rp11j.9`, `bd-dre-rp11j.9.1`, `bd-dre-vd1fk.4`,
> `bd-dre-xfs48`. Site docs: <https://ligandai.com/docs/sdk/python/maturation>.

---

## ⛔ Two different things are called "campaign". Read this first.

| | |
|---|---|
| **`client.maturation`** (this page) | A **server-side** loop. You hand it sequences you already have; the platform folds them, gates on peptide-interface ipSAE, scores, mutates the interface, re-folds the variants **and their parents as same-cycle controls**, keeps what beat its own parent, and repeats. Drives `POST /api/campaign/mature`. |
| **`client.campaigns`** | A **local** panel-folding helper. It composes fold/score primitives *in your process*: `pocket_features`, `contrastive_references`, `fold_panel`, `replicate`, `recursive`. It never calls `/api/campaign/*`. |

Want to **improve sequences you already have**? `client.maturation`.
Want to **draw new sequences** and select among them yourself? `client.campaigns`.

There is no `client.campaigns.mature`. Reaching for it raises
`LigandAISurfaceNotInThisPackage` — not a bare `AttributeError`, which reads like
a typo and would leave "this package cannot do maturation" indistinguishable from
"I called it wrong". The error names `client.maturation.start()`.

The two are deliberately **not** aliased. The other four methods on
`client.campaigns` are local; making one of the five secretly drive a server-side
campaign would give one namespace two unrelated behaviours and make the name
collision permanent instead of pointing out of it.

---

## ⛔ Availability today

The `/api/campaign/*` routes answer on the platform's internal service but are
**not yet proxied through `ligandai.com`** — a call against the public base URL
returns **404**, not 401. The proxy that exposes them (with a quote → reserve →
meter → settle path, so the GPU spend is metered rather than unbounded) is being
built under `bd-dre-xfs48` / `bd-LIGANDAI_ALPHA_V2-6qfgh`.

This client is complete and is tested against the frozen server contract. Nothing
in your code changes when the proxy lands: `start()` already accepts the `quote=`
the metered route will require, and **omits it from the wire entirely** when you
do not pass one, so one call works against both routes.

---

## Import and start

Types live in `ligandai.resources.maturation` and are not re-exported at the
`ligandai` top level.

```python
from ligandai import LigandAI
from ligandai.resources.maturation import (
    MaturationSettings, MaturationSpec, Objective, Receptor, Seed,
)

client = LigandAI(api_key="lgai_...")

spec = MaturationSpec(
    name="BRD lead maturation",
    seeds=[
        Seed("DEWQRALLSYFPE", seed_id="lead_A"),   # seed_id is the lineage anchor
        Seed("DEWQRSLLAYFPE", seed_id="lead_B"),
    ],
    target=Receptor(
        gene="BRDET1",
        receptor_sequence=on_target_sequence,
        pocket_residues=[31, 34, 38, 51],          # in THIS receptor's numbering
        avoid_residues=[77, 80],
    ),
    objective=Objective(target_kd_nM=5.0, max_cycles=3, max_credits=20000),
    maturation=MaturationSettings(cycles=3, n_variants_per_seed=12),
)

run = client.maturation.start(spec)         # POST /api/campaign/mature -> 202

for event in run.stream():
    print(event.event, event.data.get("cycle"))

page = run.leaderboard()
```

Selectivity against a panel:

```python
spec = MaturationSpec(
    seeds=[Seed("DEWQRALLSYFPE", seed_id="lead_A")],
    target=Receptor(gene="BRDET1", receptor_sequence=s1,
                    pocket_residues=[31, 34, 38, 51]),
    # ⛔ Every panel member carries its OWN pocket in its OWN residue frame.
    panel=[
        Receptor(gene="BRDET2", receptor_sequence=s2, pocket_residues=[30, 33, 37, 50]),
        Receptor(gene="BRDET3", receptor_sequence=s3, pocket_residues=[29, 32, 36, 49]),
    ],
    objective=Objective(target_kd_nM=5.0, min_selectivity_margin=2.0),
    objective_mode="selectivity",   # needs a panel AND hotspot_frame="msa_matched"
)
```

Testing against an internal deployment:

```python
client = LigandAI(api_key="...", base_url="http://127.0.0.1:5058")
```

---

## What one cycle does

1. **Fold** every current parent and every proposed variant against the target
   (and every panel receptor, in selectivity mode).
2. **Gate** on the **peptide-interface ipSAE** — the A–B peptide–receptor
   interface — at `ipsae_gate` (default `0.67`), with at least
   `min_poses_in_domain` (floor 2) poses clearing it.
3. **Score** the in-domain poses and take the **median** over them, never the
   best of N.
4. **Mutate** the binder-side interface residues (point moves).
5. **Re-fold** the variants **and the parents, in the same cycle, under the same
   settings**, and compare each variant only against its own same-cycle parent.

Step 5 is the whole design. A parent's Kd from an *earlier* round was measured on
a different pose set; comparing against it compares fold conditions, not
refinement. The server builds each cycle from **pairs**, so a cycle with variants
and no same-round parent baseline is not constructible — and the SDK surfaces
which frame you got on every row, so you can *see* the comparison was fair rather
than assume it.

---

## Reading the result

```python
page = run.leaderboard()          # sort="kd" by default

# ⛔ Three populations, and they are NOT interchangeable.
page.quotable             # a Kd survived every gate
page.assessed_and_failed  # measured, and the interface fell below the gate
page.not_assessed         # never measured at all — an unknown, not a failure

for design in page.quotable:
    d = design.parent_delta
    if d and d.is_win:
        print(design.sequence, design.kd_nM,
              f"{d.fold_improvement:.2f}x its same-cycle parent")
    elif d:
        print(design.sequence, design.kd_nM, "no verdict:", d.uncomparable_reason)

print(page.ranking_caveat.note)   # travels with the page; cannot be detached
```

### The gate: five of six reasons are not a verdict

| `gate_reason` | what happened | evidence against the design? |
|---|---|---|
| `IPSAE_BELOW_GATE` | read, and every pose fell under the gate | **YES — the only one** |
| `IPSAE_UNAVAILABLE` | no pose yielded a binder-restricted value at all | no — never assessed |
| `NO_POSES` | the fold returned nothing | no — never assessed |
| `INSUFFICIENT_POSES` | fewer in-domain poses than the floor | no — under-measured |
| `SCORER_HEAD_MISMATCH` | the energy missed the required calibration head | no — wrong instrument |
| `LENGTH_BIN_UNCALIBRATED` | peptide ≥ 40 aa; that bin's entropy term is deleted | no — out of scope |

```python
r = design.on_target
r.kd_is_quotable        # show a Kd only when this is True
r.is_negative_result    # ⛔ TRUE FOR EXACTLY ONE REASON
r.was_assessed          # False means "we do not know", not "it did badly"

# ⛔ Do NOT count `gated` as "did badly" — five of six reasons mean the
#    instrument did not read.
```

Gate precedence, when more than one condition holds, is
`NO_POSES` → the ipSAE family → `INSUFFICIENT_POSES` →
`LENGTH_BIN_UNCALIBRATED` → `SCORER_HEAD_MISMATCH`. It is derived, not chosen:
nothing else can be true of a fold that returned nothing, and ipSAE is valid at
all lengths, so the structural gate is decided before the energy bin is consulted.

### peptide_ipsae, never the whole-complex value

The binder metric is the **A–B peptide–receptor interface** ipSAE. The
whole-complex value is dragged upward by a well-folded receptor whether or not
the peptide is engaged; the **pre-fold predicted** column is not even on the same
scale — it emits values above 1.0. This SDK range-checks every ipSAE it accepts
and **refuses** anything above 1.0 rather than clamping, because a clamped value
passes every gate below it.

### The parent comparison is same-cycle or nothing

```python
d = design.parent_delta
d.control_frame          # ControlFrame.SAME_CYCLE | ABSENT | UNRECOGNIZED
d.is_win                 # ⛔ None when absent — never False-because-missing
d.fold_improvement       # parent_kd / variant_kd, or None
d.avoid_constraint_held  # None when not comparable, never True-by-default
d.uncomparable_reason    # why there is no verdict, in a sentence
```

`objective_mode="affinity_avoiding"` **holds** `avoid_hit` at or below the
same-cycle parent's. It does not *minimise* it, and a seed that already violates
the constraint is admissible: the violation is inherited, and refinement neither
fixes nor breaks it. `TargetReadout.avoid_hit_inherited_from_seed` separates the
two cases — inherited with a non-positive delta means the seed was dirty and
refinement *held the line*; not-inherited with a positive delta is a real defect.

### Selectivity is complete-and-matched or None

`selectivity_margin` and `selectivity_fold` are `None` — never 0, never a partial
number — unless the panel is **complete** and the frame is **MSA-matched**. A
worst case minimised over a subset of comparators is an upper bound wearing a
lower bound's name: it can only fall when the missing comparators arrive.
`withheld_reason` says which condition failed; `missing_genes` says what is
outstanding.

### Win rates count assessed pairs only

```python
ro = run.readout()                 # GET /api/campaign/{id}/maturation
ro.control_frame_valid             # every cycle compared against same-cycle re-folds?
ro.total_wins_vs_parent            # ⛔ None — never a partial sum — if it is not
ro.win_rate                        # wins / ASSESSED pairs, or None

for cycle in ro.cycles:
    print(cycle.wins_vs_parent, "/", cycle.comparisons, cycle.win_rate_caveat)
```

`comparisons` counts only pairs that were **actually assessed**. Pairs that had a
valid control but whose variant was never measured sit in `pairs_not_comparable`,
outside the denominator. Putting them in understates the result exactly as
counting them losses would — and it does so in the direction that reads as
caution, which is why that version survives review.

### The Kd ordering is a triage prior

The default sort is `kd` and **it is not an affinity ranking**. The deployed Kd
scorer has **no measured-affinity evaluation on any row**, so "design A's Kd <
design B's Kd, same target" is a reported number, not a validated claim. Use the
order to choose what to fold again, not to say which design binds tightest.

Every page carries `ranking_caveat` and it cannot be detached. A **cross-target
ratio** (one binder, two receptors, one scorer) is a different and legitimate
use, and is what `selectivity_fold` is.

`sort="margin"` is **refused**: contact margin fails its own composition-matched
scramble control, and its positive term (hotspot-contact count) *fell* across six
live paired variants while the Kd *improved*. Contact counts are reported on
every readout and are never a gate, a ranking, a promotion rule or a stop
condition.

`AffinityRankingCaveat` also **drops** any correlation coefficient a server sends
and records it on `dropped_statistics`, rather than deleting it silently — the
pair once quoted across this codebase was withdrawn
(`bd-LIGANDAI_ALPHA_V2-jb73b.6`).

### There is no `do_relax` on this path

`Screening` has no `do_relax` field, deliberately. The maturation loop scores the
pose **as folded** through the production scorer, which has no such parameter.
`do_relax` belongs to a different, non-production scoring path — where it does
not even return a relaxed energy: it returns `min(native, relaxed)`, a best-of-2
selection estimator with a downward bias. `ScorerProvenance.uses_forbidden_scorer`
checks that path for you.

---

## Frozen settings

Six server-side settings have **no setter** in the SDK and are emitted at their
frozen values so the payload states them:

| frozen | why |
|---|---|
| `engine="mutagenesis"` | the alternative refiner, built without a generator hook, degrades to a uniform random amino-acid draw at every masked position **while reporting a positive improvement** |
| `pose_statistic="median"` | best-of-N over a handful of poses is an optimistic order statistic |
| `refold_parent_controls=True` | the only reason a variant-vs-parent delta means anything |
| `include_uaa=False` | a non-canonical variant could not be folded, and would vanish silently |
| `gated_on_contact_margin=False` | stated in the payload rather than left to the reader |
| `gated_on_hot_hit=False` | same |

`min_poses_in_domain` has a hard floor of **2** (a median over one pose is a
best-of-1), and `pocket_conditioning` defaults to `variable_only` — measured
paired at equal length and equal ipSAE, the variable-only set was 3.6× tighter
than the full curated hotspot set (p = 0.019), and naming the full set was
*worse than naming nothing*.

---

## Methods

Every method exists on both `client.maturation` and its async twin on
`AsyncLigandAI`.

| method | endpoint |
|---|---|
| `start(spec, quote=None)` → `MaturationRun` | `POST /api/campaign/mature` |
| `status(campaign_id)` → `MaturationStatus` | `GET /api/campaign/{id}/status` |
| `readout(campaign_id)` → `MaturationReadout` | `GET /api/campaign/{id}/maturation` |
| `leaderboard(campaign_id, …)` → `MaturationLeaderboard` | `GET /api/campaign/{id}/leaderboard` |
| `iter_designs(campaign_id, …)` → `Iterator[MaturedDesign]` | paged leaderboard |
| `structure(campaign_id, design_id)` → `str` (PDB) | `GET /api/campaign/{id}/designs/{design_id}/structure` |
| `stop(campaign_id)` → `MaturationStatus` | `POST /api/campaign/{id}/stop` |
| `stream(campaign_id)` → `Iterator[MaturationEvent]` | `GET /api/campaign/{id}/stream` (SSE) |
| `wait(campaign_id, timeout=None)` → `MaturationStatus` | polls `/status` |
| `list()` → `tuple[MaturationStatus, ...]` | `GET /api/campaign` |

`stop()` halts at the next cycle boundary. It does **not** kill folds already in
flight — they were paid for, and killing them throws away the credits and the
measurement both. `wait(timeout=…)` raising means *waiting* stopped; the campaign
did not.

---

## Errors

`MaturationSpec` refuses locally whatever the server would refuse, using the same
code, so a typo and a server rejection read the same.

| code | when |
|---|---|
| `MATURATION_ENGINE_UNSUPPORTED` | a refiner other than the mutagenesis engine |
| `POSE_STATISTIC_UNSUPPORTED` | a pose statistic other than the median |
| `MIN_POSES_TOO_LOW` | `min_poses_in_domain < 2` |
| `PARENT_CONTROL_REQUIRED` | same-cycle parent re-folds disabled |
| `SELECTIVITY_PANEL_MISSING` | selectivity mode with an empty panel |
| `SELECTIVITY_FRAME_UNMATCHED` | selectivity mode in the raw hotspot frame |
| `SEED_NON_CANONICAL` | a seed carries a residue outside the canonical twenty |
| `SEED_ID_DUPLICATE` | two lineage roots would share one id |
| `AVOID_MODE_NEEDS_AVOID` | avoiding mode with no `avoid_residues` |
| `PANEL_FRAME_MISSING` | a panel receptor with no pocket in its own frame |
| `NOT_A_MATURATION_CAMPAIGN` | a maturation readout for a campaign that is not one |
| `CAMPAIGN_ALREADY_RUNNING` | the per-account concurrent-campaign cap |

A campaign id you do not own returns **404, never 403** — a 403 would confirm the
id exists, which is an existence oracle over someone else's namespace. A
malformed id is rejected by the client before it reaches the network, so "you
typed it wrong" and "it is not yours" do not render identically.

`start()` also maps the credit-quote lifecycle the metered public route will
enforce — `quote_required` / `quote_invalid` (400) and `quote_expired` /
`quote_inputs_changed` / `quote_superseded` (409). ⛔ Nothing here ever refetches
a quote or relaunches at a new price: a price that moved after you accepted it
needs a new acceptance, and that decision is yours.

It also maps the platform's **spend-authorization** denials, which arrive as
`credit_authorization_required` with a `reason`. ⛔ These are not balance
problems, and this client does not report them as one: a missing, expired,
replayed, or request-mismatched authorization means the call was never authorized
to spend, and the fix is to accept a quote — not to top up. Each reason's message
says which. A `SPEND_GRANT_UNCONFIGURED` / `_METER_UNAVAILABLE` /
`_PRICE_UNAVAILABLE` / `_CLAIM_FAILED` is a server-side fault: the platform
refuses to start GPU work it cannot price, meter or record, rather than running
it unmetered. Nothing was queued and nothing was charged in any of these cases.
