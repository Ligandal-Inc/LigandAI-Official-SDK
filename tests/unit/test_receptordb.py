# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""ReceptorDB-restricted client behaviour."""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from ligandai import ReceptorDBClient
from ligandai.errors import NotSupportedOnReceptorDB

BASE = "http://receptordb.test"


def test_anonymous_construction() -> None:
    c = ReceptorDBClient(base_url=BASE)
    assert c.api_key is None
    c.close()


def test_search_works_anonymously(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/receptordb/search?query=EGFR&limit=10",
        json=[{"id": "c1", "complexName": "EGFR", "gene": "EGFR"}],
    )
    c = ReceptorDBClient(base_url=BASE, max_retries=1)
    hits = c.search("EGFR")
    assert len(hits) == 1
    c.close()


def test_fold_without_key_raises() -> None:
    c = ReceptorDBClient(base_url=BASE)
    with pytest.raises(NotSupportedOnReceptorDB):
        c.fold(sequences=["MAEEPQSD"], target_gene="EGFR")
    c.close()


def test_generate_without_key_raises() -> None:
    c = ReceptorDBClient(base_url=BASE)
    with pytest.raises(NotSupportedOnReceptorDB):
        c.generate(gene="EGFR", num_peptides=10)
    c.close()


def test_with_key_passes_to_peptides(httpx_mock: HTTPXMock) -> None:
    """When api_key set, fold/generate routes through the peptides namespace."""
    httpx_mock.add_response(
        url=f"{BASE}/api/folding/predict",
        method="POST",
        json={"jobId": "job1", "status": "queued"},
    )
    c = ReceptorDBClient(api_key="lgai_pro_x", base_url=BASE, max_retries=1)
    job = c.fold(sequences=["MAEEPQSD"], target_gene="EGFR")
    assert job.id == "job1"
    c.close()
