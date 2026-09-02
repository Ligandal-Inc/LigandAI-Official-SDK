# Fold a peptide-receptor complex (Boltz-2)

## A. Fold an arbitrary peptide against a receptor

```python
from ligandai import LigandAI

client = LigandAI()
fold_job = client.peptides.fold(
    sequences=["HHHHHHGGGGS" + "ACDEFGHIKLMNPQRS"],
    gene="EGFR",
    num_trajectories=1,         # 1 is enough for screening
    use_msa=True,               # never fold without MSA for the receptor
)
fold_result = fold_job.wait(timeout=1800)
print(fold_result.peptides[0].ipsae, fold_result.peptides[0].predicted_kd)
```

## B. Continue folding from a previous generation

```python
# Generate 50, fold the top 5, then later fold 20 more from the same session
job = client.peptides.generate(gene="EGFR", num_peptides=50, auto_fold=True, top_n_fold=5)
result = job.wait()

more = client.peptides.continue_folding(
    session_id=result.session_id,
    additional_top_n=20,
)
more.wait()
```

## C. Hotspot-aware partitioning post-fold

```python
parts = client.folds.partition_by_hotspot(
    session_id=result.session_id,
    hotspots=[{"chain": "A", "residue": 60, "numbering": "pdb"}],
    distance_threshold_a=5.0,
)
print(len(parts.hotspot_hit), len(parts.hotspot_miss))

# Expand a hotspot residue into the full surrounding pocket
expanded = client.folds.expand_hotspot(
    gene="EGFR", chain="A", residue=60, radius_a=8.0,
)
print(expanded.pocket_residues)
```

## D. Fold with a single-residue mutation in the receptor

```python
mut = client.peptides.fold_custom_mutation(
    fold_id=fold_result.peptides[0].fold_id,
    mutation="K745R",          # PDB numbering
)
mut_done = mut.wait()
```

## Score-only (skip Boltz, just compute DeltaForge / LigandIQ)

```python
score = client.peptides.score_complex(
    sequences=["RGDFKMEYHLA"], gene="EGFR",
)
print(score.peptides[0].ligandiq, score.peptides[0].deltaforge_dg)
```

## Always pass `fold_gpus=` matching tier

```python
caps = {"free": 1, "basic": 4, "academia": 16, "pro": 25, "enterprise": 50}
fold_gpus = caps.get(client.tier, 1)
```

---

## E. Ensemble co-fold — N peptides x M engines, and a consensus

`client.peptides.cofold(...)` folds ONE complex on several engines.
`client.ensemble` is the campaign-scale layer: many candidates, many engines,
a cost projection before anything runs, a spend ceiling, and a **consensus**
instead of M separate leaderboards.

### Why you would use it

A single folding engine gives you one opinion. If you are about to pay to
synthesise 10 of 25 designs, the question that matters is not "what is the
ranking" but **"how much of my shortlist survives changing the engine"**. A
candidate every engine puts above the bar is a different proposition from one
only the default engine likes.

### Cost first — it never runs before you have seen the number

```python
from ligandai import LigandAI
client = LigandAI(api_key="...")

est = client.ensemble.estimate(
    engines=["boltz2", "esmfold2", "protenix"],
    n_peptides=25,
)
print(est)
# ensemble co-fold: 25 complexes x 3 engines = 75 folds
#   projected 14,175 credits (~$141.75), 10% bundle discount
#     boltz2      5 traj x 25 = 6250 cr
#     esmfold2    5 traj x 25 = 2125 cr
#     protenix    3 traj x 25 = 4800 cr
```

The rates come from the platform's own pricing endpoint
(`POST /api/v1/cofold/estimate`) — the SDK never re-derives them, so the
estimate cannot drift from the invoice.

### Run it, with a ceiling

```python
result = client.ensemble.run(
    receptor=my_receptor_sequence,
    peptides=my_candidate_sequences,       # 25 designs
    engines=["boltz2", "esmfold2", "protenix"],
    bar=0.67,                               # your shortlist threshold
    max_credits=15_000,                     # hard ceiling; raises before submitting
)

print(result.consensus_histogram())
# {0: 4, 1: 7, 2: 6, 3: 8}   <- 8 designs all three engines put above the bar

for row in result.unanimous():
    print(row.sequence, row.per_engine)

for a in result.rank_agreement():
    print(f"{a['a']} vs {a['b']}: rho={a['spearman']} (n={a['n']})")
```

`dry_run=True` returns the estimate and submits nothing. `max_credits` raises
**before** the first fold if the projection exceeds it — nothing is charged.
If you already hold a campaign `Budget`, pass `budget=` instead and the
ceiling comes from `budget.max_credits`, so a campaign spec and an ensemble
run share one number rather than two.

### Cofactors are part of the interface

Many targets have no meaningful binding interface without their cofactor — a
catalytic metal, a heme, a bound nucleotide. Folding such a target without it
is a confident measurement of a different molecule.

Engines split in two: those that take structured **entities** and can carry a
ligand, and those that take chains or a bare sequence and cannot. The API
makes that split visible rather than averaging over it:

```python
client.ensemble.cofactor_support(["boltz2", "esmfold2", "protenix", "opendde"])
# {'boltz2':   {'engine_can_carry': True,  'reachable_today': ..., 'reason': ...},
#  'esmfold2': {'engine_can_carry': True,  ...},
#  'protenix': {'engine_can_carry': True,  ...},
#  'opendde':  {'engine_can_carry': False, 'reason': 'engine takes chains/sequences
#               only — no ligand entity'}}
```

`engine_can_carry` is what the model can do. `reachable_today` is whether the
hosted co-fold route can currently express it. They are reported separately so
you are never told "unsupported" when the real answer is "the model supports
it, the route does not carry it yet".

Asking for a cofactor an arm cannot honour **raises**:

```python
client.ensemble.run(receptor=..., peptides=..., cofactors=["ZN", "ZN"])
# LigandAIInvalidConfig: cofactor(s) ZN requested, but no engine in
# ['boltz2','esmfold2','protenix'] can receive them through the hosted co-fold
# route ... These folds would run WITHOUT the cofactor, which on a
# cofactor-dependent target scores a different molecule. Pass
# allow_cofactor_absent=True to run them anyway; every affected arm is then
# labelled cofactor_honoured=False and is never pooled with an arm that
# carried it.
```

That refusal is deliberate. It is the difference between "your binders score
0.79" and "your binders score 0.79 *on a protein that is missing the two zincs
they bind through*".

### Worked example — carbonic anhydrase II (CA2), a zinc metalloenzyme

CA2 is the textbook case: a single catalytic Zn²⁺ coordinated by three
histidines, and the pocket does not exist without it.

```python
from ligandai import LigandAI

client = LigandAI(api_key="...")

CA2 = (  # UniProt P00918, mature enzyme
    "SHHWGYGKHNGPEHWHKDFPIAKGERQSPVDIDTHTAKYDPSLKPLSVSYDQATSLRILNNGHAFNVEFD"
    "DSQDKAVLKGGPLDGTYRLIQFHFHWGSLDGQGSEHTVDKKKYAAELHLVHWNTKYGDFGKAVQQPDGLA"
    "VLGIFLKVGSAKPGLQKVVDVLDSIKTKGKSADFTNFDPRGLLPESLDYWTYPGSLTTPPLLECVTWIVL"
    "KEPISVSSEQVLKFRKLNFNGEGEPEELMVDNWRPAQPLKNRQIKASFK"
)

candidates = [...]        # your designed binder sequences

# 1. What will this cost?
est = client.ensemble.estimate(["boltz2", "esmfold2", "protenix"], len(candidates))
print(est)

# 2. Which arms can carry the catalytic zinc?
print(client.ensemble.cofactor_support(["boltz2", "esmfold2", "protenix"]))

# 3. Run. Because CA2's pocket is defined by its zinc, ask for it explicitly —
#    and let the call refuse rather than quietly hand back a zinc-free number.
result = client.ensemble.run(
    receptor=CA2,
    peptides=candidates,
    engines=["boltz2", "esmfold2", "protenix"],
    cofactors=["ZN"],
    gene="CA2",
    bar=0.67,
    max_credits=20_000,
)

# 4. The deliverable: who does every engine agree on?
print(result.consensus_histogram())
for row in result.unanimous():
    print(row.sequence, row.per_engine, row.cofactor_honoured)
```

Substitute any target: `cofactors=["HEM"]` for a heme protein, `["MG", "ATP"]`
for a kinase, `cofactors=[{"smiles": "..."}]` for anything without a CCD code.

### Reading the result

| field | meaning |
|---|---|
| `consensus_histogram()` | how many designs had k engines above the bar |
| `unanimous()` | designs every engine that returned a score put above the bar |
| `row.contested` | some engines above the bar, some below — the ones to look at |
| `row.n_engines_scored` | the DENOMINATOR. An engine that failed is not counted as a vote against |
| `row.cofactor_honoured` | per engine, whether the cofactor you asked for actually reached that fold |
| `rank_agreement()` | pairwise Spearman rho between engines, each with its own n |
| `failures` | every fold that did not return, with its error. Nothing is imputed |

⛔ A metric an engine did not return is `None`, never `0.0`. A missing score is
not evidence of a bad interface, and it never counts toward or against the
consensus.

### Today's limitation, stated plainly

The multi-engine ensemble route cannot carry a cofactor yet — its chain spec
accepts protein chains only, for every engine. Three of the engines
(`boltz2`, `esmfold2`, `protenix`) and `openfold3` can each carry a ligand in
their own worker; the shared route in front of them cannot express one. That is
tracked as `bd-dre-3jqok.23`.

Until it lands, the way to get a **cofactor-bearing** fold through the SDK is
the single-engine Boltz-2 batch surface, which does support ligand partners:

```python
client.peptides.fold_batch(
    receptor=CA2,
    peptides=candidates,
    partners=[{"type": "ligand", "ccd": "ZN", "copies": 1}],
)
```

So the honest workflow on a cofactor-dependent target right now is: Boltz-2
**with** the cofactor for the number you act on, and the ensemble for the
agreement check — reading the ensemble as cofactor-absent, which
`cofactor_support()` and `row.cofactor_honoured` both say out loud.

### What an ensemble is and is not worth

An ensemble buys you **agreement information**, not a better ranking. Be clear
about which one you are asking for.

On the largest public dataset where these engines are scored against *real
measured binding* — 1,320 designs across 15 targets, both CROs, 354 confirmed
binders — a rank-averaged ensemble does not reliably beat the best single
engine:

| scorer | AUROC vs measured binding | 95% CI |
|---|---|---|
| best single engine (Chai-1) | 0.744 | 0.714–0.774 |
| 3-engine selection ensemble (ESMFold2 + ESMFold2-Fast + Protenix v2) | 0.721 | 0.694–0.749 |
| all 8 engines, rank-averaged | 0.745 | 0.717–0.773 |

Every interval overlaps. Our own internal benchmark found the same thing:
ensembling scored *below* Boltz-2 alone (0.620 vs 0.625 ipSAE AUROC).

So do not run an ensemble expecting a sharper ranking. Run it to find out
**which candidates only one engine likes** — `row.contested` — because that is
the group whose evidence rests on a single model, and it is the group worth a
second look before you spend synthesis money on it.

⛔ And note what the bar means. A threshold like 0.67 is calibrated on one
engine's ipSAE scale. Applying it unchanged to an engine whose ipSAE distribution
tops out lower measures the *scale*, not the *disagreement*. `rank_agreement()`
is scale-free and is the number to trust when engines are calibrated differently.
