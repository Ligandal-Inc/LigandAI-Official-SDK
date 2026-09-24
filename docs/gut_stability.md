# GI / gut stability

`client.gut_stability` is a developability screen for simulated gastric fluid
(SGF) and simulated intestinal fluid (SIF) protease motifs. It counts cleavage
motifs in a sequence and normalizes by length.

It does **not** predict whether a peptide survives the gut, and nothing it
emits is a probability. Read the next section before the numbers.

## The declaration contract

Every record carries a machine-readable `declaration` block. It is the
contract, not a disclaimer:

| field | value |
|---|---|
| `quantity` | `rule_count_normalized` |
| `not_a_measured_rate` | `True` |
| `not_a_survival_probability` | `True` |
| `not_an_oral_bioavailability_prediction` | `True` |
| `not_a_neural_network` | `True` |

`sgf_risk`, `sif_risk` and `gi_balance_score` are **rule counts normalized by
length** — dimensionless burden per residue, clamped to `[0, 1]`. Higher risk
is worse; higher balance is better. **A risk of 0.86 is not 86% of anything.**
It means the counted motif burden saturated the rule's own normalisation scale.

There is no gut-stability neural network. The whole computation is: count
pepsin / trypsin / chymotrypsin / DPP-IV motifs, divide by length, clamp, add
fixed terms for terminal exposure and length bin, subtract modification rescue.

Do not strip the `declaration` when forwarding a record into your own store —
it is the only thing travelling with the number that says what the number is
not.

### There is no probability field, on purpose

The platform schema declares `scores.sgf_stable_probability` and
`scores.sif_stable_probability`. They are **deliberately absent** from every
payload the platform emits and every payload this SDK builds.

The only model in this feature family that could produce a probability is a
200-RDKit-whole-molecule-descriptor logistic baseline measured on the source
dataset (ROC-AUC 0.8112 gastric on n=46, 0.7435 intestinal on n=63,
five-fold CV). The source dataset has **no amino-acid-sequence column and no
SMILES column**, so there is no path from a designed sequence to those
descriptors — the baseline *cannot be evaluated on a bare sequence*. It is
carried on the declaration as `descriptor_logistic_baseline_not_applied` and
never run.

Writing a rule count into a field named `..._probability` is exactly the
mislabelling the contract forbids. The same applies to
`classes.sgf_stability_class` / `sif_stability_class`: a measured stability
class needs a real SGF/SIF assay, so those stay absent too.

## Entitlement

Enterprise module, `default_enabled = False` for every account. An account
reaches it when **any one** of these holds:

- it is a superadmin, or
- its subscription tier is `enterprise`, or
- it carries an explicit `module_access` grant naming one of `gi_stability`,
  `gi_peptide_stability`, `gi_stability_proxy`, `enterprise_gi_stability`.

A Pro or Academia account without a grant gets an **absent** GI sidecar on its
peptide records. That absence is the entitlement gate working — not a failed
call.

```python
state = client.gut_stability.entitlement()
state["state"]   # 'allowed' | 'denied' | 'indeterminate'
state["reason"]
```

⛔ **Three states, never two.** `entitlement()` answers `"indeterminate"`, not
`"denied"`, for a non-enterprise account: `GET /api/auth/user` does not publish
`module_access`, and an explicit grant is precisely what would flip the answer.
It returns `"denied"` only when a `module_access` record was actually supplied
and carried none of the four keys.

## Scoring

`score_local()` evaluates the rule set in-process — no credits, no round trip —
and reproduces the platform's numbers exactly. Every record carries
`computed_by="ligandai_sdk_local_ruleset"` and a `ruleset_version`, so a
locally scored design can never be mistaken for a platform-computed one.

```python
from ligandai import LigandAI

client = LigandAI(api_key="lgai_...")

out = client.gut_stability.score_local(["QHWSYGLRPG"])   # GnRH / gonadorelin
rec = out["results"][0]

rec["sgf_risk"]          # 0.86   gastric motif burden (NOT a rate)
rec["sif_risk"]          # 0.51   intestinal motif burden (NOT a rate)
rec["gi_balance_score"]  # 0.402  triage score (NOT a probability)
rec["gi_design_class"]   # 'high_gi_developability_risk'
rec["length_bin"]        # 'short_linear_oral_exploration'
```

`score()` is the platform path (`POST /api/scoring/gut-stability`). Where the
deployment does not mount that route it raises `LigandAIParameterNotForwarded`
naming `score_local()`, rather than returning a plausible-looking empty answer.
An unmounted path on a single-page-app server often answers `200` with the app
shell instead of `404`, so both signatures are treated as "the route is not
there". A scorer that genuinely ran and found nothing returns a populated body,
and that body is returned to you.

### Modifications are flags you assert

```python
out = client.gut_stability.score_local(
    ["QHWSYGLRPG"],
    modifications={
        "n_terminal_protected": True,
        "c_terminal_protected": True,
        "cyclized": True,
        "d_residue_count": 0,
        "n_methyl_count": 0,
    },
)
rec = out["results"][0]
rec["sgf_risk"], rec["sif_risk"], rec["gi_balance_score"]
# (0.34, 0.0, 0.814)  ->  'balanced_gi_candidate'
```

⛔ **D-residues and N-methylation are not expressible in the canonical-20
alphabet LigandForge generates over.** The generator cannot produce them and
the scorer cannot detect them from a sequence. They — and `cyclized`,
`n_terminal_protected`, `c_terminal_protected` — describe post-design chemistry
you intend to apply, scored as flags, never assumed and never inferred. Each
rescue term is capped (D-residues 0.12, N-methylation 0.10) and the total caps
at 0.45.

An unsupplied flag defaults to "not modified", which is the conservative case:
it *raises* risk through the terminal-exposure term.

### Absent is not zero

An empty sequence, or one shorter than two residues, has an *undefined* motif
count. Those come back as an explicit absent record with no score fields at
all — a `0.0` would read as a clean bill of health for a design that was never
scorable.

```python
out = client.gut_stability.score_local(["", "A", "QHWSYGLRPG"])
for rec in out["results"]:
    if rec["status"] == "absent":
        print(rec["reason"])     # "... undefined, not zero"
```

### Length bins are a route policy

| bin | route |
|---|---|
| ≤ 12 aa | oral-exploration range; best default for unmodified work |
| 13–30 aa | modification / lumen-local territory |
| > 30 aa | formulation or non-oral territory |

The bin label ships beside the score deliberately, so a long peptide with a low
motif burden cannot read as oral-systemic just because its rule counts are low.

## Reading the sidecar off a design

```python
sidecar = client.gut_stability.read_sidecar(peptide)
if sidecar.get("status") == "absent":
    print(sidecar["reason"])
    local = client.gut_stability.score_local([peptide["sequence"]])
else:
    sidecar["scores"]["gi_balance_score"]
```

The payload may arrive as `gi_stability_proxy`, `gi_stability_scores`,
`giStabilityScores`, or nested under `metadata.gi_stability_proxy`;
`read_sidecar()` checks all four. It returns an explicit absent record rather
than `None`, because an absent sidecar has **two causes that are not the same
fact**: the module was never run for that design (it is default-off), or your
account is not entitled to see it. Call `entitlement()` to tell them apart.

## The generation toggle

The module also has a generation-time face: penalties that steer LigandForge
away from (`mode="resist"`) or toward (`mode="target"`, a deliberately
lumen-released design) the same motifs during denoising. The canonical wire key
is `gi_stability_enabled`; `gut_stability_enabled` is a worker-side alias.

```python
from ligandai.errors import LigandAIParameterNotForwarded

try:
    block = client.gut_stability.generation_settings(strength=2.0, mode="resist")
except LigandAIParameterNotForwarded as exc:
    exc.parameter    # 'gi_stability_enabled'
    exc.reached      # the layer that DOES read it
    exc.dropped_at   # the layer that drops it
    exc.override     # 'allow_unforwarded=True'
```

⛔ **Refused by default.** The generation worker reads `gi_stability_enabled`,
but no API hop in front of it puts the key on the wire — the generate payload
builders emit the immuno, proteolytic, residue-composition and cell-free keys
and no `gi_stability` key at all. Sending it anyway would report the knob **on**
in your request and run the job with it **off**, with nothing in the response
saying so. Pass `allow_unforwarded=True` to get the block regardless, for a
build where the plumbing has landed:

```python
block = client.gut_stability.generation_settings(allow_unforwarded=True)
job = client.peptides.generate(target_gene="GLP1R", num_peptides=100, extra=block)
```

What works today instead: generate normally, then score the returned designs
with `score_local()` and filter on `gi_balance_score`.

## Source

*Dataset: Peptide Stability in the Gastrointestinal Tract*, figshare DOI
[10.6084/m9.figshare.25941580.v1](https://doi.org/10.6084/m9.figshare.25941580.v1)
(CC BY 4.0), paper DOI
[10.1016/j.ijpharm.2023.122643](https://doi.org/10.1016/j.ijpharm.2023.122643).
109 incubation rows — 46 gastric, 63 intestinal — described by 200 RDKit
whole-molecule descriptor columns, with no amino-acid-sequence column and no
SMILES column.

Any production claim about GI stability needs paired SGF/SIF measurements under
the intended formulation and exposure conditions. This module tells you which
designs are worth that assay. It is not the assay, and it carries
`ranking_contribution: 0` — it never moves a design's position on any
leaderboard.
