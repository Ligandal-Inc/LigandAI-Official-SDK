# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Top-level package exports."""

from __future__ import annotations

from ligandai import LigandAIJobError, LigandAITimeoutError
from ligandai.errors import LigandAIJobError as ErrorsLigandAIJobError
from ligandai.errors import LigandAITimeoutError as ErrorsLigandAITimeoutError


def test_job_errors_are_top_level_exports() -> None:
    assert LigandAIJobError is ErrorsLigandAIJobError
    assert LigandAITimeoutError is ErrorsLigandAITimeoutError


# ─────────────────────────────────────────────────────────────────────────────
# bd-dre-if562 — the recursive campaign's objects were not top-level exports.
#
# `client.campaigns.recursive(...)` returns a RecursiveCampaign and takes an
# AvoidancePolicy and a ContrastiveGuidance, and every docstring on the surface
# says to build those and pass them in — but none of the three was reachable as
# `from ligandai import X`, while every other object a user constructs was. A
# customer following the docstrings had to guess a private-looking module path.
# ─────────────────────────────────────────────────────────────────────────────


def test_campaign_objects_are_top_level_exports() -> None:
    import ligandai
    from ligandai.contrastive import AvoidancePolicy as _AvoidancePolicy
    from ligandai.contrastive import ContrastiveGuidance as _ContrastiveGuidance
    from ligandai.contrastive import ContrastiveReference as _ContrastiveReference
    from ligandai.contrastive import PocketFeatureMatrix as _PocketFeatureMatrix
    from ligandai.resources.campaigns import CampaignRound as _CampaignRound
    from ligandai.resources.campaigns import PanelLeg as _PanelLeg
    from ligandai.resources.campaigns import PanelResult as _PanelResult
    from ligandai.resources.campaigns import PartialDelta as _PartialDelta
    from ligandai.resources.campaigns import RecursiveCampaign as _RecursiveCampaign
    from ligandai.resources.campaigns import ReplicationReport as _ReplicationReport
    from ligandai.resources.campaigns import SelectivityDelta as _SelectivityDelta

    canonical = {
        "AvoidancePolicy": _AvoidancePolicy,
        "CampaignRound": _CampaignRound,
        "ContrastiveGuidance": _ContrastiveGuidance,
        "ContrastiveReference": _ContrastiveReference,
        "PanelLeg": _PanelLeg,
        "PanelResult": _PanelResult,
        "PartialDelta": _PartialDelta,
        "PocketFeatureMatrix": _PocketFeatureMatrix,
        "RecursiveCampaign": _RecursiveCampaign,
        "ReplicationReport": _ReplicationReport,
        "SelectivityDelta": _SelectivityDelta,
    }
    for name, obj in canonical.items():
        # Same object, not a re-declared lookalike.
        assert getattr(ligandai, name) is obj, name
        # And declared, so `from ligandai import *` and the docs agree.
        assert name in ligandai.__all__, name
