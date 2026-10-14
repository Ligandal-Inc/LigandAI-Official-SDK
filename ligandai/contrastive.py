# Copyright © 2025 Ligandal, Inc. All rights reserved.
"""Contrastive guidance and avoidance — first-class inputs to peptide design.

A *contrastive* design run does not only ask "bind this pocket". It also carries
one or more REAL off-target receptor pockets, and steers the generator away from
them at sampling time. On the v6.9 generator line this is the ``paralog_mean``
CFG reference: the model is guided by the difference between the target pocket
and the mean of the supplied reference pockets.

Two rules the platform enforces, mirrored here so a bad request fails on your
machine instead of after GPU spend:

1. **References must be REAL and NONZERO.** An all-zero reference is a
   receptor-blind pocket wearing the right shape. The generator refuses it
   (server error ``missing_nonzero_cfg_references``) rather than fabricating a
   reference, and so does this module.
2. **Width is not convertible.** A v6.9 pocket is 107 columns
   (57 geometric + 50 all-atom); the v6.5 line is 48. The extra columns are
   measured descriptors, not padding — zero-extending a 48-wide pocket to 107
   hands the model geometry where it expects composition. Never pad, tile, or
   slice across widths; re-featurize at the width the model declares.

⛔ Paralog guidance is an AFFINITY PRIOR; it does not buy selectivity. It steers
the generator away from the off-target pockets you name, instead of drawing
freely and filtering afterwards. In our controlled test it was not more
selective than generate-and-filter. Use it as a different experiment, not as an
upgrade. Selectivity is established only by a delta-ipSAE panel comparison in the
SAME fold pass, and an absent panel fold is ABSENT, never "not selective" (same
wording as the web design form, ``ParalogConditioningControl.tsx``
``CONTRASTIVE_COPY``).

The avoidance surface (:class:`AvoidancePolicy`) is the other half: which
receptor residues a design must stay off, and which sequences are known hard
negatives that should not be re-proposed.

⛔ An avoid-residue list is a REQUEST, not a proof. Row deletion is a measured
no-op on the generated sequence distribution even when every residue resolves.
Where a design actually binds is established by folding it and checking
contacts — see ``client.campaigns`` for the panel fold that does that.

Refs: bd-dre-if562.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from ligandai.errors import LigandAIParameterNotForwarded, LigandAIValidationError

__all__ = [
    "MAX_AVOID_PARALOGS",
    "V6_5_POCKET_FEATURE_DIM",
    "V6_9_22_POCKET_FEATURE_DIM",
    "V6_9_POCKET_FEATURE_DIM",
    "AvoidancePolicy",
    "CfgReference",
    "CfgReferenceProvenance",
    "ContrastiveGuidance",
    "ContrastiveReference",
    "ParalogReferenceEntry",
    "PocketFeatureMatrix",
    "avoid_paralogs_to_wire",
    "normalize_avoid_paralogs",
    "require_avoid_paralogs_model",
]


#: Pocket-feature width declared by v6.9.21: 57 geometric + 50 all-atom columns.
#:
#: ⛔ THIS IS NOT THE WHOLE v6.9 LINE. v6.9.21 and v6.9.22 consume DIFFERENT
#: widths, and this constant used to be applied to both — which would have
#: width-checked a v6.9.22 panel against 107 and rejected the correct 65-wide
#: matrices (or, worse, accepted 107-wide ones the model cannot read).
#: 48 / 65 / 107 are three different vectors; nothing converts between them.
#: Authority: fastapi_backend/services/paralog_reference_service.py:29 —
#: "v6.9.21 consumes 107 dims (geom57 + allatom50); v6.9.22 consumes 65 dims".
V6_9_POCKET_FEATURE_DIM = 107

#: Pocket-feature width declared by v6.9.22 — the 65-dim SE(3)-invariant
#: receptor featurizer (fastapi_backend/core/rec_feat65_serving.py).
V6_9_22_POCKET_FEATURE_DIM = 65

#: Pocket-feature width declared by the v6.5 generator line.
V6_5_POCKET_FEATURE_DIM = 48

#: CFG reference operators the platform will execute.
#:
#: ``"paralog_mean"`` is the only supported value: the guidance reference is the
#: mean of the supplied real paralog / off-target pockets.
#:
#: ⛔ ``"uncond"`` appears in some checkpoint metadata as the HISTORICAL
#: declaration. It constructs an all-zero receptor pocket and the served model
#: refuses to execute it. It is not offered here.
CfgReference = Literal["paralog_mean"]

#: Generator models that take a CFG reference pocket. Supplying references to a
#: model outside this set is rejected by the server
#: (``cfg_references_not_supported``) rather than silently ignored.
_CFG_REFERENCE_MODELS: frozenset[str] = frozenset({"v6.9.21", "v6.9.22"})

#: Expected pocket-feature width per generator model, for the models this SDK
#: build knows about. An id absent from this map is NOT assumed unsupported —
#: the server's model registry is the authority — it just cannot be width-checked
#: client-side.
_MODEL_POCKET_DIM: dict[str, int] = {
    "v6.5": V6_5_POCKET_FEATURE_DIM,
    "v6.5.21": V6_5_POCKET_FEATURE_DIM,
    "v6.5.22": V6_5_POCKET_FEATURE_DIM,
    "v6.9.21": V6_9_POCKET_FEATURE_DIM,
    "v6.9.22": V6_9_22_POCKET_FEATURE_DIM,
}


def model_pocket_feature_dim(model_id: str | None) -> int | None:
    """Pocket-feature width for ``model_id``, or None if this build cannot say.

    None means "ask the server", never "any width is fine".
    """
    if not model_id:
        return None
    return _MODEL_POCKET_DIM.get(str(model_id).strip())


def model_takes_cfg_reference(model_id: str | None) -> bool | None:
    """Whether ``model_id`` consumes a CFG reference pocket.

    Returns None when this SDK build has never heard of the id — a published
    SDK cannot know the capabilities of a model released after it was built, so
    the request is forwarded and the server decides.
    """
    if not model_id:
        return None
    mid = str(model_id).strip()
    if mid in _CFG_REFERENCE_MODELS:
        return True
    if mid in _MODEL_POCKET_DIM:
        return False
    return None


#: Server-side cap on a NAMED ``avoid_paralogs=`` panel
#: (``cfg_reference_genes``). Mirrors the paralog reference producer's own
#: limit — enforced here too so an oversized panel fails on your machine
#: instead of after the request reaches the server.
MAX_AVOID_PARALOGS = 8


def normalize_avoid_paralogs(genes: Sequence[str] | None) -> tuple[str, ...] | None:
    """Normalize ``avoid_paralogs=`` into the wire gene list, or None if unset.

    * HGNC-style uppercase normalisation (``"brd3"`` -> ``"BRD3"``), so the same
      gene named twice with different casing is recognised as one duplicate
      rather than two distinct panel entries.
    * Order-preserving de-dupe.
    * Refuses a blank entry — a blank gene name is not "no restriction", it is
      a request the server cannot resolve, and failing loudly here is cheaper
      than a confusing 400 after the request goes out.
    * Refuses more than :data:`MAX_AVOID_PARALOGS` genes, matching the
      server-side cap on the paralog reference producer
      (``paralog_reference_service.py``). The SDK does not silently truncate a
      panel to the cap — the caller chose every gene in it, and dropping some
      of them without saying so is exactly the "steered away from something
      you did not choose" failure this module exists to prevent.

    Returns ``None`` for ``None`` or an empty sequence — both mean "the caller
    named nothing", which is the same as omitting the argument.
    """
    if genes is None:
        return None
    cleaned: list[str] = []
    seen: set[str] = set()
    for g in genes:
        gene = str(g).strip().upper()
        if not gene:
            raise LigandAIValidationError(
                "avoid_paralogs contains a blank entry. Every gene must be a "
                "non-empty HGNC symbol (e.g. 'BRD3') — a blank cannot be "
                "resolved to a paralog reference and is not a way to say "
                "'no restriction' (omit the argument for that)."
            )
        if gene not in seen:
            seen.add(gene)
            cleaned.append(gene)
    if not cleaned:
        return None
    if len(cleaned) > MAX_AVOID_PARALOGS:
        raise LigandAIValidationError(
            f"avoid_paralogs names {len(cleaned)} genes; the platform caps a "
            f"named panel at {MAX_AVOID_PARALOGS}. Narrow it to the paralogs "
            f"that matter most for this campaign — omitting the argument lets "
            f"the platform derive its own panel instead, it is not truncated "
            f"here for you."
        )
    return tuple(cleaned)


def avoid_paralogs_to_wire(genes: Sequence[str]) -> dict[str, Any]:
    """The exact request keys a named ``avoid_paralogs=`` panel puts on the wire.

    Both the snake and camel spelling, mirroring
    :meth:`ContrastiveGuidance.to_wire` — the request model accepts either and
    this SDK does not guess which one a given server build reads.
    """
    genes_list = list(genes)
    return {"cfg_reference_genes": genes_list, "cfgReferenceGenes": genes_list}


def require_avoid_paralogs_model(model_id: str | None) -> None:
    """Fail fast when ``avoid_paralogs=`` is paired with a non-CFG model.

    Mirrors :meth:`ContrastiveGuidance.validate_for_model`: says nothing (and
    forwards) for a model id this SDK build has never heard of, because a
    published build cannot know the capabilities of a model released after
    it — the server's model registry is the authority and answers with
    ``cfg_references_not_supported`` if the id turns out not to take one.
    """
    takes = model_takes_cfg_reference(model_id)
    if takes is False:
        raise LigandAIValidationError(
            f"avoid_paralogs requires a CFG-capable generator model "
            f"({sorted(_CFG_REFERENCE_MODELS)}); {model_id!r} does not consume "
            f"a CFG reference pocket, so a named panel would be silently "
            f"ignored rather than steering anything. The server rejects this "
            f"combination as cfg_references_not_supported — pass "
            f"model='v6.9.21' or model='v6.9.22', or drop avoid_paralogs."
        )


def _as_rows(value: Any, label: str) -> list[list[float]]:
    """Coerce a 2-D feature matrix to plain nested lists of floats.

    Accepts anything with ``tolist()`` (numpy arrays, torch tensors moved to
    CPU) as well as nested sequences. Rejects ragged, empty, or non-finite
    matrices — every one of those reaches the worker as a shape error thousands
    of GPU-seconds later.
    """
    if value is None:
        raise LigandAIValidationError(f"{label} is required and was None")
    if hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise LigandAIValidationError(
            f"{label} must be a 2-D matrix [n_residues][n_features], got {type(value).__name__}"
        )
    rows: list[list[float]] = []
    for i, row in enumerate(value):
        if hasattr(row, "tolist"):
            row = row.tolist()
        if isinstance(row, (str, bytes)) or not isinstance(row, Iterable):
            raise LigandAIValidationError(
                f"{label}[{i}] must be a sequence of floats, got {type(row).__name__} — "
                f"{label} is a 2-D matrix [n_residues][n_features], not a flat vector"
            )
        out_row: list[float] = []
        for j, cell in enumerate(row):
            try:
                f = float(cell)
            except (TypeError, ValueError) as exc:
                raise LigandAIValidationError(
                    f"{label}[{i}][{j}] is not a number: {cell!r}"
                ) from exc
            if not math.isfinite(f):
                raise LigandAIValidationError(
                    f"{label}[{i}][{j}] is {f} — NaN/Inf in a pocket feature is a "
                    f"featurization defect, not a value the generator can consume"
                )
            out_row.append(f)
        rows.append(out_row)
    if not rows:
        raise LigandAIValidationError(f"{label} has zero residues")
    widths = {len(r) for r in rows}
    if len(widths) != 1:
        raise LigandAIValidationError(
            f"{label} is ragged — rows have widths {sorted(widths)}; a pocket "
            f"feature matrix must be rectangular"
        )
    return rows


@dataclass(frozen=True)
class PocketFeatureMatrix:
    """A receptor pocket as the generator consumes it: ``[n_residues][dim]``.

    Carries its provenance alongside the numbers, because a reference pocket
    without a structure and chain cannot be recomputed and therefore cannot be
    reproduced.
    """

    rows: list[list[float]]
    gene: str | None = None
    source_pdb_id: str | None = None
    source_chain: str | None = None
    residue_numbers: list[int] | None = None
    pocket_source: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "rows", _as_rows(self.rows, "pocket features"))
        if self.residue_numbers is not None and len(self.residue_numbers) != len(self.rows):
            raise LigandAIValidationError(
                f"residue_numbers has {len(self.residue_numbers)} entries but the "
                f"matrix has {len(self.rows)} rows — the two must be index-aligned "
                f"or residue-level steering resolves to the wrong rows"
            )

    @property
    def dim(self) -> int:
        """Feature width (number of columns)."""
        return len(self.rows[0])

    @property
    def n_residues(self) -> int:
        return len(self.rows)

    def is_all_zero(self) -> bool:
        """True when every cell is exactly zero — a receptor-blind pocket."""
        return all(cell == 0.0 for row in self.rows for cell in row)

    def require_dim(self, expected: int, *, label: str = "pocket features") -> None:
        """Raise unless the width is exactly ``expected``.

        ⛔ Never "fixes" a mismatch. 48 is not a prefix of 107 and 107 is not a
        zero-extension of 48; converting between them produces a matrix of the
        right shape and the wrong meaning.
        """
        if self.dim != expected:
            raise LigandAIValidationError(
                f"{label} has feature width {self.dim}, but the selected generator "
                f"declares {expected}. These widths are NOT convertible — the extra "
                f"columns are measured descriptors, not padding. Re-featurize the "
                f"pocket at width {expected} (client.campaigns.pocket_features"
                f"(gene, dim={expected})) instead of padding or slicing."
            )

    def to_wire(self) -> list[list[float]]:
        return [list(r) for r in self.rows]

    def __len__(self) -> int:
        return len(self.rows)

    def __repr__(self) -> str:  # pragma: no cover - repr only
        who = self.gene or "pocket"
        src = f" {self.source_pdb_id}:{self.source_chain}" if self.source_pdb_id else ""
        return f"<PocketFeatureMatrix {who}{src} {self.n_residues}x{self.dim}>"


@dataclass(frozen=True)
class ContrastiveReference:
    """One off-target / paralog pocket used as a contrastive reference.

    ``gene`` is required: a reference the run cannot name is a reference nobody
    can audit afterwards, and "which paralogs did we steer away from" is the
    first question a selectivity claim has to answer.
    """

    gene: str
    features: PocketFeatureMatrix
    identity_to_target: float | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        if not self.gene or not str(self.gene).strip():
            raise LigandAIValidationError(
                "ContrastiveReference.gene is required — an unnamed reference "
                "cannot be reported in the run's provenance"
            )
        if not isinstance(self.features, PocketFeatureMatrix):
            object.__setattr__(
                self, "features", PocketFeatureMatrix(rows=self.features, gene=self.gene)
            )
        if self.features.is_all_zero():
            raise LigandAIValidationError(
                f"contrastive reference {self.gene!r} is all zeros. The generator "
                f"refuses an all-zero reference pocket (it is a fabricated "
                f"receptor, not an off-target), and so does this SDK. Supply real "
                f"featurized off-target receptor pockets."
            )

    @property
    def dim(self) -> int:
        return self.features.dim

    def __repr__(self) -> str:  # pragma: no cover - repr only
        return f"<ContrastiveReference {self.gene} {self.features.n_residues}x{self.dim}>"


@dataclass(frozen=True)
class ContrastiveGuidance:
    """Contrastive (paralog-mean) guidance for one generation request.

    Example — steer ENPP3 designs away from ENPP1 and ENPP6::

        refs = client.campaigns.contrastive_references(
            target="ENPP3", paralogs=["ENPP1", "ENPP6"], model="v6.9.21",
        )
        job = client.peptides.generate(
            gene="ENPP3", num_peptides=200, model="v6.9.21", contrastive=refs,
        )

    ``expected_dim`` defaults to the v6.9 width (107) because ``paralog_mean``
    is a v6.9-line operator. Pass it explicitly if a future model declares a
    different width.
    """

    references: tuple[ContrastiveReference, ...]
    cfg_reference: CfgReference = "paralog_mean"
    expected_dim: int = V6_9_POCKET_FEATURE_DIM
    guidance_weight: float | None = None

    def __init__(
        self,
        references: Sequence[ContrastiveReference] | ContrastiveReference,
        *,
        cfg_reference: CfgReference = "paralog_mean",
        expected_dim: int = V6_9_POCKET_FEATURE_DIM,
        guidance_weight: float | None = None,
    ) -> None:
        if isinstance(references, ContrastiveReference):
            references = [references]
        refs = tuple(references)
        if not refs:
            raise LigandAIValidationError(
                "ContrastiveGuidance needs at least one reference. "
                "cfg_reference='paralog_mean' averages REAL off-target receptor "
                "pockets; with none supplied there is nothing to average and the "
                "server refuses the run (missing_nonzero_cfg_references)."
            )
        if cfg_reference != "paralog_mean":
            raise LigandAIValidationError(
                f"unsupported cfg_reference {cfg_reference!r}. The only operator the "
                f"platform will execute is 'paralog_mean'. 'uncond' constructs an "
                f"all-zero receptor pocket and is refused by the served model even "
                f"where a checkpoint's historical metadata still declares it."
            )
        for ref in refs:
            if not isinstance(ref, ContrastiveReference):
                raise LigandAIValidationError(
                    "every entry of references must be a ContrastiveReference "
                    "(use client.campaigns.contrastive_references(...) to build them)"
                )
            ref.features.require_dim(
                expected_dim, label=f"contrastive reference {ref.gene!r}"
            )
        seen: set[str] = set()
        for ref in refs:
            key = ref.gene.strip().upper()
            if key in seen:
                raise LigandAIValidationError(
                    f"duplicate contrastive reference for {ref.gene!r}. A gene "
                    f"repeated in the panel is weighted twice in the mean, which "
                    f"silently changes the guidance direction."
                )
            seen.add(key)
        if guidance_weight is not None:
            # ⛔ VERIFIED NOT CALLER-SETTABLE (2026-08-31). The CFG weight lives in
            # the platform's model registry (v6.9.21 carries cfg_guidance_weight=2.0)
            # and is declared in the model's spawn params but never populated from a
            # request — the worker always takes the checkpoint/registry value. Sending
            # it would be a knob that reads as set and generates as unset, which is
            # the exact defect class this SDK refuses to ship. Rejected here rather
            # than silently dropped downstream.
            raise LigandAIValidationError(
                "guidance_weight is not settable per request. The CFG guidance weight "
                "is pinned by the platform's generator registry for the selected model "
                "and no request field reaches it, so passing it here would silently do "
                "nothing. Omit it; the served model applies its own validated weight."
            )
        object.__setattr__(self, "references", refs)
        object.__setattr__(self, "cfg_reference", cfg_reference)
        object.__setattr__(self, "expected_dim", int(expected_dim))
        object.__setattr__(self, "guidance_weight", guidance_weight)

    @property
    def genes(self) -> list[str]:
        """The off-target genes being steered away from, in panel order."""
        return [r.gene for r in self.references]

    def validate_for_model(self, model_id: str | None) -> None:
        """Fail fast on model/guidance combinations the server will reject.

        Raises when the named model does not consume a CFG reference pocket, or
        when its declared pocket width differs from these references'. Says
        nothing (and forwards) for a model id this build has not heard of.
        """
        takes = model_takes_cfg_reference(model_id)
        if takes is False:
            raise LigandAIValidationError(
                f"generator {model_id!r} does not use a CFG reference pocket, so "
                f"contrastive references would be silently ignored. Contrastive "
                f"guidance requires a v6.9-line model (e.g. model='v6.9.21'). "
                f"The server rejects this combination as cfg_references_not_supported."
            )
        dim = model_pocket_feature_dim(model_id)
        if dim is not None and dim != self.expected_dim:
            raise LigandAIValidationError(
                f"generator {model_id!r} declares pocket_feature_dim={dim} but these "
                f"contrastive references are {self.expected_dim}-wide. Rebuild them at "
                f"width {dim} — the widths are not convertible."
            )

    def to_wire(self) -> dict[str, Any]:
        """The exact request keys the platform reads.

        A list of 2-D matrices, one per off-target, under BOTH the snake and
        camel spelling the request model accepts as aliases. Nothing else is
        emitted:

        * ``cfg_reference`` is NOT a request field. The served model pins its
          own operator from the platform's generator registry; a request key of
          that name is read by nothing.
        * ``pocket_feature_dim`` is NOT a request field either — it is declared
          by the model and enforced against the matrices supplied here.
        * The reference GENE NAMES are deliberately not sent. No request field
          carries them, so emitting one would look like provenance and be
          discarded. The panel is recorded client-side instead (see
          :meth:`describe` and the campaign run record).
        """
        rows = [r.features.to_wire() for r in self.references]
        return {
            "reference_pocket_features": rows,
            "referencePocketFeatures": rows,
        }

    def check_invariants(self) -> None:
        """Re-assert every property a servable panel must have.

        The constructor already enforces all of these, so on a panel built by
        :meth:`Campaigns.contrastive_references` this is a no-op. It exists
        because a frozen dataclass is not tamper-proof (``object.__setattr__``
        reaches straight past ``__init__``) and because a panel can be rebuilt,
        re-ordered or widened by a campaign loop between construction and the
        request that carries it.

        Every one of these is a SILENT-CORRUPTION mode, not an error the worker
        would report:

        * no references at all — the server refuses the run
          (``missing_nonzero_cfg_references``) after the request has been
          accepted and the credits reserved;
        * an operator other than ``paralog_mean`` — read by nothing;
        * an all-zero reference — a fabricated receptor wearing the right shape;
        * a width that is not ``expected_dim`` — the extra columns are measured
          descriptors, so a mismatch is a different vector, never a slice;
        * a repeated gene — double-weighted in the mean, which moves the
          guidance direction with nothing in the response saying so.
        """
        refs = tuple(self.references)
        if not refs:
            raise LigandAIValidationError(
                "contrastive panel is empty. cfg_reference='paralog_mean' averages "
                "REAL off-target receptor pockets; with none supplied there is "
                "nothing to average and the server refuses the run "
                "(missing_nonzero_cfg_references)."
            )
        if self.cfg_reference != "paralog_mean":
            raise LigandAIValidationError(
                f"unsupported cfg_reference {self.cfg_reference!r}. The only operator "
                f"the platform will execute is 'paralog_mean'."
            )
        seen: set[str] = set()
        for ref in refs:
            if not isinstance(ref, ContrastiveReference):
                raise LigandAIValidationError(
                    "every entry of references must be a ContrastiveReference "
                    "(use client.campaigns.contrastive_references(...) to build them)"
                )
            if ref.features.is_all_zero():
                raise LigandAIValidationError(
                    f"contrastive reference {ref.gene!r} is all zeros. The generator "
                    f"refuses an all-zero reference pocket (it is a fabricated "
                    f"receptor, not an off-target), and so does this SDK."
                )
            ref.features.require_dim(
                self.expected_dim, label=f"contrastive reference {ref.gene!r}"
            )
            key = ref.gene.strip().upper()
            if key in seen:
                raise LigandAIValidationError(
                    f"duplicate contrastive reference for {ref.gene!r}. A gene "
                    f"repeated in the panel is weighted twice in the mean, which "
                    f"silently changes the guidance direction."
                )
            seen.add(key)

    def require_forwardable(
        self,
        *,
        allow_unforwarded: bool = False,
        model_id: str | None = None,
        server_forwards: bool | None = None,
    ) -> None:
        """Pre-flight a panel about to be sent, and refuse what cannot execute.

        ⛔ RE-VERIFIED 2026-09-22 by reading the request path end to end again.
        **The passthrough now exists.** Express forwards
        ``reference_pocket_features`` (snake, from the camel it persists on the
        job config) to FastAPI; FastAPI's generation request model declares the
        field under both spellings, validates every matrix against the selected
        model's declared width, and refuses a panel sent to a generator that has
        no CFG reference operator (``cfg_references_not_supported``); the v6.9.21
        / v6.9.22 Modal runtimes and the delta resident read it. So a supplied
        panel reaches the generator and the caller CHOOSES the off-targets.

        This method used to raise :class:`LigandAIParameterNotForwarded`
        unconditionally, because the intermediate hop's forward payload was a
        fixed key list that omitted the references. That was true when it was
        written and is false now, and a refusal that outlives its cause is its
        own defect — it kept a working capability unreachable and pointed the
        caller at an override kwarg instead of at the panel.

        What it still refuses, because each is a silent-corruption mode rather
        than an error the run would report:

        * every invariant in :meth:`check_invariants` — empty panel, wrong
          operator, all-zero reference, width mismatch, duplicate gene;
        * ``model_id`` naming a generator with no CFG reference pocket (the
          v6.5 line), which is :meth:`validate_for_model`;
        * a server known NOT to forward the panel — pass
          ``server_forwards=False`` when talking to a build that predates the
          passthrough, and the old refusal comes back.

        Args:
            allow_unforwarded: suppresses the ``server_forwards=False`` refusal.
                Accepted for compatibility; it has NO effect on the checks
                above, and it never did — those are properties of the panel, not
                of the server.
            model_id: the generator this panel is about to be sent to. When
                given, :meth:`validate_for_model` runs as part of the pre-flight
                so one call covers the whole request.
            server_forwards: tri-state. ``None`` (the default) means "the
                platform forwards it", which is what the current request path
                does. ``True`` is the same, stated. ``False`` restores the
                pre-passthrough refusal for a pinned old server.
        """
        self.check_invariants()
        if model_id is not None:
            self.validate_for_model(model_id)
        if server_forwards is False and not allow_unforwarded:
            raise LigandAIParameterNotForwarded(
                "this server build does not forward a caller-supplied contrastive "
                "panel. The generation request model accepts and width-checks "
                "reference_pocket_features, but the hop in front of it forwards a "
                "fixed key list that omits them on builds before the passthrough "
                "landed — so the request would succeed, the run would proceed "
                "WITHOUT your panel, and no response field would say so. Refusing "
                "to send a knob that reads as set and executes as unset.\n"
                "\n"
                "What still works on such a server: a v6.9-line model is "
                "contrastive regardless — the platform derives its own real paralog "
                "references and refuses the run if it cannot produce them. You just "
                "cannot choose the panel, and the chosen panel is not reported "
                "back.\n"
                "\n"
                "To send it anyway, pass allow_unforwarded_contrastive=True to "
                "client.peptides.generate() (auto_fold=True by default, so this "
                "already folds) or client.campaigns.recursive() — or "
                "allow_unforwarded=True if you are calling "
                "ContrastiveGuidance.require_forwardable() directly.\n"
                "\n"
                "⛔ The two names are NOT interchangeable. `generate()` swallows "
                "unknown kwargs into the request body, so allow_unforwarded=True "
                "passed THERE is accepted, forwarded as a body field, and does "
                "nothing — leaving this error in place while looking like it was "
                "answered.",
                parameter="contrastive",
                reached="generation request model (validated, width-checked)",
                dropped_at="public generation endpoint forward payload (fixed key list)",
                # ⛔ STAYS bd-dre-if562 — the contrastive bead. The passthrough
                # work is bd-dre-f4mc9, but this error is about the CAPABILITY,
                # and a caller who hits it needs the thread that owns contrastive
                # guidance, not the one that shipped one hop of plumbing.
                tracking="bd-dre-if562",
                override="allow_unforwarded_contrastive=True",
            )

    def describe(self) -> str:
        """One-line human summary for logs and run provenance."""
        return (
            f"{self.cfg_reference} over {len(self.references)} reference(s) "
            f"[{', '.join(self.genes)}] at width {self.expected_dim}"
        )

    def __len__(self) -> int:
        return len(self.references)

    def __repr__(self) -> str:  # pragma: no cover - repr only
        return f"<ContrastiveGuidance {self.describe()}>"


@dataclass
class AvoidancePolicy:
    """What a design must stay away from — surface, and sequence.

    Three independent mechanisms, deliberately separate because they act at
    different points and have very different strengths:

    * ``avoid_residues`` / ``avoid_shell_angstrom`` — receptor rows removed from
      what the generator sees. **Weak.** Measured as a no-op on the generated
      sequence distribution in several campaigns even when every residue
      resolves. Treat as a hint, and verify by folding.
    * ``off_target_genes`` — the paralogs to build contrastive references from.
      **This is the mechanism with measured effect at generation time**; it
      becomes :class:`ContrastiveGuidance`.
    * ``hard_negatives`` — sequences already shown to bind an off-target (or to
      fail). Excluded from the returned pool, and carried into the run record so
      a later round does not re-propose them.

    ``hard_negatives`` is a CLIENT-SIDE filter today: the generator has no
    sequence-repulsion term, so the SDK removes them from the returned pool
    rather than claiming the model avoided them.
    """

    avoid_residues: list[Any] = field(default_factory=list)
    avoid_shell_angstrom: float | None = None
    off_target_genes: list[str] = field(default_factory=list)
    hard_negatives: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.off_target_genes = [
            str(g).strip() for g in (self.off_target_genes or []) if str(g).strip()
        ]
        cleaned: list[str] = []
        for s in self.hard_negatives or []:
            seq = str(s).strip().upper()
            if not seq:
                continue
            if not seq.isalpha():
                raise LigandAIValidationError(
                    f"hard_negatives entry {s!r} is not an amino-acid sequence"
                )
            cleaned.append(seq)
        # Order-preserving dedupe: a sequence listed twice is one hard negative.
        self.hard_negatives = list(dict.fromkeys(cleaned))
        if self.avoid_shell_angstrom is not None:
            self.avoid_shell_angstrom = float(self.avoid_shell_angstrom)

    @property
    def hard_negative_set(self) -> frozenset[str]:
        return frozenset(self.hard_negatives)

    def filter_sequences(self, sequences: Sequence[str]) -> tuple[list[str], list[str]]:
        """Split ``sequences`` into (kept, rejected-as-hard-negative).

        Both halves are returned. A silently shrinking pool is its own failure
        mode — the caller is told exactly what was removed.
        """
        blocked = self.hard_negative_set
        kept: list[str] = []
        rejected: list[str] = []
        for s in sequences:
            (rejected if str(s).strip().upper() in blocked else kept).append(s)
        return kept, rejected

    def generation_kwargs(self) -> dict[str, Any]:
        """The subset of this policy that ``peptides.generate()`` takes directly."""
        out: dict[str, Any] = {}
        if self.avoid_residues:
            out["avoid_residues"] = list(self.avoid_residues)
        if self.avoid_shell_angstrom is not None:
            out["avoid_shell_angstrom"] = self.avoid_shell_angstrom
        return out


@dataclass(frozen=True)
class ParalogReferenceEntry:
    """One real off-target pocket a CFG-conditioned run actually used.

    Read off :attr:`CfgReferenceProvenance.paralogs` — never constructed
    directly. Mirrors one element of the ``cfg_references_resolved`` SSE
    payload (``fastapi_backend/routers/generate.py:6135``).
    """

    paralog_gene: str
    source_pdb_id: str | None = None
    source_chain: str | None = None
    identity_to_target_domain: float | None = None
    n_rows: int | None = None


@dataclass(frozen=True)
class CfgReferenceProvenance:
    """Which real off-target pockets a run's ``paralog_mean`` guidance used.

    Read off ``GenerationResult.cfg_reference_provenance`` once the server
    relays it (bd-dre-lknrx: Express persists the ``cfg_references_resolved``
    SSE event as ``cfg_reference_provenance`` on the job record). Every field
    mirrors that event's shape: ``generator_model_id``, ``expected_dim``,
    ``paralogs[]``, ``rule_version``.

    ⛔ **ABSENCE IS NOT A NEGATIVE RESULT.** ``GenerationResult.cfg_reference_provenance``
    is ``None`` exactly when THIS server build did not report provenance at
    all — that is "not reported", never "no conditioning happened". A run can
    be genuinely CFG-conditioned (a v6.9-line model always is, whether the
    panel was named or platform-derived) on a server that predates the relay,
    and the two facts must never render the same way. Once this object
    exists, :attr:`caller_requested` distinguishes a caller-NAMED panel
    (``avoid_paralogs=`` / ``contrastive=``) from one the platform derived on
    its own — both are real conditioning, and neither implies the other was
    more selective (see the measured null-band result at bd-dre-atcmd: on
    BRD4-ET, named and platform-derived panels performed indistinguishably).
    """

    generator_model_id: str | None = None
    expected_dim: int | None = None
    paralogs: tuple[ParalogReferenceEntry, ...] = ()
    rule_version: str | None = None
    #: The genes the REQUEST named, when it named any. ``None`` means either the
    #: platform chose the panel or this server build did not report the field —
    #: :attr:`caller_requested` is what tells those two apart.
    requested_reference_genes: tuple[str, ...] | None = None
    #: True  -> this request named the panel (``avoid_paralogs=``/``contrastive=``).
    #: False -> the platform derived the panel itself.
    #: None  -> this server build did not say which, and it was not derivable
    #:          from the echoed request parameters either.
    caller_requested: bool | None = None

    @classmethod
    def from_wire(
        cls,
        node: Any,
        *,
        caller_requested_hint: bool | None = None,
    ) -> CfgReferenceProvenance | None:
        """Parse a persisted/relayed provenance payload, or None if absent.

        ``node`` is whatever the response carries at ``cfg_reference_provenance``
        / ``cfgReferenceProvenance`` — every field is probed under both
        spellings, because the persisted job record and the live SSE event are
        not guaranteed to agree on casing.

        Returns ``None`` — not an empty object — when ``node`` itself is
        absent, so "not reported" and "reported, structurally empty" stay
        distinguishable to the caller. ``caller_requested_hint`` is used only
        when the payload itself carries no explicit flag; pass whatever the
        request body is independently known to have named (e.g. whether
        ``cfg_reference_genes`` was echoed back in the job's parameters).
        """
        if not isinstance(node, dict) or not node:
            return None

        def pick(*keys: str) -> Any:
            for k in keys:
                v = node.get(k)
                if v is not None:
                    return v
            return None

        raw_paralogs = pick("paralogs") or []
        paralogs: list[ParalogReferenceEntry] = []
        if isinstance(raw_paralogs, list):
            for p in raw_paralogs:
                if not isinstance(p, dict):
                    continue
                gene = p.get("paralog_gene") or p.get("paralogGene")
                if not gene:
                    continue
                n_rows_raw = (
                    p["n_rows"] if p.get("n_rows") is not None else p.get("nRows")
                )
                identity_raw = (
                    p["identity_to_target_domain"]
                    if p.get("identity_to_target_domain") is not None
                    else p.get("identityToTargetDomain")
                )
                paralogs.append(
                    ParalogReferenceEntry(
                        paralog_gene=str(gene),
                        source_pdb_id=p.get("source_pdb_id") or p.get("sourcePdbId"),
                        source_chain=p.get("source_chain") or p.get("sourceChain"),
                        identity_to_target_domain=(
                            float(identity_raw) if identity_raw is not None else None
                        ),
                        n_rows=int(n_rows_raw) if n_rows_raw is not None else None,
                    )
                )
        # ⛔ THE DISCRIMINATOR IS `requested_reference_genes`, NOT `caller_requested`.
        # The server has never emitted a `caller_requested` flag. What the
        # `cfg_references_resolved` event actually carries is
        # `requested_reference_genes` (bd-dre-siz9d.1): the caller's gene list when
        # the panel was named, and an explicit null when the platform derived its
        # own. Reading only `caller_requested` left this attribute None on BOTH
        # branches, so a caller-named panel and a platform-derived one were
        # indistinguishable — which is exactly the collapse the three-state rule
        # exists to prevent, arriving through the accessor built to prevent it.
        #
        # ⛔ ABSENT KEY vs EXPLICIT NULL. A server build predating the field omits
        # the key entirely and the honest answer is None ("this build did not say").
        # A build that HAS the field and sets it to null is stating a fact: the
        # platform chose the panel. `in` is load-bearing here; `.get()` collapses
        # the two back together.
        requested_genes: tuple[str, ...] | None = None
        has_requested_key = False
        for key in ("requested_reference_genes", "requestedReferenceGenes"):
            if key in node:
                has_requested_key = True
                raw = node[key]
                if isinstance(raw, (list, tuple)):
                    named = tuple(str(g) for g in raw if g)
                    if named:
                        requested_genes = named
                break

        caller_requested = pick("caller_requested", "callerRequested")
        if caller_requested is None:
            if requested_genes is not None:
                caller_requested = True
            elif has_requested_key:
                # Present and null/empty — the platform picked the panel.
                caller_requested = False
            else:
                caller_requested = caller_requested_hint

        expected_dim = pick("expected_dim", "expectedDim")
        return cls(
            generator_model_id=pick("generator_model_id", "generatorModelId"),
            expected_dim=int(expected_dim) if expected_dim is not None else None,
            paralogs=tuple(paralogs),
            rule_version=pick("rule_version", "ruleVersion"),
            requested_reference_genes=requested_genes,
            caller_requested=(
                bool(caller_requested) if caller_requested is not None else None
            ),
        )
