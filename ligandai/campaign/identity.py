# Copyright © 2025 Ligandal, Inc. All rights reserved.
"""Pose identity derived from structure CONTENT, never from a name.

D7 (the expensive one).  In the ENPP3 tree an arm named for one paralog
contained on-target folds: 2,760 of 8,812 sequence-matching poses carried the
wrong receptor.  Arm names, directory names and file names are metadata.  They
are not identity and this package never treats them as identity.

Identity here is:
  * the receptor chain's amino-acid sequence, SHA-256'd
  * the peptide chain's amino-acid sequence, parsed and kept verbatim
  * the observed chain order, the chain count, and the HETATM cofactors present

Which chain is the receptor is resolved by matching the parsed sequences against
the construct sequences DECLARED IN THE CAMPAIGN SPEC.  Only when the spec
declares nothing does it fall back to "longest chain is the receptor", and that
fallback is recorded on the identity so downstream code can refuse it.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import os
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable, Mapping, Sequence

# 3-letter -> 1-letter.  Non-standard residues map to 'X' rather than being
# dropped, so a modified residue cannot silently shorten a sequence and change
# its hash.
_AA3 = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C", "GLN": "Q",
    "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I", "LEU": "L", "LYS": "K",
    "MET": "M", "PHE": "F", "PRO": "P", "SER": "S", "THR": "T", "TRP": "W",
    "TYR": "Y", "VAL": "V", "SEC": "U", "PYL": "O",
    # common modified/alt names seen in our folds
    "MSE": "M", "HIE": "H", "HID": "H", "HIP": "H", "CYX": "C", "CYM": "C",
    "ASH": "D", "GLH": "E", "LYN": "K", "ARN": "R", "HYP": "P",
}

# HETATM residue names that are solvent/buffer, not cofactors we care about.
_IGNORED_HET = {"HOH", "WAT", "DOD", "SO4", "PO4", "GOL", "EDO", "PEG", "TRS", "MPD", "ACT"}


def _open_any(path: str):
    if path.endswith(".gz"):
        return io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8", errors="replace")
    return open(path, "r", encoding="utf-8", errors="replace")


def sha256_seq(seq: str) -> str:
    """Hash of an amino-acid sequence, case- and whitespace-normalised."""
    norm = "".join(seq.split()).upper()
    return hashlib.sha256(norm.encode("ascii", "replace")).hexdigest()


@dataclass(frozen=True)
class ChainRecord:
    chain_id: str
    sequence: str
    length: int
    sha256: str
    n_atoms: int
    order_index: int          # position in the file, 0-based

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PoseIdentity:
    """What a pose file actually contains.  Derived only from its bytes."""

    source_path: str
    file_sha256: str
    chains: tuple[ChainRecord, ...]
    receptor_chain_id: str | None
    peptide_chain_id: str | None
    receptor_sha256: str | None
    receptor_length: int | None
    peptide_sequence: str | None
    peptide_sha256: str | None
    peptide_length: int | None
    cofactors: tuple[str, ...]                 # HETATM resnames, sorted, deduped
    chain_order: tuple[str, ...]
    n_chains: int
    assignment_method: str                     # 'spec_match' | 'length_fallback' | 'unresolved'
    warnings: tuple[str, ...] = ()

    @property
    def is_resolved(self) -> bool:
        return self.receptor_sha256 is not None and self.peptide_sequence is not None

    @property
    def trusted_assignment(self) -> bool:
        """True only when the receptor was matched against a DECLARED construct."""
        return self.assignment_method == "spec_match"

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["chains"] = [c.as_dict() for c in self.chains]
        d["is_resolved"] = self.is_resolved
        d["trusted_assignment"] = self.trusted_assignment
        return d


def parse_chains(path: str) -> tuple[list[ChainRecord], list[str], str]:
    """Parse per-chain sequences and HETATM cofactors out of a PDB / mmCIF file.

    Returns (chains, cofactor_resnames, file_sha256).
    """
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    file_sha = h.hexdigest()

    if path.endswith((".cif", ".cif.gz", ".mmcif", ".mmcif.gz")):
        chains, het = _parse_cif(path)
    else:
        chains, het = _parse_pdb(path)
    return chains, het, file_sha


def _finalise(order: list[str], seqs: dict[str, list[str]], atoms: dict[str, int]) -> list[ChainRecord]:
    out: list[ChainRecord] = []
    for i, cid in enumerate(order):
        s = "".join(seqs[cid])
        out.append(ChainRecord(cid, s, len(s), sha256_seq(s), atoms[cid], i))
    return out


def _parse_pdb(path: str) -> tuple[list[ChainRecord], list[str]]:
    seqs: dict[str, list[str]] = {}
    seen: dict[str, set] = {}
    atoms: dict[str, int] = {}
    order: list[str] = []
    het: set[str] = set()
    with _open_any(path) as fh:
        for line in fh:
            rec = line[:6]
            if rec == "ATOM  " or rec == "HETATM":
                resname = line[17:20].strip().upper()
                cid = line[21:22].strip() or "_"
                # icode-aware residue key so insertion codes do not collapse residues
                reskey = (line[22:27].strip(), resname)
                if rec == "HETATM" and resname not in _AA3:
                    if resname not in _IGNORED_HET:
                        het.add(resname)
                    continue
                if cid not in seqs:
                    seqs[cid] = []
                    seen[cid] = set()
                    atoms[cid] = 0
                    order.append(cid)
                atoms[cid] += 1
                if reskey not in seen[cid]:
                    seen[cid].add(reskey)
                    seqs[cid].append(_AA3.get(resname, "X"))
            elif rec == "ENDMDL":
                break  # first model only — a multi-model file is one pose here
    return _finalise(order, seqs, atoms), sorted(het)


def _parse_cif(path: str) -> tuple[list[ChainRecord], list[str]]:
    """Minimal mmCIF atom_site reader.  Handles the column order we emit."""
    seqs: dict[str, list[str]] = {}
    seen: dict[str, set] = {}
    atoms: dict[str, int] = {}
    order: list[str] = []
    het: set[str] = set()
    cols: dict[str, int] = {}
    in_loop = False
    with _open_any(path) as fh:
        for line in fh:
            ls = line.strip()
            if ls.startswith("_atom_site."):
                cols[ls.split(".", 1)[1].split()[0]] = len(cols)
                in_loop = True
                continue
            if not in_loop:
                continue
            if ls.startswith(("#", "loop_", "_")) or not ls:
                if cols and ls.startswith("#"):
                    break
                continue
            parts = ls.split()
            need = ("group_PDB", "label_comp_id", "auth_asym_id", "auth_seq_id")
            idx = {}
            for k in need:
                if k in cols:
                    idx[k] = cols[k]
                elif k == "auth_asym_id" and "label_asym_id" in cols:
                    idx[k] = cols["label_asym_id"]
                elif k == "auth_seq_id" and "label_seq_id" in cols:
                    idx[k] = cols["label_seq_id"]
                else:
                    return [], sorted(het)
            if max(idx.values()) >= len(parts):
                continue
            group = parts[idx["group_PDB"]]
            resname = parts[idx["label_comp_id"]].strip('"').upper()
            cid = parts[idx["auth_asym_id"]].strip('"') or "_"
            resnum = parts[idx["auth_seq_id"]]
            if group == "HETATM" and resname not in _AA3:
                if resname not in _IGNORED_HET:
                    het.add(resname)
                continue
            if cid not in seqs:
                seqs[cid] = []
                seen[cid] = set()
                atoms[cid] = 0
                order.append(cid)
            atoms[cid] += 1
            key = (resnum, resname)
            if key not in seen[cid]:
                seen[cid].add(key)
                seqs[cid].append(_AA3.get(resname, "X"))
    return _finalise(order, seqs, atoms), sorted(het)


def identify_pose(
    path: str,
    *,
    declared_receptors: Mapping[str, str] | None = None,
    max_peptide_length: int = 80,
) -> PoseIdentity:
    """Establish what receptor and what peptide a pose file actually contains.

    ``declared_receptors`` maps a receptor NAME (e.g. "ENPP3_162_517",
    "ENPP1_ECD") to its amino-acid sequence, taken from the campaign spec.  When
    a parsed chain's hash matches one of those, the assignment is ``spec_match``
    and is trusted.  Otherwise the longest chain is assumed to be the receptor
    and the identity is marked ``length_fallback`` — downstream aggregation can
    (and by default does) refuse to pool untrusted assignments.
    """
    chains, het, file_sha = parse_chains(path)
    warnings: list[str] = []
    if not chains:
        return PoseIdentity(path, file_sha, (), None, None, None, None, None, None,
                            None, tuple(het), (), 0, "unresolved",
                            ("no protein chains parsed",))

    declared_by_hash: dict[str, str] = {}
    if declared_receptors:
        for name, seq in declared_receptors.items():
            declared_by_hash[sha256_seq(seq)] = name

    receptor: ChainRecord | None = None
    method = "unresolved"
    if declared_by_hash:
        matches = [c for c in chains if c.sha256 in declared_by_hash]
        if matches:
            # A homodimer legitimately matches twice; take the first in file order
            # and record the multiplicity.
            receptor = matches[0]
            method = "spec_match"
            if len(matches) > 1:
                warnings.append(
                    f"{len(matches)} chains match declared receptor "
                    f"{declared_by_hash[receptor.sha256]!r} (homo-oligomer)"
                )
        else:
            warnings.append(
                "NO parsed chain matches any declared receptor sequence — this pose "
                "may be the wrong receptor, or the construct in the spec is not the "
                "construct that was folded"
            )

    if receptor is None:
        ordered = sorted(chains, key=lambda c: (-c.length, c.order_index))
        receptor = ordered[0]
        method = "length_fallback" if len(chains) > 1 else "unresolved"
        if len(chains) == 1:
            warnings.append("single chain — no peptide/receptor split possible")

    peptide_candidates = [
        c for c in chains
        if c.chain_id != receptor.chain_id and c.sha256 != receptor.sha256
    ]
    peptide: ChainRecord | None = None
    if peptide_candidates:
        peptide = min(peptide_candidates, key=lambda c: (c.length, c.order_index))
        if peptide.length > max_peptide_length:
            warnings.append(
                f"shortest non-receptor chain is {peptide.length} aa, above "
                f"max_peptide_length={max_peptide_length} — assignment suspect"
            )
    else:
        warnings.append("no non-receptor chain found — nothing to score as a binder")

    if method == "spec_match" and receptor.sha256 not in declared_by_hash:
        method = "length_fallback"

    return PoseIdentity(
        source_path=os.path.abspath(path),
        file_sha256=file_sha,
        chains=tuple(chains),
        receptor_chain_id=receptor.chain_id,
        peptide_chain_id=peptide.chain_id if peptide else None,
        receptor_sha256=receptor.sha256,
        receptor_length=receptor.length,
        peptide_sequence=peptide.sequence if peptide else None,
        peptide_sha256=peptide.sha256 if peptide else None,
        peptide_length=peptide.length if peptide else None,
        cofactors=tuple(het),
        chain_order=tuple(c.chain_id for c in chains),
        n_chains=len(chains),
        assignment_method=method,
        warnings=tuple(warnings),
    )


class CrossReceptorPoolingError(RuntimeError):
    """Raised when poses of different receptors would be aggregated together."""


def assert_single_receptor(identities: Sequence[PoseIdentity], *,
                           context: str = "aggregate") -> str:
    """Refuse to aggregate poses that are not all the same receptor.

    This is the guard that would have caught the 2,760 wrong-receptor poses.  It
    groups by receptor_sha256 — content — and does not look at any name.
    """
    resolved = [i for i in identities if i.receptor_sha256]
    if not resolved:
        raise CrossReceptorPoolingError(
            f"{context}: zero poses with a resolved receptor (of {len(identities)})"
        )
    by_hash: dict[str, int] = {}
    for i in resolved:
        by_hash[i.receptor_sha256] = by_hash.get(i.receptor_sha256, 0) + 1
    if len(by_hash) > 1:
        breakdown = ", ".join(f"{h[:12]}…={n}" for h, n in
                              sorted(by_hash.items(), key=lambda kv: -kv[1]))
        raise CrossReceptorPoolingError(
            f"{context}: {len(by_hash)} distinct receptors among {len(resolved)} poses "
            f"({breakdown}). Arm and file names are not identity — group by "
            f"receptor_sha256 before aggregating."
        )
    return next(iter(by_hash))


def group_by_receptor(identities: Iterable[PoseIdentity]) -> dict[str, list[PoseIdentity]]:
    out: dict[str, list[PoseIdentity]] = {}
    for i in identities:
        key = i.receptor_sha256 or "__unresolved__"
        out.setdefault(key, []).append(i)
    return out
