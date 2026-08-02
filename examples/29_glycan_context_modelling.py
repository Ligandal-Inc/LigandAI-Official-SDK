# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""
29 — Glycosylated context modelling: covalent N-glycan TREES on the target.

Stage 5 (optional) of the target-to-order workflow. Builds the explicit
covalent N-glycan trees for a cell-context CD34 glycoform and co-folds the
binder against the GLYCOSYLATED target.

⛔ NO PUBLIC API FOR THIS YET — READ BEFORE RUNNING ⛔
Explicit glycan-tree co-folding needs per-entity `covalent_bonds`, and NO
public REST endpoint or SDK method accepts them today:
  * POST /api/v1/cofold/start takes protein `chains` only (no bonds, no glycan
    entities).
  * `client.peptides.fold(..., glycosylation=True)` is a DIFFERENT, automatic
    feature — it does not let you specify a tree topology.
So this script talks to the Modal worker `predict_structure_from_entities_b200`
directly, and needs Modal credentials. It is still useful WITHOUT them: by
default it BUILDS, VALIDATES and PRINTS the entity/bond graph and writes it to
JSON, so the payload can be inspected and reviewed. Pass --submit to fold.

⛔ GLYCANS MODEL THE NATIVE TARGET — NEVER THE SYNTHESIS CONSTRUCT ⛔
Adaptyv expresses in E. coli cell-free lysate, which does not glycosylate at
all. Glycans belong to this native-context modelling arm only and must never
appear in the sequence you submit for synthesis. (A glycosylated VENDOR target
reagent is not a conflict with this rule — the rule governs your binder, not
their protein.)

WHY TREES AND NOT A BARE GlcNAc. A single NAG is not the CD34 glycoform, and
folding one misrepresents the target: what sits on the sequons is a complex
biantennary tree with core fucosylation and context-specific sialylation, and
the antenna geometry is most of the steric footprint the binder has to bind
around.

THE THREE CONTEXTS are EXPRESSION-DERIVED from glycosyltransferase abundance,
not chosen by hand:

  hairfollicle_skin      ONE antenna, alpha-2,3 sialylation. ST6GAL1 medium,
                         FUT7 low -> mono-sialylated, no sialyl-Lewis-x.

  hair_germinal_matrix   TWO antennae, MIXED linkage: alpha-2,3 on the
                         alpha-1,3 arm (epithelial ST3GAL4) and alpha-2,6 on
                         the alpha-1,6 arm (dermal-papilla ST6GAL1).

  hsc_hematopoietic      TWO antennae, BOTH sialyl-Lewis-x. Native CD34+ HSC
                         has FUT7 and ST3GAL4 both high. CD34 is a sialomucin
                         and selectin ligand, so the alpha-1,3 fucose is the
                         BINDING DETERMINANT, not a decoration.

The hematopoietic glycoform is not a variant of the hair one: the sialylation
preference INVERTS and core fucosylation triples.

CAVEAT, stated rather than hidden: the source expression database has no CD34+
HSC, hair-follicle stem cell or dermal-papilla entry. The DIRECTION of each
contrast is well supported; the absolute transferase abundances are not
HSC-specific.

Run with:
    python 29_glycan_context_modelling.py --peptide DDENGCTAESVKCLEEAKHV
    python 29_glycan_context_modelling.py --peptide ... --context all --submit
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

# CD34 globular ectodomain used for the context-fold arm.
CD34_GLOB = (
    "CSGIREVKLTQGICLEQNKTSSCAEFKKDRGEGLARVLCGEEQADADAGAQVCSLLLAQSEV"
    "RPQCLLLVLANRTEISSKLQLMKKHQSDLKKLGILDFTEQDVASHQSYSQKT"
)
# The two N-X-S/T sequons, in local (construct) numbering.
N_SITES = [18, 73]

CONTEXTS = ("hairfollicle_skin", "hair_germinal_matrix", "hsc_hematopoietic")

# Chain-id pool for the glycan entities. A=receptor, B=peptide. Single letters
# give only 24 usable glycan chains, but the sialyl-Lewis-x context needs 28
# (2 sequons x [6 core + 2 antennae x 4 residues]), so the pool extends into
# two-letter chain ids, which mmCIF permits.
CHAIN_POOL = (
    list("CDEFGHIJKLMNOPQRSTUVWXYZ")
    + [a + b for a in "ABCDEFGH" for b in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"]
)


def _glycan(chain_id: str, ccd: str) -> dict:
    return {"chainId": chain_id, "sequence": ccd, "type": "ligand",
            "entity_type": "glycan"}


def _bond(c1: str, a1: str, c2: str, a2: str, res1: int = 1) -> dict:
    return {"chain1": c1, "res1": res1, "atom1": a1,
            "chain2": c2, "res2": 1, "atom2": a2}


def antenna_sialylated(prev_chain: str, ids: list[str], sia_link: str):
    """NAG -> GAL -> SIA hanging off `prev_chain` O2.

    `sia_link` is the GAL oxygen the sialic acid attaches to:
      "O3" = alpha-2,3   |   "O6" = alpha-2,6
    """
    nag, gal, sia = ids.pop(0), ids.pop(0), ids.pop(0)
    ents = [_glycan(nag, "NAG"), _glycan(gal, "GAL"), _glycan(sia, "SIA")]
    bonds = [
        _bond(prev_chain, "O2", nag, "C1"),
        _bond(nag, "O4", gal, "C1"),
        _bond(gal, sia_link, sia, "C2"),
    ]
    return ents, bonds


def antenna_slex(prev_chain: str, ids: list[str]):
    """Sialyl-Lewis-x antenna: Sia-a2,3-Gal-b1,4-[Fuc-a1,3]-GlcNAc.

    The E/P-selectin ligand and the functional epitope on HSC CD34. The
    alpha-1,3 fucose on the GlcNAc O3 is what makes it sLe^x rather than a
    plain alpha-2,3-sialylated antenna.
    """
    nag, gal, sia, fuc = (ids.pop(0) for _ in range(4))
    ents = [_glycan(nag, "NAG"), _glycan(gal, "GAL"),
            _glycan(sia, "SIA"), _glycan(fuc, "FUC")]
    bonds = [
        _bond(prev_chain, "O2", nag, "C1"),
        _bond(nag, "O4", gal, "C1"),
        _bond(gal, "O3", sia, "C2"),
        _bond(nag, "O3", fuc, "C1"),   # alpha-1,3 fucose — the sLe^x determinant
    ]
    return ents, bonds


def glycan_site(context: str, site: int, ids: list[str]):
    """The shared core plus the context-specific antennae, at one sequon.

    Core (every context):
        A:ND2 --C1-- NAG1 --O4/C1-- NAG2 --O4/C1-- BMA --O3/C1-- MAN(a1,3)
                       \\--O6/C1-- FUC               \\--O6/C1-- MAN(a1,6)
    FUC on NAG1 O6 is FUT8 core fucosylation.
    """
    nag1, nag2, bma, man_a13, man_a16, fuc = (ids.pop(0) for _ in range(6))
    ents = [_glycan(nag1, "NAG"), _glycan(nag2, "NAG"), _glycan(bma, "BMA"),
            _glycan(man_a13, "MAN"), _glycan(man_a16, "MAN"), _glycan(fuc, "FUC")]
    bonds = [
        _bond("A", "ND2", nag1, "C1", res1=site),   # Asn side-chain amide
        _bond(nag1, "O4", nag2, "C1"),
        _bond(nag2, "O4", bma, "C1"),
        _bond(bma, "O3", man_a13, "C1"),
        _bond(bma, "O6", man_a16, "C1"),
        _bond(nag1, "O6", fuc, "C1"),               # FUT8 core fucosylation
    ]

    if context == "hair_germinal_matrix":
        e, b = antenna_sialylated(man_a13, ids, "O3")   # epithelial ST3GAL4
        ents += e; bonds += b
        e, b = antenna_sialylated(man_a16, ids, "O6")   # DP mesenchyme ST6GAL1
        ents += e; bonds += b
    elif context == "hsc_hematopoietic":
        e, b = antenna_slex(man_a13, ids); ents += e; bonds += b
        e, b = antenna_slex(man_a16, ids); ents += e; bonds += b
    else:  # hairfollicle_skin — mono a2,3, no sLe^x
        e, b = antenna_sialylated(man_a13, ids, "O3")
        ents += e; bonds += b

    return ents, bonds


def build(peptide: str, context: str):
    """Full entity + covalent-bond graph for one (peptide, context) fold."""
    entities = [
        # Receptor chain: MSA ON. Receptor chains MUST be folded with an MSA.
        {"chainId": "A", "sequence": CD34_GLOB, "type": "protein",
         "use_msa": True, "geneName": "CD34"},
        # De novo peptide: single-sequence, no MSA (it has no homologs).
        {"chainId": "B", "sequence": peptide, "type": "protein",
         "use_msa": False, "isPeptide": True},
    ]
    bonds: list[dict] = []
    ids = list(CHAIN_POOL)
    for site in N_SITES:
        e, b = glycan_site(context, site, ids)
        entities += e
        bonds += b
    return entities, bonds


def validate(entities: list[dict], bonds: list[dict], peptide: str) -> list[str]:
    """Structural checks on the graph. Returns a list of problems (empty = ok)."""
    problems = []
    chain_ids = [e["chainId"] for e in entities]
    if len(chain_ids) != len(set(chain_ids)):
        problems.append("duplicate chainId in entities")
    known = set(chain_ids)
    for b in bonds:
        if b["chain1"] not in known:
            problems.append(f"bond references unknown chain1 {b['chain1']!r}")
        if b["chain2"] not in known:
            problems.append(f"bond references unknown chain2 {b['chain2']!r}")
    # Every glycan chain must be attached by exactly one incoming bond, or it
    # is a floating residue rather than part of a tree.
    incoming: dict[str, int] = {}
    for b in bonds:
        incoming[b["chain2"]] = incoming.get(b["chain2"], 0) + 1
    for e in entities:
        if e.get("entity_type") != "glycan":
            continue
        n = incoming.get(e["chainId"], 0)
        if n != 1:
            problems.append(
                f"glycan chain {e['chainId']} has {n} incoming bonds (expected 1)"
            )
    # The sequons must exist in the receptor and actually be asparagines.
    for site in N_SITES:
        if site > len(CD34_GLOB):
            problems.append(f"sequon {site} beyond receptor length {len(CD34_GLOB)}")
        elif CD34_GLOB[site - 1] != "N":
            problems.append(
                f"sequon {site} is {CD34_GLOB[site - 1]!r}, expected 'N' "
                "(1-based local numbering)"
            )
    if set(peptide) - set("ACDEFGHIKLMNPQRSTVWY"):
        problems.append("peptide contains non-standard residues")
    return problems


def retain(outdir: str, design_id: str, context: str, idx: int,
           traj: dict, meta: dict) -> list[str]:
    """Persist EVERY artifact a trajectory produced. Never optional.

    Structures are not regenerable without paying the fold again, and a score
    with no structure behind it cannot be re-examined, re-scored under a
    different interface definition, or shipped to a partner.
    """
    import numpy as np

    d = os.path.join(outdir, "structures", f"{design_id}__{context}")
    os.makedirs(d, exist_ok=True)
    written = []

    cif = traj.get("cifContent") or traj.get("cif_content")
    if cif:
        fp = os.path.join(d, f"sample_{idx}.cif")
        with open(fp, "w") as fh:
            fh.write(cif)
        written.append(fp)

    pdb = traj.get("pdbContent") or traj.get("pdb_content")
    if pdb:
        fp = os.path.join(d, f"sample_{idx}.pdb")
        with open(fp, "w") as fh:
            fh.write(pdb)
        written.append(fp)

    pae = traj.get("pae")
    if pae is not None:
        fp = os.path.join(d, f"sample_{idx}.pae.npy")
        np.save(fp, np.asarray(pae, dtype=float))
        written.append(fp)

    # EVERY scalar the engine returned, not only the ones we gate on today.
    scalars = {k: v for k, v in traj.items()
               if k not in ("cifContent", "cif_content", "pdbContent",
                            "pdb_content", "pae")
               and not isinstance(v, (list, dict))}
    fp = os.path.join(d, f"sample_{idx}.meta.json")
    with open(fp, "w") as fh:
        json.dump({**meta, "traj_index": idx, "engine_scalars": scalars}, fh, indent=1)
    written.append(fp)

    with open(os.path.join(outdir, "manifest.jsonl"), "a") as mf:
        for fp in written:
            with open(fp, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
            mf.write(json.dumps({
                "path": os.path.relpath(fp, outdir),
                "sha256": digest,
                "bytes": os.path.getsize(fp),
                "design_id": design_id,
                "context": context,
                "traj_index": idx,
            }) + "\n")
    return written


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Build (and optionally fold) covalent N-glycan trees for a "
                    "CD34 cell-context glycoform."
    )
    ap.add_argument("--peptide", required=True, help="Binder core sequence")
    ap.add_argument("--design-id", default="design")
    ap.add_argument("--context", default="all", choices=("all",) + CONTEXTS)
    ap.add_argument("--diffusion", type=int, default=6)
    ap.add_argument("--outdir", default="glyco_out")
    ap.add_argument("--submit", action="store_true",
                    help="actually fold via Modal (needs Modal credentials and "
                         "spends GPU). Omitted = build + validate + print only.")
    args = ap.parse_args()

    peptide = args.peptide.strip().upper()
    contexts = CONTEXTS if args.context == "all" else (args.context,)
    os.makedirs(args.outdir, exist_ok=True)

    payloads = {}
    for context in contexts:
        entities, bonds = build(peptide, context)
        problems = validate(entities, bonds, peptide)
        n_glycan = sum(1 for e in entities if e.get("entity_type") == "glycan")

        print(f"== {context} ==")
        print(f"  glycan chains : {n_glycan} across {len(N_SITES)} sequons "
              f"{N_SITES}")
        print(f"  covalent bonds: {len(bonds)}")
        print(f"  entities      : {len(entities)} "
              f"(A=CD34 {len(CD34_GLOB)}aa +MSA, B=peptide {len(peptide)}aa -MSA)")
        composition: dict[str, int] = {}
        for e in entities:
            if e.get("entity_type") == "glycan":
                composition[e["sequence"]] = composition.get(e["sequence"], 0) + 1
        print(f"  composition   : {dict(sorted(composition.items()))}")
        if problems:
            print("  !! PROBLEMS:")
            for p in problems:
                print(f"     - {p}")
        else:
            print("  graph OK — every glycan has exactly one parent bond, "
                  "every sequon is an Asn")
        print()

        payloads[context] = {"entities": entities, "covalent_bonds": bonds,
                             "problems": problems}

    payload_path = os.path.join(args.outdir, f"{args.design_id}_glyco_payloads.json")
    with open(payload_path, "w") as fh:
        json.dump(payloads, fh, indent=1)
    print(f"-> {payload_path}")

    if not args.submit:
        print("\nDry run — nothing was folded. Re-run with --submit to fold "
              "(needs Modal credentials; spends GPU).")
        print("REMINDER: these glycans model the NATIVE target. They must NEVER "
              "appear in the sequence you send to Adaptyv (E. coli cell-free "
              "does not glycosylate).")
        return 0

    if any(p["problems"] for p in payloads.values()):
        print("refusing to submit: fix the graph problems above first",
              file=sys.stderr)
        return 1

    try:
        import modal
    except ImportError:
        print("modal is not installed — `pip install modal` and authenticate, "
              "or drop --submit to stay in dry-run mode.", file=sys.stderr)
        return 1

    predict = modal.Function.from_name(
        "ligandal-boltz2", "predict_structure_from_entities_b200"
    )

    for context, payload in payloads.items():
        job_id = f"glyco_{args.design_id}_{context}"
        print(f"\nfolding {context} …")
        try:
            result = predict.remote(
                entities=payload["entities"],
                covalent_bonds=payload["covalent_bonds"],
                job_id=job_id,
                sampling_steps=50,
                diffusion_samples=args.diffusion,
                recycling_steps=3,
                gene="CD34",
            )
        except Exception as exc:  # noqa: BLE001 — worker/transport failures vary
            print(f"  fold failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue

        trajectories = result.get("trajectories") or []
        # RETAIN FIRST, score second — so a scoring failure can never cost a
        # structure that has already been paid for in GPU time.
        kept = 0
        for i, traj in enumerate(trajectories):
            try:
                kept += len(retain(
                    args.outdir, args.design_id, context, i, traj,
                    {"design_id": args.design_id, "context": context,
                     "sequence": peptide, "engine": "boltz2",
                     "n_glycan_chains": len(payload["entities"]) - 2,
                     "job_id": job_id},
                ))
            except Exception as exc:  # noqa: BLE001
                print(f"  !! retain failed for sample {i}: {exc}", file=sys.stderr)
        print(f"  {len(trajectories)} trajectories, {kept} artifacts retained "
              f"(sha256 in {args.outdir}/manifest.jsonl)")

    print("\nREMINDER: glycans model the NATIVE target only. The Adaptyv "
          "submission is the BARE, UNGLYCOSYLATED core.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
