"""Explicit BLI constructs using the existing segment designer and fold API.

``BliConfig().segment_config((40, 60))`` supplies literal fixed masks to a
scaffold-capable generator. ``fold_plan`` retains exact binder residue maps
and ``submit`` sends full frame entities and ``linker_metadata`` through
grouped ``peptides.fold_batch``. Fixed sequence never constrains coordinates.
Full-cassette folds are free-solution sensitivity tests, not sensor simulations.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Literal, Mapping

from ligandai.resources.construct_contract import resolve_linker_metadata
from ligandai.types import PeptideSegment, SegmentConfig

# Same literal software preset as shared/peptideDomains.ts. The provider may
# use custom tags: pass their resolved sequences rather than guessing a cassette.
BLI_FOLD_LINKER = "GGSGGSGGSGG"
BLI_TAG_TWIN_STREP = "WSHPQFEKGGGSAWSHPQFEK"
BLI_TAG_GFP11 = "RDHMVLHEYVNAAGIT"
_AA = frozenset("ACDEFGHIKLMNPQRSTVWY")
FoldFrame = Literal["binder_only", "with_linker", "with_full_cassette"]


def _sequence(value: str, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an amino-acid string")
    seq = value.strip().upper()
    if (not seq and not allow_empty) or set(seq) - _AA:
        raise ValueError(f"{name} must contain canonical amino acids; got an empty or invalid sequence")
    return seq


@dataclass(frozen=True)
class BliFoldFrame:
    design_id: str
    frame: FoldFrame
    sequence: str
    binder_start: int  # zero-based inclusive, in this exact fold sequence
    binder_end: int    # zero-based exclusive
    linker_residues: str = ""
    tag_residues: str = ""
    scaffold_terminus: Literal["N", "C"] | None = None

    @property
    def sequence_sha256(self) -> str:
        return sha256(self.sequence.encode()).hexdigest()

    def binder_mask(self, folded_sequence: str) -> tuple[bool, ...]:
        """Validate the returned chain before applying a score-region mask.

        A partial or different chain needs an explicit residue alignment. Never
        silently apply full-construct indices to a bare or reordered fold.
        """
        if _sequence(folded_sequence, "folded_sequence") != self.sequence:
            raise ValueError("Folded chain differs from the planned frame; align residues before scoring")
        return tuple(self.binder_start <= i < self.binder_end for i in range(len(self.sequence)))

    @property
    def linker_metadata(self) -> dict[str, Any]:
        """Exact full-chain identity and existing literal scaffold wire schema."""
        metadata = {
            "novel_length": self.binder_end - self.binder_start,
            "linker_residues": self.linker_residues,
            "tag_residues": self.tag_residues,
            "linker_origin": "fixed",
            "sequence_sha256": self.sequence_sha256,
            "binder_start": self.binder_start,
            "binder_end": self.binder_end,
        }
        if self.linker_residues:
            metadata["linker_terminus"] = self.scaffold_terminus
        if self.tag_residues:
            metadata["tag_terminus"] = self.scaffold_terminus
        resolve_linker_metadata(self.sequence, metadata)
        return metadata

    def fold_entity(self, chain_id: str = "B") -> dict[str, Any]:
        """Binder entity for a metadata-aware fold route; sequence stays whole.

        The receiving route must forward ``linker_metadata`` to the worker.
        Grouped plan submission carries the same metadata on its partner.
        """
        if not isinstance(chain_id, str) or not chain_id.strip():
            raise ValueError("chain_id must identify the full binder chain")
        return {"type": "protein", "chainId": chain_id, "sequence": self.sequence,
                "name": f"{self.design_id}_{self.frame}", "role": "binder",
                "isPeptide": True, "use_msa": False, "sequence_sha256": self.sequence_sha256,
                "linker_metadata": self.linker_metadata}


@dataclass(frozen=True)
class BliFoldPlan:
    frames: tuple[BliFoldFrame, ...]

    @property
    def sequences(self) -> list[str]:
        # The batch endpoint deduplicates sequences. Keep aliases in frames,
        # while charging/submitting each actual construct only once.
        return list(dict.fromkeys(f.sequence for f in self.frames))

    def submit(self, client: Any, **fold_kwargs: Any) -> BliFoldRun:
        """Submit the explicit constructs through the standard authenticated API.

        Pass receptor/gene, seeds, samples and GPU limits accepted by fold_batch.
        Its existing credit, tier, dedupe and concurrency guards all still apply.
        Grouped partners carry exact frame metadata to the worker. Associate
        returned folds by their actual sequence, not a guessed job-list offset.
        """
        job = client.peptides.fold_batch(**self._submission(fold_kwargs))
        return BliFoldRun(self, job)

    async def submit_async(self, client: Any, **fold_kwargs: Any) -> BliFoldRun:
        """Async equivalent of submit, with the same construct manifest."""
        job = await client.peptides.fold_batch(**self._submission(fold_kwargs))
        return BliFoldRun(self, job)

    def _submission(self, fold_kwargs: dict[str, Any]) -> dict[str, Any]:
        kwargs = dict(fold_kwargs)
        if "peptides" in kwargs or "groups" in kwargs:
            raise ValueError("The BLI plan supplies peptides; do not override peptides or groups")
        target = {}
        for source, key in (("target_gene", "gene"), ("receptor_sequence", "sequence"), ("receptor_pdb", "pdb")):
            value = kwargs.pop(source, None)
            if value is not None:
                target[key] = value
        if len(target) != 1:
            raise ValueError("BLI submission requires exactly one target_gene, receptor_sequence or receptor_pdb")
        for source, key in (("receptor_name", "name"), ("msa_enabled", "use_msa"),
                            ("msa_source_gene", "msa_source_gene"), ("parent_gene", "parent_gene"),
                            ("gene_range", "gene_range")):
            value = kwargs.pop(source, None)
            if value is not None:
                target[key] = value
        unique = {}
        for frame in self.frames:
            partner = {**frame.fold_entity(), "type": "peptide"}
            previous = unique.get(frame.sequence)
            if previous is not None and previous["linker_metadata"] != partner["linker_metadata"]:
                raise ValueError("identical full sequences carry conflicting construct boundaries")
            unique.setdefault(frame.sequence, partner)
        kwargs["groups"] = [{"target": dict(target), "partners": [partner]} for partner in unique.values()]
        return kwargs


@dataclass(frozen=True)
class BliFoldRun:
    plan: BliFoldPlan
    job: Any


@dataclass(frozen=True)
class BliConfig:
    """Literal configurable BLI cassette, C-terminal by default.

    The spacer and tags are fixed in SEQUENCE during generation, while their
    coordinates remain flexible in folding. ``linker_sequence`` is the spacer
    between binder and capture tag; the internal Twin-Strep bridge is separate.
    Existing explicit N-terminal configurations remain supported.
    """

    tag_position: Literal["c_terminal", "n_terminal"] = "c_terminal"
    linker_sequence: str = BLI_FOLD_LINKER
    capture_tag_sequence: str = BLI_TAG_TWIN_STREP
    reporter_sequence: str = BLI_TAG_GFP11

    def __post_init__(self):
        if self.tag_position not in ("c_terminal", "n_terminal"):
            raise ValueError("tag_position must be c_terminal or n_terminal")
        for name in ("linker_sequence", "capture_tag_sequence", "reporter_sequence"):
            object.__setattr__(self, name, _sequence(getattr(self, name), name,
                allow_empty=name != "capture_tag_sequence"))

    @property
    def tag_sequence(self) -> str:
        if self.tag_position == "c_terminal":
            return self.capture_tag_sequence + self.reporter_sequence
        return self.reporter_sequence + self.capture_tag_sequence

    def segment_config(self, binder_range: tuple[int, int]) -> SegmentConfig:
        """Use with peptides.generate(segment_config=..., length_range=...).

        Pin a scaffold-capable generator and verify returned provenance. This
        does not make unsupported v6.9 or local generator routes mask-capable.
        """
        lo, hi = binder_range
        if not isinstance(lo, int) or not isinstance(hi, int) or lo <= 0 or hi < lo:
            raise ValueError("binder_range must be positive ordered integers")
        binder = PeptideSegment(type="binding", length_range=(lo, hi), label="Binding domain")
        linker = PeptideSegment(type="premade", sequence=self.linker_sequence, locked=True, label="Assay spacer")
        capture = PeptideSegment(type="premade", sequence=self.capture_tag_sequence, locked=True, label="Capture tag")
        reporter = PeptideSegment(type="premade", sequence=self.reporter_sequence, locked=True, label="Reporter tag")
        parts = [binder, linker, capture, reporter] if self.tag_position == "c_terminal" else [reporter, capture, linker, binder]
        return SegmentConfig(mode="custom", length_range=(lo, hi),
            segments=[p for p in parts if p.type == "binding" or p.sequence])

    def construct(self, binder: str) -> str:
        core = _sequence(binder, "binder")
        if self.tag_position == "c_terminal":
            return core + self.linker_sequence + self.tag_sequence
        return self.tag_sequence + self.linker_sequence + core

    def binder_from_construct(self, construct: str) -> str:
        """Recover the binder only after verifying the exact configured cassette."""
        seq = _sequence(construct, "construct")
        suffix = self.linker_sequence + self.tag_sequence
        prefix = self.tag_sequence + self.linker_sequence
        if self.tag_position == "c_terminal" and seq.endswith(suffix):
            return _sequence(seq[:-len(suffix)], "binder")
        if self.tag_position == "n_terminal" and seq.startswith(prefix):
            return _sequence(seq[len(prefix):], "binder")
        raise ValueError("Construct does not match the configured cassette and attachment end")

    def fold_plan(self, binders: Mapping[str, str], *,
                  frames: tuple[FoldFrame, ...] = ("binder_only", "with_linker", "with_full_cassette")) -> BliFoldPlan:
        """Prepare paired exact-sequence frames without submitting any work.

        ``binders`` contains bare cores. For generated full constructs, first
        call binder_from_construct; never append a second cassette. The default
        plan has three constructs per design, and therefore costs more than one.
        """
        if not binders or not frames:
            raise ValueError("Provide at least one binder and one fold frame")
        if len(set(frames)) != len(frames) or set(frames) - {"binder_only", "with_linker", "with_full_cassette"}:
            raise ValueError("Frames must be unique supported BLI frame names")
        output = []
        for design_id, value in binders.items():
            core = _sequence(value, "binder")
            if self.capture_tag_sequence in core:
                raise ValueError("Binder contains the configured capture tag; use binder_from_construct first")
            for frame in frames:
                prefix = suffix = ""
                if frame == "with_linker":
                    if self.tag_position == "c_terminal": suffix = self.linker_sequence
                    else: prefix = self.linker_sequence
                elif frame == "with_full_cassette":
                    if self.tag_position == "c_terminal": suffix = self.linker_sequence + self.tag_sequence
                    else: prefix = self.tag_sequence + self.linker_sequence
                output.append(BliFoldFrame(str(design_id), frame, prefix + core + suffix,
                    len(prefix), len(prefix) + len(core),
                    self.linker_sequence if frame != "binder_only" else "",
                    self.tag_sequence if frame == "with_full_cassette" else "",
                    "C" if self.tag_position == "c_terminal" else "N"))
        return BliFoldPlan(tuple(output))
