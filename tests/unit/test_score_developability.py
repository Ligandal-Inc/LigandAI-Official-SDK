# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Unit tests for peptides.score_developability + the null-grade flatten fix.

Refs: bd-dre-h0cuo.2 (bd-dre-h0cuo.2.3 SDK, bd-dre-h0cuo.2.4 tests)

Covers:
  1. Wire body — sequences / peptide_ids / fold_ids map to the camelCase keys
     the Express route expects; explicit module opt-outs forwarded.
  2. Response parsing — the canonical nested stability_scores / immuno_scores
     plus the headline grades round-trip into DevelopabilityResult.
  3. Input validation — no-input raises ValueError; unknown module raises.
  4. Flatten regression — _flatten_peptide now maps the immuno score from the
     REAL key (immuno_risk_score), not the nonexistent "immunogenicityScore",
     and surfaces half-life from stability_scores.
"""
from __future__ import annotations

import json

import pytest
from pytest_httpx import HTTPXMock

from ligandai import DevelopabilityResult, LigandAI
from ligandai.resources.peptides import _build_developability_body, _flatten_peptide

BASE = "http://api.ligandai.test"
DEV_URL = f"{BASE}/api/v1/peptides/score-developability"


@pytest.fixture
def client() -> LigandAI:
    return LigandAI(api_key="lgai_pro_test", base_url=BASE, max_retries=1)


def _body(httpx_mock: HTTPXMock) -> dict:
    req = httpx_mock.get_request()
    assert req is not None
    return json.loads(req.content)


_RESP = {
    "success": True,
    "tier": "pro",
    "count": 1,
    "persisted": 0,
    "modules_run": ["stability", "immuno", "halflife"],
    "results": [
        {
            "sequence": "GTGHDIWIQSQNMIDINP",
            "stability_scores": {"stability_grade": "A", "predicted_halflife_hours": 30.0, "predicted_halflife_min": 1800.0},
            "immuno_scores": {"immuno_grade": "B", "immuno_risk_score": 0.07},
            "stability_grade": "A",
            "immuno_grade": "B",
            "predicted_halflife_hours": 30.0,
            "predicted_halflife_min": 1800.0,
        }
    ],
}


def test_score_developability_wire_body_sequences(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=DEV_URL, method="POST", json=_RESP)
    client.peptides.score_developability(sequences=["gtghdiwiqsqnmidinp"])
    body = _body(httpx_mock)
    assert body["sequences"] == ["GTGHDIWIQSQNMIDINP"]  # upcased
    assert "peptideIds" not in body and "foldIds" not in body


def test_score_developability_wire_body_ids_and_optouts(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=DEV_URL, method="POST", json=_RESP)
    client.peptides.score_developability(peptide_ids=[101, 202], fold_ids=[5], immuno=False)
    body = _body(httpx_mock)
    assert body["peptideIds"] == [101, 202]
    assert body["foldIds"] == ["5"]
    assert body["immuno"] is False


def test_score_developability_parses_nested_grades(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=DEV_URL, method="POST", json=_RESP)
    results = client.peptides.score_developability(sequences=["GTGHDIWIQSQNMIDINP"])
    assert len(results) == 1
    r = results[0]
    assert isinstance(r, DevelopabilityResult)
    assert r.stability_grade == "A"
    assert r.immuno_grade == "B"
    assert r.predicted_halflife_hours == 30.0
    assert r.stability_scores["stability_grade"] == "A"
    assert r.immuno_scores["immuno_risk_score"] == 0.07


def test_build_body_requires_input() -> None:
    with pytest.raises(ValueError):
        _build_developability_body(
            sequences=None, peptide_ids=None, fold_ids=None,
            modules=None, stability=None, immuno=None, halflife=None,
        )


def test_build_body_rejects_unknown_module() -> None:
    with pytest.raises(ValueError):
        _build_developability_body(
            sequences=["ACDE"], peptide_ids=None, fold_ids=None,
            modules=["bogus"], stability=None, immuno=None, halflife=None,
        )


def test_flatten_maps_immuno_from_real_key() -> None:
    """Regression: the immuno numeric score used to read a nonexistent key."""
    raw = {
        "sequence": "GTGHDIWIQSQNMIDINP",
        "stability_scores": {"stability_grade": "A", "predicted_halflife_hours": 30.0, "predicted_halflife_min": 1800.0},
        "immuno_scores": {"immuno_grade": "B", "immuno_risk_score": 0.07},
    }
    out = _flatten_peptide(raw)
    assert out["stabilityGrade"] == "A"
    assert out["immunoGrade"] == "B"
    # Previously ALWAYS None (read immuno_scores["immunogenicityScore"], which
    # does not exist). Now sourced from immuno_risk_score.
    assert out["immunogenicityScore"] == 0.07
    assert out["halfLifeHours"] == 30.0
    assert out["halfLifeMin"] == 1800.0
