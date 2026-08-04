# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""
32 — Excluding a receptor surface with avoid_residues (and proving it worked).

[bd-LIGANDAI_ALPHA_V2-x4vt8]

The scenario: a small helical domain construct, chain A, numbered 1-67. We want
a peptide that binds the back helix so the partner-binding groove is occluded,
while keeping the acidic loop at residues 36-46 clear — that surface has to stay
available.

The three things this example exists to demonstrate
---------------------------------------------------

1. `target_residues` / `avoid_residues` / `hotspot_residues` are PDB RESIDUE
   NUMBERS as numbered in the receptor structure you are designing against.
   They are NOT indices into any internal array and NOT sequence offsets.
   `chain` is part of a residue's identity and is honoured.

2. `avoid_residues` is enforced UNCONDITIONALLY, in every targeting mode. You do
   not need `target_residues`, `hotspot_residues` or a pocket config for it to
   apply. The server also drops the residues packing against the excluded ones
   within 8 A (Ca-Ca) — excluding only the named residues would leave their
   immediate neighbours in the pocket, and a peptide steered onto those
   neighbours still lies across the surface you asked to keep clear. The radius
   is the generation-API field `avoid_shell_angstrom` (default 8.0, 0 disables).

3. ⚠ Targeting acts on pocket FEATURIZATION. It is a strong prior, NOT a hard
   geometric constraint — the downstream folding step can still place a peptide
   on the excluded face. An exclusion is not verified until you have looked at
   the folded complex, which is what step 3 below does. The server-side
   equivalent is `fastapi_backend/core/residue_targeting.py::contacts_violating_avoid`.

Run with:
    LIGANDAI_API_KEY=lgai_... LIGANDAI_GENE=MY_DOMAIN python 32_avoid_residues_exclusion.py
"""

from __future__ import annotations

import os
import sys

from ligandai import LigandAI
from ligandai.errors import LigandAIError
from ligandai.types import ResidueRange

# The construct's own PDB numbering (chain A, residues 1-67).
CHAIN = "A"
BACK_HELIX = [(22, 26), (50, 60)]   # bind this — occludes the groove
ACIDIC_LOOP = (36, 46)              # keep this clear


def _resi(ranges: list[tuple[int, int]]) -> list[str]:
    """Expand (start, end) pairs into "A:37"-style residue identifiers."""
    return [f"{CHAIN}:{r}" for start, end in ranges for r in range(start, end + 1)]


def main() -> int:
    key = os.environ.get("LIGANDAI_API_KEY")
    if not key:
        print("LIGANDAI_API_KEY env var is required", file=sys.stderr)
        return 1

    gene = os.environ.get("LIGANDAI_GENE", "MY_DOMAIN")
    client = LigandAI(api_key=key)

    try:
        # ── 1. Generate ────────────────────────────────────────────────────
        # Two target ranges name the back-helix face to bind; one avoid range
        # names the acidic loop. All in the construct's PDB numbering.
        print(f"Generating against {gene} — binding {BACK_HELIX}, "
              f"excluding {ACIDIC_LOOP} (+ 8 A packing shell)")
        job = client.peptides.generate(
            gene=gene,
            num_peptides=500,
            target_residues=[
                ResidueRange(chain=CHAIN, start=s, end=e) for s, e in BACK_HELIX
            ],
            avoid_residues=[
                ResidueRange(chain=CHAIN, start=ACIDIC_LOOP[0], end=ACIDIC_LOOP[1]),
            ],
            # Omit for the 8.0 A server default; 0 excludes ONLY the named
            # residues. Range [0, 50], validated before any HTTP call.
            avoid_shell_angstrom=8.0,
            auto_fold=True,
            top_n_fold=50,
        )
        result = job.wait(timeout=2400)
        print(f"  generated={result.peptides_generated} folded={result.peptides_folded}")

        # ── 2. Did the designs land on the face we aimed at? ───────────────
        back_helix_resi = _resi(BACK_HELIX)
        partition = client.folds.partition_by_hotspot(
            session_id=result.session_id,
            hotspots=back_helix_resi,
            pocket_residues=back_helix_resi,
            contact_distance_a=5.0,
        )
        print(f"  on the intended face: {len(partition.passes_hotspot)}")
        print(f"  bound elsewhere:      {len(partition.wrong_interface)}")

        # ── 3. Prove the exclusion held on the FOLDED structures ───────────
        # Generation conditioning is a prior, not a constraint. Query the
        # EXCLUDED residues as the hotspot set: every hit is a violation.
        violators = client.peptides.search(
            session_id=result.session_id,
            hotspot_residues=_resi([ACIDIC_LOOP]),
            hotspot_hit=True,
            contact_distance_a=5.0,
            limit=500,
        )
        violating_ids = {p.peptide_id for p in violators}
        for p in violators:
            contacts = sorted(c["chain_resi"] for c in (p.hotspot_contacts or []))
            print(f"  REJECT {p.peptide_id}: contacted excluded loop {contacts}")

        # Keep only designs that left the loop alone, ranked by the binder
        # metric (peptide-chain ipSAE, not the global ipsae).
        keep = [
            p for p in client.peptides.search(
                session_id=result.session_id,
                sort="ipsae", order="desc", limit=500,
            )
            if p.peptide_id not in violating_ids
        ]
        print(f"\n{len(violating_ids)} designs violated the exclusion; "
              f"{len(keep)} kept the acidic loop clear")
        for p in keep[:10]:
            print(f"  {p.peptide_id}  peptide_ipsae={p.peptide_ipsae}  {p.sequence}")

    except LigandAIError as exc:
        print(f"LigandAI error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
