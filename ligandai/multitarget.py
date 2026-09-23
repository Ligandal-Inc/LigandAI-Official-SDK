# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Multi-target co-optimization — N POSITIVE targets, not one.

A contrastive run answers "bind X, not its paralogs". This module answers the
other question customers ask: **"bind X *and* Y"** — a cross-species-conserved
binder (human BRD4 **and** mouse Brd4), or a dual-target binder against two
different genes entirely — while still steering away from off-target references.

⛔ TWO POSITIVES AND TWO NEGATIVES ARE OPPOSITE ROLES, AND THE WIRE FIELD THAT
CARRIES NEGATIVES IS NAMED ``reference_pocket_features``. The v6.9 CFG operator
is ``paralog_mean``: guidance ≈ target − mean(references), so a *positive*
target placed in another positive's reference panel steers designs AWAY from a
receptor they are being co-optimized FOR. That request is well-formed, costs
full GPU spend, and optimises for the opposite of its goal. :class:`CoOptimization`
refuses it — see :meth:`CoOptimization.check_reference_roles`.

WHAT THE PLATFORM ACTUALLY DOES, AND WHERE THE JOINT PART LIVES
---------------------------------------------------------------
The generator conditions on ONE pocket per run (``pocket_features``) plus N
reference pockets to avoid. There is no served operator that takes several
positive pockets and conditions on all of them at once, and this module does not
pretend otherwise. Co-optimization is therefore expressed where it is real:

* **Generation** — one leg per positive target, each a normal single-target run
  with its OWN pocket and its OWN reference panel; the pools are unioned.
* **Measurement** — every design is folded against EVERY positive target and
  EVERY off-target. This is the joint step, and it is the one the claim rests on.
* **Selection** — the per-target scores are combined by an **explicitly named**
  rule (:data:`CrossTargetAggregate`) and the off-target worst case is
  subtracted. Nothing defaults; ``aggregate=`` has no default value anywhere a
  multi-target selection happens.

⛔ CROSS-SPECIES CORRECTNESS. Residue numbering frames DIFFER between orthologs
and between genes. This module never maps a residue index from one target onto
another: a caller who gives hotspots for one target must give them for every
target, each attested as derived in that target's own frame
(``hotspot_frame_attested=True``). An alignment may be used to CHECK an
equivalence; it is never used to GENERATE a spec. Every target reports its frame
(:attr:`TargetSpec.frame`) and every row carries the frames it was measured in.

⛔ A NON-DEFAULT ORGANISM NEEDS A STRUCTURE. ``client.peptides.fold_batch`` has
no species parameter — ``target_gene`` resolves the canonical (human) structure.
So ``TargetSpec(gene="BRD4", organism="mouse")`` with nothing else would fold the
HUMAN receptor and report it as the mouse leg: a fabricated cross-species result
that no response field contradicts. It is refused. Supply the ortholog's own
structure (``client.structures.get(gene, species="mouse")`` →
``receptor_pdb=``, or ``client.proteins.upload_pdb(...)`` → ``user_pdb_id=``).

Refs: bd-dre-if562 (the campaign loop), bd-dre-f4mc9 (the contrastive
passthrough this generalises).
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from ligandai.errors import LigandAIValidationError
from ligandai.species import normalize_species

__all__ = [
    "CrossTargetAggregate",
    "CROSS_TARGET_AGGREGATES",
    "TargetSpec",
    "CoOptimization",
    "aggregate_across_targets",
    "coerce_target",
    "DEFAULT_ORGANISM",
]

#: How a design's per-positive-target scores become ONE number.
#:
#: ``"worst_case"``
#:     ``min`` over the positive targets. This is what "binds BOTH" means: a
#:     design is only as good as its weakest required target, and the rule also
#:     names the limiting target so the failure is legible.
#: ``"mean"``
#:     arithmetic mean over the positive targets. Weaker and deliberately
#:     available: it lets a strong leg mask a failed one, so every row built
#:     under it ALSO carries ``per_target_pass`` and the limiting leg.
#:
#: There is no third value and no default. A silent aggregation rule is the
#: defect class this module exists to remove.
CrossTargetAggregate = Literal["worst_case", "mean"]

CROSS_TARGET_AGGREGATES: tuple[str, ...] = ("worst_case", "mean")

#: The organism ``target_gene=`` resolves to on every platform surface.
DEFAULT_ORGANISM = "human"


def aggregate_across_targets(
    per_target: Mapping[str, float],
    rule: CrossTargetAggregate,
) -> tuple[float, str | None]:
    """Combine per-positive-target scores under ``rule``.

    Returns ``(value, limiting_target)``. ``limiting_target`` is the argmin —
    present under BOTH rules, because "which target is holding this design
    back" is the actionable half of the answer even when the rule averaged it
    away.

    ⛔ Raises on an empty mapping rather than returning 0.0. No measurement is
    not a score of zero.
    """
    if not per_target:
        raise LigandAIValidationError(
            "aggregate_across_targets got no per-target values. An absent "
            "measurement is not a score of zero — the row is incomplete and "
            "belongs in the partial list, not in a verdict."
        )
    if rule not in CROSS_TARGET_AGGREGATES:
        raise LigandAIValidationError(
            f"unknown cross-target aggregate {rule!r}; use one of "
            f"{list(CROSS_TARGET_AGGREGATES)}. 'worst_case' is min over the "
            f"positive targets (what 'binds both' means); 'mean' averages them "
            f"and lets one strong leg mask a failed one."
        )
    limiting = min(per_target, key=lambda k: per_target[k])
    if rule == "worst_case":
        return float(per_target[limiting]), limiting
    return float(statistics.fmean(per_target.values())), limiting


def _clean_resnums(value: Any, label: str) -> tuple[int, ...] | None:
    """Coerce a residue list to PDB-numbered ints, refusing 0-based indices."""
    if value is None:
        return None
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise LigandAIValidationError(
            f"{label} must be a sequence of PDB residue numbers, got "
            f"{type(value).__name__}"
        )
    out: list[int] = []
    for r in value:
        try:
            n = int(r)
        except (TypeError, ValueError) as exc:
            raise LigandAIValidationError(
                f"{label} entry {r!r} is not an integer residue number"
            ) from exc
        if n < 1:
            raise LigandAIValidationError(
                f"{label} contains {n}, which is not a PDB residue number. "
                f"These are PDB numbers as they appear in the structure, never "
                f"0-based array indices — an off-by-one here resolves to a "
                f"different residue and nothing reports it."
            )
        out.append(n)
    if not out:
        return None
    return tuple(dict.fromkeys(out))


@dataclass(frozen=True)
class TargetSpec:
    """One POSITIVE target of a co-optimization, with its own frame and route.

    A bare gene symbol is enough for the canonical (human) receptor::

        TargetSpec(gene="BRD4")

    An ortholog needs its OWN structure, because no fold or generate surface
    takes a species and ``target_gene`` resolves the canonical one::

        mouse = client.structures.get("BRD4", species="mouse")
        TargetSpec(
            gene="BRD4", organism="mouse", uniprot_id="Q9ESU6",
            receptor_pdb=mouse.pdb_content, receptor_name="BRD4_MOUSE",
        )

    Attributes
    ----------
    gene
        Gene SYMBOL. ``BRD4`` resolves; ``BRD4ET`` does not — that names a
        domain construct, which goes through ``client.proteins.upload_pdb`` and
        is targeted by ``user_pdb_id``.
    organism
        Free text, normalised for comparison only. Anything other than
        ``"human"`` REQUIRES an explicit structure (see the module docstring).
    label
        The panel key. Defaults to ``GENE@organism`` so two orthologs of one
        gene are distinguishable — without it, human and mouse BRD4 collide on
        one leg key and one silently overwrites the other.
    hotspot_residues / target_residues
        PDB residue numbers **in THIS target's own numbering frame**. Setting
        them on one target of a multi-target set requires setting them on every
        target and attesting each frame.
    hotspot_frame_attested
        The caller states these residue numbers were derived in THIS target's
        frame, not copied from a sibling target. Required whenever a
        multi-target set carries residue-level targeting.
    """

    gene: str
    organism: str | None = None
    label: str | None = None
    uniprot_id: str | None = None
    pdb_code: str | None = None
    user_pdb_id: int | str | None = None
    receptor_pdb: str | None = None
    receptor_name: str | None = None
    receptor_sequence: str | None = None
    msa_source_gene: str | None = None
    chains: str | None = None
    hotspot_residues: tuple[int, ...] | None = None
    target_residues: tuple[int, ...] | None = None
    numbering: str = "pdb"
    hotspot_frame_attested: bool = False
    note: str | None = None

    def __post_init__(self) -> None:
        gene = str(self.gene or "").strip()
        if not gene:
            raise LigandAIValidationError(
                "TargetSpec.gene is required. It names the receptor a design is "
                "FOR; a target nobody can name cannot be reported in the run's "
                "provenance or folded against."
            )
        object.__setattr__(self, "gene", gene)
        object.__setattr__(
            self, "hotspot_residues",
            _clean_resnums(self.hotspot_residues, f"{gene} hotspot_residues"),
        )
        object.__setattr__(
            self, "target_residues",
            _clean_resnums(self.target_residues, f"{gene} target_residues"),
        )
        numbering = str(self.numbering or "pdb").strip().lower()
        if numbering not in ("pdb", "structure", "boltz"):
            raise LigandAIValidationError(
                f"numbering must be 'pdb', 'structure' or 'boltz'; got "
                f"{self.numbering!r}. The frame is load-bearing: the same integer "
                f"names different residues in each of them."
            )
        object.__setattr__(self, "numbering", numbering)
        if self.label is not None:
            label = str(self.label).strip()
            if not label:
                raise LigandAIValidationError(
                    "TargetSpec.label was blank. Omit it to take the default "
                    "(GENE@organism) rather than pass an empty key."
                )
            object.__setattr__(self, "label", label)
        if self.organism is not None:
            org = str(self.organism).strip()
            object.__setattr__(self, "organism", org or None)

        # ⛔ A NON-DEFAULT ORGANISM WITHOUT ITS OWN STRUCTURE IS A FABRICATION.
        # fold_batch/generate take `target_gene` and resolve the canonical
        # (human) receptor; neither has a species parameter. Labelling that leg
        # "mouse" would produce a cross-species result from a human structure
        # with nothing in the response contradicting it.
        if self.organism is not None and normalize_species(self.organism) != DEFAULT_ORGANISM:
            if not self.has_explicit_structure:
                raise LigandAIValidationError(
                    f"TargetSpec(gene={gene!r}, organism={self.organism!r}) has no "
                    f"structure of its own. No fold or generate surface on this "
                    f"platform takes a species: `target_gene={gene!r}` resolves the "
                    f"CANONICAL (human) receptor, so this leg would fold the human "
                    f"protein and be reported as {self.organism!r}. Nothing "
                    f"downstream would contradict it.\n"
                    f"\n"
                    f"Supply the ortholog's own coordinates:\n"
                    f"  s = client.structures.get({gene!r}, species={self.organism!r})\n"
                    f"  TargetSpec(gene={gene!r}, organism={self.organism!r},\n"
                    f"             receptor_pdb=s.pdb_content,\n"
                    f"             receptor_name={gene.upper()}_{str(self.organism).upper()!r})\n"
                    f"or upload it: client.proteins.upload_pdb(path, gene={gene!r}) "
                    f"→ user_pdb_id=protein.id.\n"
                    f"\n"
                    f"Available orthologs: client.structures.list_species({gene!r})."
                )
        if self.hotspot_residues and self.numbering != "pdb":
            # Not an error — stated, because the frame travels with the numbers.
            pass

    # ─── identity ───────────────────────────────────────────────────────────

    @property
    def key(self) -> str:
        """The panel key — unique per target, ortholog-safe.

        ``label`` when given, else ``GENE@organism`` when an organism was named,
        else the bare gene. Two orthologs of one gene therefore get two keys;
        without that they would collide on ``legs[(sequence, receptor)]`` and on
        the server's own per-gene session map, and one would overwrite the other.
        """
        if self.label:
            return self.label
        if self.organism:
            return f"{self.gene.upper()}@{normalize_species(self.organism)}"
        return self.gene.upper()

    @property
    def organism_normalized(self) -> str:
        return normalize_species(self.organism) if self.organism else DEFAULT_ORGANISM

    @property
    def has_explicit_structure(self) -> bool:
        """True when this target names its OWN coordinates rather than a gene."""
        return bool(
            self.receptor_pdb
            or self.user_pdb_id
            or self.pdb_code
            or self.receptor_sequence
        )

    @property
    def structure_route(self) -> str:
        """Which input actually selects this target's coordinates."""
        if self.receptor_pdb:
            return "receptor_pdb"
        if self.user_pdb_id:
            return "user_pdb_id"
        if self.pdb_code:
            return "pdb_code"
        if self.receptor_sequence:
            return "receptor_sequence"
        return "gene_symbol(canonical)"

    @property
    def frame(self) -> str:
        """The residue-numbering frame, as a string to put in a report.

        Every returned row and summary carries this. A residue number without
        its frame is not a location.
        """
        bits = [self.gene.upper(), self.organism_normalized]
        if self.uniprot_id:
            bits.append(str(self.uniprot_id))
        who = "/".join(bits)
        chain = f":{self.chains}" if self.chains else ""
        return f"{who}{chain} ({self.numbering} numbering, via {self.structure_route})"

    # ─── wire shapes ────────────────────────────────────────────────────────

    def fold_kwargs(self) -> dict[str, Any]:
        """Exactly the kwargs ``client.peptides.fold_batch`` takes for this target.

        ⛔ Only fields ``fold_batch`` actually has. ``organism`` is NOT among
        them — the organism is honoured by sending the ortholog's structure, and
        that is why a non-default organism without one is refused in
        ``__post_init__`` rather than forwarded as a knob nothing reads.
        """
        out: dict[str, Any] = {}
        if self.receptor_pdb:
            out["receptor_pdb"] = self.receptor_pdb
            out["receptor_name"] = self.receptor_name or self.key
        elif self.receptor_sequence:
            out["receptor_sequence"] = self.receptor_sequence
            out["receptor_name"] = self.receptor_name or self.key
        else:
            out["target_gene"] = self.gene
        if self.msa_source_gene:
            out["msa_source_gene"] = self.msa_source_gene
        return out

    def generate_kwargs(self) -> dict[str, Any]:
        """Kwargs for one generation leg against this target.

        ``gene`` always travels (it is the routing / label axis even when an
        uploaded structure supplies the coordinates), and residue-level
        targeting travels with ``numbering`` so the frame is never implicit.
        """
        out: dict[str, Any] = {"gene": self.gene}
        if self.user_pdb_id is not None:
            out["user_pdb_id"] = self.user_pdb_id
        if self.pdb_code:
            out["pdb_code"] = self.pdb_code
        if self.chains:
            out["target_chains"] = [c.strip() for c in self.chains.split(",") if c.strip()]
        if self.hotspot_residues:
            out["hotspot_residues"] = list(self.hotspot_residues)
        if self.target_residues:
            out["target_residues"] = list(self.target_residues)
        if self.hotspot_residues or self.target_residues:
            out["numbering"] = self.numbering
        return out

    def to_wire(self) -> dict[str, Any]:
        """The per-target entry for the platform's ``targets: [...]`` array.

        Mirrors ``POST /api/ptf/parallel/generate``. Residue-level targeting is
        emitted PER TARGET (never hoisted to the shared top level), because a
        shared residue list resolved against a second structure is the
        cross-frame defect this module exists to prevent.
        """
        out: dict[str, Any] = {"gene": self.gene}
        if self.label:
            out["label"] = self.label
        out["targetKey"] = self.key
        if self.organism:
            out["organism"] = self.organism_normalized
        if self.uniprot_id:
            out["uniprotId"] = self.uniprot_id
        if self.pdb_code:
            out["pdbId"] = self.pdb_code
        if self.user_pdb_id is not None:
            out["userPdbId"] = self.user_pdb_id
        if self.receptor_pdb:
            out["pdbData"] = self.receptor_pdb
        if self.chains:
            out["targetChains"] = [c.strip() for c in self.chains.split(",") if c.strip()]
        if self.hotspot_residues:
            out["hotspotResidues"] = list(self.hotspot_residues)
        if self.target_residues:
            out["targetResidues"] = list(self.target_residues)
        if self.hotspot_residues or self.target_residues:
            out["residueNumbering"] = self.numbering
            out["hotspotFrameAttested"] = bool(self.hotspot_frame_attested)
        if self.note:
            out["note"] = self.note
        return out

    def __repr__(self) -> str:  # pragma: no cover - repr only
        return f"<TargetSpec {self.key} {self.frame}>"


def coerce_target(value: TargetSpec | str | Mapping[str, Any]) -> TargetSpec:
    """Accept a :class:`TargetSpec`, a bare gene symbol, or a plain dict.

    A bare string is the canonical receptor for that gene — no organism, no
    residue targeting — which is exactly what the single-target surface has
    always meant, so the existing call shape keeps working unchanged.
    """
    if isinstance(value, TargetSpec):
        return value
    if isinstance(value, str):
        return TargetSpec(gene=value)
    if isinstance(value, Mapping):
        return TargetSpec(**dict(value))
    raise LigandAIValidationError(
        f"a target must be a TargetSpec, a gene symbol string, or a dict of "
        f"TargetSpec fields; got {type(value).__name__}"
    )


@dataclass(frozen=True)
class CoOptimization:
    """N positive targets plus the rule that combines them. Nothing implicit.

    Example — a cross-species-conserved BRD4 binder, steered off BRD2/BRD3::

        human = TargetSpec(gene="BRD4", organism="human")
        mouse = TargetSpec(
            gene="BRD4", organism="mouse", uniprot_id="Q9ESU6",
            receptor_pdb=client.structures.get("BRD4", species="mouse").pdb_content,
            receptor_name="BRD4_MOUSE",
        )
        co = CoOptimization(targets=[human, mouse], aggregate="worst_case")

    ``aggregate`` has NO DEFAULT. ``"worst_case"`` (min across positives) is
    what "binds both" means; ``"mean"`` is available and weaker. Choosing it is
    one keyword and it removes an entire class of misread result.
    """

    targets: tuple[TargetSpec, ...]
    aggregate: CrossTargetAggregate
    require_all_targets: bool = True

    def __init__(
        self,
        targets: Sequence[TargetSpec | str | Mapping[str, Any]] | TargetSpec | str,
        *,
        aggregate: CrossTargetAggregate,
        require_all_targets: bool = True,
    ) -> None:
        if isinstance(targets, (TargetSpec, str, Mapping)):
            targets = [targets]  # type: ignore[list-item]
        specs = tuple(coerce_target(t) for t in targets)
        if not specs:
            raise LigandAIValidationError(
                "CoOptimization needs at least one positive target."
            )
        if aggregate not in CROSS_TARGET_AGGREGATES:
            raise LigandAIValidationError(
                f"aggregate must be one of {list(CROSS_TARGET_AGGREGATES)}; got "
                f"{aggregate!r}.\n"
                f"  'worst_case' — min across the positive targets. A design is "
                f"only as good as its weakest required target; this is what "
                f"'binds BOTH' means and it names the limiting target.\n"
                f"  'mean'       — average across the positive targets. Weaker: a "
                f"strong leg can mask a failed one, so read per_target_pass on "
                f"every row built under it."
            )

        # ⛔ KEY COLLISION IS THE ORTHOLOG FAILURE MODE. Human BRD4 and mouse
        # Brd4 both carry gene symbol "BRD4" and the platform canonicalises
        # symbols at the request boundary, so two entries would land on ONE key
        # — in this SDK's `legs[(sequence, receptor)]` and in the server's own
        # per-gene session map (`job.generationJobs.set(target.gene, …)`). One
        # silently overwrites the other and the panel reports a two-species
        # result measured once.
        seen: dict[str, TargetSpec] = {}
        for spec in specs:
            if spec.key in seen:
                other = seen[spec.key]
                raise LigandAIValidationError(
                    f"two positive targets share the panel key {spec.key!r} "
                    f"({other.frame} and {spec.frame}). Panel legs and the "
                    f"platform's own per-target session map are keyed by this, so "
                    f"one would silently overwrite the other and a two-target "
                    f"result would be reported from one measurement.\n"
                    f"Distinguish them: pass organism= (key becomes GENE@organism) "
                    f"or an explicit label=."
                )
            seen[spec.key] = spec

        # ⛔ RESIDUE FRAMES DO NOT CROSS. Either every target states its own
        # residue-level targeting, or none does. A hotspot list set on one
        # target and absent on another is the shape that makes the shared,
        # top-level list get reused — which resolves the caller's numbers
        # against a DIFFERENT protein that happens to have those resnums.
        with_hotspots = [s for s in specs if s.hotspot_residues or s.target_residues]
        if with_hotspots and len(with_hotspots) != len(specs):
            missing = [s.key for s in specs if s not in with_hotspots]
            raise LigandAIValidationError(
                f"residue-level targeting was given for "
                f"{[s.key for s in with_hotspots]} but not for {missing}. "
                f"Numbering frames differ between orthologs and between genes, so "
                f"a hotspot list cannot be shared: reusing these numbers on "
                f"{missing} would resolve them against a different structure that "
                f"happens to carry the same residue numbers, and nothing would "
                f"report it.\n"
                f"Give each target its OWN hotspot_residues in its OWN frame, or "
                f"give none of them any. An alignment may CHECK an equivalence; "
                f"it must not generate the spec."
            )
        if len(specs) > 1:
            for spec in with_hotspots:
                if not spec.hotspot_frame_attested:
                    raise LigandAIValidationError(
                        f"target {spec.key!r} carries residue-level targeting "
                        f"{list(spec.hotspot_residues or spec.target_residues or [])} "
                        f"in a MULTI-TARGET set, without "
                        f"hotspot_frame_attested=True.\n"
                        f"Frame: {spec.frame}.\n"
                        f"Set hotspot_frame_attested=True to state that these "
                        f"numbers were derived in THIS target's own frame rather "
                        f"than copied from a sibling. This SDK cannot verify that "
                        f"for you — and it will not silently assume it, because a "
                        f"copied residue number resolves to a real residue on the "
                        f"wrong protein and produces no error anywhere."
                    )
        object.__setattr__(self, "targets", specs)
        object.__setattr__(self, "aggregate", aggregate)
        object.__setattr__(self, "require_all_targets", bool(require_all_targets))

    # ─── introspection ──────────────────────────────────────────────────────

    @property
    def keys(self) -> list[str]:
        return [s.key for s in self.targets]

    @property
    def genes(self) -> list[str]:
        return [s.gene for s in self.targets]

    @property
    def is_multi(self) -> bool:
        return len(self.targets) > 1

    @property
    def is_cross_species(self) -> bool:
        """True when the positive targets span more than one organism.

        ⛔ DEFINED ON THE SET OF ORGANISMS, NOT ON "the same gene under two of
        them". The same-gene form cannot be GENERATED as one session — see
        :meth:`check_generation_persistable` — and its supported route gives the
        ortholog its own symbol (``BRD4`` + ``BRD4_MOUSE``). A same-gene test
        would therefore report False for exactly the requests that took the
        route that works, which is the wrong answer on the only runnable shape.
        """
        organisms = {
            spec.organism_normalized
            for spec in self.targets
            if spec.organism is not None
        }
        return len(organisms) > 1

    @property
    def frames(self) -> dict[str, str]:
        """Per-target numbering frame. Goes into every report."""
        return {s.key: s.frame for s in self.targets}

    def spec(self, key: str) -> TargetSpec:
        for s in self.targets:
            if s.key == key:
                return s
        raise LigandAIValidationError(
            f"no positive target with key {key!r}; have {self.keys}"
        )

    # ─── role separation ────────────────────────────────────────────────────

    def check_reference_roles(self, off_targets: Sequence[Any]) -> None:
        """Refuse an off-target that is also one of the positive targets.

        ⛔ THE SIGN MATTERS AND NOTHING DOWNSTREAM CHECKS IT. The v6.9 CFG
        operator is ``paralog_mean`` — guidance is the difference between the
        target pocket and the MEAN of the reference pockets — so a positive
        target inside the reference panel steers designs AWAY from a receptor
        they are being co-optimized FOR. The request is well-formed, the run
        completes, the spend is real, and the objective is inverted.
        """
        off_keys = {coerce_target(o).key.upper() for o in off_targets}
        off_genes = {coerce_target(o).gene.upper() for o in off_targets}
        clash_keys = [k for k in self.keys if k.upper() in off_keys]
        if clash_keys:
            raise LigandAIValidationError(
                f"{clash_keys} appear as BOTH a positive target and an off-target. "
                f"Δ against a positive target is zero by construction, and as a "
                f"contrastive reference it steers generation away from a receptor "
                f"you are co-optimizing FOR (cfg_reference='paralog_mean' guides "
                f"by target − mean(references)). Remove it from one side."
            )
        clash_genes = [g for g in self.genes if g.upper() in off_genes]
        if clash_genes:
            raise LigandAIValidationError(
                f"gene(s) {sorted(set(clash_genes))} appear on both sides of the "
                f"panel: as a positive target and as an off-target reference. "
                f"Even under different organisms this is almost never what you "
                f"want — the ortholog you are selecting FOR would also be the "
                f"reference you steer AWAY from.\n"
                f"positives : {self.frames}\n"
                f"off-targets: {sorted(off_genes)}\n"
                f"If you really mean species-DISCRIMINATING design (bind human, "
                f"avoid mouse), then mouse is an off-target and must NOT also be "
                f"in targets=."
            )

    # ─── selection ──────────────────────────────────────────────────────────

    def check_generation_persistable(self) -> None:
        """Refuse a GENERATION whose positives share a gene symbol.

        ⛔ MEASURED 2026-09-23 against the live generation handler. Two positive
        targets under one gene symbol (the cross-species case: human ``BRD4`` +
        mouse ``Brd4``) are accepted by the co-optimization gate and then
        rejected one layer deeper by the parentage boundary — "Parentage cannot
        durably index multiple variants for one gene in a single session" —
        which the handler converts into a 503 whose message says **"Please
        retry."** Retrying can never work: the parentage layer builds its
        per-target variant maps keyed by GENE, so the second target would
        overwrite the first. A "please retry" on a structural constraint is a
        false blocker, and people act on a false blocker by waiting.

        So this refuses before any spend and names the route that does work:
        upload the ortholog under its own gene label, so the two legs carry
        distinct symbols.

        ⛔ NOT called by :meth:`Campaigns.fold_panel`, deliberately. Folding is
        unaffected by this limit — each leg reaches its own receptor through its
        own ``fold_kwargs()`` and nothing keys a fold by gene — so a panel over
        human and mouse ``BRD4`` is a perfectly good MEASUREMENT. Only the
        generation session cannot be persisted under one symbol. Refusing the
        panel too would remove a capability that works.
        """
        by_symbol: dict[str, list[str]] = {}
        for spec in self.targets:
            by_symbol.setdefault(spec.gene.strip().upper(), []).append(spec.key)
        shared = {k: v for k, v in by_symbol.items() if len(v) > 1}
        if not shared:
            return
        symbol, legs = next(iter(shared.items()))
        raise LigandAIValidationError(
            f"positive targets {legs} share the gene symbol {symbol!r}, and a "
            f"generation session cannot be persisted under one symbol: the "
            f"platform's parentage layer keys its per-target variant maps by gene, "
            f"so the second target would overwrite the first. It is a structural "
            f"limit, not a transient failure — the server returns a 503 saying "
            f"'Please retry', and retrying will not change it.\n"
            f"\n"
            f"What works today — give the ortholog its own symbol:\n"
            f"  p = client.proteins.upload_pdb(ortholog_pdb, gene='{symbol}_MOUSE')\n"
            f"  mouse = TargetSpec(gene='{symbol}_MOUSE', organism='mouse',\n"
            f"                     user_pdb_id=p.id, receptor_pdb=ortholog_text)\n"
            f"The two legs then carry distinct symbols and the co-optimization runs.\n"
            f"\n"
            f"⛔ This limit is on GENERATION only. client.campaigns.fold_panel() "
            f"accepts these same two targets and measures both legs correctly — "
            f"each reaches its own receptor through its own structure route, and "
            f"nothing keys a fold by gene. If you already have designs, the panel "
            f"is available to you now."
        )

    def aggregate_scores(
        self, per_target: Mapping[str, float]
    ) -> tuple[float, str | None]:
        """Combine measured per-target scores under this object's own rule."""
        return aggregate_across_targets(per_target, self.aggregate)

    def target_leg_passes(
        self, per_target: Mapping[str, float], target_leg_min: float
    ) -> tuple[bool, dict[str, bool]]:
        """Does the positive side clear its floor, and which legs individually?

        Under ``worst_case`` the set passes only when EVERY positive leg clears
        the floor. Under ``mean`` the AVERAGE is what is tested — which can pass
        with a failing leg, so the per-leg verdicts are returned either way and
        the row always carries them.
        """
        per_leg = {k: v >= target_leg_min for k, v in per_target.items()}
        if self.aggregate == "worst_case":
            return all(per_leg.values()), per_leg
        agg, _limiting = self.aggregate_scores(per_target)
        return agg >= target_leg_min, per_leg

    def describe(self) -> str:
        """One-line summary for logs and the run record."""
        kind = (
            "cross-species" if self.is_cross_species
            else ("multi-gene" if self.is_multi else "single")
        )
        return (
            f"{kind} co-optimization over {len(self.targets)} positive target(s) "
            f"[{', '.join(self.keys)}] aggregated by {self.aggregate}"
        )

    def to_wire(self) -> dict[str, Any]:
        """The request block the platform reads for a co-optimization run."""
        return {
            "coOptimization": {
                "targets": [s.to_wire() for s in self.targets],
                "aggregate": self.aggregate,
                "requireAllTargets": self.require_all_targets,
                "crossSpecies": self.is_cross_species,
                "frames": self.frames,
            },
            "targets": [s.to_wire() for s in self.targets],
        }

    def __len__(self) -> int:
        return len(self.targets)

    def __iter__(self):
        return iter(self.targets)

    def __repr__(self) -> str:  # pragma: no cover - repr only
        return f"<CoOptimization {self.describe()}>"
