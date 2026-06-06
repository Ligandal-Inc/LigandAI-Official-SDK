# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Unit tests for the cascade + ensemble auto-fold wire-format on generate().

Covers (bd-LIGANDAI_ALPHA_V2-7y1uh):
  - Ensemble keys (foldEngines/foldPerEngine/foldSharedMsa) emitted ONLY for a
    real ensemble; absent for None / ["boltz2"] (legacy bytes preserved).
  - Cascade keys (foldCascade/cascadeGate*) emitted ONLY when fold_cascade is
    truthy; absent otherwise.
  - Cascade param validation: cascade without boltz2 → ValueError; bad gate
    metric → ValueError; non-positive top_n → ValueError.
  - camelCase / snake_case alias resolution (snake wins on conflict).

All tests intercept the POST to /api/ptf/parallel/generate via pytest-httpx and
inspect the request body without touching a real server.
"""

from __future__ import annotations

import json

import pytest
from pytest_httpx import HTTPXMock

from ligandai import LigandAI

BASE = "http://api.ligandai.test"
GEN_URL = f"{BASE}/api/ptf/parallel/generate"

_QUEUED = {"sessionId": "sid_cascade", "status": "queued"}


@pytest.fixture
def client() -> LigandAI:
    return LigandAI(api_key="lgai_pro_test", base_url=BASE, max_retries=1)


def _body(httpx_mock: HTTPXMock) -> dict:
    req = httpx_mock.get_request()
    assert req is not None
    return json.loads(req.content)


# ---------------------------------------------------------------------------
# Legacy preservation — no ensemble / cascade keys emitted by default.
# ---------------------------------------------------------------------------


def test_legacy_no_ensemble_no_cascade_keys(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="EGFR", auto_fold=True, top_n_fold=5)
    body = _body(httpx_mock)
    for k in (
        "foldEngines", "foldPerEngine", "foldSharedMsa",
        "foldCascade", "cascadeGateMetric", "cascadeGateThreshold", "cascadeGateTopN",
    ):
        assert k not in body, f"legacy body must not emit {k}"


def test_boltz2_only_engine_list_stays_legacy(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="EGFR", auto_fold=True, fold_engines=["boltz2"])
    body = _body(httpx_mock)
    # ["boltz2"] is NOT a real ensemble — no foldEngines key.
    assert "foldEngines" not in body
    assert "foldCascade" not in body


# ---------------------------------------------------------------------------
# Ensemble emission.
# ---------------------------------------------------------------------------


def test_real_ensemble_emits_fold_engines(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(
        gene="EGFR", auto_fold=True,
        fold_engines=["boltz2", "protenix"],
        fold_shared_msa="auto",
    )
    body = _body(httpx_mock)
    assert body["foldEngines"] == ["boltz2", "protenix"]
    assert body["foldSharedMsa"] == {"mode": "auto"}
    assert "foldCascade" not in body  # cascade off by default


def test_single_non_boltz2_engine_is_real_ensemble(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="EGFR", auto_fold=True, fold_engines=["esmfold2"])
    body = _body(httpx_mock)
    assert body["foldEngines"] == ["esmfold2"]


def test_per_engine_msa_key_rejected(client: LigandAI) -> None:
    # Raises client-side BEFORE any HTTP request — no mock response registered.
    with pytest.raises(ValueError, match="MSA"):
        client.peptides.generate(
            gene="EGFR", auto_fold=True,
            fold_engines=["boltz2", "protenix"],
            fold_per_engine={"protenix": {"msa": "external.a3m"}},
        )


# ---------------------------------------------------------------------------
# Cascade emission.
# ---------------------------------------------------------------------------


def test_cascade_emits_defaults(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(
        gene="EGFR", auto_fold=True,
        fold_engines=["boltz2", "protenix"],
        fold_cascade=True,
    )
    body = _body(httpx_mock)
    assert body["foldCascade"] is True
    assert body["cascadeGateMetric"] == "ipsae"
    assert body["cascadeGateThreshold"] == 0.67
    assert "cascadeGateTopN" not in body  # None by default
    assert body["foldEngines"] == ["boltz2", "protenix"]


def test_cascade_top_n_with_threshold_none(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(
        gene="EGFR", auto_fold=True,
        fold_engines=["boltz2", "openfold3"],
        fold_cascade=True,
        cascade_gate_metric="iptm",
        cascade_gate_threshold=None,
        cascade_gate_top_n=3,
    )
    body = _body(httpx_mock)
    assert body["cascadeGateMetric"] == "iptm"
    assert body["cascadeGateTopN"] == 3
    assert "cascadeGateThreshold" not in body  # None → omitted


# ---------------------------------------------------------------------------
# Cascade validation.
# ---------------------------------------------------------------------------


def test_cascade_without_boltz2_raises(client: LigandAI) -> None:
    with pytest.raises(ValueError, match="boltz2"):
        client.peptides.generate(
            gene="EGFR", auto_fold=True,
            fold_engines=["protenix", "openfold3"],
            fold_cascade=True,
        )


def test_cascade_bad_metric_raises(client: LigandAI) -> None:
    with pytest.raises(ValueError, match="cascade_gate_metric"):
        client.peptides.generate(
            gene="EGFR", auto_fold=True,
            fold_engines=["boltz2", "protenix"],
            fold_cascade=True,
            cascade_gate_metric="garbage",
        )


def test_cascade_nonpositive_top_n_raises(client: LigandAI) -> None:
    with pytest.raises(ValueError, match="cascade_gate_top_n"):
        client.peptides.generate(
            gene="EGFR", auto_fold=True,
            fold_engines=["boltz2", "protenix"],
            fold_cascade=True,
            cascade_gate_top_n=0,
        )


# ---------------------------------------------------------------------------
# Alias resolution — camelCase accepted, snake_case wins on conflict.
# ---------------------------------------------------------------------------


def test_camelcase_cascade_aliases_resolve(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(
        gene="EGFR", auto_fold=True,
        foldEngines=["boltz2", "protenix"],
        foldCascade=True,
        cascadeGateMetric="iptm",
        cascadeGateThreshold=0.8,
        cascadeGateTopN=5,
    )
    body = _body(httpx_mock)
    assert body["foldEngines"] == ["boltz2", "protenix"]
    assert body["foldCascade"] is True
    assert body["cascadeGateMetric"] == "iptm"
    assert body["cascadeGateThreshold"] == 0.8
    assert body["cascadeGateTopN"] == 5


def test_snake_wins_over_camel_on_conflict(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(
        gene="EGFR", auto_fold=True,
        fold_engines=["boltz2", "esmfold2"],
        foldEngines=["boltz2", "openfold3"],  # camel ignored
        fold_cascade=True,
        cascade_gate_metric="iptm",            # snake non-default
        cascadeGateMetric="ipsae",             # camel ignored when snake set
        cascade_gate_top_n=4,                  # snake (None-default sentinel)
        cascadeGateTopN=9,                     # camel ignored when snake set
    )
    body = _body(httpx_mock)
    assert body["foldEngines"] == ["boltz2", "esmfold2"]  # snake won
    assert body["cascadeGateMetric"] == "iptm"            # snake won
    assert body["cascadeGateTopN"] == 4                   # snake won
