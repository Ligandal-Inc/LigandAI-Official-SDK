# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""
30 — Assemble the BLI cassette, then place an Adaptyv Foundry order.

Stage 6, the last one. Assembles the TwinStrep-L1-GFP11-L2-core cassette for
the record, then creates an Adaptyv experiment as a DRAFT. Submission (the step
that costs real money) is a separate, explicit flag.

⛔ SUBMIT BARE CORES — NOT THE ASSEMBLED CONSTRUCT ⛔
Adaptyv's cell-free platform appends its OWN TwinStrep + splitGFP tag
server-side, per design. Submitting your pre-fused cassette on top of that
DOUBLE-TAGS every design, or mismatches their splitGFP11 fragment against
yours. This is not a preference — it is the format of the only prior order that
reached results (LGL-001-010), whose spec carries:
    "sequence_policy": "binder_core_only_provider_appended_tag"
    "provider_appended_tag": "TwinStrep+splitGFP"
    metadata.tag_location: "N"   (per sequence)
You still assemble the cassette locally: the BLI gate scores tag-junction
interference on it, and you need its length and extinction coefficient for the
transmittal record. It just is not what you submit.

⛔ CONTROLS ARE NOT OPTIONAL ⛔
LGL-001-010 shipped 10 CD8 designs with ZERO controls and came back 6/10
expressed, 0/10 bound. With no positive control there is no internal check that
the assay or the target lot was even capable of detecting binding that week —
and that target had a history of non-specific binding. A 0/10 result with no
controls cannot distinguish "the designs do not bind" from "the assay did not
work". This script REFUSES to build an order without a negative control and
warns loudly without a positive one.

⛔ NO GLYCANS IN THE SUBMITTED SEQUENCE ⛔
Adaptyv is E. coli cell-free, which does not glycosylate. Glycans belong to the
native-context modelling arm only (see 29_glycan_context_modelling.py).

ORDER MECHANICS (verified against the live API, not docs):
  * MINIMUM 24 SEQUENCES per experiment, for both `affinity` and `screening`
    types. Below that, creation returns HTTP 400. Controls count toward it.
  * There is NO CSV bulk-ingest endpoint. Sequences go in as a JSON object
    keyed by name. Keep a CSV as your own transmittal record — it converts 1:1.
  * Draft first, submit second. Creating an experiment costs nothing; the
    submit call triggers the quote and the charge.
  * Not every catalogue target is self-service priced — some return
    "materials unavailable" and need a manual Adaptyv quote before submission.

⚠ SDK/SERVER SHAPE MISMATCH (as of this writing)
`client.synthesis.adaptyv_create()` posts {name, targetId, sequences,
includeBli}, but POST /api/adaptyv/experiments requires {name,
experiment_spec:{...}} and rejects anything else with
`400 name and experiment_spec are required`. So this example builds the
experiment_spec itself and posts through `client.transport`, the SDK's
documented escape hatch for endpoints not yet wrapped by a typed namespace.
Swap back to the typed method once the shapes agree.

Run with:
    LIGANDAI_API_KEY=lgai_... python 30_adaptyv_cassette_order.py \\
        folds_median.json --gene CD34 --target-id <adaptyv-target-uuid>
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys

from ligandai import LigandAI
from ligandai.errors import LigandAIError, LigandAITierError

# --- The cassette. Fixed parts are fixed. -----------------------------------
# TwinStrep's internal GGGSGGGSGG is part of the TwinStrep definition itself,
# NOT a linker you control. L1/L2 are the variable part.
TWINSTREP = "SAWSHPQFEK" + "GGGSGGGSGG" + "SAWSHPQFEK"   # 30 aa
GFP11 = "RDHMVLHEYVNAAGIT"                                # 16 aa
REF_L1, REF_L2 = "GSGG", "GSGS"                           # reference linkers
AA20 = set("ACDEFGHIKLMNPQRSTVWY")

# Live-verified minimum. Controls count toward it.
ADAPTYV_MIN_SEQUENCES = 24

# BLI dose series used by the campaign's payloads (nM), plus the zero-antigen
# reference well.
ANTIGEN_CONCENTRATIONS = [1000.0, 316.2, 100.0, 31.2, 0.0]
N_REPLICATES = 3


def assemble(core: str, l1: str = REF_L1, l2: str = REF_L2) -> str:
    """TwinStrep - L1 - GFP11 - L2 - core, N-terminal tag placement."""
    return TWINSTREP + l1 + GFP11 + l2 + core


def core_span(core: str, l1: str = REF_L1, l2: str = REF_L2) -> tuple[int, int]:
    start = len(TWINSTREP) + len(l1) + len(GFP11) + len(l2)
    return start, start + len(core)


def validate_cassette(core: str, l1: str = REF_L1, l2: str = REF_L2) -> str:
    """Assemble and prove the core round-trips out of the construct."""
    full = assemble(core, l1, l2)
    bad = sorted(set(full) - AA20)
    if bad:
        raise ValueError(f"non-standard residues {bad} in construct")
    if not core:
        raise ValueError("empty core")
    start, end = core_span(core, l1, l2)
    if full[start:end] != core:
        raise AssertionError("core span does not round-trip")
    return full


def load_deliverable(path: str) -> list[dict]:
    """Accept the output of 28_median_of_k_validation.py, or a plain list."""
    raw = json.load(open(path))
    rows = raw.get("scored", raw) if isinstance(raw, dict) else raw
    out = []
    for i, r in enumerate(rows):
        if isinstance(r, str):
            out.append({"cand_id": f"d{i}", "core": r.strip().upper()})
            continue
        core = r.get("core") or r.get("sequence")
        if not core:
            continue
        # Only ship designs that cleared the MEDIAN gate, when that is recorded.
        if r.get("passes_median_gate") is False:
            continue
        out.append({
            "cand_id": r.get("cand_id") or f"d{i}",
            "core": str(core).strip().upper(),
            "peptide_ipsae_median": r.get("peptide_ipsae_median"),
            "peptide_ipsae_spread": r.get("peptide_ipsae_spread"),
            "n_samples": r.get("n_samples"),
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Assemble the BLI cassette and create an Adaptyv draft order."
    )
    ap.add_argument("deliverable", help="JSON from 28_median_of_k_validation.py")
    ap.add_argument("--gene", required=True)
    ap.add_argument("--target-id", help="Adaptyv catalogue target uuid. Omit to "
                                        "search and print candidates, then stop.")
    ap.add_argument("--n", type=int, default=25, help="designs to include")
    ap.add_argument("--positive-control", metavar="SEQ",
                    help="Known natural ligand for this target.")
    ap.add_argument("--positive-control-name", default="CTRL-POS")
    ap.add_argument("--negative-control", metavar="SEQ",
                    help="Composition-matched scramble of the positive control.")
    ap.add_argument("--negative-control-name", default="CTRL-NEG-scram")
    ap.add_argument("--create", action="store_true",
                    help="actually create the DRAFT experiment (still free)")
    ap.add_argument("--out-prefix", default="adaptyv_order")
    args = ap.parse_args()

    if not os.environ.get("LIGANDAI_API_KEY"):
        print("LIGANDAI_API_KEY env var is required", file=sys.stderr)
        return 1

    designs = load_deliverable(args.deliverable)[: args.n]
    if not designs:
        print(f"no median-gate-passing designs in {args.deliverable}",
              file=sys.stderr)
        return 1

    # --- Assemble the cassette for every design (record, not submission) ----
    print(f"== cassette assembly ({len(designs)} designs) ==")
    print(f"  prefix ({len(TWINSTREP) + len(REF_L1) + len(GFP11) + len(REF_L2)} aa): "
          f"{TWINSTREP + REF_L1 + GFP11 + REF_L2}")
    for d in designs:
        try:
            d["construct"] = validate_cassette(d["core"])
            d["construct_len"] = len(d["construct"])
        except (ValueError, AssertionError) as exc:
            print(f"  !! {d['cand_id']}: {exc}", file=sys.stderr)
            return 1
        # Count cysteines one final time. cys_mode is a soft bias, and a prior
        # CD34 deliverable shipped 8/25 designs at 4, 6 and 8 Cys because
        # nothing checked the finished sequence.
        d["n_cys"] = d["core"].count("C")
        if d["n_cys"] not in (0, 2):
            print(f"  !! {d['cand_id']}: {d['n_cys']} cysteines — the Adaptyv "
                  "lysate is REDUCING; 0 or exactly 2 only", file=sys.stderr)
            return 1
    print(f"  all {len(designs)} cassettes assemble and round-trip "
          f"(core lengths {min(len(d['core']) for d in designs)}-"
          f"{max(len(d['core']) for d in designs)})")

    # --- Controls -----------------------------------------------------------
    if not args.negative_control:
        print("\n!! REFUSING TO BUILD AN ORDER WITHOUT A NEGATIVE CONTROL.\n"
              "   LGL-001-010 shipped with zero controls: 6/10 expressed, 0/10\n"
              "   bound, and no way to tell a real negative from a dead assay.\n"
              "   Pass --negative-control <composition-matched scramble>.",
              file=sys.stderr)
        return 1
    if not args.positive_control:
        print("\n!! WARNING: no positive control.\n"
              "   Without one there is no internal check that the assay and the\n"
              "   target lot could detect binding at all this week. Source a\n"
              "   known ligand for this target before final submission.\n")

    # --- Build the sequences map. BARE CORES ONLY. --------------------------
    sequences: dict[str, dict] = {}
    for d in designs:
        sequences[str(d["cand_id"])] = {
            "aa_string": d["core"],          # <- BARE CORE, never d["construct"]
            "control": False,
            "metadata": {"tag_location": "N"},
        }
    if args.positive_control:
        sequences[args.positive_control_name] = {
            "aa_string": args.positive_control.strip().upper(),
            "control": True,
            "metadata": {"tag_location": "N"},
        }
    sequences[args.negative_control_name] = {
        "aa_string": args.negative_control.strip().upper(),
        "control": True,
        "metadata": {"tag_location": "N"},
    }

    n_seq = len(sequences)
    print(f"\n== order composition ==")
    print(f"  designs        : {len(designs)}")
    print(f"  controls       : {n_seq - len(designs)}")
    print(f"  total sequences: {n_seq}  (Adaptyv minimum {ADAPTYV_MIN_SEQUENCES})")
    below_minimum = n_seq < ADAPTYV_MIN_SEQUENCES
    if below_minimum:
        print(f"  !! {ADAPTYV_MIN_SEQUENCES - n_seq} SHORT of the minimum. "
              "Experiment creation will return HTTP 400.\n"
              "     Fold more designs rather than padding with filler "
              "sequences.")

    experiment_spec = {
        "experiment_type": "affinity",
        "method": "bli",
        "target_id": args.target_id,
        "sequences": sequences,
        "n_replicates": N_REPLICATES,
        "antigen_concentrations": ANTIGEN_CONCENTRATIONS,
        "parameters": {
            # The policy that makes bare-core submission explicit on the record.
            "sequence_policy": "binder_core_only_provider_appended_tag",
            "provider_appended_tag": "TwinStrep+splitGFP",
            "validated_no_refold": True,
            "additional_info": (
                "Sequences are binder-core only (no glycans, E. coli cell-free "
                "compatible). Adaptyv appends its own TwinStrep+splitGFP tag "
                "N-terminally per design. Designs selected on the MEDIAN of k "
                "fold trajectories, not the max."
            ),
        },
    }

    # --- Write the local transmittal record ---------------------------------
    # There is no CSV bulk-ingest endpoint; this CSV is our own audit format and
    # converts 1:1 into the JSON sequences map above.
    csv_path = f"{args.out_prefix}_{args.gene.lower()}_sequences.csv"
    with open(csv_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["name", "aa_string_core", "control", "tag_location",
                    "peptide_ipsae_median", "peptide_ipsae_spread", "n_samples",
                    "n_cys", "construct_len_ref"])
        for d in designs:
            w.writerow([d["cand_id"], d["core"], False, "N",
                        d.get("peptide_ipsae_median"), d.get("peptide_ipsae_spread"),
                        d.get("n_samples"), d["n_cys"], d["construct_len"]])
        for name, s in sequences.items():
            if s["control"]:
                w.writerow([name, s["aa_string"], True, "N", "", "", "",
                            s["aa_string"].count("C"), ""])

    payload_path = f"{args.out_prefix}_{args.gene.lower()}_experiment_spec.json"
    with open(payload_path, "w") as fh:
        json.dump(experiment_spec, fh, indent=1)
    print(f"\n-> {csv_path}      (internal transmittal record)")
    print(f"-> {payload_path}  (exact experiment_spec body)")

    if not args.target_id:
        print("\nNo --target-id given. Searching the Adaptyv catalogue…")
        try:
            client = LigandAI()
            targets = client.synthesis.adaptyv_search_targets(args.gene, limit=10)
            for t in targets:
                print(f"  {t.id}  {t.name}  organism={t.organism}")
            print("\nRe-run with --target-id <uuid> to build the draft.")
        except LigandAITierError as exc:
            print(f"TIER-LOCKED: {exc}", file=sys.stderr)
            return 3
        except LigandAIError as exc:
            print(f"API error: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        return 0

    if not args.create:
        print("\nDry run — no experiment was created. Re-run with --create to "
              "create the DRAFT (still free; submission is a separate step).")
        return 0

    if below_minimum:
        print("\nrefusing to call the API below the sequence minimum",
              file=sys.stderr)
        return 1

    # --- Create the DRAFT ---------------------------------------------------
    client = LigandAI()
    body = {
        "name": f"{args.gene} BLI affinity — LigandAI top {len(designs)} + controls",
        "experiment_spec": experiment_spec,
        # Recombinant mode keeps submitted sequences binder-only and lets
        # Adaptyv append the assay tag. 'n_term' is the default placement.
        "synthesis_mode": "recombinant",
        "tag_position": "n_term",
    }
    try:
        # client.transport is the SDK's documented escape hatch for endpoints
        # not yet wrapped by a typed namespace. See the SDK/server shape
        # mismatch note in this file's docstring.
        created = client.transport.request(
            "POST", "/api/adaptyv/experiments", json=body
        ) or {}
    except LigandAIError as exc:
        print(f"create failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    exp_id = created.get("id") or (created.get("experiment") or {}).get("id")
    code = created.get("code") or (created.get("experiment") or {}).get("code")
    status = created.get("status") or (created.get("experiment") or {}).get("status")
    print(f"\n== DRAFT created ==")
    print(f"  id     : {exp_id}")
    print(f"  code   : {code}")
    print(f"  status : {status}")
    print("\nNothing has been charged. The submit call below triggers the quote")
    print("and the charge — run it deliberately, after reviewing the draft:")
    print(f"  client.synthesis.adaptyv_submit({exp_id!r})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
