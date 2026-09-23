# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Affinity / selectivity maturation — ``client.maturation``.  [bd-dre-rp11j.9.1]

⛔ TWO DIFFERENT THINGS ON THIS PLATFORM ARE CALLED "CAMPAIGN". READ THIS FIRST.

===============================================================================
``client.maturation``  (THIS MODULE)          ``client.campaigns``
-------------------------------------------------------------------------------
A SERVER-SIDE loop. You hand it              A LOCAL panel-folding helper. It
binder sequences you ALREADY have;           composes fold/score primitives in
the platform folds them, gates on            YOUR process to answer "does this
peptide-interface ipSAE, scores the          design bind X and not its paralogs,
in-domain poses, mutates the                 and does it hold at an independent
binder-side interface, re-folds the          draw". Its five public methods are
variants AND re-folds the parents as         ``pocket_features``,
same-cycle controls, and keeps what          ``contrastive_references``,
beat its own parent. Then repeats.           ``fold_panel``, ``replicate``,
                                             ``recursive``. It never calls
``POST /api/campaign/mature``                ``/api/campaign/*``.
===============================================================================

If you want to IMPROVE sequences you already have, you want this module. If you
want to draw NEW sequences and select among them in your own process, you want
``client.campaigns``. The two share a word and nothing else — ``client.campaigns``
has no ``mature``, and reaching for one says so by name rather than raising a bare
``AttributeError`` that reads like a typo.

PUBLIC AVAILABILITY
-------------------
⛔ As of this release the ``/api/campaign/*`` routes answer on the platform's
internal FastAPI port but are **not yet proxied through ligandai.com** — a call
against the public base URL returns 404, not 401. The Express proxy (quote →
reserve → grant → meter → settle, so the GPU spend is metered rather than
fail-open) is in flight under ``bd-dre-xfs48`` / ``bd-LIGANDAI_ALPHA_V2-6qfgh``.
This client is built against the frozen server contract and is exercised against
the internal port; it starts working publicly the moment that proxy lands, with
no change here. :meth:`Maturation.start` already accepts the ``quote=`` the proxy
will require (see :class:`~ligandai.resources.objectives.CampaignQuote`), and
omits it entirely when you do not pass one, so the same call works against both.

THE LOOP, PER CYCLE
-------------------
1. Fold every current parent and every proposed variant against the target (and
   every panel receptor, in ``selectivity`` mode).
2. Gate on **peptide-interface ipSAE** ≥ ``ipsae_gate`` (default 0.67), with a
   floor of ``min_poses_in_domain`` (≥ 2) poses clearing it.
3. Score the in-domain poses with DeltaForge v10.2 and take the **median** over
   them — never the best of N.
4. Mutate the binder-side interface residues (MutagenesisEngine, point moves).
5. Re-fold the variants **and the parents, in the same cycle, under the same
   settings**, and compare each variant only against its own same-cycle parent.

⛔ MEASUREMENT RULES THIS MODULE ENFORCES — each one is a number that would
otherwise be quotable and wrong.

``PEPTIDE_IPSAE_IS_THE_BINDER_METRIC``
    The gate is the A–B peptide–receptor interface ipSAE, never the
    whole-complex ``ipsae`` (a well-folded receptor drags that upward whether or
    not the peptide is engaged) and never a pre-fold PREDICTED ipSAE column
    (which emits values above 1.0 and is therefore not even on the same scale).
    Every ipSAE this module accepts is range-checked: a value above
    :data:`MAX_VALID_IPSAE` is refused as a plumbing error, not clamped.

``A_GATED_NUMBER_IS_NOT_A_BAD_NUMBER``
    Six reasons withhold a Kd and **exactly one of them is a negative result
    about the design** (:attr:`GateReason.IPSAE_BELOW_GATE`). The other five say
    the measurement is absent or the instrument was wrong. Read
    :attr:`TargetReadout.is_negative_result`, never ``gated``, when counting
    designs that did badly.

``THE_PARENT_CONTROL_IS_SAME_CYCLE_OR_IT_IS_NOTHING``
    A parent's Kd from an EARLIER round, measured on a different pose set, is not
    a control — comparing against it compares fold conditions, not refinement.
    The server re-folds every parent in the same cycle as its variants and the
    cycle record is built from PAIRS, so a cycle with variants and no same-round
    parent baseline is not constructible. :attr:`ParentDelta.control_frame` says
    which frame you got, and ``fold_improvement``/``is_win`` are ``None`` — never
    ``False`` — when it is ``absent``.

``AN_UNASSESSED_ROW_IS_NOT_A_LOSS_AND_MUST_NOT_DILUTE_A_RATE``
    :attr:`CycleRecord.comparisons` counts only pairs that were actually
    ASSESSED. Pairs that had a valid control but whose variant was never measured
    live in :attr:`CycleRecord.pairs_not_comparable`. Putting them in the
    denominator understates the win rate exactly as counting them losses would,
    and it does so in the direction that reads as caution, which is why it
    survives review.

``SELECTIVITY_IS_MSA_MATCHED_AND_COMPLETE_OR_IT_IS_NONE``
    A cross-isoform margin is only comparable in the MSA-matched hotspot frame
    (raw per-isoform hotspot lists are unequal in size and the margin is an
    unnormalised count), and only over a COMPLETE panel. Outside those two
    conditions :attr:`SelectivityReadout.selectivity_margin` is ``None`` — never
    0, never a partial number.

``CONTACT_COUNTS_ARE_REPORTED_NEVER_RANKED``
    ``n_contacts``, ``hot_hit`` and the contact ``margin`` are carried on every
    readout and are never a gate, a ranking, a promotion rule or a stop
    condition. Contact margin fails its own composition-matched scramble control,
    and across six live paired variants median ``hot_hit`` FELL while Kd
    IMPROVED. ``sort="margin"`` is refused by this client for that reason.

``THE_Kd_ORDERING_IS_A_TRIAGE_PRIOR``
    ⛔ The deployed Kd scorer has **no measured-affinity evaluation on any row**,
    so "design A's Kd < design B's Kd, same target" is a reported number and not
    a validated affinity claim. Every Kd-sorted page carries
    :class:`AffinityRankingCaveat`, which cannot be detached from it. This module
    quotes NO correlation coefficient for predicted Kd against measured affinity,
    and REFUSES to pass one through if a server sends it — see
    :attr:`AffinityRankingCaveat.dropped_statistics` and
    ``bd-LIGANDAI_ALPHA_V2-jb73b.6``, which withdrew the pair that used to be
    quoted here. The cross-target RATIO (one binder, two receptors, one scorer)
    is a legitimate and different use, and is what ``selectivity_fold`` is.

``NO_do_relax``
    There is no ``do_relax`` knob on this path and this module does not expose or
    forward one. The maturation loop scores the pose AS FOLDED through
    ``score_deltaforge_v102`` (whose own aggregation knob is
    ``aggregate_method``, defaulting to ``boltzmann_parallel``). ``do_relax``
    belongs to ``compute_deltaforge_energy``, the non-production V9 path with a
    length-bin discontinuity at 40 aa — which must not appear anywhere in a
    maturation number's provenance. :attr:`ScorerProvenance.uses_forbidden_scorer`
    checks that for you.

ENDPOINT MAP
------------
- :meth:`Maturation.start`       → ``POST /api/campaign/mature``
- :meth:`Maturation.status`      → ``GET  /api/campaign/{id}/status``
- :meth:`Maturation.readout`     → ``GET  /api/campaign/{id}/maturation``
- :meth:`Maturation.leaderboard` → ``GET  /api/campaign/{id}/leaderboard``
- :meth:`Maturation.structure`   → ``GET  /api/campaign/{id}/designs/{design_id}/structure``
- :meth:`Maturation.stop`        → ``POST /api/campaign/{id}/stop``
- :meth:`Maturation.stream`      → ``GET  /api/campaign/{id}/stream``  (SSE)
- :meth:`Maturation.list`        → ``GET  /api/campaign``

Every one of these is mirrored on :class:`AsyncMaturation`.

Refs: bd-dre-rp11j.9, bd-dre-rp11j.9.1, bd-dre-vd1fk.4, bd-dre-xfs48.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ligandai.errors import (
    LigandAICreditError,
    LigandAIError,
    LigandAIValidationError,
)
from ligandai.resources._base import AsyncResource, Resource
from ligandai.resources.objectives import (
    _QUOTE_STALE_CODES,
    CampaignQuote,
    _quote_dict,
    _quote_token,
)

__all__ = [
    # constants
    "IPSAE_GATE",
    "MAX_VALID_IPSAE",
    "MIN_POSES_IN_DOMAIN_FLOOR",
    "UNCALIBRATED_LENGTH_FLOOR",
    "REQUIRED_CALIBRATION_HEAD",
    "FORBIDDEN_SCORER",
    "SCORER_ENTRYPOINT",
    "MATURATION_MIN_CREDITS",
    # enums
    "ObjectiveMode",
    "GateReason",
    "IpsaeSource",
    "HotspotFrame",
    "PocketConditioning",
    "ControlFrame",
    "LeaderboardSort",
    # request types
    "Seed",
    "Receptor",
    "Objective",
    "Filters",
    "Screening",
    "MaturationSettings",
    "MaturationSpec",
    # response types
    "ScorerProvenance",
    "TargetReadout",
    "ParentDelta",
    "SelectivityReadout",
    "AffinityRankingCaveat",
    "MaturedDesign",
    "CycleRecord",
    "MaturationReadout",
    "MaturationLeaderboard",
    "MaturationStatus",
    "MaturationStarted",
    "MaturationEvent",
    "MaturationRun",
    # resources
    "Maturation",
    "AsyncMaturation",
]


# ─── Constants ────────────────────────────────────────────────────────────────

#: Platform default peptide-interface ipSAE gate. Below this DeltaForge is
#: scoring an interface that is not resolved, so the platform returns no Kd at
#: all — that is an INDETERMINATE leg, never a weak one.
IPSAE_GATE: float = 0.67

#: ⛔ ipSAE is a probability-like quantity bounded at 1.0. A value above this is
#: not a very good binder: it is the PRE-FOLD PREDICTED column plumbed into a
#: measured field, and it is refused rather than clamped. Clamping would turn a
#: wiring bug into a plausible number.
MAX_VALID_IPSAE: float = 1.0

#: ⛔ A median over ONE pose is a best-of-1. The server refuses ``1`` with a coded
#: 422 (``MIN_POSES_TOO_LOW``) and this client refuses it locally first.
MIN_POSES_IN_DOMAIN_FLOOR: int = 2

#: Peptides at or above this length fall in the DeltaForge 40–80 aa bin, whose
#: entropy term is deleted. ipSAE stays valid at every length; the ENERGY does
#: not, and is withheld with ``LENGTH_BIN_UNCALIBRATED``.
UNCALIBRATED_LENGTH_FLOOR: int = 40

#: The calibration head a Kd must have come through to be quotable: it asserts
#: that Boltz-2 structures reached the Boltz-2-calibrated head. ⛔ This is a
#: CAPABILITY claim, not a version string, which is why it is pinned strictly
#: while the scorer NAME below deliberately is not.
REQUIRED_CALIBRATION_HEAD: str = "v10_boltz2_autodetected"

#: ⛔ Must never appear in a maturation number's provenance. On identical
#: structures it found 0 single-digit-nM designs where the production scorer
#: found 32; its V9 bin coefficients carry a 6.44 kcal/mol discontinuity at chain
#: length 40 and a wrong-signed entropy term below it. It is also the only place
#: ``do_relax`` exists — and there it returns ``min(native, relaxed)``, a
#: best-of-2 selection estimator, not a relaxed energy.
FORBIDDEN_SCORER: str = "compute_deltaforge_energy"

#: The production scoring entrypoint the maturation loop calls.
SCORER_ENTRYPOINT: str = "score_deltaforge_v102"

#: The server's credit floor for admitting a maturation campaign.
MATURATION_MIN_CREDITS: int = 2000

_CANONICAL_AA = frozenset("ACDEFGHIKLMNPQRSTVWY")
_CAMPAIGN_ID_RE = re.compile(r"^camp_[A-Za-z0-9]{26}$")

#: ⛔ Statistics this client REFUSES to pass through from a server payload.
#: ``bd-LIGANDAI_ALPHA_V2-jb73b.6``: the within-target Spearman pair once carried
#: here was withdrawn — the model half has zero joinable rows behind it (no
#: measured-affinity evaluation exists for the deployed scorer at all), and the
#: length half was a grouping artifact of bucketing NULL genes as a target. A
#: deployment that has not yet dropped them must not hand a customer a number the
#: platform has retracted. What survives is the ENGINEERING FACT — the ordering is
#: not validated — which needs no coefficient to state.
_WITHDRAWN_CAVEAT_KEYS = ("within_target_spearman", "length_baseline_spearman")

#: ⛔ Phrases that assert the WITHDRAWN claim itself, not merely its coefficient.
#: "binder length out-ranked the model" IS the retracted finding: the length half
#: of that pair came from bucketing rows with a NULL gene as though they were a
#: target, and over the buckets that actually name a protein the effect is
#: absent. A server note carrying this sentence is passed over, not passed on.
_WITHDRAWN_NOTE_MARKERS = ("out-ranked the model", "outranked the model",
                           "length out-ranked", "length outranked")

_UNVALIDATED_RANKING_NOTE = (
    "Predicted-Kd order within one target is a TRIAGE PRIOR, not a validated "
    "affinity ranking: the deployed Kd scorer has no measured-affinity "
    "evaluation on any row. Use it to choose what to fold again, not to say "
    "which design binds tightest. A CROSS-target ratio (one binder, two "
    "receptors, one scorer) is a different and legitimate use."
)


# ─── Enums ────────────────────────────────────────────────────────────────────
#
# ⛔ THE ASYMMETRY RULE, and it is deliberate on both sides.
#
# REQUEST enums stay STRICT and name their own valid set: we control what we
# send, so a typo must fail locally rather than be forwarded as "unrecognized".
#
# RESPONSE enums are PERMISSIVE: an unknown value maps to ``UNRECOGNIZED``,
# preserves the server's raw string, and degrades in the SAFE direction. A pinned
# client is always older than the API it talks to; hard-failing a whole run on
# one unknown label trades a labelling gap for total blindness, including on the
# rows that are perfectly quotable. The invariant that survives both: an
# unrecognised value NEVER becomes a quotable number.


class ObjectiveMode(str, Enum):
    """What the loop steers toward.  ⛔ REQUEST enum — strict."""

    #: Maximise on-target affinity.
    AFFINITY = "affinity"
    #: Maximise on-target minus the WORST off-target. Requires a panel AND the
    #: MSA-matched hotspot frame.
    SELECTIVITY = "selectivity"
    #: Affinity SUBJECT TO ``avoid_hit`` not increasing versus the same-cycle
    #: parent. ⛔ It does NOT minimise ``avoid_hit`` and is not scored as if it
    #: did: avoidance is a constraint to hold, and a seed that already violates
    #: it is admissible — refinement neither fixes nor breaks it.
    AFFINITY_AVOIDING = "affinity_avoiding"


class GateReason(str, Enum):
    """Why a Kd was WITHHELD.  ⛔ RESPONSE enum — permissive.

    ⛔ EXACTLY ONE member is a negative result about the design:
    :attr:`IPSAE_BELOW_GATE`. Every other member is a statement about the
    INSTRUMENT, not the binder. Counting an unread instrument as a failed design
    is the failure this enum exists to prevent — use
    :attr:`is_negative_result`, never ``gated``.
    """

    #: The fold returned nothing at all. Nothing else can be true of it.
    NO_POSES = "NO_POSES"
    #: Poses exist but NONE yielded a binder-restricted ipSAE. ⛔ The instrument
    #: DID NOT READ. Emphatically not :attr:`IPSAE_BELOW_GATE`.
    IPSAE_UNAVAILABLE = "IPSAE_UNAVAILABLE"
    #: The instrument READ and every pose fell under the gate. ⛔ The only member
    #: that is evidence against the design.
    IPSAE_BELOW_GATE = "IPSAE_BELOW_GATE"
    #: Some poses cleared the gate, just fewer than ``min_poses_in_domain``.
    INSUFFICIENT_POSES = "INSUFFICIENT_POSES"
    #: The energy did not come through the required calibration head, or arrived
    #: with no provenance at all.
    SCORER_HEAD_MISMATCH = "SCORER_HEAD_MISMATCH"
    #: Peptide ≥ 40 aa: that bin's entropy term is deleted.
    LENGTH_BIN_UNCALIBRATED = "LENGTH_BIN_UNCALIBRATED"
    #: ⛔ A reason this SDK version does not know. Still WITHHOLDS. The server's
    #: exact string survives on :attr:`TargetReadout.gate_reason_raw`.
    UNRECOGNIZED = "UNRECOGNIZED"

    @classmethod
    def _missing_(cls, value: object) -> GateReason:
        return cls.UNRECOGNIZED

    @property
    def is_negative_result(self) -> bool:
        """True only for :attr:`IPSAE_BELOW_GATE` — assessed, and it failed."""
        return self is GateReason.IPSAE_BELOW_GATE

    @property
    def is_absent_measurement(self) -> bool:
        """True when the number is MISSING rather than bad."""
        return not self.is_negative_result


_KNOWN_GATE_REASONS = frozenset(m.value for m in GateReason)


class IpsaeSource(str, Enum):
    """Which ipSAE the server put in ``ipsae_median``.  ⛔ RESPONSE — permissive.

    Only :attr:`BINDER_RESTRICTED` is an interface metric for a peptide binder.
    :attr:`OVERALL_FALLBACK` mirrors the WHOLE-COMPLEX value into the peptide
    field. Anything unrecognised is treated as not-binder-restricted for the same
    reason: an unknown provenance is not a verified one.

    ⛔ The engine does not currently EMIT this field at all — it enforces the
    check internally and a non-binder-restricted pose never reaches the median.
    So ABSENT is admissible here and does not gate; a DECLARED non-binder-
    restricted (or unknown) value is not trusted. When the engine starts emitting
    it, absent becomes a real signal and this can be tightened.
    """

    BINDER_RESTRICTED = "binder_restricted"
    OVERALL_FALLBACK = "overall_fallback"
    UNRECOGNIZED = "unrecognized"

    @classmethod
    def _missing_(cls, value: object) -> IpsaeSource:
        return cls.UNRECOGNIZED


class HotspotFrame(str, Enum):
    """The frame cross-isoform margins were computed in.  Permissive on response.

    :attr:`MSA_MATCHED` projects every receptor's hotspots through the panel MSA
    and keeps only alignment COLUMNS where every member has one. :attr:`RAW` uses
    per-isoform lists that are unequal in size, against a margin that is an
    unnormalised COUNT — the artifact was roughly twice any real effect. A
    selectivity number is never quoted in :attr:`RAW`, and an unrecognised frame
    is never equal to :attr:`MSA_MATCHED`.
    """

    MSA_MATCHED = "msa_matched"
    RAW = "raw"
    UNRECOGNIZED = "unrecognized"

    @classmethod
    def _missing_(cls, value: object) -> HotspotFrame:
        return cls.UNRECOGNIZED


class PocketConditioning(str, Enum):
    """What to name at fold time.  ⛔ REQUEST enum — strict.

    Measured paired, identical peptide set, equal length, equal ipSAE, on-target
    native Kd at ipSAE ≥ 0.67::

        unconditioned               6235 nM  (n=10)
        FULL curated hotspot set   11086 nM  (n=11)
        VARIABLE-only               2530 nM  (n=10)   3.6x tighter, p=0.019

    ⛔ Naming the FULL set is WORSE than naming nothing, which is why
    :attr:`VARIABLE_ONLY` is the default rather than the thorough-sounding one.
    """

    VARIABLE_ONLY = "variable_only"
    FULL_HOTSPOTS = "full_hotspots"
    NONE = "none"


class ControlFrame(str, Enum):
    """Whether a parent comparison had a same-cycle control.  Permissive.

    :attr:`SAME_CYCLE` means the parent was re-folded in THIS cycle under the
    same settings. :attr:`ABSENT` means it was not, and every paired number is
    ``None`` — a Kd from a previous cycle on a different pose set is not a
    control. An unrecognised value is never :attr:`SAME_CYCLE`.
    """

    SAME_CYCLE = "same_cycle"
    ABSENT = "absent"
    UNRECOGNIZED = "unrecognized"

    @classmethod
    def _missing_(cls, value: object) -> ControlFrame:
        return cls.UNRECOGNIZED


class LeaderboardSort(str, Enum):
    """Leaderboard ordering.  ⛔ REQUEST enum — strict.

    ⛔ :attr:`MARGIN` is accepted by the server but REFUSED by this client on a
    maturation leaderboard: contact margin is reported, never ranked on.
    """

    KD = "kd"
    SELECTIVITY = "selectivity"
    CYCLE = "cycle"
    MARGIN = "margin"


# ─── Small helpers ────────────────────────────────────────────────────────────


def _as_dict(payload: Any, *, what: str) -> dict[str, Any]:
    if isinstance(payload, Mapping):
        return dict(payload)
    raise LigandAIValidationError(
        f"expected a JSON object for {what}, got {type(payload).__name__}"
    )


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _i(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _checked_ipsae(value: Any, *, where: str) -> float | None:
    """Accept an ipSAE, or refuse it loudly if it cannot be one.

    ⛔ A value above 1.0 is the PRE-FOLD PREDICTED column in a measured field. It
    is refused, not clamped: clamping converts a wiring bug into a plausible
    number, and every downstream gate would then pass on a quantity nobody
    measured.
    """
    out = _f(value)
    if out is None:
        return None
    if out > MAX_VALID_IPSAE:
        raise LigandAIValidationError(
            f"{where}: ipSAE {out!r} is above {MAX_VALID_IPSAE}. ipSAE is bounded "
            "at 1.0, so this is the pre-fold PREDICTED column plumbed into a "
            "measured field, not an exceptionally good interface. Refusing it "
            "rather than clamping — a clamped value would pass every gate below."
        )
    return out


def _require_campaign_id(campaign_id: str) -> str:
    if not isinstance(campaign_id, str) or not _CAMPAIGN_ID_RE.match(campaign_id):
        raise LigandAIValidationError(
            f"campaign_id must look like camp_<26 alphanumerics>; got "
            f"{campaign_id!r}. The server returns 404 for a malformed id, which "
            "is indistinguishable from 'not yours' — so this fails locally first."
        )
    return campaign_id


def _sse_payload(line: str) -> tuple[str | None, dict[str, Any] | None]:
    """Parse one SSE line into ``(event_name, data)``; ``(None, None)`` if neither."""
    if line.startswith("event:"):
        return line[len("event:"):].strip(), None
    if line.startswith("data:"):
        body = line[len("data:"):].strip()
        try:
            parsed = json.loads(body)
        except (TypeError, ValueError):
            return None, {"raw": body}
        return None, parsed if isinstance(parsed, dict) else {"value": parsed}
    return None, None


# ─── Request types ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Seed:
    """One binder the customer ALREADY has, entering the loop as a lineage root.

    ``seed_id`` is the LINEAGE ANCHOR: every descendant carries it as
    ``root_seed_id`` at every cycle depth, which is what makes
    :attr:`TargetReadout.avoid_hit_inherited_from_seed` answerable without
    walking N parents. Name your seeds if you intend to follow one of your own
    leads through the cycles; the server assigns ``seed_000``-style ids otherwise.
    """

    sequence: str
    name: str | None = None
    seed_id: str | None = None

    def __post_init__(self) -> None:
        seq = (self.sequence or "").strip().upper()
        if len(seq) < 4:
            raise LigandAIValidationError(
                f"seed sequence must be at least 4 residues; got {self.sequence!r}"
            )
        bad = sorted({c for c in seq if c not in _CANONICAL_AA})
        if bad:
            # ⛔ Local mirror of the server's SEED_NON_CANONICAL. Downstream Boltz
            # folding takes CANONICAL sequences, so a UAA-bearing seed cannot be
            # folded or scored — it would silently drop out of the run while still
            # being counted as a seed you submitted.
            raise LigandAIValidationError(
                f"seed sequence contains non-canonical residue(s) {bad}. Only "
                "ACDEFGHIKLMNPQRSTVWY is accepted: Boltz folding takes canonical "
                "sequences, so a UAA variant could not be folded or scored and "
                "would drop out of the run without failing it. "
                "(server code: SEED_NON_CANONICAL)"
            )
        object.__setattr__(self, "sequence", seq)

    def to_wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {"sequence": self.sequence}
        if self.name:
            out["name"] = self.name
        if self.seed_id:
            out["seed_id"] = self.seed_id
        return out


@dataclass(frozen=True)
class Receptor:
    """A receptor in its OWN residue frame — the target, or one panel member.

    ⛔ ``pocket_residues`` and ``avoid_residues`` are resnums in THIS receptor's
    numbering and are NEVER cross-mapped onto another receptor. A panel entry
    without its own pocket is a 422 (``PANEL_FRAME_MISSING``), not an inheritance
    from the target: folding a paralog against the on-target's resnums answers a
    question about the wrong residues and reads as a clean, wrong result.
    """

    gene: str
    receptor_sequence: str
    pocket_residues: Sequence[int]
    avoid_residues: Sequence[int] = ()
    receptor_name: str | None = None
    receptor_chain: str = "A"
    msa_path: str | None = None
    structure_pdb: str | None = None

    def __post_init__(self) -> None:
        if not (self.gene or "").strip():
            raise LigandAIValidationError("receptor.gene is required")
        if not (self.receptor_sequence or "").strip():
            raise LigandAIValidationError(
                f"receptor {self.gene!r} needs receptor_sequence — the platform "
                "resolves which receptor a pose belongs to from the chain-A "
                "SEQUENCE, never from a filename (fold stems collide across arms, "
                "and trusting one silently attributes a pose to the wrong receptor)"
            )
        if not list(self.pocket_residues):
            raise LigandAIValidationError(
                f"receptor {self.gene!r} needs pocket_residues in its OWN residue "
                "frame (server code: PANEL_FRAME_MISSING)"
            )

    def to_wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "gene": self.gene,
            "receptor_chain": self.receptor_chain,
            "receptor_sequence": self.receptor_sequence,
            "pocket_residues": [int(r) for r in self.pocket_residues],
            "avoid_residues": [int(r) for r in self.avoid_residues],
        }
        if self.receptor_name:
            out["receptor_name"] = self.receptor_name
        if self.msa_path:
            out["msa_path"] = self.msa_path
        if self.structure_pdb:
            out["structure_pdb"] = self.structure_pdb
        return out


@dataclass(frozen=True)
class Objective:
    """The numeric stop condition. Only ``target_kd_nM`` is required.

    ⛔ There is deliberately no contact-margin leg here. The server's objective
    check reads Kd, ``selectivity_margin`` and ``selectivity_fold`` and nothing
    else — contact margin and hotspot-contact count are REPORTED and are never
    acceptance tests, because in the live paired round median ``hot_hit`` fell
    while Kd improved. An acceptance test on contact count selects against the
    thing being optimised.
    """

    target_kd_nM: float
    min_selectivity_margin: float | None = None
    min_selectivity_fold: float | None = None
    n_to_satisfy: int = 1
    max_cycles: int = 12
    max_credits: int = 50000

    def __post_init__(self) -> None:
        if not (_f(self.target_kd_nM) or 0) > 0:
            raise LigandAIValidationError("objective.target_kd_nM must be > 0")

    def to_wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "target_kd_nM": float(self.target_kd_nM),
            "n_to_satisfy": int(self.n_to_satisfy),
            "max_cycles": int(self.max_cycles),
            "max_credits": int(self.max_credits),
        }
        if self.min_selectivity_margin is not None:
            out["min_selectivity_margin"] = float(self.min_selectivity_margin)
        if self.min_selectivity_fold is not None:
            out["min_selectivity_fold"] = float(self.min_selectivity_fold)
        return out


@dataclass(frozen=True)
class Filters:
    """Composition filters applied to PROPOSED variants.

    ⛔ Charge DENSITY, not net charge: ``max_acidic_cluster >= min_acidic_cluster``
    is the discriminator. Two peptides both at net −4 differed by 1.6 in binding
    margin and only the acidic cluster separated them.
    """

    min_acidic_cluster: int = 3
    min_basic_cluster: int = 0
    require_net_negative: bool = True
    charge_window: int = 5
    stability_grades: Sequence[str] = ("A", "B")
    immunogenicity_grades: Sequence[str] = ("A", "B", "C")

    def to_wire(self) -> dict[str, Any]:
        return {
            "min_acidic_cluster": int(self.min_acidic_cluster),
            "min_basic_cluster": int(self.min_basic_cluster),
            "require_net_negative": bool(self.require_net_negative),
            "charge_window": int(self.charge_window),
            "stability_grades": list(self.stability_grades),
            "immunogenicity_grades": list(self.immunogenicity_grades),
        }


@dataclass(frozen=True)
class Screening:
    """Fold settings for each cycle.

    ⛔ THERE IS NO ``do_relax`` FIELD HERE, ON PURPOSE. The maturation loop scores
    the pose AS FOLDED via ``score_deltaforge_v102``, which has no such parameter
    (its aggregation knob is ``aggregate_method``, default
    ``boltzmann_parallel``). ``do_relax`` belongs to ``compute_deltaforge_energy``
    — the non-production V9 path — where it does not even return a relaxed energy:
    it returns ``min(native, relaxed)``, a best-of-2 selection estimator with a
    downward bias. Exposing the flag here would advertise a quality knob that is
    inert at best and certifies a biased estimator at worst.
    """

    diffusion_samples: int = 4
    screen_batch_size: int = 120
    promote_top_n: int = 12
    negative_control: bool = True
    contact_cutoff_angstrom: float = 5.0
    fold_timeout_s: int = 5400

    def to_wire(self) -> dict[str, Any]:
        return {
            "screen_batch_size": int(self.screen_batch_size),
            "diffusion_samples": int(self.diffusion_samples),
            "promote_top_n": int(self.promote_top_n),
            "negative_control": bool(self.negative_control),
            "contact_cutoff_angstrom": float(self.contact_cutoff_angstrom),
            "fold_timeout_s": int(self.fold_timeout_s),
        }


@dataclass(frozen=True)
class MaturationSettings:
    """How the loop runs. Six server-side settings are FROZEN and unreachable here.

    The frozen values are measurements, not preferences, and this class does not
    expose a way to set them — they are emitted at their frozen values so the
    payload states them rather than relying on a server default:

    ``engine="mutagenesis"``
        ⛔ ``seed_optimizer`` is rejected. Built without a ``generate_fn`` it
        degrades to a uniform random amino-acid draw at every masked position
        WHILE REPORTING a positive improvement, and our generator exposes no
        masked-infill path to supply one.
    ``pose_statistic="median"``
        ⛔ ``best`` is rejected. A best-of-N over a handful of poses is an
        optimistic order statistic, and every revision of it moves the same way.
    ``refold_parent_controls=True``
        ⛔ Cannot be disabled. It is the only reason a variant-vs-parent delta
        means anything.
    ``include_uaa=False``
        ⛔ Boltz folds canonical sequences; a UAA variant would drop out silently.
    ``gated_on_contact_margin=False`` / ``gated_on_hot_hit=False``
        Stated in every payload so the reader does not have to assume it.

    ⛔ ``min_poses_in_domain`` has a hard floor of 2 — one pose is a best-of-1
    wearing a median's name — and ``ipsae_gate`` defaults to the platform gate.
    Lowering the gate does not buy more measurements; it buys Kd numbers computed
    on interfaces that were not resolved.
    """

    cycles: int = 3
    n_variants_per_seed: int = 12
    max_seeds_per_cycle: int = 6
    interface_cutoff_angstrom: float = 5.0
    ipsae_gate: float = IPSAE_GATE
    min_poses_in_domain: int = MIN_POSES_IN_DOMAIN_FLOOR
    hotspot_frame: HotspotFrame | str = HotspotFrame.MSA_MATCHED
    pocket_conditioning: PocketConditioning | str = PocketConditioning.VARIABLE_ONLY
    include_non_conservative: bool = True

    def __post_init__(self) -> None:
        if int(self.min_poses_in_domain) < MIN_POSES_IN_DOMAIN_FLOOR:
            raise LigandAIValidationError(
                f"min_poses_in_domain must be >= {MIN_POSES_IN_DOMAIN_FLOOR}; got "
                f"{self.min_poses_in_domain!r}. A median over one pose is a "
                "best-of-1 — an optimistic order statistic wearing a median's "
                "name. (server code: MIN_POSES_TOO_LOW)"
            )
        gate = _f(self.ipsae_gate)
        if gate is None or not 0.0 <= gate <= 1.0:
            raise LigandAIValidationError(
                f"ipsae_gate must be between 0.0 and 1.0; got {self.ipsae_gate!r}"
            )
        # ⛔ REQUEST enums stay strict: a typo fails here rather than travelling.
        frame = self.hotspot_frame
        if not isinstance(frame, HotspotFrame):
            frame = HotspotFrame(str(frame))
        if frame is HotspotFrame.UNRECOGNIZED:
            raise LigandAIValidationError(
                f"hotspot_frame must be 'msa_matched' or 'raw'; got "
                f"{self.hotspot_frame!r}"
            )
        object.__setattr__(self, "hotspot_frame", frame)
        cond = self.pocket_conditioning
        if not isinstance(cond, PocketConditioning):
            cond = PocketConditioning(str(cond))
        object.__setattr__(self, "pocket_conditioning", cond)

    def to_wire(self) -> dict[str, Any]:
        return {
            # ── frozen, and emitted so the payload states them ──────────────
            "engine": "mutagenesis",
            "pose_statistic": "median",
            "refold_parent_controls": True,
            "include_uaa": False,
            # ── caller-settable ─────────────────────────────────────────────
            "cycles": int(self.cycles),
            "n_variants_per_seed": int(self.n_variants_per_seed),
            "max_seeds_per_cycle": int(self.max_seeds_per_cycle),
            "interface_cutoff_angstrom": float(self.interface_cutoff_angstrom),
            "ipsae_gate": float(self.ipsae_gate),
            "min_poses_in_domain": int(self.min_poses_in_domain),
            "hotspot_frame": HotspotFrame(self.hotspot_frame).value,
            "pocket_conditioning": PocketConditioning(self.pocket_conditioning).value,
            "include_non_conservative": bool(self.include_non_conservative),
        }


@dataclass(frozen=True)
class MaturationSpec:
    """A complete maturation request. Validated locally before it costs anything.

    Every local refusal below mirrors a coded server 422 and names the same code,
    so the message you get from a typo and the message you get from the server are
    the same message.
    """

    seeds: Sequence[Seed]
    target: Receptor
    objective: Objective
    panel: Sequence[Receptor] = ()
    objective_mode: ObjectiveMode | str = ObjectiveMode.AFFINITY
    filters: Filters = field(default_factory=Filters)
    screening: Screening = field(default_factory=Screening)
    maturation: MaturationSettings = field(default_factory=MaturationSettings)
    name: str | None = None
    resume_from: str | None = None

    def __post_init__(self) -> None:
        if not list(self.seeds):
            raise LigandAIValidationError(
                "seeds is required — maturation IMPROVES binders you already "
                "have. To draw new ones, that is a different capability."
            )
        mode = self.objective_mode
        if not isinstance(mode, ObjectiveMode):
            mode = ObjectiveMode(str(mode))
        object.__setattr__(self, "objective_mode", mode)

        # ⛔ A seed id is the lineage anchor; two roots sharing one make the
        # inherited-avoidance readout resolve to whichever was written last.
        # This checks ids AFTER the server's own defaults would be stamped, which
        # is the case a validator over explicit ids alone cannot see: an explicit
        # "seed_001" colliding with the generated default for the seed at index 1.
        seen: dict[str, int] = {}
        for idx, seed in enumerate(self.seeds):
            sid = seed.seed_id or "seed_%03d" % idx
            if sid in seen:
                raise LigandAIValidationError(
                    f"seed_id {sid!r} at position {idx} collides with the id at "
                    f"position {seen[sid]} once server-side defaults are stamped. "
                    "A seed id is the lineage anchor for every descendant; two "
                    "roots sharing one id make the inherited-avoidance readout "
                    "unanswerable. Name the seeds explicitly. "
                    "(server code: SEED_ID_DUPLICATE)"
                )
            seen[sid] = idx

        genes = {r.gene for r in self.panel}
        if self.target.gene in genes:
            raise LigandAIValidationError(
                f"panel contains the on-target gene {self.target.gene!r}. A "
                "receptor cannot be its own off-target comparator; the "
                "selectivity margin against yourself is zero by construction."
            )

        if mode is ObjectiveMode.SELECTIVITY:
            if not list(self.panel):
                raise LigandAIValidationError(
                    "objective_mode='selectivity' needs a panel: selectivity is "
                    "on-target minus the WORST off-target, and there is no worst "
                    "off-target over an empty set. "
                    "(server code: SELECTIVITY_PANEL_MISSING)"
                )
            if HotspotFrame(self.maturation.hotspot_frame) is not HotspotFrame.MSA_MATCHED:
                raise LigandAIValidationError(
                    "objective_mode='selectivity' requires "
                    "maturation.hotspot_frame='msa_matched'. Raw per-isoform "
                    "hotspot lists are unequal in size and the margin is an "
                    "unnormalised COUNT, so an unmatched frame produced an "
                    "artifact roughly twice the size of any real effect. "
                    "(server code: SELECTIVITY_FRAME_UNMATCHED)"
                )
        if mode is ObjectiveMode.AFFINITY_AVOIDING and not list(self.target.avoid_residues):
            raise LigandAIValidationError(
                "objective_mode='affinity_avoiding' needs target.avoid_residues — "
                "there is no constraint to hold without them. Note the mode holds "
                "avoid_hit at or below the same-cycle parent's; it does not "
                "minimise it. (server code: AVOID_MODE_NEEDS_AVOID)"
            )
        if self.resume_from is not None:
            _require_campaign_id(self.resume_from)

    def to_payload(self) -> dict[str, Any]:
        """The exact JSON body ``POST /api/campaign/mature`` receives.

        ⛔ The server model is ``extra="forbid"``: an unknown top-level field is a
        422, never ignored. A silently-dropped field is how a customer spends a
        campaign's worth of credits on settings the server never read.
        """
        payload: dict[str, Any] = {
            "seeds": [s.to_wire() for s in self.seeds],
            "target": self.target.to_wire(),
            "panel": [r.to_wire() for r in self.panel],
            "objective": self.objective.to_wire(),
            "objective_mode": ObjectiveMode(self.objective_mode).value,
            "filters": self.filters.to_wire(),
            "screening": self.screening.to_wire(),
            "maturation": self.maturation.to_wire(),
        }
        if self.name:
            payload["name"] = self.name
        if self.resume_from:
            payload["resume_from"] = self.resume_from
        return payload


# ─── Response types ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ScorerProvenance:
    """Which scorer produced an energy, and through which calibration head.

    ⛔ :attr:`calibration_head` is the field that carries the rule, and it is
    pinned strictly: it is a CAPABILITY claim (did Boltz-2 structures reach the
    Boltz-2-calibrated head), not a version string. :attr:`scorer` deliberately is
    NOT pinned — it is a server-side name this client does not control, and a
    ``Literal`` on it would reject every real response after the next release.
    """

    scorer: str | None = None
    scorer_version: str | None = None
    affinity_scorer: str | None = None
    affinity_scorer_version: str | None = None
    calibration_head: str | None = None
    model_sha256: str | None = None
    scope: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_wire(cls, raw: Any) -> ScorerProvenance | None:
        if not isinstance(raw, Mapping):
            return None
        return cls(
            scorer=raw.get("scorer"),
            scorer_version=raw.get("scorer_version"),
            affinity_scorer=raw.get("affinity_scorer"),
            affinity_scorer_version=raw.get("affinity_scorer_version"),
            calibration_head=raw.get("calibration_head"),
            model_sha256=raw.get("model_sha256"),
            scope=raw.get("scope") or raw.get("platform_length_scope"),
            raw=dict(raw),
        )

    @property
    def head_matches(self) -> bool:
        """True only when the energy came through the required calibration head."""
        return self.calibration_head == REQUIRED_CALIBRATION_HEAD

    @property
    def uses_forbidden_scorer(self) -> bool:
        """⛔ True if any provenance string names the non-production V9 path."""
        blob = " ".join(
            str(v) for v in (self.scorer, self.scorer_version, self.affinity_scorer,
                             self.affinity_scorer_version) if v
        )
        return FORBIDDEN_SCORER in blob


@dataclass(frozen=True)
class TargetReadout:
    """One receptor's measurement of one design, in one cycle.

    Read :attr:`kd_is_quotable` before displaying a Kd, and
    :attr:`is_negative_result` — never :attr:`gated` — before calling a design
    bad. ``gated`` is true for six different reasons and five of them mean "we do
    not know".
    """

    gene: str
    n_poses_total: int | None = None
    n_poses_in_domain: int | None = None
    ipsae_median: float | None = None
    iptm_median: float | None = None
    plddt_median: float | None = None
    kd_nM: float | None = None
    delta_g: float | None = None
    gated: bool = True
    gate_reason: GateReason | None = None
    gate_reason_raw: str | None = None
    scorer_provenance: ScorerProvenance | None = None
    ipsae_source: IpsaeSource | None = None
    ipsae_gate: float = IPSAE_GATE
    min_poses_in_domain: int = MIN_POSES_IN_DOMAIN_FLOOR
    peptide_length: int | None = None
    # ── reported, NEVER gated or ranked ────────────────────────────────────
    n_contacts: int | None = None
    hot_hit: int | None = None
    avoid_hit: int | None = None
    margin: float | None = None
    contact_frame: str | None = None
    # ── lineage-root avoidance ─────────────────────────────────────────────
    avoid_hit_inherited_from_seed: bool | None = None
    root_seed_avoid_hit: int | None = None
    is_root_seed: bool | None = None
    # ── withheld values, kept visible rather than deleted ──────────────────
    withheld_kd_nM: float | None = None
    withheld_delta_g: float | None = None
    withheld_ipsae_median: float | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    #: ⛔ Frozen False, and stated rather than implied: contact margin and
    #: hotspot-contact count did not gate, rank or promote anything here.
    gated_on_contact_margin: bool = False
    gated_on_hot_hit: bool = False

    @classmethod
    def from_wire(cls, raw: Any, *, peptide_length: int | None = None) -> TargetReadout:
        data = _as_dict(raw, what="a TargetReadout")
        gene = str(data.get("gene") or "")
        reason_raw = data.get("gate_reason")
        reason = GateReason(reason_raw) if isinstance(reason_raw, str) else None
        unknown = (
            reason_raw
            if isinstance(reason_raw, str) and reason_raw not in _KNOWN_GATE_REASONS
            else None
        )
        source_raw = data.get("ipsae_source")
        source = IpsaeSource(source_raw) if isinstance(source_raw, str) else None

        ipsae = _checked_ipsae(
            data.get("ipsae_median"), where=f"TargetReadout(gene={gene!r}).ipsae_median"
        )
        withheld_ipsae = None
        # ⛔ Refuse an unverified ipSAE FIRST, before the gate is derived. A
        # whole-complex value left standing makes an UNREAD instrument look like a
        # design that fell below the gate — IPSAE_BELOW_GATE where the truth is
        # IPSAE_UNAVAILABLE. The BRANCH ORDER is the semantics here, and it has a
        # twin in the server's own maturation engine: a change to the ORDER (not
        # merely the member set) has to land on both or the two surfaces will
        # label the same row differently.
        if source is not None and source is not IpsaeSource.BINDER_RESTRICTED:
            withheld_ipsae, ipsae = ipsae, None

        length = peptide_length if peptide_length is not None else _i(data.get("length"))

        obj = cls(
            gene=gene,
            n_poses_total=_i(data.get("n_poses_total")),
            n_poses_in_domain=_i(data.get("n_poses_in_domain")),
            ipsae_median=ipsae,
            iptm_median=_f(data.get("iptm_median")),
            plddt_median=_f(data.get("plddt_median")),
            kd_nM=_f(data.get("kd_nM")),
            delta_g=_f(data.get("delta_g")),
            gated=bool(data.get("gated", True)),
            gate_reason=reason,
            gate_reason_raw=unknown,
            scorer_provenance=ScorerProvenance.from_wire(data.get("scorer_provenance")),
            ipsae_source=source,
            ipsae_gate=_f(data.get("ipsae_gate")) or IPSAE_GATE,
            min_poses_in_domain=_i(data.get("min_poses_in_domain"))
            or MIN_POSES_IN_DOMAIN_FLOOR,
            peptide_length=length,
            n_contacts=_i(data.get("n_contacts")),
            hot_hit=_i(data.get("hot_hit")),
            avoid_hit=_i(data.get("avoid_hit")),
            margin=_f(data.get("margin")),
            contact_frame=data.get("contact_frame"),
            avoid_hit_inherited_from_seed=(
                None
                if data.get("avoid_hit_inherited_from_seed") is None
                else bool(data.get("avoid_hit_inherited_from_seed"))
            ),
            root_seed_avoid_hit=_i(data.get("root_seed_avoid_hit")),
            is_root_seed=(
                None if data.get("is_root_seed") is None else bool(data.get("is_root_seed"))
            ),
            withheld_ipsae_median=withheld_ipsae,
            raw=dict(data),
        )
        return obj._with_gate_applied()

    # -- gate ---------------------------------------------------------------

    def _with_gate_applied(self) -> TargetReadout:
        """Re-derive the gate locally and WITHHOLD what it covers.

        ⛔ A payload that states a withholding ``gate_reason`` AND a number is
        REFUSED rather than repaired. An omission is a server mistake a client can
        correct; a contradiction is a payload whose two halves disagree, and
        silently believing one half is how a withheld number gets quoted anyway.
        """
        declared = bool(self.gated) or self.gate_reason is not None
        if declared and (self.kd_nM is not None or self.delta_g is not None):
            shown = self.gate_reason.value if self.gate_reason else None
            if self.gate_reason is GateReason.UNRECOGNIZED and self.gate_reason_raw:
                shown = f"{self.gate_reason_raw} (unrecognised by this SDK version)"
            raise LigandAIValidationError(
                f"contradictory readout for gene {self.gene!r}: "
                f"gate_reason={shown!r} / gated={self.gated} withholds the energy, "
                f"but kd_nM={self.kd_nM!r} and delta_g={self.delta_g!r} were also "
                "supplied. A gated readout has no quotable number by construction "
                "— send the number or the gate, not both."
            )
        local = self._local_gate_reason()
        if local is None:
            return self
        object.__setattr__(self, "withheld_kd_nM", self.kd_nM)
        object.__setattr__(self, "withheld_delta_g", self.delta_g)
        object.__setattr__(self, "kd_nM", None)
        object.__setattr__(self, "delta_g", None)
        object.__setattr__(self, "gated", True)
        if self.gate_reason is None:
            object.__setattr__(self, "gate_reason", local)
        return self

    def _local_gate_reason(self) -> GateReason | None:
        """Re-derive the gate from this row alone, ignoring the server's flags.

        ⛔ GATE PRECEDENCE — the branch ORDER is the contract, not an
        implementation detail, and it is DERIVED rather than chosen::

            1. NO_POSES
            2. the ipSAE family (IPSAE_UNAVAILABLE, then IPSAE_BELOW_GATE)
            3. INSUFFICIENT_POSES
            4. LENGTH_BIN_UNCALIBRATED
            5. SCORER_HEAD_MISMATCH

        Nothing else can be true of a fold that returned nothing, so ``NO_POSES``
        is first — claiming ``LENGTH_BIN_UNCALIBRATED`` for a fold that produced
        NOTHING asserts we measured something we never folded. ipSAE is valid at
        ALL lengths, so the STRUCTURAL gate is decided before the ENERGY bin is
        consulted: a length bin is a fact about whether a Kd is calibrated, not
        about whether a pose exists. ``SCORER_HEAD_MISMATCH`` is last because it
        is the only leg that needs a number to exist before it can be evaluated.

        ⛔ This derivation has a TWIN in the platform's own maturation engine,
        which is authoritative. If its ORDER changes, this must change with it.
        """
        if self.n_poses_total is not None and self.n_poses_total <= 0:
            return GateReason.NO_POSES
        if self.ipsae_source is not None and self.ipsae_source is not IpsaeSource.BINDER_RESTRICTED:
            # ⛔ The in-domain COUNT is DEFINED on the binder-restricted ipSAE.
            # If the declared provenance is a fallback — or one this version does
            # not know — the gate was never verifiably applied, so the count is as
            # unverified as the value. Trusting the count while refusing the value
            # would quote a Kd from behind a gate nobody checked.
            return GateReason.IPSAE_UNAVAILABLE
        n_in = self.n_poses_in_domain
        if n_in is not None and n_in <= 0:
            if self.ipsae_median is None:
                return GateReason.IPSAE_UNAVAILABLE
            return GateReason.IPSAE_BELOW_GATE
        if n_in is not None and n_in < self.pose_floor:
            return GateReason.INSUFFICIENT_POSES
        if self.peptide_length is not None and self.peptide_length >= UNCALIBRATED_LENGTH_FLOOR:
            return GateReason.LENGTH_BIN_UNCALIBRATED
        # ⛔ When the server already withheld the number there is nothing left to
        # check the head against, so this leg yields None rather than a guess.
        # That asymmetry is legitimate: a fabricated derivation here would make
        # the two surfaces agree by inventing evidence.
        energy = (
            self.kd_nM if self.kd_nM is not None else self.withheld_kd_nM,
            self.delta_g if self.delta_g is not None else self.withheld_delta_g,
        )
        if any(v is not None for v in energy):
            if self.scorer_provenance is None or not self.scorer_provenance.head_matches:
                return GateReason.SCORER_HEAD_MISMATCH
        return None

    # -- reading ------------------------------------------------------------

    @property
    def pose_floor(self) -> int:
        """Effective in-domain pose floor — never below 2."""
        return max(int(self.min_poses_in_domain or 0), MIN_POSES_IN_DOMAIN_FLOOR)

    @property
    def kd_is_quotable(self) -> bool:
        """True only when a Kd survived every gate. Gate every display on this."""
        return self.kd_nM is not None

    @property
    def is_negative_result(self) -> bool:
        """True only when the design WAS assessed and failed.

        ⛔ Use this, not :attr:`gated`, to count designs that did badly.
        """
        return self.gate_reason is not None and self.gate_reason.is_negative_result

    @property
    def was_assessed(self) -> bool:
        """True when a number exists, or when the instrument read and refused it."""
        return self.kd_is_quotable or self.is_negative_result

    @property
    def ipsae_frame_valid(self) -> bool | None:
        """True when the ipSAE was binder-restricted; ``None`` when unstated.

        ⛔ ``None`` is the NORMAL case today — the engine enforces the check and
        does not emit the field, so the client can trust it but cannot verify it.
        """
        if self.ipsae_source is None:
            return None
        return self.ipsae_source is IpsaeSource.BINDER_RESTRICTED

    @property
    def scorer_is_production(self) -> bool:
        """True only when this number came through the required calibration head."""
        return self.scorer_provenance is not None and self.scorer_provenance.head_matches


@dataclass(frozen=True)
class ParentDelta:
    """A variant measured against its OWN parent, re-folded in the SAME cycle.

    ⛔ Read :attr:`control_frame` before any number on this object.
    ``same_cycle`` means the parent was re-folded in this cycle under the same
    settings. ``absent`` means it was not, and then :attr:`fold_improvement`,
    :attr:`is_win` and every paired delta are ``None`` — never ``False``, never
    ``0``. A parent Kd from an earlier round on a different pose set compares fold
    conditions, not refinement.
    """

    parent_design_id: str | None = None
    parent_kd_nM: float | None = None
    variant_kd_nM: float | None = None
    fold_improvement: float | None = None
    delta_ipsae: float | None = None
    delta_avoid_hit: int | None = None
    delta_hot_hit: int | None = None
    delta_n_contacts: int | None = None
    control_frame: ControlFrame = ControlFrame.ABSENT
    is_win: bool | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_wire(cls, raw: Any) -> ParentDelta | None:
        if not isinstance(raw, Mapping):
            return None
        frame_raw = raw.get("control_frame")
        frame = ControlFrame(frame_raw) if isinstance(frame_raw, str) else ControlFrame.ABSENT
        paired = frame is ControlFrame.SAME_CYCLE
        # ⛔ Everything paired is dropped when the frame is not same_cycle, even
        # if the server sent it. A paired number without a paired control is not a
        # smaller version of the comparison; it is a different comparison.
        return cls(
            parent_design_id=raw.get("parent_design_id"),
            parent_kd_nM=_f(raw.get("parent_kd_nM")) if paired else None,
            variant_kd_nM=_f(raw.get("variant_kd_nM")),
            fold_improvement=_f(raw.get("fold_improvement")) if paired else None,
            delta_ipsae=_f(raw.get("delta_ipsae")) if paired else None,
            delta_avoid_hit=_i(raw.get("delta_avoid_hit")) if paired else None,
            delta_hot_hit=_i(raw.get("delta_hot_hit")) if paired else None,
            delta_n_contacts=_i(raw.get("delta_n_contacts")) if paired else None,
            control_frame=frame,
            is_win=(None if raw.get("is_win") is None or not paired else bool(raw["is_win"])),
            raw=dict(raw),
        )

    @property
    def has_same_cycle_control(self) -> bool:
        return self.control_frame is ControlFrame.SAME_CYCLE

    @property
    def comparable(self) -> bool:
        """True only when a verdict exists — a same-cycle control AND both Kds."""
        return self.has_same_cycle_control and self.is_win is not None

    @property
    def avoid_constraint_held(self) -> bool | None:
        """Did avoidance survive refinement? ``None`` when not comparable.

        ⛔ Never ``True`` by default. "Held" means ``avoid_hit`` did not INCREASE
        against the same-cycle parent — avoidance is a constraint to hold, not a
        quantity to minimise.
        """
        if not self.has_same_cycle_control or self.delta_avoid_hit is None:
            return None
        return self.delta_avoid_hit <= 0

    @property
    def uncomparable_reason(self) -> str | None:
        """Why there is no verdict, in a sentence — or ``None`` when there is one."""
        if self.comparable:
            return None
        if not self.has_same_cycle_control:
            return (
                "no same-cycle parent control: this variant's parent was not "
                "re-folded in this cycle, so there is nothing to compare against "
                "under the same fold conditions. This is a bookkeeping gap, NOT a "
                "loss for the variant."
            )
        return (
            "the control frame is valid but one side of the pair has no quotable "
            "Kd — the comparison was not made, which is not the same as the "
            "variant failing it."
        )


@dataclass(frozen=True)
class SelectivityReadout:
    """Cross-isoform selectivity for one design.

    ⛔ :attr:`selectivity_margin` and :attr:`selectivity_fold` are ``None`` —
    never 0, never a partial number — unless the panel is COMPLETE and the frame
    is MSA-matched. A worst case minimised over a SUBSET of comparators is an
    upper bound wearing a lower bound's name, and it can only fall when the
    missing comparators arrive.
    """

    panel_genes_expected: tuple[str, ...] = ()
    panel_genes_evaluated: tuple[str, ...] = ()
    panel_complete: bool = False
    hotspot_frame: HotspotFrame = HotspotFrame.UNRECOGNIZED
    hotspot_frame_resolved: HotspotFrame | None = None
    on_target_margin: float | None = None
    worst_off_target_margin: float | None = None
    worst_off_target_gene: str | None = None
    selectivity_margin: float | None = None
    selectivity_fold: float | None = None
    gated_on_contact_margin: bool = False
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_wire(cls, raw: Any) -> SelectivityReadout | None:
        if not isinstance(raw, Mapping):
            return None
        frame_raw = raw.get("hotspot_frame")
        frame = HotspotFrame(frame_raw) if isinstance(frame_raw, str) else HotspotFrame.UNRECOGNIZED
        resolved_raw = raw.get("hotspot_frame_resolved")
        resolved = HotspotFrame(resolved_raw) if isinstance(resolved_raw, str) else None
        complete = bool(raw.get("panel_complete"))
        matched = frame is HotspotFrame.MSA_MATCHED and (
            resolved is None or resolved is HotspotFrame.MSA_MATCHED
        )
        quotable = complete and matched
        return cls(
            panel_genes_expected=tuple(raw.get("panel_genes_expected") or ()),
            panel_genes_evaluated=tuple(raw.get("panel_genes_evaluated") or ()),
            panel_complete=complete,
            hotspot_frame=frame,
            hotspot_frame_resolved=resolved,
            on_target_margin=_f(raw.get("on_target_margin")),
            worst_off_target_margin=_f(raw.get("worst_off_target_margin")),
            worst_off_target_gene=raw.get("worst_off_target_gene"),
            # ⛔ Withheld locally too, not merely trusted: the client re-asserts
            # the rule so a server that regresses cannot hand a customer a partial
            # margin dressed as a verdict.
            selectivity_margin=_f(raw.get("selectivity_margin")) if quotable else None,
            selectivity_fold=_f(raw.get("selectivity_fold")) if quotable else None,
            gated_on_contact_margin=False,
            raw=dict(raw),
        )

    @property
    def frame_is_matched(self) -> bool:
        return self.hotspot_frame is HotspotFrame.MSA_MATCHED and (
            self.hotspot_frame_resolved is None
            or self.hotspot_frame_resolved is HotspotFrame.MSA_MATCHED
        )

    @property
    def quotable(self) -> bool:
        """True only when a selectivity number may be shown."""
        return self.panel_complete and self.frame_is_matched

    @property
    def missing_genes(self) -> tuple[str, ...]:
        done = set(self.panel_genes_evaluated)
        return tuple(g for g in self.panel_genes_expected if g not in done)

    @property
    def withheld_reason(self) -> str | None:
        """Why no selectivity number, in a sentence — ``None`` when there is one."""
        if self.quotable:
            return None
        if not self.panel_complete:
            missing = ", ".join(self.missing_genes) or "at least one comparator"
            return (
                f"the panel is incomplete ({missing} not evaluated). A worst case "
                "minimised over a SUBSET of comparators is an upper bound, and it "
                "can only fall when the rest arrive — so it is not reported as a "
                "margin."
            )
        return (
            f"the hotspot frame is {HotspotFrame(self.hotspot_frame).value!r}, not "
            "'msa_matched'. Raw per-isoform hotspot lists are unequal in size and "
            "the margin is an unnormalised COUNT, so a cross-isoform difference in "
            "that frame is dominated by the frame, not by the binder."
        )


@dataclass(frozen=True)
class AffinityRankingCaveat:
    """Travels with every Kd-sorted page and cannot be detached from it.

    ⛔ :attr:`ranking_is_validated` is a frozen ``False``. The deployed Kd scorer
    has **no measured-affinity evaluation on any row**, so a within-target Kd
    order is a triage prior — which designs are worth folding again — and not a
    statement about which one binds tightest.

    ⛔ THIS OBJECT QUOTES NO CORRELATION COEFFICIENT, and it drops any that a
    server sends, recording what it dropped in :attr:`dropped_statistics` rather
    than deleting it silently. The pair that used to ride here was withdrawn: the
    model half had zero joinable rows behind it, and the length half was an
    artifact of bucketing NULL genes as though they were a target. The
    engineering conclusion does not depend on either number and is unchanged.
    """

    ranking_is_validated: bool = False
    n_rows: int | None = None
    note: str = _UNVALIDATED_RANKING_NOTE
    #: Keys this client refused to pass through, and why, so the removal is
    #: visible in the data rather than invisible in the client.
    dropped_statistics: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_wire(cls, raw: Any, *, n_rows: int | None = None) -> AffinityRankingCaveat:
        data = dict(raw) if isinstance(raw, Mapping) else {}
        dropped = tuple(k for k in _WITHDRAWN_CAVEAT_KEYS if data.get(k) is not None)
        for key in _WITHDRAWN_CAVEAT_KEYS:
            data.pop(key, None)
        note = data.get("note")
        note = str(note) if isinstance(note, str) and note.strip() else ""
        lowered = note.lower()
        if any(marker in lowered for marker in _WITHDRAWN_NOTE_MARKERS):
            # ⛔ The sentence asserts the withdrawn finding even with the
            # coefficient stripped. Replace it and SAY that it was replaced —
            # a silently rewritten note is its own kind of dishonesty.
            dropped = dropped + ("note:length_outranked_the_model",)
            data["note_withheld"] = note
            note = ""
        return cls(
            ranking_is_validated=False,
            n_rows=_i(data.get("n_rows")) if data.get("n_rows") is not None else n_rows,
            note=note or _UNVALIDATED_RANKING_NOTE,
            dropped_statistics=dropped,
            raw=data,
        )


@dataclass(frozen=True)
class MaturedDesign:
    """One row of a maturation leaderboard: a sequence, its measurement, its lineage.

    ⛔ ``parent_delta`` is how you check the comparison was fair. A design whose
    :attr:`ParentDelta.control_frame` is ``absent`` has no verdict against its
    parent — not a losing one.
    """

    design_id: str
    sequence: str = ""
    cycle: int | None = None
    rank: int | None = None
    origin: str | None = None
    parent_id: str | None = None
    seed_id: str | None = None
    root_seed_id: str | None = None
    on_target: TargetReadout | None = None
    panel: tuple[TargetReadout, ...] = ()
    selectivity: SelectivityReadout | None = None
    parent_delta: ParentDelta | None = None
    meets_objective: bool = False
    structure_url: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_wire(cls, raw: Any) -> MaturedDesign:
        data = _as_dict(raw, what="a maturation leaderboard row")
        design_id = str(data.get("design_id") or "")
        sequence = str(data.get("sequence") or "")
        length = _i(data.get("length")) or (len(sequence) or None)
        on_target_raw = data.get("on_target")
        if not isinstance(on_target_raw, Mapping):
            # Older/leaner payloads carry the on-target numbers flat on the row.
            on_target_raw = data
        return cls(
            design_id=design_id,
            sequence=sequence,
            cycle=_i(data.get("cycle")),
            rank=_i(data.get("rank")),
            origin=data.get("origin"),
            parent_id=data.get("parent_id"),
            seed_id=data.get("seed_id"),
            root_seed_id=data.get("root_seed_id") or data.get("seed_id"),
            on_target=TargetReadout.from_wire(on_target_raw, peptide_length=length),
            panel=tuple(
                TargetReadout.from_wire(p, peptide_length=length)
                for p in (data.get("panel") or [])
                if isinstance(p, Mapping)
            ),
            selectivity=SelectivityReadout.from_wire(data.get("selectivity")),
            parent_delta=ParentDelta.from_wire(data.get("parent_delta")),
            meets_objective=bool(data.get("meets_objective")),
            structure_url=data.get("structure_url"),
            raw=dict(data),
        )

    @property
    def kd_nM(self) -> float | None:
        """The on-target Kd, or ``None`` when it was withheld. Never a 0 stand-in."""
        return self.on_target.kd_nM if self.on_target else None

    @property
    def kd_is_quotable(self) -> bool:
        return bool(self.on_target and self.on_target.kd_is_quotable)

    @property
    def peptide_ipsae(self) -> float | None:
        """The binder-interface ipSAE median. ⛔ Never the whole-complex value."""
        return self.on_target.ipsae_median if self.on_target else None

    @property
    def gate_reason(self) -> GateReason | None:
        return self.on_target.gate_reason if self.on_target else None

    @property
    def is_negative_result(self) -> bool:
        """⛔ The only honest way to count "this design did badly"."""
        return bool(self.on_target and self.on_target.is_negative_result)

    @property
    def was_assessed(self) -> bool:
        return bool(self.on_target and self.on_target.was_assessed)

    @property
    def is_root_seed(self) -> bool:
        return self.parent_id is None or self.design_id == self.root_seed_id

    @property
    def is_parent_control(self) -> bool:
        """True for a parent re-folded in this cycle as its variants' control."""
        return str(self.origin or "").lower() in ("parent_control", "parent", "control")

    @property
    def beat_its_parent(self) -> bool | None:
        """``True``/``False`` only with a same-cycle control; else ``None``."""
        return self.parent_delta.is_win if self.parent_delta else None

    def __repr__(self) -> str:  # pragma: no cover - repr only
        kd = f"{self.kd_nM:.3g} nM" if self.kd_nM is not None else "Kd withheld"
        return f"<MaturedDesign {self.design_id} {self.sequence[:14]}… {kd}>"


@dataclass(frozen=True)
class CycleRecord:
    """One maturation cycle, as pairs.

    ⛔ THE DENOMINATOR RULE. :attr:`comparisons` counts only pairs that were
    ACTUALLY ASSESSED, and :attr:`win_rate` divides by it. Pairs that had a valid
    same-cycle control but whose variant was never measured are in
    :attr:`pairs_not_comparable`, not in the denominator: an unassessed row there
    biases the rate DOWNWARD, which reads as appropriate caution and is therefore
    the version nobody challenges.
    """

    cycle: int
    seeds_in: tuple[str, ...] = ()
    parent_controls_refolded: tuple[str, ...] = ()
    variants_proposed: int = 0
    variants_folded: int = 0
    variants_scored: int = 0
    variants_gated_out: int = 0
    variants_assessed_and_failed: int = 0
    variants_not_assessed: int = 0
    gate_reason_breakdown: dict[str, int] = field(default_factory=dict)
    survivors: tuple[str, ...] = ()
    comparisons: int = 0
    wins_vs_parent: int = 0
    pairs_with_same_cycle_control: int = 0
    pairs_not_comparable: int = 0
    control_frame_valid: bool = False
    best_kd_nM: float | None = None
    best_design_id: str | None = None
    parent_avoid_hit_mean: float | None = None
    variant_avoid_hit_mean: float | None = None
    avoid_constraint_held: bool | None = None
    pocket_conditioning: str | None = None
    pairs: tuple[MaturedDesign, ...] = ()
    started_at: str | None = None
    ended_at: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_wire(cls, raw: Any) -> CycleRecord:
        data = _as_dict(raw, what="a maturation cycle record")
        return cls(
            cycle=_i(data.get("cycle")) or 0,
            seeds_in=tuple(data.get("seeds_in") or ()),
            parent_controls_refolded=tuple(data.get("parent_controls_refolded") or ()),
            variants_proposed=_i(data.get("variants_proposed")) or 0,
            variants_folded=_i(data.get("variants_folded")) or 0,
            variants_scored=_i(data.get("variants_scored")) or 0,
            variants_gated_out=_i(data.get("variants_gated_out")) or 0,
            variants_assessed_and_failed=_i(data.get("variants_assessed_and_failed")) or 0,
            variants_not_assessed=_i(data.get("variants_not_assessed")) or 0,
            gate_reason_breakdown=dict(data.get("gate_reason_breakdown") or {}),
            survivors=tuple(data.get("survivors") or ()),
            comparisons=_i(data.get("comparisons")) or 0,
            wins_vs_parent=_i(data.get("wins_vs_parent")) or 0,
            pairs_with_same_cycle_control=_i(data.get("pairs_with_same_cycle_control")) or 0,
            pairs_not_comparable=_i(data.get("pairs_not_comparable")) or 0,
            control_frame_valid=bool(data.get("control_frame_valid")),
            best_kd_nM=_f(data.get("best_kd_nM")),
            best_design_id=data.get("best_design_id"),
            parent_avoid_hit_mean=_f(data.get("parent_avoid_hit_mean")),
            variant_avoid_hit_mean=_f(data.get("variant_avoid_hit_mean")),
            avoid_constraint_held=(
                None
                if data.get("avoid_constraint_held") is None
                else bool(data.get("avoid_constraint_held"))
            ),
            pocket_conditioning=data.get("pocket_conditioning"),
            pairs=tuple(
                MaturedDesign.from_wire(p) for p in (data.get("pairs") or []) if isinstance(p, Mapping)
            ),
            started_at=data.get("started_at"),
            ended_at=data.get("ended_at"),
            raw=dict(data),
        )

    @property
    def win_rate(self) -> float | None:
        """Wins over ASSESSED pairs, or ``None`` when nothing was assessed.

        ⛔ ``None`` rather than ``0.0``: a cycle where nothing could be compared
        did not produce a 0% win rate, it produced no rate at all.
        """
        if not self.control_frame_valid or self.comparisons <= 0:
            return None
        return self.wins_vs_parent / self.comparisons

    @property
    def win_rate_caveat(self) -> str | None:
        """What a reader must know before quoting :attr:`win_rate`."""
        if not self.control_frame_valid:
            return (
                "no win rate: this cycle has variants without same-cycle parent "
                "re-folds, so no comparison in it is against a matched control."
            )
        if self.comparisons <= 0:
            return (
                f"no win rate: {self.pairs_with_same_cycle_control} pair(s) had a "
                "same-cycle control but none produced a verdict — the variants "
                "were never measured, which is not the same as losing."
            )
        if self.pairs_not_comparable:
            return (
                f"{self.wins_vs_parent}/{self.comparisons} is over ASSESSED pairs "
                f"only; {self.pairs_not_comparable} further pair(s) had a valid "
                "control but no measurement. Counting those in the denominator "
                "would understate the result exactly as counting them losses would."
            )
        return None


@dataclass(frozen=True)
class MaturationReadout:
    """``GET /api/campaign/{id}/maturation`` — the per-cycle detail.

    ⛔ A campaign that is NOT a maturation campaign is a 409 here, not an empty
    readout: "this campaign has no maturation cycles" and "this campaign is not a
    maturation campaign" are different facts and must not render identically.
    """

    campaign_id: str
    name: str | None = None
    status: str | None = None
    phase: str | None = None
    cycle: int = 0
    cycles_planned: int | None = None
    cycles_run: int = 0
    objective_mode: ObjectiveMode | str | None = None
    engine: str | None = None
    seeds: tuple[dict[str, Any], ...] = ()
    cycles: tuple[CycleRecord, ...] = ()
    selectivity: SelectivityReadout | None = None
    hotspot_frame: HotspotFrame | None = None
    pocket_conditioning: str | None = None
    ipsae_gate: float = IPSAE_GATE
    min_poses_in_domain: int = MIN_POSES_IN_DOMAIN_FLOOR
    root_seed_avoid_hit: dict[str, Any] = field(default_factory=dict)
    best_kd_nM: float | None = None
    best_design_id: str | None = None
    scorer: dict[str, Any] = field(default_factory=dict)
    ranking_caveat: AffinityRankingCaveat = field(default_factory=AffinityRankingCaveat)
    warnings: tuple[Any, ...] = ()
    error: dict[str, Any] | None = None
    updated_at: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    #: Frozen, and stated in the payload rather than left to the reader.
    pose_statistic: str = "median"
    refold_parent_controls: bool = True
    gated_on_contact_margin: bool = False
    gated_on_hot_hit: bool = False

    @classmethod
    def from_wire(cls, raw: Any, *, campaign_id: str | None = None) -> MaturationReadout:
        data = _as_dict(raw, what="a maturation readout")
        frame_raw = data.get("hotspot_frame")
        records = tuple(
            CycleRecord.from_wire(c) for c in (data.get("cycles") or []) if isinstance(c, Mapping)
        )
        return cls(
            campaign_id=str(data.get("campaign_id") or campaign_id or ""),
            name=data.get("name"),
            status=data.get("status"),
            phase=data.get("phase"),
            cycle=_i(data.get("cycle")) or 0,
            cycles_planned=_i(data.get("cycles_planned")),
            cycles_run=_i(data.get("cycles_run")) or 0,
            objective_mode=data.get("objective_mode"),
            engine=data.get("engine"),
            seeds=tuple(data.get("seeds") or ()),
            cycles=records,
            selectivity=SelectivityReadout.from_wire(data.get("selectivity")),
            hotspot_frame=HotspotFrame(frame_raw) if isinstance(frame_raw, str) else None,
            pocket_conditioning=data.get("pocket_conditioning"),
            ipsae_gate=_f(data.get("ipsae_gate")) or IPSAE_GATE,
            min_poses_in_domain=_i(data.get("min_poses_in_domain")) or MIN_POSES_IN_DOMAIN_FLOOR,
            root_seed_avoid_hit=dict(data.get("root_seed_avoid_hit") or {}),
            best_kd_nM=_f(data.get("best_kd_nM")),
            best_design_id=data.get("best_design_id"),
            scorer=dict(data.get("scorer") or {}),
            ranking_caveat=AffinityRankingCaveat.from_wire(data.get("affinity_ranking_caveat")),
            warnings=tuple(data.get("warnings") or ()),
            error=data.get("error") if isinstance(data.get("error"), Mapping) else None,
            updated_at=data.get("updated_at"),
            raw=dict(data),
        )

    @property
    def control_frame_valid(self) -> bool:
        """True only when EVERY cycle compared against same-cycle parent re-folds."""
        return bool(self.cycles) and all(c.control_frame_valid for c in self.cycles)

    @property
    def total_comparisons(self) -> int | None:
        """Assessed pairs across all cycles, or ``None`` if any frame is invalid."""
        if not self.control_frame_valid:
            return None
        return sum(c.comparisons for c in self.cycles)

    @property
    def total_wins_vs_parent(self) -> int | None:
        """⛔ ``None`` — never a partial sum — when any control frame is invalid."""
        if not self.control_frame_valid:
            return None
        return sum(c.wins_vs_parent for c in self.cycles)

    @property
    def win_rate(self) -> float | None:
        """Wins over assessed pairs across the run, or ``None``."""
        wins, total = self.total_wins_vs_parent, self.total_comparisons
        if wins is None or not total:
            return None
        return wins / total

    @property
    def scorer_is_production(self) -> bool | None:
        """Whether the run declared the required calibration head up front."""
        head = self.scorer.get("required_calibration_head") if self.scorer else None
        if head is None:
            return None
        return head == REQUIRED_CALIBRATION_HEAD


@dataclass(frozen=True)
class MaturationLeaderboard:
    """One page of matured designs, with the ranking caveat welded on.

    ⛔ The default sort is ``kd`` and IT IS NOT AN AFFINITY RANKING — see
    :class:`AffinityRankingCaveat`, which travels on :attr:`ranking_caveat` and
    cannot be separated from the page.
    """

    campaign_id: str
    designs: tuple[MaturedDesign, ...] = ()
    total: int = 0
    limit: int = 50
    offset: int = 0
    sort: str = "kd"
    mode: str | None = None
    ranking_caveat: AffinityRankingCaveat = field(default_factory=AffinityRankingCaveat)
    updated_at: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    #: Stated, not assumed: neither contact margin nor hotspot-contact count
    #: ordered this list.
    gated_on_contact_margin: bool = False
    gated_on_hot_hit: bool = False

    @classmethod
    def from_wire(cls, raw: Any, *, campaign_id: str | None = None,
                  sort: str | None = None) -> MaturationLeaderboard:
        data = _as_dict(raw, what="a maturation leaderboard")
        rows = tuple(
            MaturedDesign.from_wire(d) for d in (data.get("designs") or []) if isinstance(d, Mapping)
        )
        total = _i(data.get("total")) or len(rows)
        return cls(
            campaign_id=str(data.get("campaign_id") or campaign_id or ""),
            designs=rows,
            total=total,
            limit=_i(data.get("limit")) or 50,
            offset=_i(data.get("offset")) or 0,
            sort=str(data.get("sort") or sort or "kd"),
            mode=data.get("mode"),
            # ⛔ The caveat is constructed even when the server omits it. Omission
            # is the failure this object exists to prevent, so a missing block
            # degrades to the default text rather than to no caveat at all.
            ranking_caveat=AffinityRankingCaveat.from_wire(
                data.get("affinity_ranking_caveat"), n_rows=total
            ),
            updated_at=data.get("updated_at"),
            raw=dict(data),
        )

    def __len__(self) -> int:
        return len(self.designs)

    def __iter__(self) -> Iterator[MaturedDesign]:
        return iter(self.designs)

    def __getitem__(self, idx: int) -> MaturedDesign:
        return self.designs[idx]

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.designs) < self.total

    @property
    def quotable(self) -> tuple[MaturedDesign, ...]:
        """Rows whose Kd survived every gate. ⛔ Show these, not all rows."""
        return tuple(d for d in self.designs if d.kd_is_quotable)

    @property
    def assessed_and_failed(self) -> tuple[MaturedDesign, ...]:
        """Rows that WERE measured and fell below the interface gate."""
        return tuple(d for d in self.designs if d.is_negative_result)

    @property
    def not_assessed(self) -> tuple[MaturedDesign, ...]:
        """Rows with no measurement at all. ⛔ NOT failures — unknowns."""
        return tuple(d for d in self.designs if not d.was_assessed)


@dataclass(frozen=True)
class MaturationStatus:
    """``GET /api/campaign/{id}/status`` — cheap, carries no leaderboard rows."""

    campaign_id: str
    name: str | None = None
    status: str | None = None
    phase: str | None = None
    mode: str | None = None
    cycle: int = 0
    max_cycles: int | None = None
    objective: dict[str, Any] = field(default_factory=dict)
    progress: dict[str, Any] = field(default_factory=dict)
    credits: dict[str, Any] = field(default_factory=dict)
    maturation: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[Any, ...] = ()
    error: dict[str, Any] | None = None
    created_at: str | None = None
    updated_at: str | None = None
    finished_at: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    #: Statuses after which the engine never publishes again.
    TERMINAL = ("completed", "failed", "stopped", "cancelled", "objective_met")

    @classmethod
    def from_wire(cls, raw: Any, *, campaign_id: str | None = None) -> MaturationStatus:
        data = _as_dict(raw, what="a campaign status")
        return cls(
            campaign_id=str(data.get("campaign_id") or campaign_id or ""),
            name=data.get("name"),
            status=data.get("status"),
            phase=data.get("phase"),
            mode=data.get("mode"),
            cycle=_i(data.get("cycle")) or 0,
            max_cycles=_i(data.get("max_cycles")),
            objective=dict(data.get("objective") or {}),
            progress=dict(data.get("progress") or {}),
            credits=dict(data.get("credits") or {}),
            maturation=dict(data.get("maturation") or {}),
            warnings=tuple(data.get("warnings") or ()),
            error=data.get("error") if isinstance(data.get("error"), Mapping) else None,
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            finished_at=data.get("finished_at"),
            raw=dict(data),
        )

    @property
    def is_maturation(self) -> bool:
        return (self.mode or "campaign") == "maturation"

    @property
    def is_terminal(self) -> bool:
        return (self.status or "") in self.TERMINAL

    @property
    def objective_met(self) -> bool:
        return bool(self.objective.get("met"))

    @property
    def best_kd_nM(self) -> float | None:
        return _f(self.progress.get("best_kd_nM"))


@dataclass(frozen=True)
class MaturationStarted:
    """``POST /api/campaign/mature`` → 202 Accepted. Nothing has folded yet."""

    campaign_id: str
    status: str | None = None
    name: str | None = None
    mode: str = "maturation"
    objective_mode: str | None = None
    seeds: tuple[dict[str, Any], ...] = ()
    maturation: dict[str, Any] = field(default_factory=dict)
    scorer: dict[str, Any] = field(default_factory=dict)
    ranking_caveat: AffinityRankingCaveat = field(default_factory=AffinityRankingCaveat)
    estimated_credits: int | None = None
    created_at: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_wire(cls, raw: Any) -> MaturationStarted:
        data = _as_dict(raw, what="a maturation start response")
        cid = str(data.get("campaign_id") or "")
        if not cid:
            raise LigandAIValidationError(
                "the server accepted the maturation request but returned no "
                f"campaign_id, so nothing can be polled or stopped. Body: {data!r}"
            )
        return cls(
            campaign_id=cid,
            status=data.get("status"),
            name=data.get("name"),
            mode=str(data.get("mode") or "maturation"),
            objective_mode=data.get("objective_mode"),
            seeds=tuple(data.get("seeds") or ()),
            maturation=dict(data.get("maturation") or {}),
            scorer=dict(data.get("scorer") or {}),
            ranking_caveat=AffinityRankingCaveat.from_wire(
                data.get("affinity_ranking_caveat")
            ),
            estimated_credits=_i(data.get("estimated_credits")),
            created_at=data.get("created_at"),
            raw=dict(data),
        )

    @property
    def estimated_credits_is_a_quote(self) -> bool:
        """⛔ Always ``False``. ``estimated_credits`` is an advisory forecast from
        the engine, not a price you accepted and not a billing authority."""
        return False


@dataclass(frozen=True)
class MaturationEvent:
    """One server-sent event off ``GET /api/campaign/{id}/stream``."""

    event: str
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.event in ("campaign_complete", "error")


# ─── Errors ───────────────────────────────────────────────────────────────────


_MATURATION_422_CODES = {
    "MATURATION_ENGINE_UNSUPPORTED": (
        "the mutagenesis engine is the only one accepted. seed_optimizer is "
        "refused: built without a generate_fn it degrades to a uniform random "
        "amino-acid draw at every masked position while still reporting a "
        "positive improvement."
    ),
    "POSE_STATISTIC_UNSUPPORTED": (
        "the pose statistic is frozen to the median. best-of-N over a handful of "
        "poses is an optimistic order statistic."
    ),
    "MIN_POSES_TOO_LOW": "a median over one pose is a best-of-1.",
    "PARENT_CONTROL_REQUIRED": (
        "same-cycle parent re-folds cannot be disabled — they are the only reason "
        "a variant-vs-parent delta means anything."
    ),
    "SELECTIVITY_PANEL_MISSING": (
        "selectivity is on-target minus the WORST off-target, and there is no "
        "worst off-target over an empty panel."
    ),
    "SELECTIVITY_FRAME_UNMATCHED": (
        "selectivity needs the MSA-matched hotspot frame — raw per-isoform lists "
        "are unequal in size against an unnormalised count."
    ),
    "SEED_NON_CANONICAL": (
        "Boltz folds canonical sequences, so a UAA-bearing seed could not be "
        "folded or scored and would drop out of the run without failing it."
    ),
    "SEED_ID_DUPLICATE": (
        "a seed id is the lineage anchor for every descendant; two roots sharing "
        "one make the inherited-avoidance readout unanswerable."
    ),
    "AVOID_MODE_NEEDS_AVOID": (
        "affinity_avoiding holds avoid_hit at or below the same-cycle parent's; "
        "with no avoid_residues there is no constraint to hold."
    ),
    "PANEL_FRAME_MISSING": (
        "each panel receptor carries its own pocket in its own residue frame; "
        "residue numbers are never inherited from the target."
    ),
}


#: ⛔ The Express→FastAPI spend-grant deny reasons, as the platform's own frozen
#: table names them. The route is metered by a single-use EXECUTION GRANT that
#: Express mints only AFTER it has reserved credits against a quote you accepted,
#: so "no grant" is a statement about the AUTHORIZATION, not about your balance —
#: and telling a customer "you need more credits" when the real problem is an
#: unauthorized call sends them to the wrong fix.
#:
#: Two reasons are deliberately NOT distinguished here: a grant for a different
#: surface and a grant for a different user are folded by the server into one
#: message, because saying which would confirm the existence of the other. Do not
#: un-fold them client-side.
_SPEND_GRANT_REASONS: dict[str, str] = {
    "SPEND_GRANT_MISSING": (
        "this route is metered, and the call carried no spend authorization. "
        "Accept a quote and pass it as quote= to start(); the platform mints the "
        "execution grant after it reserves the credits. Nothing was queued and "
        "nothing was charged."
    ),
    "SPEND_GRANT_MALFORMED": (
        "the spend authorization could not be read. Accept a fresh quote — do not "
        "hand-build or edit this token."
    ),
    "SPEND_GRANT_BAD_SIGNATURE": (
        "the spend authorization did not verify. Accept a fresh quote; a token "
        "that fails signature check is never retried."
    ),
    "SPEND_GRANT_EXPIRED": (
        "the spend authorization expired before the run started. Accept a fresh "
        "quote. ⛔ Nothing here refetches one for you: a price that moved after "
        "you accepted it needs a NEW acceptance, and that decision is yours."
    ),
    "SPEND_GRANT_REQUEST_MISMATCH": (
        "the request does not match the one that was priced and authorized. A "
        "grant covers the EXACT inputs it was quoted for — change the spec and it "
        "has to be re-quoted, or the ceiling you accepted would not be the "
        "ceiling that runs."
    ),
    "SPEND_GRANT_CEILING_EXCEEDED": (
        "this run prices above the ceiling the authorization covers. Re-quote and "
        "decide whether the new number is acceptable; the SDK will not raise a "
        "ceiling on your behalf."
    ),
    "SPEND_GRANT_REPLAYED": (
        "this spend authorization was already used. Each one is single-use, so a "
        "second campaign needs a second acceptance — that is what stops a lost "
        "response from turning into two charged runs."
    ),
    "SPEND_GRANT_UNCONFIGURED": (
        "the deployment cannot verify spend authorizations, so it refuses to run "
        "GPU work rather than run it unmetered. This is a server-side "
        "provisioning fault, not your call being wrong."
    ),
    "SPEND_GRANT_METER_UNAVAILABLE": (
        "the platform cannot record what this run spends, so it will not start "
        "it. Server-side, not your call."
    ),
    "SPEND_GRANT_PRICE_UNAVAILABLE": (
        "the platform could not price this run, and refuses to guess. Server-side, "
        "not your call."
    ),
    "SPEND_GRANT_CLAIM_FAILED": (
        "the single-use claim on the authorization could not be recorded, so the "
        "run was not started. A claim that cannot be recorded is not a claim."
    ),
}


def _interpret_maturation_error(exc: LigandAIError, *,
                                accepted_quote: dict[str, Any] | None = None) -> None:
    """Re-raise a server error with the rule that produced it attached.

    Returns normally when the error is not one this module can explain, so the
    caller's own ``raise`` re-raises it untouched.

    ⛔ NEVER refetches a quote and NEVER retries a start. A price that moved after
    you accepted it needs a NEW acceptance, and the customer makes that call.
    """
    payload = exc.response if isinstance(exc.response, Mapping) else {}
    detail = payload.get("detail") if isinstance(payload.get("detail"), Mapping) else payload
    code = str((detail or {}).get("code") or payload.get("code") or "").strip()
    err = str(payload.get("error") or "").strip().lower()
    status = exc.status_code

    if code in _MATURATION_422_CODES:
        raise LigandAIValidationError(
            f"{code}: {(detail or {}).get('error') or exc.message}\n"
            f"  why this rule exists: {_MATURATION_422_CODES[code]}",
            code=code,
            status_code=status,
            request_id=exc.request_id,
            response=dict(payload),
        ) from exc

    if code == "NOT_A_MATURATION_CAMPAIGN":
        raise LigandAIValidationError(
            f"{exc.message}\n"
            "  This campaign exists but was not started with "
            "client.maturation.start(), so it has no cycles, no parent pairs and "
            "no maturation readout. That is a different fact from 'it has not run "
            "a cycle yet', which is why it is an error rather than an empty page.",
            code=code,
            status_code=status,
            request_id=exc.request_id,
            response=dict(payload),
        ) from exc

    if code == "MATURATION_ENGINE_UNAVAILABLE":
        raise LigandAIError(
            f"{exc.message}\n"
            "  The maturation engine is not installed on the deployment this key "
            "points at. Nothing was queued and nothing was charged.",
            code=code,
            status_code=status,
            request_id=exc.request_id,
            response=dict(payload),
        ) from exc

    # ── the credit-authorization lifecycle the metered route enforces ──────
    #
    # ⛔ THIS LEG RUNS BEFORE THE GENERIC 402. A missing or stale spend grant
    # arrives as a 402 too, and answering it with "you need more credits" points
    # a customer at their balance when the actual problem is that the call was
    # never authorized. Those are different fixes.
    reason = str(
        (detail or {}).get("reason") or payload.get("reason") or ""
    ).strip().upper()
    if err == "credit_authorization_required" or reason in _SPEND_GRANT_REASONS:
        explanation = _SPEND_GRANT_REASONS.get(
            reason,
            "this route is metered and the call was not authorized to spend. "
            "Accept a quote and pass it as quote= to start().",
        )
        message = (
            f"{exc.message}\n"
            f"  reason  : {reason or 'unspecified'}\n"
            f"  meaning : {explanation}"
        )
        if status == 402 and isinstance(exc, LigandAICreditError):
            raise LigandAICreditError(
                message,
                required=exc.required,
                available=exc.available,
                code=exc.code or reason or None,
                status_code=status,
                request_id=exc.request_id,
                response=dict(payload),
            ) from exc
        raise LigandAIError(
            message,
            code=exc.code or reason or None,
            status_code=status,
            request_id=exc.request_id,
            response=dict(payload),
        ) from exc

    if status == 402 and isinstance(exc, LigandAICreditError):
        raise LigandAICreditError(
            f"{exc.message}\n"
            f"  A maturation campaign needs at least {MATURATION_MIN_CREDITS} "
            "credits before the platform will admit it.",
            required=exc.required or MATURATION_MIN_CREDITS,
            available=exc.available,
            code=exc.code,
            status_code=status,
            request_id=exc.request_id,
            response=dict(payload),
        ) from exc

    if status == 400 and err in ("quote_required", "quote_invalid"):
        raise LigandAIValidationError(
            (
                "This maturation campaign needs an accepted credit quote before "
                "it can start. Pass the quote block you accepted as quote= to "
                f"start(). Server message: {exc.message}"
            )
            if err == "quote_required"
            else (
                f"The quote token was not accepted by the server ({exc.message}). "
                "It may be expired, malformed, or issued for a different account "
                "or a different surface. Accept a fresh quote and start again."
            ),
            code=exc.code or err,
            status_code=status,
            request_id=exc.request_id,
            response=dict(payload),
        ) from exc

    if status == 409 and err in _QUOTE_STALE_CODES:
        enriched = dict(payload)
        if accepted_quote is not None:
            enriched["acceptedQuote"] = accepted_quote
        raise LigandAIError(
            f"{err}: the price you accepted is no longer valid. The SDK will NOT "
            "relaunch at a new price automatically — inspect exc.response['quote'] "
            "/ exc.response['estimate'] against exc.response['acceptedQuote'] and "
            f"start again only if the new numbers are acceptable. {exc.message}",
            code=exc.code or err,
            status_code=status,
            request_id=exc.request_id,
            response=enriched,
        ) from exc

    if status == 409 and code == "CAMPAIGN_ALREADY_RUNNING":
        raise LigandAIError(
            f"{exc.message}\n"
            "  Campaigns are capped per account because each one holds GPU lanes. "
            "Stop one with client.maturation.stop(campaign_id) — completed cycles "
            "stay on its leaderboard — before starting another.",
            code=code,
            status_code=status,
            request_id=exc.request_id,
            response=dict(payload),
        ) from exc
    return


def _sort_param(sort: LeaderboardSort | str) -> str:
    ordering = sort if isinstance(sort, LeaderboardSort) else LeaderboardSort(str(sort))
    if ordering is LeaderboardSort.MARGIN:
        raise LigandAIValidationError(
            "sort='margin' is not available on a maturation leaderboard. Contact "
            "margin is REPORTED, never ranked on: it fails its own "
            "composition-matched scramble control, and across six live paired "
            "variants its positive term (hotspot-contact count) FELL while Kd "
            "IMPROVED. Ordering by it would select against the thing being "
            "optimised. Use sort='kd' (a triage prior — read the page's "
            "ranking_caveat) or sort='selectivity'."
        )
    return ordering.value


def _leaderboard_params(*, limit: int, offset: int, sort: LeaderboardSort | str,
                        max_kd_nM: float | None, cycle: int | None) -> dict[str, Any]:
    if not 1 <= int(limit) <= 500:
        raise LigandAIValidationError("limit must be between 1 and 500")
    if int(offset) < 0:
        raise LigandAIValidationError("offset must be >= 0")
    params: dict[str, Any] = {
        "limit": int(limit),
        "offset": int(offset),
        "sort": _sort_param(sort),
    }
    if max_kd_nM is not None:
        params["max_kd_nM"] = float(max_kd_nM)
    if cycle is not None:
        params["cycle"] = int(cycle)
    return params


def _start_body(spec: MaturationSpec,
                quote: CampaignQuote | dict[str, Any] | str | None) -> dict[str, Any]:
    """Build the start body, with the quote token only when one was accepted.

    ⛔ The token rides as ``quoteToken`` — the name the platform's shipped quote
    mechanism already uses — and is OMITTED entirely when no quote is passed. The
    FastAPI request model is ``extra="forbid"``, so an always-present
    ``quoteToken`` would 422 against the engine directly; omitting it keeps one
    client working against both the metered public route and the internal port.
    """
    body = spec.to_payload()
    if quote is not None:
        body["quoteToken"] = _quote_token(quote)
    return body


# ─── Resource — sync ──────────────────────────────────────────────────────────


class Maturation(Resource):
    """``client.maturation`` — the platform's binder maturation loop.

    ⛔ NOT ``client.campaigns``. That namespace is a LOCAL panel-folding helper
    that never calls ``/api/campaign/*``; the two share a word and nothing else.

    Example::

        from ligandai import LigandAI
        from ligandai.resources.maturation import (
            MaturationSpec, MaturationSettings, Objective, Receptor, Seed,
        )

        client = LigandAI(api_key="...")
        spec = MaturationSpec(
            name="BRD lead maturation",
            seeds=[Seed("DEWQRALLSYFPE", seed_id="lead_A")],
            target=Receptor(gene="BRDET1", receptor_sequence=seq,
                            pocket_residues=[31, 34, 38, 51],
                            avoid_residues=[77, 80]),
            objective=Objective(target_kd_nM=5.0, max_cycles=3),
            maturation=MaturationSettings(cycles=3, n_variants_per_seed=8),
        )
        run = client.maturation.start(spec)

        for event in run.stream():
            print(event.event, event.data.get("cycle"))

        page = run.leaderboard()
        for design in page.quotable:            # ⛔ .quotable, not all rows
            delta = design.parent_delta
            print(design.sequence, design.kd_nM,
                  "beat its same-cycle parent" if delta and delta.is_win else
                  (delta.uncomparable_reason if delta else "no parent"))
        print(page.ranking_caveat.note)
    """

    # -- start --------------------------------------------------------------

    def start(
        self,
        spec: MaturationSpec,
        *,
        quote: CampaignQuote | dict[str, Any] | str | None = None,
        idempotency_key: str | None = None,
    ) -> MaturationRun:
        """``POST /api/campaign/mature`` → 202. Returns a :class:`MaturationRun`.

        Args:
            spec: The validated :class:`MaturationSpec`. Everything it can refuse
                locally, it already refused before this call.
            quote: The accepted credit quote, when the deployment requires one.
                ⛔ Omitted from the wire entirely when ``None``, so the same call
                works against a metered public route and against an unmetered
                internal one. Nothing here ever refetches a quote: a price that
                moved needs a new acceptance and that is your decision.
            idempotency_key: Opts this POST into the transport's retry policy.
                ⛔ Without one a start is sent AT MOST ONCE — retrying a POST
                whose response was merely lost risks a second campaign, a second
                set of GPU lanes and a second charge.

        Raises:
            LigandAIValidationError: a coded 422 — the message names the rule.
            LigandAICreditError: 402, or below the campaign credit floor.
            LigandAIError: 409 (a campaign is already running), or a stale quote.
        """
        if not isinstance(spec, MaturationSpec):
            raise LigandAIValidationError(
                "spec must be a MaturationSpec. It validates locally what the "
                "server would otherwise refuse after you have already paid the "
                f"round trip; got {type(spec).__name__}."
            )
        body = _start_body(spec, quote)
        try:
            payload = self._transport.request(
                "POST", "/api/campaign/mature", json=body,
                idempotency_key=idempotency_key,
            )
        except LigandAIError as exc:
            _interpret_maturation_error(exc, accepted_quote=_quote_dict(quote) if quote else None)
            raise
        started = MaturationStarted.from_wire(payload)
        return MaturationRun(self, started.campaign_id, started=started, spec=spec)

    # -- read ---------------------------------------------------------------

    def status(self, campaign_id: str) -> MaturationStatus:
        """``GET /api/campaign/{id}/status``. Cheap; polling is not billed."""
        cid = _require_campaign_id(campaign_id)
        payload = self._transport.request("GET", f"/api/campaign/{cid}/status")
        return MaturationStatus.from_wire(payload, campaign_id=cid)

    def readout(self, campaign_id: str) -> MaturationReadout:
        """``GET /api/campaign/{id}/maturation`` — the per-cycle pairs and counters.

        ⛔ Read ``result.control_frame_valid`` before quoting a win count.
        ``total_wins_vs_parent`` is ``None`` — not a partial sum — when any cycle
        compared variants against parents that were not re-folded in that cycle.
        """
        cid = _require_campaign_id(campaign_id)
        try:
            payload = self._transport.request("GET", f"/api/campaign/{cid}/maturation")
        except LigandAIError as exc:
            _interpret_maturation_error(exc)
            raise
        return MaturationReadout.from_wire(payload, campaign_id=cid)

    def leaderboard(
        self,
        campaign_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        sort: LeaderboardSort | str = LeaderboardSort.KD,
        max_kd_nM: float | None = None,
        cycle: int | None = None,
    ) -> MaturationLeaderboard:
        """``GET /api/campaign/{id}/leaderboard`` — one page, caveat attached.

        ⛔ ``sort='kd'`` is the default and IS NOT AN AFFINITY RANKING. Read it as
        a triage prior. ⛔ ``sort='margin'`` is refused — see :func:`_sort_param`.
        """
        cid = _require_campaign_id(campaign_id)
        params = _leaderboard_params(
            limit=limit, offset=offset, sort=sort, max_kd_nM=max_kd_nM, cycle=cycle
        )
        payload = self._transport.request(
            "GET", f"/api/campaign/{cid}/leaderboard", params=params
        )
        return MaturationLeaderboard.from_wire(payload, campaign_id=cid, sort=params["sort"])

    def iter_designs(
        self,
        campaign_id: str,
        *,
        page_size: int = 100,
        max_designs: int | None = None,
        **kwargs: Any,
    ) -> Iterator[MaturedDesign]:
        """Every matured design across pages.

        ⛔ This yields rows one at a time, which strips the page object carrying
        :class:`AffinityRankingCaveat`. Use it for bulk export and filtering, NOT
        to build a "ranked by affinity" presentation — for that keep the page.
        """
        offset = int(kwargs.pop("offset", 0))
        emitted = 0
        while True:
            page = self.leaderboard(campaign_id, limit=page_size, offset=offset, **kwargs)
            if not page.designs:
                return
            for design in page.designs:
                yield design
                emitted += 1
                if max_designs is not None and emitted >= max_designs:
                    return
            offset += len(page.designs)
            if not page.has_more:
                return

    def structure(self, campaign_id: str, design_id: str) -> str:
        """``GET /api/campaign/{id}/designs/{design_id}/structure`` → PDB text.

        ⛔ This is the pose the design's quoted Kd was measured ON, not an
        arbitrary trajectory. A 404 means that design's pose never landed on
        disk, never that the campaign is hidden from you.
        """
        cid = _require_campaign_id(campaign_id)
        if not design_id:
            raise LigandAIValidationError("design_id is required")
        resp = self._transport.request(
            "GET", f"/api/campaign/{cid}/designs/{design_id}/structure", expect_json=False
        )
        return resp.text

    def list(self) -> tuple[MaturationStatus, ...]:
        """``GET /api/campaign`` — your campaigns, maturation and otherwise.

        Filter on :attr:`MaturationStatus.is_maturation`; ``mode`` is always
        present on the wire, so nothing here has to guess which kind a row is.
        """
        payload = self._transport.request("GET", "/api/campaign")
        data = _as_dict(payload, what="a campaign list")
        return tuple(
            MaturationStatus.from_wire(row)
            for row in (data.get("campaigns") or [])
            if isinstance(row, Mapping)
        )

    # -- control ------------------------------------------------------------

    def stop(self, campaign_id: str) -> MaturationStatus:
        """``POST /api/campaign/{id}/stop`` — halt at the NEXT cycle boundary.

        ⛔ This does not kill a GPU lane. Folds already in flight were paid for
        and are allowed to land and be scored; killing them throws away the
        credits and the measurement both.
        """
        cid = _require_campaign_id(campaign_id)
        try:
            payload = self._transport.request("POST", f"/api/campaign/{cid}/stop")
        except LigandAIError as exc:
            _interpret_maturation_error(exc)
            raise
        return MaturationStatus.from_wire(payload, campaign_id=cid)

    def stream(self, campaign_id: str) -> Iterator[MaturationEvent]:
        """``GET /api/campaign/{id}/stream`` — SSE, attachable at any time.

        A late subscriber receives ONE ``snapshot`` carrying the full status body,
        then live events, with a heartbeat every 20s. The stream closes after
        ``campaign_complete`` or ``error``.
        """
        cid = _require_campaign_id(campaign_id)
        name: str | None = None
        for line in self._transport.stream_lines("GET", f"/api/campaign/{cid}/stream"):
            evt, data = _sse_payload(line)
            if evt is not None:
                name = evt
                continue
            if data is None:
                continue
            yield MaturationEvent(event=name or "message", data=data)
            name = None

    def wait(
        self,
        campaign_id: str,
        *,
        poll_interval: float = 20.0,
        timeout: float | None = None,
    ) -> MaturationStatus:
        """Poll ``/status`` until the campaign reaches a terminal state.

        ⛔ A timeout here does NOT stop the campaign — it stops waiting. Call
        :meth:`stop` if you want the run to end.
        """
        started = time.monotonic()
        while True:
            state = self.status(campaign_id)
            if state.is_terminal:
                return state
            if timeout is not None and time.monotonic() - started >= timeout:
                raise TimeoutError(
                    f"campaign {campaign_id} was still {state.status!r} after "
                    f"{timeout}s. It is STILL RUNNING — waiting stopped, the "
                    "campaign did not. Call client.maturation.stop() to end it."
                )
            time.sleep(max(1.0, float(poll_interval)))


@dataclass
class MaturationRun:
    """A handle on one maturation campaign. Everything on :class:`Maturation`,
    bound to a campaign id so the id cannot drift between calls."""

    _resource: Maturation
    campaign_id: str
    started: MaturationStarted | None = None
    spec: MaturationSpec | None = None

    def status(self) -> MaturationStatus:
        return self._resource.status(self.campaign_id)

    def readout(self) -> MaturationReadout:
        return self._resource.readout(self.campaign_id)

    def leaderboard(self, **kwargs: Any) -> MaturationLeaderboard:
        return self._resource.leaderboard(self.campaign_id, **kwargs)

    def iter_designs(self, **kwargs: Any) -> Iterator[MaturedDesign]:
        return self._resource.iter_designs(self.campaign_id, **kwargs)

    def structure(self, design_id: str) -> str:
        return self._resource.structure(self.campaign_id, design_id)

    def stop(self) -> MaturationStatus:
        return self._resource.stop(self.campaign_id)

    def stream(self) -> Iterator[MaturationEvent]:
        return self._resource.stream(self.campaign_id)

    def wait(self, **kwargs: Any) -> MaturationStatus:
        return self._resource.wait(self.campaign_id, **kwargs)

    def __repr__(self) -> str:  # pragma: no cover - repr only
        return f"<MaturationRun {self.campaign_id}>"


# ─── Resource — async ─────────────────────────────────────────────────────────


class AsyncMaturation(AsyncResource):
    """Async mirror of :class:`Maturation`. Same contract, same refusals.

    Every method is the awaitable twin of its sync counterpart; read
    :class:`Maturation` for the rules, which are identical.
    """

    async def start(
        self,
        spec: MaturationSpec,
        *,
        quote: CampaignQuote | dict[str, Any] | str | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """``POST /api/campaign/mature`` → the ``campaign_id``.

        ⛔ Returns the id rather than a handle: the sync handle's convenience is
        chaining, and chaining awaits reads better at the call site than a handle
        whose every method must be awaited anyway.
        """
        if not isinstance(spec, MaturationSpec):
            raise LigandAIValidationError(
                f"spec must be a MaturationSpec; got {type(spec).__name__}"
            )
        body = _start_body(spec, quote)
        try:
            payload = await self._transport.request(
                "POST", "/api/campaign/mature", json=body,
                idempotency_key=idempotency_key,
            )
        except LigandAIError as exc:
            _interpret_maturation_error(exc, accepted_quote=_quote_dict(quote) if quote else None)
            raise
        return MaturationStarted.from_wire(payload).campaign_id

    async def status(self, campaign_id: str) -> MaturationStatus:
        cid = _require_campaign_id(campaign_id)
        payload = await self._transport.request("GET", f"/api/campaign/{cid}/status")
        return MaturationStatus.from_wire(payload, campaign_id=cid)

    async def readout(self, campaign_id: str) -> MaturationReadout:
        cid = _require_campaign_id(campaign_id)
        try:
            payload = await self._transport.request("GET", f"/api/campaign/{cid}/maturation")
        except LigandAIError as exc:
            _interpret_maturation_error(exc)
            raise
        return MaturationReadout.from_wire(payload, campaign_id=cid)

    async def leaderboard(
        self,
        campaign_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        sort: LeaderboardSort | str = LeaderboardSort.KD,
        max_kd_nM: float | None = None,
        cycle: int | None = None,
    ) -> MaturationLeaderboard:
        cid = _require_campaign_id(campaign_id)
        params = _leaderboard_params(
            limit=limit, offset=offset, sort=sort, max_kd_nM=max_kd_nM, cycle=cycle
        )
        payload = await self._transport.request(
            "GET", f"/api/campaign/{cid}/leaderboard", params=params
        )
        return MaturationLeaderboard.from_wire(payload, campaign_id=cid, sort=params["sort"])

    async def iter_designs(
        self,
        campaign_id: str,
        *,
        page_size: int = 100,
        max_designs: int | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[MaturedDesign]:
        offset = int(kwargs.pop("offset", 0))
        emitted = 0
        while True:
            page = await self.leaderboard(campaign_id, limit=page_size, offset=offset, **kwargs)
            if not page.designs:
                return
            for design in page.designs:
                yield design
                emitted += 1
                if max_designs is not None and emitted >= max_designs:
                    return
            offset += len(page.designs)
            if not page.has_more:
                return

    async def structure(self, campaign_id: str, design_id: str) -> str:
        cid = _require_campaign_id(campaign_id)
        if not design_id:
            raise LigandAIValidationError("design_id is required")
        resp = await self._transport.request(
            "GET", f"/api/campaign/{cid}/designs/{design_id}/structure", expect_json=False
        )
        return resp.text

    async def list(self) -> tuple[MaturationStatus, ...]:
        payload = await self._transport.request("GET", "/api/campaign")
        data = _as_dict(payload, what="a campaign list")
        return tuple(
            MaturationStatus.from_wire(row)
            for row in (data.get("campaigns") or [])
            if isinstance(row, Mapping)
        )

    async def stop(self, campaign_id: str) -> MaturationStatus:
        cid = _require_campaign_id(campaign_id)
        try:
            payload = await self._transport.request("POST", f"/api/campaign/{cid}/stop")
        except LigandAIError as exc:
            _interpret_maturation_error(exc)
            raise
        return MaturationStatus.from_wire(payload, campaign_id=cid)

    async def stream(self, campaign_id: str) -> AsyncIterator[MaturationEvent]:
        cid = _require_campaign_id(campaign_id)
        name: str | None = None
        async for line in self._transport.stream_lines("GET", f"/api/campaign/{cid}/stream"):
            evt, data = _sse_payload(line)
            if evt is not None:
                name = evt
                continue
            if data is None:
                continue
            yield MaturationEvent(event=name or "message", data=data)
            name = None

    async def wait(
        self,
        campaign_id: str,
        *,
        poll_interval: float = 20.0,
        timeout: float | None = None,
    ) -> MaturationStatus:
        import asyncio

        started = time.monotonic()
        while True:
            state = await self.status(campaign_id)
            if state.is_terminal:
                return state
            if timeout is not None and time.monotonic() - started >= timeout:
                raise TimeoutError(
                    f"campaign {campaign_id} was still {state.status!r} after "
                    f"{timeout}s. It is STILL RUNNING — waiting stopped, the "
                    "campaign did not."
                )
            await asyncio.sleep(max(1.0, float(poll_interval)))
