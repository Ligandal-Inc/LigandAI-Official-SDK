# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Client-side species / organism selection for the LIGANDAI SDK.

This mirrors the SERVER contract in ``server/species-targeting-entitlement.ts``
(and the receptordb SDK's ``species.py``):

* The supported species enum is exactly ``human`` | ``mouse`` (default ``human``).
* The species selector is an *entitled capability*: ``superadmin`` OR an org
  granted ``species_targeting``.  The server is the single source of truth — the
  client's choice is advisory and is re-validated server-side before it reaches
  any bridge.
* **Fail-closed**: a non-entitled caller can NEVER force a non-default species.
  The client therefore coerces ``mouse`` -> ``human`` locally when it knows it is
  not entitled (from ``GET /api/cross-species/entitlement``), so the SDK's own
  results/labels match what the server will actually do — it does not merely
  send ``mouse`` and hope the server rejects it.

The normalization here is byte-for-byte compatible with the server's
``normalizeSpecies`` (``mus_musculus`` / ``mmu`` / ``mus musculus`` all map to
``mouse``; everything else maps to ``human``).

Note: this is a *targeting-namespace* selector for the transcriptomics /
gene / isoform funnel (human vs mouse atlas + orthologs). It is distinct from
the per-structure ``species`` lookup on ``client.structures`` (which spans
human / mouse / rat / cyno for individual UniProt structure resolution). The
two do not collide — ``structures`` keeps its free-form ``species=`` kwarg.
"""

from __future__ import annotations

from typing import Literal

Species = Literal["human", "mouse"]

DEFAULT_SPECIES: Species = "human"

# Capability id — matches SPECIES_TARGETING_CAPABILITY on the server.
SPECIES_TARGETING_CAPABILITY = "species_targeting"

# Aliases accepted for "mouse" — kept in sync with the server's normalizeSpecies.
_MOUSE_ALIASES = frozenset(
    {"mouse", "mus_musculus", "mus musculus", "mmu", "mm", "10090", "mgi"}
)
_HUMAN_ALIASES = frozenset(
    {"human", "homo_sapiens", "homo sapiens", "hsa", "hs", "9606", "hgnc"}
)


def normalize_species(value: object | None) -> Species:
    """Normalize an arbitrary species/organism input to the supported enum.

    Mirrors the server's ``normalizeSpecies``: anything recognizably "mouse"
    becomes ``"mouse"``; everything else (including ``None`` / unknown) falls
    back to the default ``"human"``.
    """

    text = str(value if value is not None else "").strip().lower()
    if text in _MOUSE_ALIASES:
        return "mouse"
    return "human"


def is_mouse(value: object | None) -> bool:
    """True if ``value`` normalizes to the mouse species."""

    return normalize_species(value) == "mouse"
