# Copyright © 2025 Ligandal, Inc. All rights reserved.
"""Client-side validation for PDB-residue-numbered targeting fields.

[bd-LIGANDAI_ALPHA_V2-x4vt8]

``target_residues`` / ``avoid_residues`` / ``hotspot_residues`` are **PDB
residue numbers as numbered in the uploaded/resolved structure** — never
0-based indices into the pocket-feature array, and never sequence positions.
``chain`` is part of the identity of a residue and must never be dropped.

A P0 customer bug (bead x4vt8) shipped because the server briefly treated
``start``/``end`` as array indices: a customer excluded BRD4-ET loop residues
36-46 and 25/25 designed peptides bound that exact loop. The server-side fix
lives in ``fastapi_backend/core/residue_targeting.py`` (authoritative,
read-only reference for this module — do not diverge from its accepted
shapes). This module makes the same mistake impossible to make *through the
SDK*: it validates every targeting field BEFORE any HTTP call, using the
exact hard-error rules and message strings from the x4vt8 contract (§4/§6),
so a caller who passes ``start=0`` gets a loud, actionable
:class:`~ligandai.errors.LigandAIValidationError` instead of a silent no-op.

The Python and Node SDKs are required to raise byte-identical messages for
the same input (contract §6, "Parity requirement") — do not edit the message
strings here without making the matching edit in the Node SDK.

Every non-fatal condition below is emitted TWICE, on purpose
[bd-LIGANDAI_ALPHA_V2-x4vt8.3.2]:

1. as a :class:`~ligandai.errors.LigandAITargetingWarning` on the ``warnings``
   channel — so a caller who never inspects anything still gets told; and
2. as a structured :class:`~ligandai.diagnostics.Diagnostic` on
   :attr:`TargetingValidationResult.diagnostics` — so a caller CAN branch on it
   programmatically (``pytest.warns`` is not an API). This is the Python
   counterpart of the Node SDK's ``TargetingValidationResult.warnings``.

The warning channel's behaviour is unchanged: existing callers relying on
``pytest.warns(LigandAITargetingWarning)`` or
``warnings.simplefilter("error", ...)`` keep working exactly as before.
"""

from __future__ import annotations

import warnings
from typing import Any

from ligandai._constants import (
    DEFAULT_AVOID_SHELL_ANGSTROM,
    MAX_AVOID_SHELL_ANGSTROM,
    MAX_REGION_SPAN_RESIDUES,
    MIN_AVOID_SHELL_ANGSTROM,
)
from ligandai.diagnostics import Diagnostic, make_diagnostic
from ligandai.errors import LigandAITargetingWarning, LigandAIValidationError

__all__ = [
    "TargetingValidationResult",
    "normalize_region",
    "validate_and_normalize_goal_targeting",
    "validate_and_normalize_targeting",
    "validate_avoid_shell_angstrom",
    "validate_hotspot_residues",
    "validate_region_list",
]


class TargetingValidationResult(dict):
    """Normalized targeting fields **plus** the structured diagnostics raised
    while validating them.  [bd-LIGANDAI_ALPHA_V2-x4vt8.3.2]

    Subclasses :class:`dict` and carries exactly the same four keys as before
    (``target_residues``, ``avoid_residues``, ``hotspot_residues``,
    ``avoid_shell_angstrom``), so every existing caller and equality assertion
    is unaffected. The diagnostics hang off an *attribute*, not a key::

        result = validate_and_normalize_targeting(
            target_residues=[{"chain": "A", "start": 1, "end": 10}],
            avoid_residues=[{"chain": "A", "start": 1, "end": 10}],
        )
        for d in result.diagnostics:
            print(d.code, d.severity, d.message)

    This is the Python parity surface for the Node SDK's
    ``TargetingValidationResult.warnings``.
    """

    #: Structured diagnostics raised during validation, in emission order.
    diagnostics: list[Diagnostic]

    def __init__(
        self,
        values: dict[str, Any] | None = None,
        *,
        diagnostics: list[Diagnostic] | None = None,
    ) -> None:
        super().__init__(values or {})
        self.diagnostics = list(diagnostics or [])

    def __repr__(self) -> str:
        return f"TargetingValidationResult({dict(self)!r}, diagnostics={self.diagnostics!r})"


def _emit(
    sink: list[Diagnostic] | None,
    code: str,
    message: str,
    details: dict[str, Any],
    *,
    stacklevel: int,
) -> None:
    """Raise a targeting warning AND record its structured form.

    ``sink`` is ``None`` when a validator is called standalone (the public
    helpers are callable on their own) — the warning still fires; only the
    structured copy is dropped, because there is nowhere to put it.
    """
    warnings.warn(message, LigandAITargetingWarning, stacklevel=stacklevel + 1)
    if sink is not None:
        sink.append(make_diagnostic(code, message, details))


def _is_intlike(value: Any) -> bool:
    """True for a real integer value: ``int`` (never ``bool``), or a ``float``
    with no fractional part and not NaN/inf. False for strings, ``None``, etc.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
            return False
        return value.is_integer()
    return False


def _shape_error(field: str, index: int, region: Any) -> LigandAIValidationError:
    return LigandAIValidationError(
        f"{field}[{index}]: must be an object with start/end (or "
        f"startResidue/endResidue), a single residue, or an integer residue "
        f"number; got {type(region).__name__}."
    )


def normalize_region(
    region: Any,
    *,
    field: str,
    index: int,
    max_span: int = MAX_REGION_SPAN_RESIDUES,
    diagnostics: list[Diagnostic] | None = None,
) -> dict[str, Any]:
    """Validate and normalize ONE region entry to canonical ``{chain,start,end}``.

    Accepts every shape ``residue_targeting.normalize_regions()`` accepts:
    ``{chain,start,end}`` | ``{chain,startResidue,endResidue}`` |
    ``{chain,residue}`` | ``{chain,resnum}`` | a bare int/float residue
    number. A pydantic model (e.g. :class:`~ligandai.types.ResidueRange`) is
    accepted via ``.model_dump()`` duck-typing so this module never has to
    import ``ligandai.types`` (avoids a circular import).

    Raises :class:`~ligandai.errors.LigandAIValidationError` (hard, before
    any HTTP call) on:

    - unrecognized shape / missing start+end (and no residue/resnum/int form)
    - start or end not integer-valued (float w/ fraction, NaN, str, bool)
    - start or end < 1 (the array-index mistake this module exists to kill)
    - ``chain`` present but not a 1-2 character alphanumeric identifier
    - region spans more than ``max_span`` residues

    Emits :class:`~ligandai.errors.LigandAITargetingWarning` (non-fatal) when
    ``start > end`` — the pair is auto-swapped to match server behaviour, but
    the warning is mandatory per contract §4.

    Returns the canonical ``{"chain": ..., "start": ..., "end": ...}`` dict
    (``chain`` omitted when the caller didn't supply one).

    ``diagnostics``, when supplied, receives the structured
    :class:`~ligandai.diagnostics.Diagnostic` form of any warning raised here.
    """
    r: Any = region
    if hasattr(r, "model_dump") and not isinstance(r, dict):
        r = r.model_dump()

    chain: Any = None
    start_raw: Any = None
    end_raw: Any = None

    if isinstance(r, bool):
        raise _shape_error(field, index, region)
    if isinstance(r, (int, float)):
        start_raw = end_raw = r
    elif isinstance(r, dict):
        chain = r.get("chain")
        if chain == "":
            # Mirrors residue_targeting.normalize_regions(): an explicit empty
            # string means "no chain given", not an invalid one-char chain.
            chain = None
        if "start" in r or "end" in r:
            start_raw = r.get("start")
            end_raw = r.get("end")
        elif "startResidue" in r or "endResidue" in r:
            start_raw = r.get("startResidue")
            end_raw = r.get("endResidue")
        elif "residue" in r and r.get("residue") is not None:
            start_raw = end_raw = r.get("residue")
        elif "resnum" in r and r.get("resnum") is not None:
            start_raw = end_raw = r.get("resnum")
        else:
            raise _shape_error(field, index, region)
        if start_raw is None and end_raw is None:
            raise _shape_error(field, index, region)
    else:
        raise _shape_error(field, index, region)

    # start/end must be present and integer-valued before anything else.
    if start_raw is None:
        start_raw = end_raw
    if end_raw is None:
        end_raw = start_raw
    if not _is_intlike(start_raw) or not _is_intlike(end_raw):
        raise LigandAIValidationError(
            f"{field}[{index}]: start ({start_raw}) and end ({end_raw}) must be integers."
        )
    start = int(start_raw)
    end = int(end_raw)

    # The whole point of the bead: 0/negative is the array-index mistake.
    if start < 1:
        raise LigandAIValidationError(
            f"{field}[{index}]: residue numbers are PDB numbering and start at 1; "
            f"got start={start}. If you meant an array index, that is not "
            f"supported — pass the residue number shown in the structure."
        )
    if end < 1:
        raise LigandAIValidationError(
            f"{field}[{index}]: residue numbers are PDB numbering and start at 1; "
            f"got start={end}. If you meant an array index, that is not "
            f"supported — pass the residue number shown in the structure."
        )

    if start > end:
        _emit(
            diagnostics,
            "targeting_region_swapped",
            f"{field}[{index}]: start ({start}) > end ({end}) — swapped "
            f"automatically to match server behaviour. Pass start <= end to "
            f"silence this warning.",
            {"field": field, "index": index, "start": start, "end": end},
            stacklevel=3,
        )
        start, end = end, start

    if chain is not None and (
        not isinstance(chain, str) or not (1 <= len(chain) <= 2) or not chain.isalnum()
    ):
        raise LigandAIValidationError(
            f"{field}[{index}]: chain must be a 1-2 character identifier; got {chain!r}."
        )

    span = end - start + 1
    if span > max_span:
        raise LigandAIValidationError(
            f"{field}[{index}]: region spans {span} residues, which exceeds "
            f"the {max_span}-residue sanity limit."
        )

    out: dict[str, Any] = {"start": start, "end": end}
    if chain is not None:
        out["chain"] = chain
    return out


def validate_region_list(
    regions: Any,
    *,
    field: str,
    max_span: int = MAX_REGION_SPAN_RESIDUES,
    diagnostics: list[Diagnostic] | None = None,
) -> list[dict[str, Any]] | None:
    """Validate + normalize a ``target_residues`` / ``avoid_residues`` list.

    Accepts ``None``, a bare single region (dict/int/ResidueRange), or a
    list of any mix of accepted shapes. Returns ``None`` for ``None`` input,
    otherwise a list of canonical ``{chain,start,end}`` dicts.

    ``diagnostics``, when supplied, receives the structured
    :class:`~ligandai.diagnostics.Diagnostic` form of any warning raised here.
    """
    if regions is None:
        return None
    if isinstance(regions, (dict, int, float)) or hasattr(regions, "model_dump"):
        regions = [regions]
    return [
        normalize_region(r, field=field, index=i, max_span=max_span, diagnostics=diagnostics)
        for i, r in enumerate(regions)
    ]


def validate_hotspot_residues(hotspots: Any) -> list[int] | None:
    """Validate + normalize ``hotspot_residues`` — a flat list of PDB resnums.

    Wire shape (contract §2) is ``Optional[List[int]]`` — a bare int/float is
    the expected per-entry shape. A ``{"chain": ..., "residue": ...}`` /
    ``{"resnum": ...}`` dict is also accepted for consistency with the other
    targeting fields (only the residue number is kept; ``chain`` is not part
    of this field's wire shape). Unlike :func:`normalize_region`, a single
    hotspot has no ``end`` — it is always exactly one residue, so this does
    NOT go through the start/end range machinery.
    """
    if hotspots is None:
        return None
    if isinstance(hotspots, (int, float)) and not isinstance(hotspots, bool):
        hotspots = [hotspots]
    field = "hotspot_residues"
    out: list[int] = []
    for i, h in enumerate(hotspots):
        value: Any = h
        if hasattr(value, "model_dump") and not isinstance(value, dict):
            value = value.model_dump()
        if isinstance(value, dict):
            value = value.get("residue", value.get("resnum"))
        if isinstance(value, bool) or not _is_intlike(value):
            raise _shape_error(field, i, h)
        v = int(value)
        if v < 1:
            raise LigandAIValidationError(
                f"{field}[{i}]: residue numbers are PDB numbering and start at 1; "
                f"got {v}. If you meant an array index, that is not "
                f"supported — pass the residue number shown in the structure."
            )
        out.append(v)
    return out


def validate_avoid_shell_angstrom(
    value: Any,
    *,
    diagnostics: list[Diagnostic] | None = None,
) -> float | None:
    """Validate ``avoid_shell_angstrom``. ``None`` means "use the server
    default" (:data:`~ligandai._constants.DEFAULT_AVOID_SHELL_ANGSTROM`) and
    passes through unchanged — the SDK does not inject the default onto the
    wire so a legacy server keeps its own default unchanged.

    Raises on non-numeric input or a value outside ``[0, 50]``. Warns (does
    not raise) when the shell is explicitly disabled (``0``) — legal, but
    means only the named avoid residues are excluded, not their packing
    neighbours.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LigandAIValidationError(
            f"avoid_shell_angstrom must be between {MIN_AVOID_SHELL_ANGSTROM:g} "
            f"and {MAX_AVOID_SHELL_ANGSTROM:g} Angstrom; got {value!r}."
        )
    v = float(value)
    if v != v or not (MIN_AVOID_SHELL_ANGSTROM <= v <= MAX_AVOID_SHELL_ANGSTROM):
        raise LigandAIValidationError(
            f"avoid_shell_angstrom must be between {MIN_AVOID_SHELL_ANGSTROM:g} "
            f"and {MAX_AVOID_SHELL_ANGSTROM:g} Angstrom; got {value!r}."
        )
    if v == 0:
        _emit(
            diagnostics,
            "avoid_shell_disabled",
            "avoid_shell_angstrom=0 disables the packing shell — only the "
            "named avoid_residues are excluded, not their surrounding "
            f"contacts (server default is {DEFAULT_AVOID_SHELL_ANGSTROM:g} A).",
            {
                "avoid_shell_angstrom": 0.0,
                "server_default": float(DEFAULT_AVOID_SHELL_ANGSTROM),
            },
            stacklevel=3,
        )
    return v


def _region_span(region: dict[str, Any]) -> int:
    return int(region["end"]) - int(region["start"]) + 1


def _warn_missing_chain_in_multichain_context(
    target: list[dict[str, Any]] | None,
    avoid: list[dict[str, Any]] | None,
    *,
    target_field: str = "target_residues",
    avoid_field: str = "avoid_residues",
    diagnostics: list[Diagnostic] | None = None,
) -> None:
    """WARN: a region omitted ``chain`` while other regions in the same call
    named more than one distinct chain — ambiguous which chain it applies to.

    ``target_field`` / ``avoid_field`` name the caller's own parameters so the
    message is accurate on both the ``Peptides.generate`` surface
    (``target_residues``) and the ``Goals.campaign_start`` surface
    (``hotspots``).
    """
    all_regions = list(target or []) + list(avoid or [])
    chains_named = {r["chain"] for r in all_regions if r.get("chain") is not None}
    if len(chains_named) <= 1:
        return
    for field, regions in ((target_field, target), (avoid_field, avoid)):
        for i, r in enumerate(regions or []):
            if r.get("chain") is None:
                _emit(
                    diagnostics,
                    "targeting_chain_ambiguous",
                    f"{field}[{i}]: no chain given, but this request also names "
                    f"multiple chains ({sorted(chains_named)}) — this region will "
                    f"match any chain with that residue number, which may not be "
                    f"what you want. Pass chain= to disambiguate.",
                    {"field": field, "index": i, "chains_named": sorted(chains_named)},
                    stacklevel=4,
                )


def _warn_degenerate_pocket_risk(
    target: list[dict[str, Any]] | None,
    avoid: list[dict[str, Any]] | None,
    *,
    target_field: str = "target_residues",
    avoid_field: str = "avoid_residues",
    diagnostics: list[Diagnostic] | None = None,
) -> None:
    """WARN: total avoid span >= total target span — likely to leave nothing.

    This is the CLIENT-SIDE, pre-flight cousin of the server's
    ``degenerate_pocket_after_avoid``: it is a span heuristic on what the caller
    typed, not a measurement of the featurized pocket, so it carries its own
    code (``degenerate_pocket_risk``) and a lower severity.
    """
    if not target or not avoid:
        return
    target_span = sum(_region_span(r) for r in target)
    avoid_span = sum(_region_span(r) for r in avoid)
    if avoid_span >= target_span:
        _emit(
            diagnostics,
            "degenerate_pocket_risk",
            f"{avoid_field} spans {avoid_span} residues, which is >= the "
            f"{target_span}-residue span of {target_field} — this is likely "
            f"to leave a degenerate (near-empty) design pocket. Check for "
            f"overlapping/transposed ranges.",
            {
                "target_field": target_field,
                "avoid_field": avoid_field,
                "target_span": target_span,
                "avoid_span": avoid_span,
            },
            stacklevel=4,
        )


def _residue_keys(regions: list[dict[str, Any]] | None) -> dict[tuple[str, int], None]:
    """Every ``(chain, resnum)`` a region list covers, order-preserving.

    ``chain`` is ``""`` when the caller did not name one — an unchained region
    is deliberately NOT treated as matching a chained one, because "residue 745"
    and "chain A residue 745" are different assertions and guessing which the
    caller meant is how the original bug happened.
    """
    out: dict[tuple[str, int], None] = {}
    for r in regions or []:
        chain = str(r.get("chain") or "")
        for resnum in range(int(r["start"]), int(r["end"]) + 1):
            out[(chain, resnum)] = None
    return out


def _warn_targeting_contradiction(
    target: list[dict[str, Any]] | None,
    avoid: list[dict[str, Any]] | None,
    *,
    target_field: str,
    avoid_field: str = "avoid_residues",
    diagnostics: list[Diagnostic] | None = None,
) -> None:
    """WARN: a residue is in BOTH the must-contact set and the must-avoid set.

    Worth catching precisely BECAUSE neither list constrains generation: nothing
    downstream rejects a contradictory request, so it would run to completion and
    then have every design discarded by the post-fold contact filter. The client
    is the only place this is cheap to notice.

    Only meaningful where the "target" side is a hard contact requirement (the
    goal-campaign ``hotspots`` field). On ``Peptides.generate`` the equivalent
    overlap is normal and intended — excluding a sub-region of the targeted face
    is the whole point of ``avoid_residues`` — so this is NOT run there.
    """
    if not target or not avoid:
        return
    avoid_keys = _residue_keys(avoid)
    clashing = [key for key in _residue_keys(target) if key in avoid_keys]
    if not clashing:
        return
    rendered = [f"{chain + ':' if chain else ''}{resnum}" for chain, resnum in clashing]
    _emit(
        diagnostics,
        "targeting_contradiction",
        f"{len(rendered)} residue(s) appear in BOTH {target_field} and {avoid_field} "
        f"({', '.join(rendered)}) — a design cannot both contact and avoid the same "
        f"residue. Neither list constrains generation, so nothing downstream would "
        f"reject this: the run would complete and the post-fold contact filter would "
        f"discard all of it. Remove the overlap from one list.",
        {
            "target_field": target_field,
            "avoid_field": avoid_field,
            "contradicting_residues": rendered,
        },
        stacklevel=4,
    )


def validate_and_normalize_targeting(
    *,
    target_residues: Any = None,
    avoid_residues: Any = None,
    hotspot_residues: Any = None,
    avoid_shell_angstrom: Any = None,
) -> TargetingValidationResult:
    """Validate every targeting field for one generation call, in one place.

    Call this BEFORE building the HTTP request body and BEFORE any HTTP call
    — every hard-error rule below raises :class:`LigandAIValidationError`
    client-side rather than letting a malformed request reach the server (or
    worse, silently no-op there). Non-fatal issues are emitted via
    :class:`~ligandai.errors.LigandAITargetingWarning`.

    Returns
    -------
    TargetingValidationResult
        A ``dict`` subclass with the same four keys as always
        (``{"target_residues": [...] | None, "avoid_residues": [...] | None,
        "hotspot_residues": [...] | None, "avoid_shell_angstrom": float | None}``)
        — normalized, canonical-shape values ready to serialize onto the wire —
        plus a ``.diagnostics`` attribute holding the structured
        :class:`~ligandai.diagnostics.Diagnostic` form of every warning raised
        (the Node SDK's ``TargetingValidationResult.warnings`` parity surface).
        [bd-LIGANDAI_ALPHA_V2-x4vt8.3.2]
    """
    diagnostics: list[Diagnostic] = []
    norm_target = validate_region_list(
        target_residues, field="target_residues", diagnostics=diagnostics
    )
    norm_avoid = validate_region_list(
        avoid_residues, field="avoid_residues", diagnostics=diagnostics
    )
    norm_hotspots = validate_hotspot_residues(hotspot_residues)
    norm_shell = validate_avoid_shell_angstrom(avoid_shell_angstrom, diagnostics=diagnostics)

    _warn_missing_chain_in_multichain_context(norm_target, norm_avoid, diagnostics=diagnostics)
    _warn_degenerate_pocket_risk(norm_target, norm_avoid, diagnostics=diagnostics)

    return TargetingValidationResult(
        {
            "target_residues": norm_target,
            "avoid_residues": norm_avoid,
            "hotspot_residues": norm_hotspots,
            "avoid_shell_angstrom": norm_shell,
        },
        diagnostics=diagnostics,
    )


def validate_and_normalize_goal_targeting(
    *,
    hotspots: Any = None,
    avoid_residues: Any = None,
    avoid_shell_angstrom: Any = None,
) -> TargetingValidationResult:
    """Goal-campaign twin of :func:`validate_and_normalize_targeting`.
    [bd-LIGANDAI_ALPHA_V2-x4vt8.3.2]

    ``Goals.campaign_start()`` / ``Goals.drive()`` used to call
    :func:`validate_region_list` and :func:`validate_avoid_shell_angstrom`
    piecemeal, which meant the goal path silently skipped the two CROSS-FIELD
    warnings that only the aggregate entry point runs: the missing-``chain``
    ambiguity check and the degenerate-pocket-risk check. The goal-campaign
    engine calls the same ``/api/generate/stream`` pocket machinery as
    ``Peptides.generate``, so both conditions apply there identically — the
    warnings were missing, not inapplicable.

    ``hotspots`` plays the role ``target_residues`` plays on the generate
    surface (the residues the generator is SHOWN — naming them changes which
    sequences come out, but does not aim the design at them), so it is passed
    as the "target" side of both cross-field checks. Messages name the caller's
    own parameter (``hotspots``), not ``target_residues``.

    Returns the same :class:`TargetingValidationResult` shape, keyed
    ``{"hotspots", "avoid_residues", "avoid_shell_angstrom"}``. Callers keep
    sending their ORIGINAL dicts on the wire — this validates and warns; it
    does not rewrite the request body.
    """
    diagnostics: list[Diagnostic] = []
    norm_hotspots = validate_region_list(hotspots, field="hotspots", diagnostics=diagnostics)
    norm_avoid = validate_region_list(
        avoid_residues, field="avoid_residues", diagnostics=diagnostics
    )
    norm_shell = validate_avoid_shell_angstrom(avoid_shell_angstrom, diagnostics=diagnostics)

    _warn_missing_chain_in_multichain_context(
        norm_hotspots, norm_avoid, target_field="hotspots", diagnostics=diagnostics
    )
    # NOTE: the degenerate-pocket SPAN heuristic from
    # validate_and_normalize_targeting is deliberately NOT run here. On the
    # generate surface ``target_residues`` is a contiguous pocket FACE, so
    # "avoid span >= target span" means the face is gone. On the goal surface
    # ``hotspots`` are point CONTACT CRITERIA, not a pocket definition — a
    # campaign with 2 hotspots and 6 excluded orthosteric residues is completely
    # ordinary, and firing there would put a false positive on the diagnostics
    # channel every run. Training users to filter LigandAITargetingWarning is
    # the one outcome this bead cannot afford. The contradiction check below is
    # the goal-surface equivalent that IS always meaningful.
    _warn_targeting_contradiction(
        norm_hotspots, norm_avoid, target_field="hotspots", diagnostics=diagnostics
    )

    return TargetingValidationResult(
        {
            "hotspots": norm_hotspots,
            "avoid_residues": norm_avoid,
            "avoid_shell_angstrom": norm_shell,
        },
        diagnostics=diagnostics,
    )
