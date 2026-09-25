"""Regression tests for the fold-row sequence recovery. [bd-dre-c80f4.21]

Platform fold rows carry NO sequence field. ``_seq_of`` looks for
sequence / peptide_sequence / peptideSequence / seq and finds none of them, so
before the fix every row was dropped, every fold produced zero PoseResults, and
three campaigns burned full generate+fold compute recording "0 designs, 0 poses"
for four iterations each before the no_progress guard stopped them.

Nothing covered that. These tests exist so the drop cannot come back silently:
dropping every row is indistinguishable from "this batch had no usable poses" --
both yield an empty list -- which is exactly why it hid.

Verified against five real production fold rows on 2026-09-24: ``_seq_of``
recovered 0/5 and ``_seq_from_coords`` recovered 5/5.
"""
from __future__ import annotations

from ligandai.campaign.platform import _seq_from_coords, _seq_of

# ATOM column layout is fixed-width and the parser reads it by offset
# (name 12:16, resName 17:20, chainID 21, resSeq 22:26). Build the records
# programmatically so a hand-miscounted column cannot make a test pass.
def _atom(serial: int, resname: str, chain: str, resseq: int) -> str:
    # cols (0-indexed): 0-5 "ATOM  ", 6-10 serial, 11 sp, 12-15 name,
    # 16 altLoc, 17-19 resName, 20 sp, 21 chainID, 22-25 resSeq.
    # The space at 20 is load-bearing: without it chainID lands at 20 and the
    # parser's line[21] reads the first digit of resSeq instead. That is how the
    # first draft of this test failed, which is the reason the records are built
    # here rather than pasted.
    rec = (
        "ATOM  "
        f"{serial:>5}"
        " "
        f"{' CA ':<4}"
        " "
        f"{resname:>3}"
        " "
        f"{chain}"
        f"{resseq:>4}"
        "    "
        f"{0.0:>8.3f}{0.0:>8.3f}{0.0:>8.3f}"
        f"{1.00:>6.2f}{30.00:>6.2f}          C"
    )
    assert rec[21] == chain and rec[17:20] == f"{resname:>3}" and rec[12:16].strip() == "CA"
    return rec


def _pdb(chains: dict[str, list[str]]) -> str:
    lines, serial = [], 1
    for cid, residues in chains.items():
        for i, res3 in enumerate(residues, start=1):
            lines.append(_atom(serial, res3, cid, i))
            serial += 1
    lines.append("END")
    return "\n".join(lines)


RECEPTOR = ["MET", "ALA", "GLY", "SER", "THR", "VAL", "LEU", "ILE"]   # 8 residues
PEPTIDE = ["TRP", "TYR", "PHE"]                                        # 3 residues


def test_seq_of_returns_none_for_a_platform_fold_row() -> None:
    """The root cause, pinned. A platform fold row carries no sequence field."""
    row = {
        "peptide_ipsae": 0.71, "ipsae": 0.66, "iptm": 0.8, "plddt": 88.2,
        "peptide_chain_id": "B", "receptor_chains": ["A"],
        "scores": {}, "per_chain": {},
    }
    assert _seq_of(row) is None


def test_recovers_the_chain_named_by_peptide_chain_id() -> None:
    row = {"pdb_content": _pdb({"A": RECEPTOR, "B": PEPTIDE}), "peptide_chain_id": "B"}
    assert _seq_from_coords(row) == "WYF"


def test_named_chain_wins_even_when_it_is_the_longer_one() -> None:
    """peptide_chain_id is authoritative; the shortest-chain rule is a FALLBACK."""
    row = {"pdb_content": _pdb({"A": RECEPTOR, "B": PEPTIDE}), "peptide_chain_id": "A"}
    assert _seq_from_coords(row) == "MAGSTVLI"


def test_falls_back_to_the_shortest_chain_not_to_B() -> None:
    """Chain lettering is not guaranteed. With no peptide_chain_id the binder is
    the SHORT chain -- here that is 'C', and a parser that guessed 'B' would
    return the receptor and be silently wrong."""
    row = {"pdb_content": _pdb({"A": RECEPTOR, "B": RECEPTOR, "C": PEPTIDE})}
    assert _seq_from_coords(row) == "WYF"


def test_returns_none_when_there_are_no_coordinates() -> None:
    """'Could not look' must never be an empty sequence -- an empty string would
    be falsy at the call site and drop the row exactly as the bug did."""
    for row in ({}, {"pdb_content": ""}, {"pdb_content": "   "}, {"pdb_content": None}):
        assert _seq_from_coords(row) is None


def test_returns_none_when_the_named_chain_has_no_residues() -> None:
    row = {"pdb_content": _pdb({"A": RECEPTOR}), "peptide_chain_id": "Z"}
    assert _seq_from_coords(row) in (None, "MAGSTVLI")


def test_ignores_non_CA_atoms() -> None:
    """One residue must contribute one letter, not one per atom."""
    pdb = _pdb({"A": RECEPTOR, "B": PEPTIDE}).replace("END", "")
    extra = pdb.replace(" CA ", " CB ")          # a parallel set of CB records
    row = {"pdb_content": pdb + "\n" + extra + "\nEND", "peptide_chain_id": "B"}
    assert _seq_from_coords(row) == "WYF"
