# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Validate billing quotes without inventing a zero for an absent measurement."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from ligandai.errors import LigandAIUnreadableResponse
from ligandai.types import CostEstimate


def parse_cost_estimate(payload: Any) -> CostEstimate:
    """Accept complete numeric quotes, including an explicit zero, in either casing."""
    if isinstance(payload, Mapping):
        credits = payload.get("credits")
        usd = payload.get("costUsd", payload.get("cost_usd"))
        values = (credits, usd)
        valid = all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0 for value in values
        )
        aliases_agree = (
            "costUsd" not in payload or "cost_usd" not in payload
            or payload["costUsd"] == payload["cost_usd"]
        )
        if valid and credits == int(credits) and aliases_agree:
            return CostEstimate.model_validate(dict(payload))
    raise LigandAIUnreadableResponse(
        "GET /api/billing/estimate did not return a complete numeric quote "
        "(credits and cost_usd/costUsd). The cost is unknown, not zero.",
        cause="unrecognised_envelope", method="GET", url="/api/billing/estimate",
    )
