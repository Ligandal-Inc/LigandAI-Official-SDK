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
from ligandai.errors import LigandAITargetingWarning, LigandAIValidationError

__all__ = [
    "normalize_region",
    "validate_and_normalize_targeting",
    "validate_avoid_shell_angstrom",
    "validate_hotspot_residues",
    "validate_region_list",
]


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
        warnings.warn(
            f"{field}[{index}]: start ({start}) > end ({end}) — swapped "
            f"automatically to match server behaviour. Pass start <= end to "
            f"silence this warning.",
            LigandAITargetingWarning,
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
) -> list[dict[str, Any]] | None:
    """Validate + normalize a ``target_residues`` / ``avoid_residues`` list.

    Accepts ``None``, a bare single region (dict/int/ResidueRange), or a
    list of any mix of accepted shapes. Returns ``None`` for ``None`` input,
    otherwise a list of canonical ``{chain,start,end}`` dicts.
    """
    if regions is None:
        return None
    if isinstance(regions, (dict, int, float)) or hasattr(regions, "model_dump"):
        regions = [regions]
    return [
        normalize_region(r, field=field, index=i, max_span=max_span)
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


def validate_avoid_shell_angstrom(value: Any) -> float | None:
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
        warnings.warn(
            "avoid_shell_angstrom=0 disables the packing shell — only the "
            "named avoid_residues are excluded, not their surrounding "
            f"contacts (server default is {DEFAULT_AVOID_SHELL_ANGSTROM:g} A).",
            LigandAITargetingWarning,
            stacklevel=3,
        )
    return v


def _region_span(region: dict[str, Any]) -> int:
    return int(region["end"]) - int(region["start"]) + 1


def _warn_missing_chain_in_multichain_context(
    target: list[dict[str, Any]] | None,
    avoid: list[dict[str, Any]] | None,
) -> None:
    """WARN: a region omitted ``chain`` while other regions in the same call
    named more than one distinct chain — ambiguous which chain it applies to.
    """
    all_regions = list(target or []) + list(avoid or [])
    chains_named = {r["chain"] for r in all_regions if r.get("chain") is not None}
    if len(chains_named) <= 1:
        return
    for field, regions in (("target_residues", target), ("avoid_residues", avoid)):
        for i, r in enumerate(regions or []):
            if r.get("chain") is None:
                warnings.warn(
                    f"{field}[{i}]: no chain given, but this request also names "
                    f"multiple chains ({sorted(chains_named)}) — this region will "
                    f"match any chain with that residue number, which may not be "
                    f"what you want. Pass chain= to disambiguate.",
                    LigandAITargetingWarning,
                    stacklevel=4,
                )


def _warn_degenerate_pocket_risk(
    target: list[dict[str, Any]] | None,
    avoid: list[dict[str, Any]] | None,
) -> None:
    """WARN: total avoid span >= total target span — likely to leave nothing."""
    if not target or not avoid:
        return
    target_span = sum(_region_span(r) for r in target)
    avoid_span = sum(_region_span(r) for r in avoid)
    if avoid_span >= target_span:
        warnings.warn(
            f"avoid_residues spans {avoid_span} residues, which is >= the "
            f"{target_span}-residue span of target_residues — this is likely "
            f"to leave a degenerate (near-empty) design pocket. Check for "
            f"overlapping/transposed ranges.",
            LigandAITargetingWarning,
            stacklevel=4,
        )


def validate_and_normalize_targeting(
    *,
    target_residues: Any = None,
    avoid_residues: Any = None,
    hotspot_residues: Any = None,
    avoid_shell_angstrom: Any = None,
) -> dict[str, Any]:
    """Validate every targeting field for one generation call, in one place.

    Call this BEFORE building the HTTP request body and BEFORE any HTTP call
    — every hard-error rule below raises :class:`LigandAIValidationError`
    client-side rather than letting a malformed request reach the server (or
    worse, silently no-op there). Non-fatal issues are emitted via
    :class:`~ligandai.errors.LigandAITargetingWarning`.

    Returns
    -------
    dict
        ``{"target_residues": [...] | None, "avoid_residues": [...] | None,
        "hotspot_residues": [...] | None, "avoid_shell_angstrom": float | None}``
        — normalized, canonical-shape values ready to serialize onto the wire.
    """
    norm_target = validate_region_list(target_residues, field="target_residues")
    norm_avoid = validate_region_list(avoid_residues, field="avoid_residues")
    norm_hotspots = validate_hotspot_residues(hotspot_residues)
    norm_shell = validate_avoid_shell_angstrom(avoid_shell_angstrom)

    _warn_missing_chain_in_multichain_context(norm_target, norm_avoid)
    _warn_degenerate_pocket_risk(norm_target, norm_avoid)

    return {
        "target_residues": norm_target,
        "avoid_residues": norm_avoid,
        "hotspot_residues": norm_hotspots,
        "avoid_shell_angstrom": norm_shell,
    }
