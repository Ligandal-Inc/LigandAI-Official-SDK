# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Unit tests for the goal-campaign engine SDK surface.  [bd-dre-1vij1]

Covers:
  - campaign_scan() body, response, zero-cost scan
  - campaign_start() body validation: engine, goal_type, receptor sequence
  - seed_peptides=None → LigandAIValidationError with actionable message
  - campaign_status() route and scrub of internal keys
  - campaign_stream() SSE parsing
  - campaign_cancel() route
  - campaign_cross_validate() body (falsy-zero agreement_threshold=0.0)
  - drive() returns GoalCampaign handle with correct campaign_id
  - GoalCampaign.wait() polls until terminal
  - GoalCampaign.cancel() delegates
  - GoalCampaign.cross_validate() delegates
  - GoalCampaign.stream() yields events
  - _scrub_public_parameters: internalPoolPeptidesPerTarget never surfaces
  - domain-rule constants: ipsae metric labelling in error/docstrings
"""

from __future__ import annotations

import json

import pytest
from pytest_httpx import HTTPXMock, IteratorStream

from ligandai import LigandAI
from ligandai.errors import LigandAIValidationError
from ligandai.resources.goals import (
    AsyncGoalCampaign,
    GoalCampaign,
    _campaign_start_body,
    _interpret_campaign_400,
    _scrub_campaign_response,
    _scrub_public_parameters,
)

BASE = "http://api.ligandai.test"
SCAN_URL = f"{BASE}/api/v1/goal-campaign/scan"
START_URL = f"{BASE}/api/v1/goal-campaign/start"
RECEPTOR_SEQ = "ACDEFGHIKLMNPQRSTVWYACDEFGHIKLMNPQRSTVWY"

_CAMPAIGN_STARTED = {
    "campaign_id": "camp_abc123",
    "status": "queued",
    "engine": "boltz2",
    "max_iterations": 5,
    "max_credits": 5000,
    "batch_size": 8,
    "seed_count": 2,
    "goal": {"type": "allosteric", "hotspots": 2, "avoid_residues": 1},
}


@pytest.fixture
def client() -> LigandAI:
    return LigandAI(api_key="lgai_pro_test", base_url=BASE, max_retries=1)


def _body(httpx_mock: HTTPXMock) -> dict:
    req = httpx_mock.get_request()
    assert req is not None
    return json.loads(req.content)


# ─────────────────────────────────────────────────────────────────────────────
# _scrub_public_parameters — internal key never surfaces
# ─────────────────────────────────────────────────────────────────────────────


def test_scrub_drops_internal_pool_key() -> None:
    payload = {
        "peptidesPerTarget": 50,
        "internalPoolPeptidesPerTarget": 200,
        "engine": "boltz2",
    }
    scrubbed = _scrub_public_parameters(payload)
    assert "internalPoolPeptidesPerTarget" not in scrubbed
    assert scrubbed["peptidesPerTarget"] == 50
    assert scrubbed["engine"] == "boltz2"


def test_scrub_passes_through_non_dict() -> None:
    assert _scrub_public_parameters(None) is None
    assert _scrub_public_parameters("hello") == "hello"
    assert _scrub_public_parameters([1, 2, 3]) == [1, 2, 3]


def test_scrub_campaign_response_scrubs_config_and_parameters() -> None:
    resp = {
        "campaign_id": "camp_1",
        "config": {"engine": "boltz2", "internalPoolPeptidesPerTarget": 100},
        "parameters": {"batchSize": 8, "internalPoolPeptidesPerTarget": 999},
    }
    out = _scrub_campaign_response(resp)
    assert "internalPoolPeptidesPerTarget" not in out["config"]
    assert "internalPoolPeptidesPerTarget" not in out["parameters"]
    assert out["config"]["engine"] == "boltz2"
    assert out["parameters"]["batchSize"] == 8


# ─────────────────────────────────────────────────────────────────────────────
# _campaign_start_body validation
# ─────────────────────────────────────────────────────────────────────────────


def test_campaign_start_body_minimal() -> None:
    body = _campaign_start_body(
        receptor_sequence=RECEPTOR_SEQ,
        goal_type="allosteric",
        receptor_chain=None,
        receptor_gene=None,
        receptor_msa_csv=None,
        seed_peptides=["ACDEFGHIKLMN"],
        peptide_length=None,
        engine="boltz2",
        diffusion_samples=4,
        batch_size=8,
        max_iterations=5,
        max_credits=5000,
        survivor_ipsae_threshold=None,
        cross_validate_engine=None,
        hotspots=None,
        avoid_residues=None,
        target_ipsae=None,
        target_iptm=None,
        contact_threshold_a=None,
        goal_description=None,
    )
    assert body["receptor"]["sequence"] == RECEPTOR_SEQ.upper()
    assert body["goal"]["type"] == "allosteric"
    assert body["engine"] == "boltz2"
    assert body["diffusion_samples"] == 4
    assert body["batch_size"] == 8
    assert body["max_iterations"] == 5
    assert body["max_credits"] == 5000
    assert body["seed_peptides"] == ["ACDEFGHIKLMN"]
    # None values must not be in the body (falsy-zero discipline)
    assert "survivor_ipsae_threshold" not in body
    assert "cross_validate_engine" not in body
    assert "hotspots" not in body["goal"]
    assert "avoid_residues" not in body["goal"]
    assert "target_ipsae" not in body["goal"]
    assert "target_iptm" not in body["goal"]
    assert "contact_threshold_a" not in body["goal"]


def test_campaign_start_body_full() -> None:
    body = _campaign_start_body(
        receptor_sequence=RECEPTOR_SEQ,
        goal_type="allosteric",
        receptor_chain="A",
        receptor_gene="EGFR",
        receptor_msa_csv="msa,data",
        seed_peptides=["ACDEFGHIKLMN", "PQRSTVWYA"],
        peptide_length=20,
        engine="protenix",
        diffusion_samples=2,
        batch_size=4,
        max_iterations=3,
        max_credits=2000,
        survivor_ipsae_threshold=0.67,
        cross_validate_engine="esmfold2",
        hotspots=[{"chain": "A", "residue": 792}],
        avoid_residues=[{"chain": "A", "residue": 719}],
        target_ipsae=0.7,
        target_iptm=0.6,
        contact_threshold_a=5.0,
        goal_description="test goal",
    )
    assert body["receptor"]["chain"] == "A"
    assert body["receptor"]["gene"] == "EGFR"
    assert body["receptor"]["msa_csv"] == "msa,data"
    assert body["peptide_length"] == 20
    assert body["engine"] == "protenix"
    assert body["survivor_ipsae_threshold"] == 0.67
    assert body["cross_validate_engine"] == "esmfold2"
    assert body["goal"]["hotspots"] == [{"chain": "A", "residue": 792}]
    assert body["goal"]["avoid_residues"] == [{"chain": "A", "residue": 719}]
    assert body["goal"]["target_ipsae"] == 0.7
    assert body["goal"]["target_iptm"] == 0.6
    assert body["goal"]["contact_threshold_a"] == 5.0
    assert body["goal"]["description"] == "test goal"


def test_campaign_start_body_bad_receptor_sequence() -> None:
    with pytest.raises(ValueError, match="receptor_sequence"):
        _campaign_start_body(
            receptor_sequence="ABC",  # too short
            goal_type="allosteric",
            receptor_chain=None,
            receptor_gene=None,
            receptor_msa_csv=None,
            seed_peptides=None,
            peptide_length=None,
            engine="boltz2",
            diffusion_samples=4,
            batch_size=8,
            max_iterations=5,
            max_credits=5000,
            survivor_ipsae_threshold=None,
            cross_validate_engine=None,
            hotspots=None,
            avoid_residues=None,
            target_ipsae=None,
            target_iptm=None,
            contact_threshold_a=None,
            goal_description=None,
        )


def test_campaign_start_body_bad_engine() -> None:
    with pytest.raises(ValueError, match="engine"):
        _campaign_start_body(
            receptor_sequence=RECEPTOR_SEQ,
            goal_type="allosteric",
            receptor_chain=None,
            receptor_gene=None,
            receptor_msa_csv=None,
            seed_peptides=None,
            peptide_length=None,
            engine="alphafold3",  # invalid
            diffusion_samples=4,
            batch_size=8,
            max_iterations=5,
            max_credits=5000,
            survivor_ipsae_threshold=None,
            cross_validate_engine=None,
            hotspots=None,
            avoid_residues=None,
            target_ipsae=None,
            target_iptm=None,
            contact_threshold_a=None,
            goal_description=None,
        )


def test_campaign_start_body_bad_goal_type() -> None:
    with pytest.raises(ValueError, match="goal_type"):
        _campaign_start_body(
            receptor_sequence=RECEPTOR_SEQ,
            goal_type="orthosteric",  # invalid
            receptor_chain=None,
            receptor_gene=None,
            receptor_msa_csv=None,
            seed_peptides=None,
            peptide_length=None,
            engine="boltz2",
            diffusion_samples=4,
            batch_size=8,
            max_iterations=5,
            max_credits=5000,
            survivor_ipsae_threshold=None,
            cross_validate_engine=None,
            hotspots=None,
            avoid_residues=None,
            target_ipsae=None,
            target_iptm=None,
            contact_threshold_a=None,
            goal_description=None,
        )


def test_campaign_start_body_zero_contact_threshold_transmitted() -> None:
    """Falsy-zero: contact_threshold_a=0.0 must be transmitted, not dropped."""
    body = _campaign_start_body(
        receptor_sequence=RECEPTOR_SEQ,
        goal_type="allosteric",
        receptor_chain=None,
        receptor_gene=None,
        receptor_msa_csv=None,
        seed_peptides=None,
        peptide_length=None,
        engine="boltz2",
        diffusion_samples=4,
        batch_size=8,
        max_iterations=5,
        max_credits=5000,
        survivor_ipsae_threshold=None,
        cross_validate_engine=None,
        hotspots=None,
        avoid_residues=None,
        target_ipsae=None,
        target_iptm=None,
        contact_threshold_a=0.0,  # falsy-zero — must appear in body
        goal_description=None,
    )
    assert "contact_threshold_a" in body["goal"]
    assert body["goal"]["contact_threshold_a"] == 0.0


def test_campaign_start_body_zero_survivor_threshold_transmitted() -> None:
    """Falsy-zero: survivor_ipsae_threshold=0.0 must be transmitted."""
    body = _campaign_start_body(
        receptor_sequence=RECEPTOR_SEQ,
        goal_type="hotspot",
        receptor_chain=None,
        receptor_gene=None,
        receptor_msa_csv=None,
        seed_peptides=None,
        peptide_length=None,
        engine="boltz2",
        diffusion_samples=4,
        batch_size=8,
        max_iterations=5,
        max_credits=5000,
        survivor_ipsae_threshold=0.0,  # falsy-zero
        cross_validate_engine=None,
        hotspots=None,
        avoid_residues=None,
        target_ipsae=None,
        target_iptm=None,
        contact_threshold_a=None,
        goal_description=None,
    )
    assert "survivor_ipsae_threshold" in body
    assert body["survivor_ipsae_threshold"] == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# _interpret_campaign_400 — clear error on missing seed_peptides
# ─────────────────────────────────────────────────────────────────────────────


def test_interpret_400_seed_peptides_raises_validation_error() -> None:
    with pytest.raises(LigandAIValidationError, match="seed_peptides"):
        _interpret_campaign_400({"error": "seed_peptides required"})


def test_interpret_400_other_error_raises_validation_error() -> None:
    with pytest.raises(LigandAIValidationError, match="bad request"):
        _interpret_campaign_400({"error": "bad request"})


def test_interpret_400_string_body() -> None:
    with pytest.raises(LigandAIValidationError, match="seed_peptides"):
        _interpret_campaign_400("seed_peptides required: provide at least one sequence")


# ─────────────────────────────────────────────────────────────────────────────
# campaign_scan — wire format
# ─────────────────────────────────────────────────────────────────────────────


def test_campaign_scan_minimal(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=SCAN_URL,
        method="POST",
        json={
            "peptide_chain": "B",
            "hotspot_hit": True,
            "hotspot_coverage": 1.0,
            "hotspot_contact_count": 2,
            "hotspot_miss_count": 0,
            "non_hotspot_contact_count": 5,
            "orthosteric_drift": False,
            "contacted_residues": [{"chain": "A", "residue": 792}],
            "hotspot_contacts": [{"chain": "A", "residue": 792}],
            "contact_threshold_a": 5.0,
            "error": None,
        },
    )
    result = client.goals.campaign_scan(
        structure="ATOM 1 N ...",
        format="pdb",
        peptide_chain="B",
    )
    assert result["hotspot_hit"] is True
    assert result["hotspot_coverage"] == 1.0

    body = _body(httpx_mock)
    assert body["structure"] == "ATOM 1 N ..."
    assert body["format"] == "pdb"
    assert body["peptide_chain"] == "B"
    assert "hotspots" not in body
    assert "contact_threshold_a" not in body


def test_campaign_scan_with_hotspots_and_threshold(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=SCAN_URL, method="POST", json={"hotspot_hit": False})
    client.goals.campaign_scan(
        structure="MODEL ...",
        format="cif",
        peptide_chain="C",
        hotspots=[{"chain": "A", "residue": 148}, {"chain": "A", "residue": 162}],
        contact_threshold_a=4.5,
    )
    body = _body(httpx_mock)
    assert body["hotspots"] == [{"chain": "A", "residue": 148}, {"chain": "A", "residue": 162}]
    assert body["contact_threshold_a"] == 4.5


def test_campaign_scan_zero_threshold_transmitted(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    """Falsy-zero discipline: contact_threshold_a=0.0 must be in request body."""
    httpx_mock.add_response(url=SCAN_URL, method="POST", json={"hotspot_hit": False})
    client.goals.campaign_scan(
        structure="MODEL ...",
        contact_threshold_a=0.0,
    )
    body = _body(httpx_mock)
    assert "contact_threshold_a" in body
    assert body["contact_threshold_a"] == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# campaign_start — HTTP surface
# ─────────────────────────────────────────────────────────────────────────────


def test_campaign_start_posts_correct_body(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=START_URL, method="POST", json=_CAMPAIGN_STARTED)
    result = client.goals.campaign_start(
        receptor_sequence=RECEPTOR_SEQ,
        goal_type="allosteric",
        receptor_gene="EGFR",
        seed_peptides=["ACDEFGHIKLMN", "PQRSTVWYACED"],
        hotspots=[{"chain": "A", "residue": 792}, {"chain": "A", "residue": 855}],
        avoid_residues=[{"chain": "A", "residue": 719}],
        engine="boltz2",
        diffusion_samples=4,
        max_iterations=5,
        max_credits=5000,
        cross_validate_engine="protenix",
    )
    assert result["campaign_id"] == "camp_abc123"
    assert result["status"] == "queued"

    body = _body(httpx_mock)
    assert body["receptor"]["sequence"] == RECEPTOR_SEQ.upper()
    assert body["receptor"]["gene"] == "EGFR"
    assert body["goal"]["type"] == "allosteric"
    assert body["goal"]["hotspots"] == [{"chain": "A", "residue": 792}, {"chain": "A", "residue": 855}]
    assert body["goal"]["avoid_residues"] == [{"chain": "A", "residue": 719}]
    assert body["engine"] == "boltz2"
    assert body["diffusion_samples"] == 4
    assert body["seed_peptides"] == ["ACDEFGHIKLMN", "PQRSTVWYACED"]
    assert body["cross_validate_engine"] == "protenix"


def test_campaign_start_scrubs_internal_keys(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    """Server response with internalPoolPeptidesPerTarget must not reach the caller."""
    httpx_mock.add_response(
        url=START_URL,
        method="POST",
        json={
            "campaign_id": "camp_x",
            "config": {
                "engine": "boltz2",
                "internalPoolPeptidesPerTarget": 500,
            },
        },
    )
    result = client.goals.campaign_start(
        receptor_sequence=RECEPTOR_SEQ,
        goal_type="hotspot",
        seed_peptides=["ACDEFGHIKLMN"],
    )
    assert "internalPoolPeptidesPerTarget" not in result.get("config", {})
    assert result["config"]["engine"] == "boltz2"


# ─────────────────────────────────────────────────────────────────────────────
# campaign_status
# ─────────────────────────────────────────────────────────────────────────────


def test_campaign_status(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/goal-campaign/camp_abc123/status",
        method="GET",
        json={"campaign_id": "camp_abc123", "status": "running", "iteration": 2},
    )
    status = client.goals.campaign_status("camp_abc123")
    assert status["campaign_id"] == "camp_abc123"
    assert status["status"] == "running"


# ─────────────────────────────────────────────────────────────────────────────
# campaign_stream — SSE
# ─────────────────────────────────────────────────────────────────────────────


def test_campaign_stream_parses_sse_events(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/goal-campaign/camp_abc123/stream",
        stream=IteratorStream([
            b'data: {"event":"status","data":{"status":"running","iteration":1}}\n\n',
            b'data: {"event":"design_scored","data":{"peptide_seq":"ACDEFG","peptide_ipsae":0.72}}\n\n',
        ]),
    )
    events = list(client.goals.campaign_stream("camp_abc123"))
    assert len(events) == 2
    assert events[0]["event"] == "status"
    assert events[1]["data"]["peptide_ipsae"] == 0.72


# ─────────────────────────────────────────────────────────────────────────────
# campaign_cancel
# ─────────────────────────────────────────────────────────────────────────────


def test_campaign_cancel(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/goal-campaign/camp_abc123/cancel",
        method="POST",
        json={"campaign_id": "camp_abc123", "cancelled": True, "status": "running"},
    )
    result = client.goals.campaign_cancel("camp_abc123")
    assert result["cancelled"] is True


# ─────────────────────────────────────────────────────────────────────────────
# campaign_cross_validate — wire format including falsy-zero
# ─────────────────────────────────────────────────────────────────────────────


def test_campaign_cross_validate_default_engine(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/goal-campaign/camp_abc123/cross-validate",
        method="POST",
        json={"agreement_rate": 0.9, "concordant": 9, "discordant": 1},
    )
    result = client.goals.campaign_cross_validate("camp_abc123")
    assert result["agreement_rate"] == 0.9

    body = _body(httpx_mock)
    assert body["secondary_engine"] == "protenix"
    # No agreement_threshold or peptides when not supplied
    assert "agreement_threshold" not in body
    assert "peptides" not in body


def test_campaign_cross_validate_with_threshold_and_peptides(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/goal-campaign/camp_abc123/cross-validate",
        method="POST",
        json={"agreement_rate": 0.8},
    )
    client.goals.campaign_cross_validate(
        "camp_abc123",
        secondary_engine="esmfold2",
        agreement_threshold=0.5,
        peptides=["ACDEFGHIKLMN"],
    )
    body = _body(httpx_mock)
    assert body["secondary_engine"] == "esmfold2"
    assert body["agreement_threshold"] == 0.5
    assert body["peptides"] == ["ACDEFGHIKLMN"]


def test_campaign_cross_validate_zero_agreement_threshold_transmitted(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    """Falsy-zero: agreement_threshold=0.0 must be sent, not dropped."""
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/goal-campaign/camp_abc123/cross-validate",
        method="POST",
        json={"agreement_rate": 1.0},
    )
    client.goals.campaign_cross_validate("camp_abc123", agreement_threshold=0.0)
    body = _body(httpx_mock)
    assert "agreement_threshold" in body
    assert body["agreement_threshold"] == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# drive() — high-level entry point
# ─────────────────────────────────────────────────────────────────────────────


def test_drive_returns_goal_campaign_handle(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=START_URL, method="POST", json=_CAMPAIGN_STARTED)
    campaign = client.goals.drive(
        receptor_sequence=RECEPTOR_SEQ,
        goal_type="allosteric",
        gene="EGFR",
        goal="allosteric site binder",
        seed_peptides=["ACDEFGHIKLMN"],
        engine="boltz2",
        diffusion_samples=4,
        max_iterations=5,
        survivor_ipsae_threshold=0.67,
        cross_validate_engine="protenix",
    )
    assert isinstance(campaign, GoalCampaign)
    assert campaign.campaign_id == "camp_abc123"
    assert campaign.gene == "EGFR"
    assert campaign.goal == "allosteric site binder"


def test_drive_uses_gene_as_receptor_gene_fallback(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    """gene= should be used as receptor_gene when receptor_gene not supplied."""
    httpx_mock.add_response(url=START_URL, method="POST", json=_CAMPAIGN_STARTED)
    client.goals.drive(
        receptor_sequence=RECEPTOR_SEQ,
        gene="TNF",
        seed_peptides=["ACDEFGHIKLMN"],
    )
    body = _body(httpx_mock)
    assert body["receptor"].get("gene") == "TNF"


def test_drive_goal_description_fallback(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    """goal= should be used as goal_description when goal_description not supplied."""
    httpx_mock.add_response(url=START_URL, method="POST", json=_CAMPAIGN_STARTED)
    client.goals.drive(
        receptor_sequence=RECEPTOR_SEQ,
        goal="bind the allosteric site",
        seed_peptides=["ACDEFGHIKLMN"],
    )
    body = _body(httpx_mock)
    assert body["goal"]["description"] == "bind the allosteric site"


def test_drive_missing_campaign_id_raises(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    """Server response without campaign_id raises LigandAIError."""
    from ligandai.errors import LigandAIError
    httpx_mock.add_response(url=START_URL, method="POST", json={"status": "queued"})
    with pytest.raises(LigandAIError, match="campaign_id"):
        client.goals.drive(
            receptor_sequence=RECEPTOR_SEQ,
            seed_peptides=["ACDEFGHIKLMN"],
        )


# ─────────────────────────────────────────────────────────────────────────────
# GoalCampaign handle — wait, stream, cancel, cross_validate
# ─────────────────────────────────────────────────────────────────────────────


def test_goal_campaign_repr(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=START_URL, method="POST", json=_CAMPAIGN_STARTED)
    campaign = client.goals.drive(
        receptor_sequence=RECEPTOR_SEQ,
        gene="EGFR",
        goal="test goal",
        seed_peptides=["ACDEFGHIKLMN"],
    )
    r = repr(campaign)
    assert "camp_abc123" in r
    assert "EGFR" in r


def test_goal_campaign_wait_polls_until_terminal(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    """wait() polls status until terminal state is reached."""
    # Sequence: running → running → completed
    status_url = f"{BASE}/api/v1/goal-campaign/camp_abc123/status"
    httpx_mock.add_response(url=status_url, method="GET", json={"status": "running"})
    httpx_mock.add_response(url=status_url, method="GET", json={"status": "running"})
    httpx_mock.add_response(url=status_url, method="GET", json={
        "status": "completed",
        "hits": [{"peptide_seq": "ACDEFG", "peptide_ipsae": 0.81}],
    })

    httpx_mock.add_response(url=START_URL, method="POST", json=_CAMPAIGN_STARTED)
    campaign = client.goals.drive(
        receptor_sequence=RECEPTOR_SEQ,
        seed_peptides=["ACDEFGHIKLMN"],
    )
    result = campaign.wait(poll_interval=0.01)
    assert result["status"] == "completed"
    assert result["hits"][0]["peptide_ipsae"] == 0.81


def test_goal_campaign_wait_timeout_raises(client: LigandAI) -> None:
    """GoalCampaign.wait() raises LigandAIWaitTimeout when timeout is exceeded.

    Tests the timeout logic directly without HTTP mocking, by injecting a
    GoalCampaign whose status() method always returns a non-terminal state.
    This avoids httpx_mock's "unused responses" assertion.
    """
    from unittest.mock import MagicMock
    from ligandai.errors import LigandAIWaitTimeout
    from ligandai.resources.goals import GoalCampaign

    goals_resource = MagicMock()
    goals_resource.campaign_status.return_value = {"status": "running", "campaign_id": "camp_abc123"}

    campaign = GoalCampaign(
        campaign_id="camp_abc123",
        goals_resource=goals_resource,
        gene="EGFR",
        goal="test",
    )
    with pytest.raises(LigandAIWaitTimeout, match="camp_abc123"):
        campaign.wait(timeout=0.05, poll_interval=0.01)


def test_goal_campaign_cancel_delegates(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=START_URL, method="POST", json=_CAMPAIGN_STARTED)
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/goal-campaign/camp_abc123/cancel",
        method="POST",
        json={"cancelled": True, "campaign_id": "camp_abc123", "status": "running"},
    )
    campaign = client.goals.drive(
        receptor_sequence=RECEPTOR_SEQ,
        seed_peptides=["ACDEFGHIKLMN"],
    )
    result = campaign.cancel()
    assert result["cancelled"] is True


def test_goal_campaign_cross_validate_delegates(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=START_URL, method="POST", json=_CAMPAIGN_STARTED)
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/goal-campaign/camp_abc123/cross-validate",
        method="POST",
        json={"agreement_rate": 0.85},
    )
    campaign = client.goals.drive(
        receptor_sequence=RECEPTOR_SEQ,
        seed_peptides=["ACDEFGHIKLMN"],
    )
    result = campaign.cross_validate(secondary_engine="protenix", agreement_threshold=0.4)
    assert result["agreement_rate"] == 0.85


def test_goal_campaign_stream_delegates(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=START_URL, method="POST", json=_CAMPAIGN_STARTED)
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/goal-campaign/camp_abc123/stream",
        stream=IteratorStream([
            b'data: {"event":"status","status":"running"}\n\n',
        ]),
    )
    campaign = client.goals.drive(
        receptor_sequence=RECEPTOR_SEQ,
        seed_peptides=["ACDEFGHIKLMN"],
    )
    events = list(campaign.stream())
    assert len(events) == 1
    assert events[0]["event"] == "status"


# ─────────────────────────────────────────────────────────────────────────────
# Existing AutoResearch surface still works (regression guard)
# ─────────────────────────────────────────────────────────────────────────────


def test_autoresearch_start_still_requires_automatic_mode(client: LigandAI) -> None:
    with pytest.raises(ValueError, match="automatic_mode=True"):
        client.goals.start("optimize IL31 peptide selectivity")


def test_autoresearch_pause_resume_stop(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    for action in ("pause", "resume", "stop"):
        httpx_mock.add_response(
            url=f"{BASE}/api/autoresearch/runs/arun_123/{action}",
            method="POST",
            json={"ok": True},
        )
    assert client.goals.pause("arun_123") is True
    assert client.goals.resume("arun_123") is True
    assert client.goals.stop("arun_123") is True
