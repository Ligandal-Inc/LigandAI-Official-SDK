# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Unit tests for the run-until-criteria objectives SDK surface.
[bd-LIGANDAI_ALPHA_V2-zwiz5.4]

Covers:
  - estimate() wire body (byte-exact field names) and the returned quote block
  - start() forwards quoteToken; refuses locally with no HTTP call when the
    quote is missing/malformed
  - 400 quote_required / quote_invalid raise actionable LigandAIValidationError
  - 409 quote_expired / quote_inputs_changed / quote_superseded raise carrying
    BOTH the accepted and current numbers, and never silently relaunch
  - 402 insufficient_credits enriches required/available from the estimate
  - status() keeps 'indeterminate' distinct from 'not_met'
  - kd_selectivity_criterion_leg wire shape (offTargets/minFoldSelectivity/
    nullCorrected/onTargetIpsaeGate)
  - declare() refuses to start anything priced above accept_up_to
  - Goals.__doc__ is not None (regression for the goals.py docstring bug)
  - async parity for start()'s quote-token forwarding
"""

from __future__ import annotations

import json

import pytest
from pytest_httpx import HTTPXMock

from ligandai import AsyncLigandAI, LigandAI
from ligandai.errors import LigandAICreditError, LigandAIError, LigandAIValidationError
from ligandai.resources.goals import Goals
from ligandai.resources.objectives import (
    CampaignQuote,
    CriterionOutcome,
    ObjectiveStatus,
    campaign_criteria,
    ipsae_criterion_leg,
    kd_selectivity_criterion_leg,
    panel_comparator,
)

BASE = "http://api.ligandai.test"
ESTIMATE_URL = f"{BASE}/api/ptf/campaigns/estimate"
START_URL = f"{BASE}/api/ptf/campaigns"


@pytest.fixture
def client() -> LigandAI:
    return LigandAI(api_key="lgai_pro_test", base_url=BASE, max_retries=1)


@pytest.fixture
def async_client() -> AsyncLigandAI:
    return AsyncLigandAI(api_key="lgai_pro_test", base_url=BASE, max_retries=1)


def _body(httpx_mock: HTTPXMock) -> dict:
    req = httpx_mock.get_requests()[-1]
    assert req is not None
    return json.loads(req.content)


def _criteria() -> dict:
    return campaign_criteria(
        target_qualified_designs=25,
        legs=[ipsae_criterion_leg(min_peptide_ipsae=0.67, statistic="median")],
        max_credits=50_000,
    )


_QUOTE_WIRE = {
    "token": "tok_abc123.sig",
    "expiresAt": "2026-09-23T10:30:00.000Z",
    "ceilingCredits": 5000,
    "firstRoundCredits": 800,
    "inputsHash": "deadbeef",
}


# ─────────────────────────────────────────────────────────────────────────────
# Goals.__doc__ regression
# ─────────────────────────────────────────────────────────────────────────────


def test_goals_docstring_is_not_none() -> None:
    assert Goals.__doc__ is not None
    assert "client.goals" in Goals.__doc__


# ─────────────────────────────────────────────────────────────────────────────
# estimate() — wire body + quote passthrough
# ─────────────────────────────────────────────────────────────────────────────


def test_estimate_posts_exact_wire_body(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    criteria = _criteria()
    httpx_mock.add_response(
        url=ESTIMATE_URL,
        method="POST",
        json={
            "success": True,
            "estimate": {"firstRoundCredits": 800, "spendCeilingCredits": 5000},
            "quote": _QUOTE_WIRE,
            "criteriaDiagnostics": [],
            "criteriaBlocking": False,
            "defaultContinuationMode": "keep_going",
            "tier": "pro",
        },
    )

    result = client.objectives.estimate(
        criteria=criteria,
        target_gene="EGFR",
        program_id="ptf_program_uuid",
        trajectories=4,
        sampling_steps=50,
        first_round_peptides=300,
        first_round_folds=30,
        spend_ceiling_credits=5000,
    )

    assert _body(httpx_mock) == {
        "criteria": criteria,
        "targetGene": "EGFR",
        "programId": "ptf_program_uuid",
        "trajectories": 4,
        "samplingSteps": 50,
        "firstRoundPeptides": 300,
        "firstRoundFolds": 30,
        "spendCeilingCredits": 5000.0,
    }
    assert result["quote"] == _QUOTE_WIRE


def test_estimate_omits_unset_optionals(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    criteria = _criteria()
    httpx_mock.add_response(url=ESTIMATE_URL, method="POST", json={"success": True, "estimate": {}})

    client.objectives.estimate(criteria=criteria)

    assert _body(httpx_mock) == {"criteria": criteria}


def test_estimate_rejects_malformed_criteria(client: LigandAI) -> None:
    with pytest.raises(LigandAIValidationError, match="legs"):
        client.objectives.estimate(criteria={"targetQualifiedDesigns": 1})


def test_campaign_quote_from_wire_round_trips() -> None:
    quote = CampaignQuote.from_wire(_QUOTE_WIRE)
    assert quote.token == "tok_abc123.sig"
    assert quote.ceiling_credits == 5000
    assert quote.first_round_credits == 800
    assert quote.to_wire() == _QUOTE_WIRE


# ─────────────────────────────────────────────────────────────────────────────
# start() — quoteToken forwarding, and local refusal with no HTTP call
# ─────────────────────────────────────────────────────────────────────────────


def test_start_forwards_quote_token_from_raw_dict(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    criteria = _criteria()
    httpx_mock.add_response(
        url=START_URL,
        method="POST",
        status_code=201,
        json={"success": True, "campaign": {"campaignId": "camp_1"}},
    )

    client.objectives.start(
        criteria=criteria,
        target_gene="EGFR",
        program_id="ptf_program_uuid",
        quote=_QUOTE_WIRE,
    )

    body = _body(httpx_mock)
    assert body["quoteToken"] == "tok_abc123.sig"
    assert body["targetGene"] == "EGFR"
    assert body["programId"] == "ptf_program_uuid"
    assert body["criteria"] == criteria


def test_start_forwards_quote_token_from_campaign_quote_object(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    httpx_mock.add_response(
        url=START_URL,
        method="POST",
        status_code=201,
        json={"success": True, "campaign": {"campaignId": "camp_1"}},
    )
    quote = CampaignQuote.from_wire(_QUOTE_WIRE)

    client.objectives.start(
        criteria=_criteria(), target_gene="EGFR", program_id="p1", quote=quote,
    )

    assert _body(httpx_mock)["quoteToken"] == "tok_abc123.sig"


def test_start_forwards_quote_token_string_directly(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=START_URL,
        method="POST",
        status_code=201,
        json={"success": True, "campaign": {"campaignId": "camp_1"}},
    )

    client.objectives.start(
        criteria=_criteria(), target_gene="EGFR", program_id="p1", quote="tok_bare",
    )

    assert _body(httpx_mock)["quoteToken"] == "tok_bare"


def test_start_missing_quote_raises_locally_with_no_http_call(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    with pytest.raises(LigandAIValidationError, match="quote"):
        client.objectives.start(
            criteria=_criteria(), target_gene="EGFR", program_id="p1", quote=None,  # type: ignore[arg-type]
        )
    # No request should have been sent — refused before the network call.
    assert len(httpx_mock.get_requests()) == 0


def test_start_quote_dict_without_token_raises_locally(client: LigandAI) -> None:
    with pytest.raises(LigandAIValidationError, match="token"):
        client.objectives.start(
            criteria=_criteria(), target_gene="EGFR", program_id="p1", quote={"ceilingCredits": 100},
        )


# ─────────────────────────────────────────────────────────────────────────────
# Server-side quote errors — 400 quote_required / quote_invalid
# ─────────────────────────────────────────────────────────────────────────────


def test_start_400_quote_required_raises_actionable_validation_error(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    httpx_mock.add_response(
        url=START_URL,
        method="POST",
        status_code=400,
        json={"error": "quote_required", "message": "a quote is required to start a campaign"},
    )
    with pytest.raises(LigandAIValidationError, match="estimate\\(\\)"):
        client.objectives.start(
            criteria=_criteria(), target_gene="EGFR", program_id="p1", quote="tok_x",
        )


def test_start_400_quote_invalid_raises_actionable_validation_error(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    httpx_mock.add_response(
        url=START_URL,
        method="POST",
        status_code=400,
        json={"error": "quote_invalid", "message": "bad signature"},
    )
    with pytest.raises(LigandAIValidationError, match="not accepted by the server"):
        client.objectives.start(
            criteria=_criteria(), target_gene="EGFR", program_id="p1", quote="tok_bad",
        )


# ─────────────────────────────────────────────────────────────────────────────
# 409 quote_expired / quote_inputs_changed / quote_superseded — never relaunch
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "err_code", ["quote_expired", "quote_inputs_changed", "quote_superseded"]
)
def test_start_409_stale_quote_carries_old_and_new_numbers(
    httpx_mock: HTTPXMock, client: LigandAI, err_code: str
) -> None:
    fresh_quote = {**_QUOTE_WIRE, "token": "tok_fresh", "ceilingCredits": 7000, "firstRoundCredits": 1200}
    httpx_mock.add_response(
        url=START_URL,
        method="POST",
        status_code=409,
        json={
            "error": err_code,
            "message": "price moved",
            "estimate": {"firstRoundCredits": 1200, "spendCeilingCredits": 7000},
            "quote": fresh_quote,
        },
    )

    with pytest.raises(LigandAIError) as excinfo:
        client.objectives.start(
            criteria=_criteria(), target_gene="EGFR", program_id="p1", quote=_QUOTE_WIRE,
        )

    exc = excinfo.value
    assert err_code in str(exc)
    assert exc.status_code == 409
    # OLD numbers (what was accepted) survive on the exception.
    assert exc.response["acceptedQuote"] == _QUOTE_WIRE
    assert exc.response["acceptedQuote"]["ceilingCredits"] == 5000
    # NEW numbers (the server's current price) are attached too.
    assert exc.response["quote"] == fresh_quote
    assert exc.response["quote"]["ceilingCredits"] == 7000
    # Only one request was ever made — the SDK did not relaunch on its own.
    assert len(httpx_mock.get_requests()) == 1


# ─────────────────────────────────────────────────────────────────────────────
# 402 insufficient_credits — enrich from the nested estimate
# ─────────────────────────────────────────────────────────────────────────────


def test_start_402_insufficient_credits_enriches_from_estimate(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    httpx_mock.add_response(
        url=START_URL,
        method="POST",
        status_code=402,
        json={
            "error": "insufficient_credits",
            "message": "The first round costs 800 credits and 200 are available.",
            "estimate": {
                "firstRoundCredits": 800,
                "position": {"available": 200},
            },
        },
    )

    with pytest.raises(LigandAICreditError) as excinfo:
        client.objectives.start(
            criteria=_criteria(), target_gene="EGFR", program_id="p1", quote=_QUOTE_WIRE,
        )

    exc = excinfo.value
    assert exc.required == 800
    assert exc.available == 200
    assert exc.shortfall == 600


# ─────────────────────────────────────────────────────────────────────────────
# status() — indeterminate stays distinct from not_met
# ─────────────────────────────────────────────────────────────────────────────


def test_status_keeps_indeterminate_distinct_from_not_met(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/ptf/campaigns/camp_1",
        method="GET",
        json={
            "success": True,
            "campaign": {"campaignId": "camp_1", "status": "running"},
            "rounds": [{"roundIndex": 0}],
            "evaluation": {"summary": "1 met, 1 indeterminate"},
            "criteria": [
                {"kind": "peptide_ipsae", "verdict": "met", "progress": 1.0, "reasons": []},
                {
                    "kind": "kd_selectivity",
                    "verdict": "indeterminate",
                    "progress": 0.4,
                    "reasons": ["below_ipsae_gate"],
                },
                {"kind": "seed_replication", "verdict": "not_met", "progress": 0.5, "reasons": ["only 2/5 seeds"]},
            ],
            "ceilingRemaining": 4200,
            "creditsSpentFromLedger": 800,
        },
    )

    status = client.objectives.status("camp_1")

    assert isinstance(status, ObjectiveStatus)
    assert len(status.met) == 1
    assert len(status.not_met) == 1
    assert len(status.indeterminate) == 1
    assert status.met[0].kind == "peptide_ipsae"
    assert status.not_met[0].kind == "seed_replication"
    assert status.indeterminate[0].kind == "kd_selectivity"
    # Never collapsed: indeterminate is not counted as met, and all_met is False
    # for a reason distinct from "something failed".
    assert status.all_met is False
    assert status.status == "running"
    assert status.ceiling_remaining == 4200


def test_criterion_outcome_unknown_verdict_defaults_to_indeterminate() -> None:
    outcome = CriterionOutcome.from_wire({"kind": "x", "verdict": "bogus", "progress": None})
    assert outcome.verdict == "indeterminate"
    assert outcome.is_indeterminate is True
    assert outcome.is_met is False


# ─────────────────────────────────────────────────────────────────────────────
# kd_selectivity_criterion_leg — wire shape
# ─────────────────────────────────────────────────────────────────────────────


def test_kd_selectivity_leg_wire_shape() -> None:
    leg = kd_selectivity_criterion_leg(
        off_targets=[
            panel_comparator("CD8B", role="isoform"),
            panel_comparator("CD4", label="CD4 receptor", role="off_target"),
        ],
        min_kd_fold_selectivity=3.0,
    )
    assert leg == {
        "kind": "kd_selectivity",
        "offTargets": [
            {"id": "CD8B", "label": "CD8B", "role": "isoform"},
            {"id": "CD4", "label": "CD4 receptor", "role": "off_target"},
        ],
        "minFoldSelectivity": 3.0,
        "nullCorrected": True,
        "onTargetIpsaeGate": 0.67,
    }


def test_kd_selectivity_leg_requires_off_targets() -> None:
    with pytest.raises(LigandAIValidationError, match="off_target"):
        kd_selectivity_criterion_leg(off_targets=[], min_kd_fold_selectivity=3.0)


# ─────────────────────────────────────────────────────────────────────────────
# declare() — never accepts a price above accept_up_to
# ─────────────────────────────────────────────────────────────────────────────


def test_declare_refuses_estimate_above_accept_up_to(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=ESTIMATE_URL,
        method="POST",
        json={
            "success": True,
            "estimate": {"firstRoundCredits": 800, "spendCeilingCredits": 9000},
            "quote": {**_QUOTE_WIRE, "ceilingCredits": 9000},
        },
    )

    with pytest.raises(LigandAIValidationError, match="accept_up_to"):
        client.objectives.declare(
            criteria=_criteria(),
            target_gene="EGFR",
            program_id="p1",
            accept_up_to=5000,
        )

    # Only the estimate call happened — declare() never called start().
    assert len(httpx_mock.get_requests()) == 1


def test_declare_starts_when_within_ceiling(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=ESTIMATE_URL,
        method="POST",
        json={
            "success": True,
            "estimate": {"firstRoundCredits": 800, "spendCeilingCredits": 5000},
            "quote": _QUOTE_WIRE,
        },
    )
    httpx_mock.add_response(
        url=START_URL,
        method="POST",
        status_code=201,
        json={"success": True, "campaign": {"campaignId": "camp_42"}},
    )

    handle = client.objectives.declare(
        criteria=_criteria(),
        target_gene="EGFR",
        program_id="p1",
        accept_up_to=5000,
    )

    assert handle.campaign_id == "camp_42"
    assert handle.target_gene == "EGFR"
    start_body = json.loads(httpx_mock.get_requests()[-1].content)
    assert start_body["quoteToken"] == _QUOTE_WIRE["token"]


def test_declare_raises_when_estimate_has_no_quote(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=ESTIMATE_URL,
        method="POST",
        json={"success": True, "estimate": {"firstRoundCredits": 800}},
    )
    with pytest.raises(LigandAIValidationError, match="no 'quote' block"):
        client.objectives.declare(
            criteria=_criteria(), target_gene="EGFR", program_id="p1", accept_up_to=5000,
        )


# ─────────────────────────────────────────────────────────────────────────────
# raise_ceiling() — also requires a fresh accepted quote
# ─────────────────────────────────────────────────────────────────────────────


def test_raise_ceiling_forwards_quote_token(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/ptf/campaigns/camp_1/raise-ceiling",
        method="POST",
        json={"success": True, "campaign": {"campaignId": "camp_1"}, "message": "resumed"},
    )

    client.objectives.raise_ceiling("camp_1", quote=_QUOTE_WIRE, spend_ceiling_credits=9000)

    body = _body(httpx_mock)
    assert body["quoteToken"] == _QUOTE_WIRE["token"]
    assert body["spendCeilingCredits"] == 9000.0


# ─────────────────────────────────────────────────────────────────────────────
# list / get / stop — thin route wrappers
# ─────────────────────────────────────────────────────────────────────────────


def test_list_get_stop_routes(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=f"{BASE}/api/ptf/campaigns?limit=50", method="GET", json={"campaigns": []})
    httpx_mock.add_response(url=f"{BASE}/api/ptf/campaigns/camp_1", method="GET", json={"campaign": {}})
    httpx_mock.add_response(url=f"{BASE}/api/ptf/campaigns/camp_1/stop", method="POST", json={"success": True})

    assert client.objectives.list() == {"campaigns": []}
    assert client.objectives.get("camp_1") == {"campaign": {}}
    assert client.objectives.stop("camp_1") == {"success": True}


# ─────────────────────────────────────────────────────────────────────────────
# Async parity — quoteToken forwarding
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_async_start_forwards_quote_token(
    httpx_mock: HTTPXMock, async_client: AsyncLigandAI
) -> None:
    httpx_mock.add_response(
        url=START_URL,
        method="POST",
        status_code=201,
        json={"success": True, "campaign": {"campaignId": "camp_async"}},
    )

    await async_client.objectives.start(
        criteria=_criteria(), target_gene="EGFR", program_id="p1", quote=_QUOTE_WIRE,
    )

    assert _body(httpx_mock)["quoteToken"] == _QUOTE_WIRE["token"]
    await async_client.close()


@pytest.mark.asyncio
async def test_async_status_keeps_indeterminate_distinct(
    httpx_mock: HTTPXMock, async_client: AsyncLigandAI
) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/ptf/campaigns/camp_9",
        method="GET",
        json={
            "campaign": {"campaignId": "camp_9", "status": "running"},
            "criteria": [
                {"kind": "peptide_ipsae", "verdict": "indeterminate", "progress": None, "reasons": []},
            ],
        },
    )

    status = await async_client.objectives.status("camp_9")
    assert len(status.indeterminate) == 1
    assert len(status.not_met) == 0
    await async_client.close()
