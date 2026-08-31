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
    "CfgReference",
    "ContrastiveGuidance",
    "ContrastiveReference",
    "AvoidancePolicy",
    "PocketFeatureMatrix",
    "V6_9_POCKET_FEATURE_DIM",
    "V6_5_POCKET_FEATURE_DIM",
]


#: Pocket-feature width declared by the v6.9 generator line (v6.9.21 / v6.9.22).
#: 57 geometric + 50 all-atom columns.
V6_9_POCKET_FEATURE_DIM = 107

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
    "v6.9.22": V6_9_POCKET_FEATURE_DIM,
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

    def require_forwardable(self, *, allow_unforwarded: bool = False) -> None:
        """Refuse to send references down a request path that discards them.

        ⛔ VERIFIED 2026-08-31 by reading the request path end to end.

        The generation request model DOES accept ``reference_pocket_features``
        (both spellings) and validates every matrix against the selected
        model's declared width. What it never sees is a request that arrived
        through the public generation endpoint: the intermediate hop builds its
        forward payload from a fixed key list, and that list does not contain
        the references. Neither hop rejects unknown keys, so the request
        succeeds, the run proceeds, and nothing anywhere reports that the panel
        was ignored.

        What DOES happen on that path is that the platform derives its OWN
        reference panel for a model that needs one, and refuses the run outright
        if it cannot. So a v6.9-line run through the public endpoint is
        genuinely paralog-contrastive — you just do not choose the panel and are
        not told which paralogs were used. That is fine for a first pass and
        fatal for a recursive loop, whose whole point is feeding chosen
        off-targets (and last round's survivors) back in as the references.

        Pass ``allow_unforwarded=True`` only when you have a server where the
        passthrough exists (a local/staging build, or after the plumbing lands).
        """
        if allow_unforwarded:
            return
        raise LigandAIParameterNotForwarded(
            "contrastive references cannot reach the generator through the public "
            "generation endpoint yet. The request model accepts and validates "
            "reference_pocket_features, but the hop in front of it forwards a fixed "
            "key list that omits them — so the request would succeed, the run would "
            "proceed WITHOUT your panel, and no response field would say so. "
            "Refusing to send a knob that reads as set and executes as unset.\n"
            "\n"
            "What still works today: a v6.9-line model is contrastive regardless — "
            "the platform derives its own real paralog references and refuses the run "
            "if it cannot produce them. You just cannot choose the panel, and the "
            "chosen panel is not reported back.\n"
            "\n"
            "To run anyway against a server where the passthrough exists, pass "
            "allow_unforwarded=True.",
            parameter="contrastive",
            reached="generation request model (validated, width-checked)",
            dropped_at="public generation endpoint forward payload (fixed key list)",
            tracking="bd-dre-if562",
            override="allow_unforwarded=True",
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
