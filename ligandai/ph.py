# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""pH-switch helpers for pocket-conditioned generation.

``bind_ph`` / ``release_ph`` are first-class generate arguments (v0.16.4+).
They travel on the wire as ``phBind`` / ``phAvoid`` (challenge activity schema)
and as ``bindPh`` / ``releasePh`` (challenge spec vocabulary) so both the
challenge preview and the main Autofold path can record the requested pair.

These values are **screening objectives on the request**, not measured binding
selectivity. Folding at both pH conditions (or experimental readout) is still
required to establish a switch.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "validate_ph_pair",
    "ph_generation_fields",
    "henderson_hasselbalch_fraction",
]


def henderson_hasselbalch_fraction(pka: float, ph: float) -> float:
    """Predicted protonated fraction for a monoprotic acid at ``ph`` given ``pka``."""
    return 1.0 / (1.0 + 10 ** (ph - pka))


def validate_ph_pair(
    bind_ph: float | None,
    release_ph: float | None,
    *,
    lo: float = 5.0,
    hi: float = 8.0,
) -> tuple[float, float] | None:
    """Validate a bind/release pH pair.

    Returns ``(bind_ph, release_ph)`` when both are provided and valid, or
    ``None`` when both are omitted. Raises ``ValueError`` on partial or
    out-of-range input, or when the two values are equal (no switch).
    """
    if bind_ph is None and release_ph is None:
        return None
    if bind_ph is None or release_ph is None:
        raise ValueError(
            "bind_ph and release_ph must be supplied together "
            "(or both omitted). A single pH is not a switch objective."
        )
    b, r = float(bind_ph), float(release_ph)
    for name, value in (("bind_ph", b), ("release_ph", r)):
        if not (lo <= value <= hi):
            raise ValueError(f"{name}={value} is outside the supported range [{lo}, {hi}].")
    if b == r:
        raise ValueError(
            f"bind_ph and release_ph are both {b}; choose distinct bind and release conditions."
        )
    return b, r


def ph_generation_fields(
    bind_ph: float | None = None,
    release_ph: float | None = None,
    *,
    enabled: bool | None = None,
    mechanism: str = "interface_protonation",
) -> dict[str, Any]:
    """Build the pH fields to merge into a generation request body.

    Empty dict when both pH values are omitted. Otherwise emits:
    - ``phEnabled``, ``phBind``, ``phAvoid`` (challenge activity / preview)
    - ``bindPh``, ``releasePh`` (challenge spec vocabulary)
    - ``phMechanism`` when ``mechanism`` is set
    """
    pair = validate_ph_pair(bind_ph, release_ph)
    if pair is None:
        return {}
    b, r = pair
    on = True if enabled is None else bool(enabled)
    out: dict[str, Any] = {
        "phEnabled": on,
        "phBind": b,
        "phAvoid": r,
        "bindPh": b,
        "releasePh": r,
    }
    if mechanism:
        out["phMechanism"] = mechanism
    return out
