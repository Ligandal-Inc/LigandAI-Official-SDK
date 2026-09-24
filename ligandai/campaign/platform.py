# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Where work actually happens.

The engine never calls the API directly.  It calls a :class:`Platform`, which
has exactly three implementations:

``SDKPlatform``     the real one — LigandForge generation, Boltz-2 folding and
                    the scoring resources, through the ``ligandai`` client the
                    customer already has.
``ReplayPlatform``  reads poses that were already computed, off disk.  This is
                    how a finished campaign is re-derived and audited without
                    spending a credit, and how the test suite runs against REAL
                    structures rather than invented ones.
``DryRunPlatform``  submits nothing and returns nothing.  It exists so that
                    ``--dry-run`` can print the full plan and its cost, and it
                    NEVER fabricates a score — every design it returns carries
                    ``Measured.absent``.  A dry run that made up numbers would
                    be worse than no dry run.
"""

from __future__ import annotations

import glob
import os
import statistics
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Protocol, Sequence

from .identity import PoseIdentity, identify_pose
from .spec import CampaignSpec, Construct, PeptideClass
from .values import Measured, Status


@dataclass
class PoseResult:
    """One folded pose, tied to the structure it came from."""

    design_id: str
    sequence: str
    seed_unit: str
    identity: PoseIdentity | None
    peptide_ipsae: Measured
    iptm: Measured
    complex_plddt: Measured
    peptide_plddt: Measured
    peptide_interface_plddt: Measured
    receptor_construct: str
    cofactors_present: tuple[str, ...] = ()
    pose_path: str | None = None
    source: str = ""


@dataclass
class GeneratedDesign:
    design_id: str
    sequence: str
    peptide_class: str
    topology: str
    molecule_class: str
    provenance: dict[str, Any] = field(default_factory=dict)


class Platform(Protocol):
    name: str

    def generate(self, spec: CampaignSpec, pc: PeptideClass,
                 n: int) -> list[GeneratedDesign]: ...

    def fold(self, spec: CampaignSpec, sequences: Sequence[str], *,
             construct: Construct, cofactors: Sequence[str],
             n_seed_units: int, label: str) -> list[PoseResult]: ...

    def score_developability(self, spec: CampaignSpec,
                             sequences: Sequence[str]) -> dict[str, dict[str, Any]]: ...

    def score_affinity(self, spec: CampaignSpec,
                       poses: Sequence[PoseResult]) -> dict[str, dict[str, Any]]: ...


# ---------------------------------------------------------------------------

class DryRunPlatform:
    """Plans, costs, submits nothing, and invents nothing."""

    name = "dry-run"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def generate(self, spec, pc, n):
        self.calls.append({"op": "generate", "class": pc.name, "n": n})
        return []

    def fold(self, spec, sequences, *, construct, cofactors, n_seed_units, label):
        self.calls.append({"op": "fold", "n_sequences": len(sequences),
                           "construct": construct.name, "cofactors": list(cofactors),
                           "n_seed_units": n_seed_units, "label": label})
        return []

    def score_developability(self, spec, sequences):
        self.calls.append({"op": "developability", "n": len(sequences)})
        return {}

    def score_affinity(self, spec, poses):
        self.calls.append({"op": "affinity", "n": len(poses)})
        return {}


# ---------------------------------------------------------------------------

class ReplayPlatform:
    """Serve poses that already exist on disk, identified by CONTENT.

    ``roots`` are directories of ``.pdb`` / ``.pdb.gz`` / ``.cif`` files.  Each
    file is parsed, its receptor hashed and its peptide read out of chain
    coordinates, and it is filed under (peptide sequence, receptor hash).  A
    request for a fold is then served only by poses whose RECEPTOR actually
    matches the construct asked for — which is the whole point: in the ENPP3
    tree an arm named for one paralog held on-target folds.

    Scores come from a metrics callback (a sidecar reader), never from the
    filename and never invented.  With no callback, every metric is ABSENT.
    """

    name = "replay"

    def __init__(self, roots: Sequence[str], declared_receptors: Mapping[str, str],
                 metrics_fn=None, *, patterns: Sequence[str] = ("*.pdb", "*.pdb.gz", "*.cif")):
        self.roots = list(roots)
        self.declared = dict(declared_receptors)
        self.metrics_fn = metrics_fn
        self.patterns = list(patterns)
        self._index: dict[tuple[str, str], list[PoseIdentity]] | None = None
        self.skipped_unresolved = 0
        self.skipped_wrong_receptor = 0

    def index(self) -> dict[tuple[str, str], list[PoseIdentity]]:
        if self._index is not None:
            return self._index
        idx: dict[tuple[str, str], list[PoseIdentity]] = {}
        by_hash = {self._h(s): n for n, s in self.declared.items()}
        for root in self.roots:
            for pat in self.patterns:
                for p in glob.glob(os.path.join(root, "**", pat), recursive=True):
                    pid = identify_pose(p, declared_receptors=self.declared)
                    if not pid.is_resolved:
                        self.skipped_unresolved += 1
                        continue
                    if pid.receptor_sha256 not in by_hash:
                        self.skipped_wrong_receptor += 1
                        continue
                    key = (pid.peptide_sequence or "", pid.receptor_sha256 or "")
                    idx.setdefault(key, []).append(pid)
        self._index = idx
        return idx

    @staticmethod
    def _h(seq: str) -> str:
        from .identity import sha256_seq
        return sha256_seq(seq)

    def generate(self, spec, pc, n):
        """Replay cannot generate.  It returns what it already holds for the class."""
        out: list[GeneratedDesign] = []
        seen: set[str] = set()
        for (seq, _rh) in self.index():
            if not seq or seq in seen:
                continue
            if not (pc.length_range[0] <= len(seq) <= pc.length_range[1]):
                continue
            seen.add(seq)
            out.append(GeneratedDesign(design_id=self._h(seq)[:12], sequence=seq,
                                       peptide_class=pc.name, topology=pc.topology,
                                       molecule_class=pc.molecule_class or pc.topology,
                                       provenance={"source": "replay"}))
            if len(out) >= n:
                break
        return out

    def fold(self, spec, sequences, *, construct, cofactors, n_seed_units, label):
        rh = self._h(construct.sequence)
        want_cof = {c.upper() for c in cofactors}
        results: list[PoseResult] = []
        for seq in sequences:
            for i, pid in enumerate(self.index().get((seq, rh), [])):
                got_cof = {c.upper() for c in pid.cofactors}
                if want_cof and not want_cof <= got_cof:
                    continue
                if not want_cof and got_cof:
                    continue          # a cofactor-free leg must be cofactor-free
                m = self.metrics_fn(pid) if self.metrics_fn else {}
                results.append(PoseResult(
                    design_id=self._h(seq)[:12], sequence=seq,
                    seed_unit=str(m.get("seed_unit", i // max(1, spec.fold.diffusion_samples))),
                    identity=pid,
                    peptide_ipsae=_as_measured(m, "peptide_ipsae"),
                    iptm=_as_measured(m, "iptm"),
                    complex_plddt=_as_measured(m, "complex_plddt"),
                    peptide_plddt=_as_measured(m, "peptide_plddt"),
                    peptide_interface_plddt=_as_measured(m, "peptide_interface_plddt"),
                    receptor_construct=construct.name,
                    cofactors_present=pid.cofactors, pose_path=pid.source_path,
                    source="replay"))
        return results

    def score_developability(self, spec, sequences):
        return {}

    def score_affinity(self, spec, poses):
        return {}


def _as_measured(m: Mapping[str, Any], key: str) -> Measured:
    v = m.get(key)
    if isinstance(v, Measured):
        return v
    if v is None:
        return Measured.absent(f"{key} not supplied by the metrics reader")
    try:
        return Measured(float(v), Status.MEASURED, n=1, method="replay")
    except (TypeError, ValueError):
        return Measured.failed(f"{key} was {v!r}")


# ---------------------------------------------------------------------------

class SDKPlatform:
    """The real platform, through the ``ligandai`` client.

    Every fold goes out with the spec's fold settings — diffusion samples,
    sampling and recycling steps, potentials, and receptor MSA on.  Seed units
    are submitted as SEPARATE batches: they are independent draws, and counting
    diffusion samples inside one seed as independent runs is the winner's-curse
    defect the reliability block exists to prevent.
    """

    name = "ligandai"

    def __init__(self, client: Any = None, *, program_id: Any = None,
                 timeout: float = 7200.0, poll_interval: float = 15.0,
                 progress=None):
        if client is None:
            from ligandai.client import LigandAI  # noqa: PLC0415
            client = LigandAI()
        self.client = client
        self.program_id = program_id
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.progress = progress or (lambda *_a, **_k: None)

    # -- generation -----------------------------------------------------
    def generate(self, spec: CampaignSpec, pc: PeptideClass, n: int) -> list[GeneratedDesign]:
        g = spec.generation
        kwargs: dict[str, Any] = {
            "num_peptides": n,
            "length_range": tuple(pc.length_range),
            "model": pc.generator_overrides.get("model", g.model),
            "auto_fold": False,           # the engine decides what to fold
        }
        if g.target_residues:
            kwargs["target_residues"] = list(g.target_residues)
        if g.avoid_residues:
            kwargs["avoid_residues"] = list(g.avoid_residues)

        # ⛔ generation.heads MUST reach the generator [bd-dre-c80f4.5].
        #
        # spec.py VALIDATES this field -- it refuses four stacked heads and caps
        # immuno at 0.5 -- which makes a spec author reasonably believe it is
        # applied. It was never read here, so every campaign declaring
        # `heads: {proteolytic: true, halflife: true}` ran with those modules
        # OFF while its own record claimed otherwise. Validating a field and
        # then discarding it is worse than not having the field.
        #
        # The head names are the campaign vocabulary; these are the kwargs the
        # SDK actually exposes. Note `halflife` is deliberately mapped to
        # residue_composition: the SDK's own `halflife=` is a DEPRECATED ALIAS
        # for the residue-composition knob and predicts nothing (no N-end rule,
        # no PEST term). The learned half-life head is a separate POST-HOC
        # scorer, not a generation-time control.
        _heads = dict(g.heads or {})
        _strength = lambda v, d: (float(v) if isinstance(v, (int, float))
                                  and not isinstance(v, bool) else d)
        if _heads.get("proteolytic"):
            kwargs["serum_stability"] = True
            kwargs["stability_strength"] = _strength(_heads["proteolytic"], 1.0)
            kwargs["stability_mode"] = g.extra.get("proteolytic_mode", "resist")
            if g.extra.get("stability_modules"):
                kwargs["stability_modules"] = dict(g.extra["stability_modules"])
        if _heads.get("halflife") or _heads.get("residue_composition"):
            kwargs["residue_composition"] = True
            kwargs["residue_composition_strength"] = _strength(
                _heads.get("residue_composition", _heads.get("halflife")), 1.0)
        if _heads.get("immuno"):
            kwargs["immunogenicity"] = True
            kwargs["immuno_strength"] = _strength(_heads["immuno"], 0.3)
        if _heads.get("cellfree"):
            kwargs["cellfree"] = True
            kwargs["cellfree_strength"] = _strength(_heads["cellfree"], 1.0)
        # Record what was actually asked for, so a record can be audited without
        # re-deriving this mapping.
        _heads_sent = {k: kwargs[k] for k in
                       ("serum_stability", "stability_strength", "stability_mode",
                        "stability_modules", "residue_composition",
                        "residue_composition_strength", "immunogenicity",
                        "immuno_strength", "cellfree", "cellfree_strength")
                       if k in kwargs}
        # ⛔ EVERY campaign generate call is a DELIBERATE NEW BATCH — say so on
        # the wire [bd-dre-c80f4.4].
        #
        # The platform reuses an existing session when a later request looks like
        # a repeat of an earlier one: a 60 s idempotency cache (built to stop
        # double-DEBITS on a transport retry) and a gene/variant reuse window.
        # Neither can tell "the client retried the same HTTP call" from "the
        # engine has come round the loop and wants another 150 designs of this
        # class", because from the body alone those are identical.
        #
        # This is NOT ``allow_reused_session`` and must never become it. That
        # flag ACCEPTS someone else's peptides under this call's label. These two
        # say the opposite: run it for real.
        #   * ``additionalBatch`` — the server's own name for "generate more",
        #     the exact intent of an iteration step.
        #   * ``requestNonce`` — distinguishes a new logical call from a retry.
        #     An HTTP retry re-sends this same body, and therefore this same
        #     nonce, so double-debit protection is preserved exactly.
        #
        # ``Peptides.generate`` collects unknown keywords into ``**extra`` and
        # the body builder does ``body.update(extra)``, so these two reach the
        # wire verbatim — they are set as plain kwargs, not under an ``extra=``
        # key, which that signature would treat as a wire field literally named
        # "extra".
        kwargs["additionalBatch"] = True
        kwargs["requestNonce"] = uuid.uuid4().hex
        kwargs.update(g.extra)
        kwargs.update(pc.generator_overrides)
        kwargs.pop("model", None)
        model = pc.generator_overrides.get("model", g.model)
        self.progress(f"generate {n} × {pc.name} ({pc.length_range[0]}-{pc.length_range[1]} aa) "
                      f"vs {spec.target.gene} on {model}")
        job = self.client.peptides.generate(spec.target.gene, model=model, **kwargs)
        result = job.wait(timeout=self.timeout, poll_interval=self.poll_interval)
        out: list[GeneratedDesign] = []
        for p in _iter_peptides(result):
            seq = _seq_of(p)
            if not seq:
                continue
            out.append(GeneratedDesign(
                design_id=ReplayPlatform._h(seq)[:12], sequence=seq,
                peptide_class=pc.name, topology=pc.topology,
                molecule_class=pc.molecule_class or pc.topology,
                provenance={"model": model, "job": getattr(job, "id", None),
                            "heads_requested": dict(_heads),
                            "heads_sent_as_kwargs": _heads_sent,
                            "generator_kwargs": {k: v for k, v in kwargs.items()
                                                 if k != "num_peptides"}}))
        # Length is sampled PER BATCH on this platform; a collapsed range has
        # silently produced 1,300 designs at exactly one length.
        lens = {len(d.sequence) for d in out}
        if out and len(lens) < 2:
            raise RuntimeError(
                f"generation returned {len(out)} designs at a SINGLE length {lens}. "
                f"The requested range was {pc.length_range}. This is the collapsed "
                f"length-range defect; do not fold this batch."
            )
        return out

    # -- folding --------------------------------------------------------
    def fold(self, spec: CampaignSpec, sequences: Sequence[str], *,
             construct: Construct, cofactors: Sequence[str],
             n_seed_units: int, label: str) -> list[PoseResult]:
        f = spec.fold
        results: list[PoseResult] = []
        for unit in range(max(1, n_seed_units)):
            self.progress(f"fold[{label}] unit {unit + 1}/{n_seed_units}: "
                          f"{len(sequences)} designs vs {construct.name} "
                          f"cofactors={list(cofactors) or 'none'}")
            kwargs: dict[str, Any] = {
                "receptor_sequence": construct.sequence,
                "diffusion_samples": f.diffusion_samples,
                "sampling_steps": f.sampling_steps,
                "recycling_steps": f.recycling_steps,
                "use_potentials": f.use_potentials,
                "msa_enabled": f.msa_receptor,
            }
            if cofactors:
                # ⛔ bd-dre-c80f4.7 — `fold_batch` HAS NO COFACTOR PARAMETER.
                # `cofactors=` was passed here and would have died on a TypeError
                # deep in the SDK; worse, quietly dropping it would fold a
                # cofactor-dependent interface WITHOUT its cofactor and report
                # the result as if the cofactor had been there. Cofactor co-fold
                # lives on the ensemble surface (ligandai/resources/ensemble.py),
                # and that module records that the DEPLOYED co-fold route cannot
                # express a cofactor at all. Say so, here, before any credit is
                # spent, instead of producing a number nobody can trust.
                raise RuntimeError(
                    f"construct {construct.name!r} declares cofactors "
                    f"{list(cofactors)} but peptides.fold_batch() has no cofactor "
                    f"parameter — this fold would silently run WITHOUT them. "
                    f"A cofactor-dependent interface folded cofactor-free is not "
                    f"the same measurement. Remove the cofactor declaration to "
                    f"run an explicitly apo campaign, or fold this arm through "
                    f"the ensemble co-fold surface."
                )
            if construct.gene:
                # ⛔ bd-dre-c80f4.7 — NOT `target_gene=`.
                #
                # `_build_batch_fold_body()` requires EXACTLY ONE of target_gene=
                # / receptor_pdb= / receptor_sequence=, so passing the gene next
                # to the construct sequence raised
                #   ValueError: Pass exactly one of target_gene=, receptor_pdb=,
                #               or receptor_sequence=
                # before a single pose existed. Every real spec sets a gene on
                # its constructs — including the shipped worked example
                # examples/campaign/ca12_selective_binders.yaml — so the fold
                # stage could not run at all.
                #
                # receptor_sequence is AUTHORITATIVE: it carries the exact
                # construct and residue range the spec declares, which is the
                # whole point of declaring a construct rather than a gene name.
                # The gene is still worth sending — as `msa_source_gene=`, which
                # is exactly "derive this arbitrary sequence's MSA from that
                # gene's cached, homolog-rich MSA" (bd-dre-h0cuo.1). Folding a
                # receptor without an MSA produces garbage ipSAE, and a fresh
                # fragment search on a construct slice is often degenerate, so
                # dropping the gene entirely would trade a hard error for a
                # quietly worse fold.
                if f.msa_receptor:
                    kwargs["msa_source_gene"] = construct.gene
            job = self.client.peptides.fold_batch(list(sequences), **kwargs)
            payload = job.wait(timeout=self.timeout, poll_interval=self.poll_interval)
            _rows = list(_iter_folds(payload))
            # ⛔ ZERO ROWS FOR A NON-EMPTY REQUEST IS A FAILURE, NOT A RESULT
            # [bd-dre-c80f4.21]. fold_batch returns a bare list; an empty one is
            # indistinguishable from "nothing scored well" unless we say so
            # here. Measured 2026-09-24: fold_batch(2 sequences vs FSHR ECD)
            # returned [] with no exception, and the campaign engine read that
            # as an empty result, re-queued, and reported "0 designs, 0 poses"
            # for four iterations while paying for every generation and fold.
            if sequences and not _rows:
                raise RuntimeError(
                    f"fold[{label}] unit {unit + 1}: asked to fold "
                    f"{len(sequences)} sequence(s) against {construct.name!r} "
                    f"and the platform returned ZERO rows. That is a failed "
                    f"fold, not an empty result — the batch was paid for. Do "
                    f"not re-queue on it."
                )
            _dropped_no_seq = 0
            for row in _rows:
                # Platform fold rows carry no sequence field; read it from the
                # coordinates [bd-dre-c80f4.21]. _seq_of is tried first so a
                # future sequence-bearing row still works.
                seq = _seq_of(row) or _seq_from_coords(row)
                if not seq:
                    _dropped_no_seq += 1
                    continue
                # ⛔ bd-dre-c80f4.7 — A COMPLEX FOLD THAT CAME BACK AS ONE CHAIN
                # IS NOT A COMPLEX FOLD.
                #
                # Asking for a receptor and getting a monomer back is SILENT:
                # the job succeeds, the row looks normal, and the pdb header
                # reads "Boltz-2 Single Chain Prediction ... CHAIN: A; LENGTH:
                # 10". No interface exists on that output, so every interface
                # metric downstream is absent — and "absent" is then read as
                # "this design scored badly" rather than "nobody folded a
                # complex". Reproduced 2026-09-23 folding GnRH QHWSYGLRPG with
                # target_gene= and no receptor_sequence=.
                #
                # Checked by PARSING the coordinates, never by a status field.
                # Absent coordinates leave the frame UNVERIFIED — which is not a
                # pass and not a failure, so it does not raise here; it is
                # recorded so a caller can tell the two apart.
                _chains = _pdb_chain_ids(row)
                if _chains is not None and len(_chains) < 2:
                    raise RuntimeError(
                        f"fold[{label}] asked for {seq} against construct "
                        f"{construct.name!r} ({len(construct.sequence)} aa) but the "
                        f"returned structure has ONE chain {sorted(_chains)} — it is a "
                        f"single-chain prediction of the peptide alone, not a complex. "
                        f"No interface metric can exist on it; do not score this batch."
                    )
                results.append(PoseResult(
                    design_id=ReplayPlatform._h(seq)[:12], sequence=seq,
                    seed_unit=f"{label}:u{unit}", identity=None,
                    peptide_ipsae=_metric(row, "peptide_ipsae", "peptideIpsae", "ipsae"),
                    iptm=_metric(row, "iptm", "ipTM"),
                    complex_plddt=_plddt(row, "complex_plddt", "plddt", "complexPlddt"),
                    peptide_plddt=_plddt(row, "peptide_plddt", "peptidePlddt"),
                    peptide_interface_plddt=_plddt(row, "peptide_interface_plddt",
                                                   "peptideIfacePlddt"),
                    receptor_construct=construct.name,
                    cofactors_present=tuple(c.upper() for c in cofactors),
                    pose_path=row.get("pdb_path") if isinstance(row, dict) else None,
                    source="ligandai"))
            # ⛔ A FOLD THAT RECORDS NOTHING MUST NOT LOOK LIKE A FOLD THAT
            # FOUND NOTHING [bd-dre-c80f4.21].
            #
            # Dropping every row silently is indistinguishable, from the
            # engine's side, from "this batch simply had no usable poses": both
            # yield an empty list, the loop re-queues, and the campaign reports
            # "0 designs, 0 poses" while paying full price for generation AND
            # folding. Measured: three campaigns x three peptide classes ran to
            # the no_progress guard over four iterations having recorded zero
            # designs, because rows carry no sequence field.
            if _dropped_no_seq and not results:
                raise RuntimeError(
                    f"fold[{label}] unit {unit + 1}: every one of "
                    f"{_dropped_no_seq} returned rows was dropped because no "
                    f"peptide sequence could be recovered — neither a sequence "
                    f"field nor parseable CA coordinates. The batch was folded "
                    f"and paid for; nothing was recorded. Do not treat this as "
                    f"an empty result."
                )
            if _dropped_no_seq:
                self.progress(
                    f"fold[{label}] unit {unit + 1}: WARNING {_dropped_no_seq} of "
                    f"{_dropped_no_seq + len(results)} rows dropped (no recoverable "
                    f"sequence)")
        return results

    def score_developability(self, spec, sequences):
        out: dict[str, dict[str, Any]] = {}
        for seq in sequences:
            try:
                out[seq] = self.client.peptides.score_developability(seq)
            except Exception as exc:                       # noqa: BLE001
                out[seq] = {"__error__": str(exc)}
        return out

    def score_affinity(self, spec, poses):
        out: dict[str, dict[str, Any]] = {}
        for p in poses:
            if not p.pose_path or not os.path.exists(p.pose_path):
                continue
            try:
                with open(p.pose_path, encoding="utf-8") as fh:
                    out[p.design_id] = self.client.deltaforge.score_pdb(fh.read())
            except Exception as exc:                       # noqa: BLE001
                out[p.design_id] = {"__error__": str(exc)}
        return out


# -- payload helpers -------------------------------------------------------

# ⛔ bd-dre-c80f4.7 — A DICT IS CHECKED FIRST, AND "items" IS NEVER AN ATTRIBUTE
# LOOKUP.
#
# Both helpers used to try `getattr(payload, "items")` BEFORE the dict branch.
# Every dict has an `.items` METHOD, so `getattr({"folds": [...]}, "items")` is
# truthy and the helper returned the bound method instead of the rows — the
# caller then raised `TypeError: 'builtin_function_or_method' object is not
# iterable`, or, on the peptide side, silently iterated nothing. The dict shape
# is the one the JSON API actually returns.


def _rows_from(payload: Any, keys: Sequence[str]) -> Iterable[Any]:
    """Pull the row list out of a dict payload or a response object."""
    if isinstance(payload, dict):
        for k in keys:
            v = payload.get(k)
            if v:
                return v
        return []
    for attr in (*keys, "items"):
        v = getattr(payload, attr, None)
        # A bound method is an attribute that EXISTS, not a row list.
        if v and not callable(v):
            return v
    return payload if isinstance(payload, (list, tuple)) else []


def _iter_peptides(result: Any) -> Iterable[Any]:
    return _rows_from(result, ("peptides", "designs", "results"))


def _iter_folds(payload: Any) -> Iterable[Any]:
    return _rows_from(payload, ("folds", "results"))


def _get(row: Any, *keys: str) -> Any:
    for k in keys:
        if isinstance(row, dict) and row.get(k) is not None:
            return row[k]
        v = getattr(row, k, None)
        if v is not None:
            return v
    return None


def _seq_of(row: Any) -> str | None:
    v = _get(row, "sequence", "peptide_sequence", "peptideSequence", "seq")
    return str(v).strip().upper() if v else None


_AA3TO1 = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C", "GLN": "Q",
    "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I", "LEU": "L", "LYS": "K",
    "MET": "M", "PHE": "F", "PRO": "P", "SER": "S", "THR": "T", "TRP": "W",
    "TYR": "Y", "VAL": "V",
}


def _seq_from_coords(row: Any) -> str | None:
    """The PEPTIDE chain's sequence, read from the fold's own coordinates.

    ⛔ bd-dre-c80f4.21 — fold rows on this platform carry NO sequence field.
    Their attributes are peptide_ipsae / ipsae / iptm / plddt /
    peptide_chain_id / receptor_chains / scores / per_chain, and nothing else.
    ``_seq_of`` therefore returned None for every row, the loop ``continue``d,
    and EVERY fold produced zero PoseResults while spending full compute. Three
    campaigns ran generate+fold across three peptide classes and recorded
    "0 designs, 0 poses" for four iterations before stopping on no_progress.

    Reading it from the coordinates is also the RIGHT source, not merely an
    available one: guard 7 requires a pose's identity to come from its atoms,
    never from an arm name or a request-order index. If the platform later adds
    a sequence field, the coordinates remain authoritative when they disagree.

    Returns None when there are no coordinates, or no residues on the peptide
    chain — both are "could not look", never an empty sequence.
    """
    pdb = _get(row, "pdb_content", "pdbContent", "pdb_data", "pdbData", "pdb")
    if not isinstance(pdb, str) or not pdb.strip():
        return None
    want = _get(row, "peptide_chain_id", "peptideChainId")
    want = str(want).strip() if want else None
    # Fall back to the SHORTEST chain: in a peptide-receptor complex the binder
    # is the short one. Never guess "B" — chain lettering is not guaranteed.
    per_chain: dict[str, list[tuple[int, str]]] = {}
    for line in pdb.splitlines():
        if not line.startswith("ATOM  ") or len(line) < 27:
            continue
        if line[12:16].strip() != "CA":
            continue
        cid = line[21].strip()
        res3 = line[17:20].strip().upper()
        try:
            num = int(line[22:26])
        except ValueError:
            continue
        per_chain.setdefault(cid, []).append((num, _AA3TO1.get(res3, "X")))
    if not per_chain:
        return None
    if want and want in per_chain:
        chain = want
    else:
        chain = min(per_chain, key=lambda c: len(per_chain[c]))
    residues = sorted(set(per_chain[chain]))
    seq = "".join(aa for _, aa in residues)
    return seq or None


def _pdb_chain_ids(row: Any) -> set[str] | None:
    """Chain IDs actually present in a fold row's COORDINATES.

    ⛔ bd-dre-c80f4.7 — read from ATOM/HETATM records, never from a status
    field, a chain count, or a header line. A fold that was asked to be against
    a receptor and came back single-chain is otherwise indistinguishable from a
    successful complex: same ``status``, same row shape, metrics merely absent.

    Returns ``None`` when the row carries no coordinates at all. That is
    UNVERIFIED, not "one chain" — a missing file is not a measurement, and
    collapsing the two would turn "I could not look" into "I looked and it was
    wrong". The caller must treat ``None`` as indeterminate.
    """
    pdb = _get(row, "pdb_content", "pdbContent", "pdb_data", "pdbData", "pdb")
    if not isinstance(pdb, str) or not pdb.strip():
        return None
    chains: set[str] = set()
    for line in pdb.splitlines():
        if line.startswith(("ATOM  ", "HETATM")) and len(line) > 21:
            cid = line[21].strip()
            if cid:
                chains.add(cid)
    # Coordinates present but unparseable is also "could not look", not "one
    # chain" — an empty set must never read as a single-chain verdict.
    return chains or None


def _metric(row: Any, *keys: str) -> Measured:
    v = _get(row, *keys)
    if v is None:
        return Measured.absent(f"{keys[0]} not present in the fold record")
    try:
        f = float(v)
    except (TypeError, ValueError):
        return Measured.failed(f"{keys[0]} was {v!r}")
    if f > 1.0 and keys[0] in ("peptide_ipsae", "iptm"):
        return Measured.failed(
            f"{keys[0]}={f} exceeds 1.0 — this is a PREDICTED pre-fold value, not a "
            f"measured Boltz-2 metric, and must not be scored as one")
    return Measured(f, Status.MEASURED, n=1, method="boltz2")


def _plddt(row: Any, *keys: str) -> Measured:
    """pLDDT is stored on two scales in this platform.  Normalise to 0-100."""
    v = _get(row, *keys)
    if v is None:
        return Measured.absent(f"{keys[0]} not present in the fold record")
    try:
        f = float(v)
    except (TypeError, ValueError):
        return Measured.failed(f"{keys[0]} was {v!r}")
    if f <= 1.0:
        f *= 100.0
    return Measured(f, Status.MEASURED, n=1, units="0-100", method="boltz2")
