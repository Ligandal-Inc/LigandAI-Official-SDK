# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""
27 — Pre-fold gating, standalone: score YOUR OWN sequences, show every gate.

Stage 3 of the target-to-order workflow, usable on its own. Feed it any list of
peptide sequences (a JSON file, or the pool written by 26_gated_generation.py)
and it prints every gate value and the pass/fail reason for each design.

WHY THIS STAGE EXISTS. Generation costs seconds; folding costs GPU-hours.
Immunogenicity, expressability, proteolytic stability, thermostability and BLI
suitability are ALL decidable from sequence alone. An earlier campaign folded
~2,800 designs screened on nothing but length, surfaced 3 over the ipSAE gate,
and then found its best binder (ipSAE 0.8259) carried an MHC-I burden of 9.20
per 100 residues — worse than the 5.23 mean of UNGUIDED generation. Guidance is
a soft logit bias that moves the population mean; individual designs escape it
in both directions. Having the immuno head on does NOT license skipping the
per-design check. Measured pass rate here: 10-35% of designs.

WHAT IS GATED vs WHAT IS ONLY REPORTED. A threshold with nothing behind it is
worse than no threshold. Three cutoffs were REMOVED from this gate after
measurement:

  * Guruprasad instability > 40 — calibrated on proteins, no discrimination on
    our peptide wet-lab outcomes. On the 10-design CD8v2 Adaptyv set (6
    expressed / 4 failed): expressed median 36.2 vs failed 39.7, and the two
    HIGHEST-instability designs (66.6, 66.4) both expressed HIGH. The cutoff
    flagged 2/4 true failures while discarding 2/6 good designs — chance — and
    rejected 67% of the pool. Reported and ranked, never gated.

  * A280 chromophore on the BARE CORE — the cassette always carries
    chromophores (TwinStrep contributes 2 Trp, GFP11 one Tyr; eps280 = 12,490
    even for a completely W/Y-free core). Gating the core rejected 92.7% of
    designs for a problem the construct does not have, and collided head-on
    with de-immunisation since Trp/Tyr are exactly the MHC anchor residues the
    immuno head removes. Measured on the CONSTRUCT here.

  * Cleavage density > 0.60 — invented, not calibrated. Once density was
    computed correctly (it had silently been 0.000 for every design, i.e. a
    gate that always passes) that cutoff rejected 60 of 61 CD34 designs. Kept
    in the rank score only.

Also: the cell-free module's `terminal_consecutive_basic` flag is NOT gated.
Re-deriving its claimed accuracy against Adaptyv ground truth found the labels
inverted — both designs it flags (CD8v2-008-N, CD8v2-011-N) expressed HIGH
across three replicates.

Tier: academia+ for score_developability and the immunogenicity scanner.

Run with:
    LIGANDAI_API_KEY=lgai_... python 27_prefold_gating.py gated_pool_cd34.json
    LIGANDAI_API_KEY=lgai_... python 27_prefold_gating.py DDELNQYSRSISGTSPPLHLRD
"""

from __future__ import annotations

import json
import math
import os
import sys

from ligandai import LigandAI
from ligandai.errors import LigandAIError, LigandAITierError, LigandAIUpgradeRequired

# --- The Adaptyv BLI cassette (see 30_adaptyv_cassette_order.py) -------------
# TwinStrep's internal GGGSGGGSGG is part of the TwinStrep definition itself,
# not a linker you control. L1/L2 are the variable part.
TWINSTREP = "SAWSHPQFEK" + "GGGSGGGSGG" + "SAWSHPQFEK"
GFP11 = "RDHMVLHEYVNAAGIT"
REF_L1, REF_L2 = "GSGG", "GSGS"

AA20 = set("ACDEFGHIKLMNPQRSTVWY")

# Thresholds. Provenance for each is stated because an invented cutoff that
# looks principled is worse than no cutoff at all.
THRESHOLDS = {
    # Unguided population mean 5.23; guided mean at immuno@3.0 is 3.47. 3.0
    # keeps designs at or better than the guided mean rather than merely inside
    # the generated distribution.
    "max_mhc_i_per100": 3.0,
    "max_mhc_ii_per100": 6.0,
    # Ikai 1980. A low bar chosen to exclude only the degenerate tail.
    "min_aliphatic_index": 20.0,
    # Module-internal 0-1 scores.
    "min_cellfree_score": 0.50,
    "min_bli_score": 0.50,
    # 0 Cys, or exactly 2 forming one intramolecular disulfide.
    "allowed_cys_counts": (0, 2),
}

# Recorded but NOT gated — the module's labels were re-derived and found
# inverted against Adaptyv ground truth.
MISDERIVED_CELLFREE_RULE = "terminal_consecutive_basic"

USAGE = (
    "usage: python 27_prefold_gating.py <sequences.json | SEQUENCE>\n"
    "  e.g. python 27_prefold_gating.py gated_pool_cd34.json\n"
    "       python 27_prefold_gating.py DDELNQYSRSISGTSPPLHLRD"
)

# DevelopabilityResult surfaces the headline GRADES at the top level and keeps
# the numeric detail in a nested `immuno_scores` dict. The exact key spelling
# has varied across server builds, so probe a candidate list rather than
# hard-coding one — and treat "not found" as UNSCORED, never as a pass.
_MHC_I_KEYS = ("mhc_i_per100", "mhc_i_burden_per_100", "mhc_i_burden", "class_i_per100")
_MHC_II_KEYS = ("mhc_ii_per100", "mhc_ii_burden_per_100", "mhc_ii_burden", "class_ii_per100")


def _first_numeric(payload: dict | None, keys: tuple[str, ...]) -> float | None:
    """First numeric value among `keys` in `payload`, else None."""
    if not isinstance(payload, dict):
        return None
    for k in keys:
        v = payload.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


def assemble_construct(core: str, l1: str = REF_L1, l2: str = REF_L2) -> str:
    """TwinStrep - L1 - GFP11 - L2 - core, N-terminal tag placement."""
    return TWINSTREP + l1 + GFP11 + l2 + core


def aliphatic_index(seq: str) -> float:
    """Ikai 1980: 100*(A + 2.9*V + 3.9*(I+L)) as mole fractions."""
    n = len(seq) or 1
    f = {a: seq.count(a) / n for a in "AVIL"}
    return 100.0 * (f["A"] + 2.9 * f["V"] + 3.9 * (f["I"] + f["L"]))


def halflife_cavaco(seq: str, pi: float) -> dict:
    """Cavaco et al. 2021 (PMID 33641212) plasma half-life regression.

        ln(t1/2) = 2.226 + 0.053*NP% - 1.515*W(0,1) + 1.290*Y(0,1) - 1.052*pI(0,1)

    Trp SHORTENS half-life (metabolic oxidation via the kynurenine pathway)
    despite being nonpolar; Tyr EXTENDS it; high pI shortens it (renal clearance
    of basic peptides).

    CAVEAT, stated rather than hidden: the paper's (0,1) normalisation for the
    W / Y / pI terms is not recorded in our source, so W and Y are taken as
    presence indicators and pI is min-max scaled over 3-12. That makes the
    ABSOLUTE t1/2 indicative, not a quantitative prediction — which is why this
    is reported and ranked, never gated. The RANKING depends only on the signs,
    which are unambiguous.
    """
    n = len(seq) or 1
    np_frac = 100.0 * sum(seq.count(a) for a in "AVILMFPGW") / n
    w = 1.0 if "W" in seq else 0.0
    y = 1.0 if "Y" in seq else 0.0
    pi_n = max(0.0, min(1.0, (pi - 3.0) / 9.0))
    ln_t = 2.226 + 0.053 * np_frac - 1.515 * w + 1.290 * y - 1.052 * pi_n
    return {
        "ln_t_half": ln_t,
        "np_percent": np_frac,
        "has_trp": w,
        "has_tyr": y,
        "score_0_1": 1.0 / (1.0 + math.exp(-(ln_t - 4.0))),
    }


def local_biophysics(core: str) -> dict:
    """GRAVY / pI / instability via Biopython — real implementations, not
    re-derived. Biopython is optional; the fields are omitted (never guessed)
    when it is absent."""
    try:
        from Bio.SeqUtils.ProtParam import ProteinAnalysis
    except ImportError:
        return {"_biopython": False}
    pa = ProteinAnalysis(core)
    try:
        instability = pa.instability_index()
    except Exception:  # noqa: BLE001 — degenerate composition
        instability = float("nan")
    return {
        "_biopython": True,
        "gravy": pa.gravy(),
        "isoelectric_point": pa.isoelectric_point(),
        "instability_index": instability,
        "construct_molar_extinction": ProteinAnalysis(
            assemble_construct(core)
        ).molar_extinction_coefficient()[0],
    }


def load_sequences(arg: str) -> list[str]:
    """Accept a JSON file (list of strings or of dicts) or a bare sequence."""
    if os.path.exists(arg):
        raw = json.load(open(arg))
        out = []
        for item in raw:
            seq = item if isinstance(item, str) else (
                item.get("sequence") or item.get("core")
            )
            if seq:
                out.append(str(seq).strip().upper())
        return out
    return [arg.strip().upper()]


def main() -> int:
    if not os.environ.get("LIGANDAI_API_KEY"):
        print("LIGANDAI_API_KEY env var is required", file=sys.stderr)
        return 1
    if len(sys.argv) < 2:
        print(USAGE, file=sys.stderr)
        return 1

    sequences = load_sequences(sys.argv[1])
    if not sequences:
        print("no sequences found", file=sys.stderr)
        return 1
    print(f"gating {len(sequences)} design(s)\n")

    client = LigandAI()

    try:
        # --- Server-side grades (academia+) ---------------------------------
        # score_developability returns the stability / immunogenicity /
        # half-life grades with NO regeneration and NO refold.
        grades = client.peptides.score_developability(sequences=sequences)
        by_seq = {
            str(getattr(g, "sequence", "")).upper(): g for g in grades
        }

        # Cell-free (CFPS) expression risk + BLI suitability in one call.
        expression = client.analysis.score_expression(sequences, include_bli=True)
        expr_rows = expression.get("results") or []
        expr_by_seq = {
            str(r.get("sequence", "")).upper(): r for r in expr_rows
        }

    except LigandAIUpgradeRequired as exc:
        print(f"UPGRADE REQUIRED: {exc}", file=sys.stderr)
        print("  score_developability needs academia / pro / enterprise.", file=sys.stderr)
        return 3
    except LigandAITierError as exc:
        print(f"TIER-LOCKED: {exc}", file=sys.stderr)
        return 3
    except LigandAIError as exc:
        print(f"API error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    passed, report = [], []
    for core in sequences:
        fails: list[str] = []
        row: dict = {"core": core, "core_len": len(core)}

        # --- construct assembly: the cassette must actually build ------------
        bad = sorted(set(core) - AA20)
        if bad:
            fails.append(f"non_standard_aa {bad}")
        construct = assemble_construct(core)
        row["construct"] = construct
        row["construct_len"] = len(construct)

        # --- cysteine count, PER DESIGN, not inherited from cys_mode --------
        # cys_mode is a soft bias. A prior CD34 deliverable came back with 8/25
        # designs at 4, 6 and 8 cysteines (8.75% Cys overall vs ~1.4% natural)
        # because nothing counted them on the finished sequence.
        n_cys = core.count("C")
        row["n_cys"] = n_cys
        row["cys_positions"] = [i + 1 for i, a in enumerate(core) if a == "C"]
        if n_cys not in THRESHOLDS["allowed_cys_counts"]:
            fails.append(
                f"n_cys {n_cys} not in {THRESHOLDS['allowed_cys_counts']} "
                "(reducing E. coli cell-free lysate: unpaired thiols scramble)"
            )

        # --- immunogenicity, PER DESIGN, not inherited from generation ------
        # The numeric burden lives in the nested immuno_scores dict; the
        # top-level field is the letter grade. An UNSCORED design is a FAIL,
        # never a pass — a gate that silently passes when its input is missing
        # is exactly the bug that left cleavage_density at 0.000 for every
        # design while looking like a working filter.
        g = by_seq.get(core)
        immuno_scores = getattr(g, "immuno_scores", None) if g else None
        mhc_i = _first_numeric(immuno_scores, _MHC_I_KEYS)
        mhc_ii = _first_numeric(immuno_scores, _MHC_II_KEYS)
        row["immuno_grade"] = getattr(g, "immuno_grade", None) if g else None
        row["stability_grade"] = getattr(g, "stability_grade", None) if g else None
        row["immuno_scores"] = immuno_scores
        row["mhc_i_per100"], row["mhc_ii_per100"] = mhc_i, mhc_ii

        if mhc_i is None:
            fails.append("unscored:mhc_i (no numeric burden in immuno_scores)")
        elif mhc_i > THRESHOLDS["max_mhc_i_per100"]:
            fails.append(f"mhc_i {mhc_i:.2f} > {THRESHOLDS['max_mhc_i_per100']}")

        if mhc_ii is None:
            fails.append("unscored:mhc_ii (no numeric burden in immuno_scores)")
        elif mhc_ii > THRESHOLDS["max_mhc_ii_per100"]:
            fails.append(f"mhc_ii {mhc_ii:.2f} > {THRESHOLDS['max_mhc_ii_per100']}")

        # --- cell-free expression + BLI (Adaptyv's platform) ----------------
        e = expr_by_seq.get(core) or {}
        risk = e.get("composite_risk")
        cf_score = None if risk is None else 1.0 - float(risk)
        row["cellfree_score"] = cf_score
        row["cellfree_prediction"] = e.get("expression_prediction")
        row["cellfree_flags"] = e.get("hard_flags")
        # Drop the mis-derived rule BEFORE gating (labels were inverted).
        hard = [f for f in (e.get("hard_flags") or []) if f != MISDERIVED_CELLFREE_RULE]
        row["cellfree_hard_gating"] = hard
        if hard:
            fails.append(f"cellfree_hard:{','.join(map(str, hard))}")
        elif cf_score is None:
            fails.append("unscored:cellfree (no composite_risk returned)")
        elif cf_score < THRESHOLDS["min_cellfree_score"]:
            fails.append(f"cellfree {cf_score:.2f} < {THRESHOLDS['min_cellfree_score']}")

        bli = e.get("bli") or e.get("bli_suitability") or {}
        bli_overall = bli.get("overall_bli_score") if isinstance(bli, dict) else None
        row["bli_overall"] = bli_overall
        # tag_interference_risk is driven by terminal_consecutive_basic and
        # inherits its mis-derivation, so it is recorded but not gated.
        row["bli_tag_interference"] = (
            bli.get("tag_interference_risk") if isinstance(bli, dict) else None
        )
        if bli_overall is None:
            fails.append("unscored:bli (call score_expression with include_bli=True)")
        elif bli_overall < THRESHOLDS["min_bli_score"]:
            fails.append(f"bli {bli_overall:.2f} < {THRESHOLDS['min_bli_score']}")

        # --- local biophysics ------------------------------------------------
        bio = local_biophysics(core)
        row.update({k: v for k, v in bio.items() if k != "_biopython"})
        ali = aliphatic_index(core)
        row["aliphatic_index"] = ali
        if ali < THRESHOLDS["min_aliphatic_index"]:
            fails.append(f"aliphatic {ali:.1f} < {THRESHOLDS['min_aliphatic_index']}")

        # A280 measured on the CONSTRUCT, never the bare core.
        if not bio.get("_biopython"):
            # Say so rather than skipping silently: three gates (A280 on the
            # construct, GRAVY/pI, and the Cavaco half-life that needs pI)
            # cannot be evaluated without it.
            row["local_biophysics_unavailable"] = "pip install biopython"
        else:
            ext = row.get("construct_molar_extinction")
            if ext == 0:
                fails.append("no_a280_chromophore_on_construct")

        # --- REPORTED, NOT GATED --------------------------------------------
        if bio.get("_biopython"):
            hl = halflife_cavaco(core, row["isoelectric_point"])
            row["halflife_ln_t_half"] = hl["ln_t_half"]
            row["halflife_score"] = hl["score_0_1"]
            row["halflife_has_trp"] = hl["has_trp"]   # Trp SHORTENS half-life
            row["halflife_has_tyr"] = hl["has_tyr"]   # Tyr EXTENDS it
        row["halflife_gated"] = False
        row["instability_gated"] = False   # Guruprasad: no measured discrimination
        row["cleavage_density_gated"] = False

        row["fails"] = fails
        row["passes"] = not fails
        report.append(row)
        if row["passes"]:
            passed.append(row)

    # --- Print every gate value, per design ---------------------------------
    for r in report:
        flag = "PASS" if r["passes"] else "FAIL"
        print(f"[{flag}] {r['core'][:44]}  len={r['core_len']} "
              f"construct_len={r['construct_len']}")
        print(f"       n_cys={r['n_cys']} at {r['cys_positions']}")
        print(f"       MHC-I={r['mhc_i_per100']} (gate <= {THRESHOLDS['max_mhc_i_per100']})  "
              f"MHC-II={r['mhc_ii_per100']} (gate <= {THRESHOLDS['max_mhc_ii_per100']})")
        print(f"       cellfree={r['cellfree_score']} (gate >= {THRESHOLDS['min_cellfree_score']})  "
              f"prediction={r['cellfree_prediction']}  flags={r['cellfree_flags']}")
        print(f"       BLI={r['bli_overall']} (gate >= {THRESHOLDS['min_bli_score']})  "
              f"tag_interference={r['bli_tag_interference']} (reported, not gated)")
        print(f"       aliphatic={r['aliphatic_index']:.1f} (gate >= "
              f"{THRESHOLDS['min_aliphatic_index']})  "
              f"instability={r.get('instability_index')} (reported, NOT gated)")
        print(f"       GRAVY={r.get('gravy')}  pI={r.get('isoelectric_point')}  "
              f"t1/2 ln={r.get('halflife_ln_t_half')} (reported, NOT gated)")
        print(f"       construct eps280={r.get('construct_molar_extinction')} "
              "(measured on the CONSTRUCT, not the bare core)")
        if r["fails"]:
            print(f"       FAILS: {r['fails']}")
        print()

    rate = 100.0 * len(passed) / max(len(report), 1)
    print(f"{len(passed)}/{len(report)} passed ({rate:.1f}%) — "
          "campaign-measured range is 10-35%")

    # Two files, deliberately: the survivors feed the next stage, and the FULL
    # report (including every failure and its reason) is kept so a gate can be
    # re-examined later without re-running anything.
    with open("prefold_report.json", "w") as fh:
        json.dump(report, fh, indent=1)
    with open("prefold_survivors.json", "w") as fh:
        json.dump(passed, fh, indent=1)
    print(f"-> prefold_report.json     (all {len(report)} designs + fail reasons)")
    print(f"-> prefold_survivors.json  ({len(passed)} designs worth a GPU)")
    print("   Next: 28_median_of_k_validation.py — quote first, then fold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
