# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""[bd-LIGANDAI_ALPHA_V2-44z2l] Addressing ONE design out of a fold BATCH.

THE GAP. A ``ligandforge_folding`` job — every fold a customer runs on
ligandai.com — is a BATCH: smartfold folds every peptide it was handed.
Measured on production 2026-09-23, 271 of 291 linked jobs span more than one
distinct sequence; ``smartfold_session_parallel_1777525211377_e5e64a6e_AQP2_
1777525246917`` alone holds 66 designs, 27 of them at or above the 0.67
interface gate. bd-dre-dk6l9 made the metric surfaces REFUSE such a job id
(``job_is_a_batch_of_designs``) because returning ``candidates[0]`` reported one
arbitrary design's ``peptide_ipsae`` as the job's. Correct — and it left those 66
measured values with no id the SDK could ask for.

``client.folds.list_designs(job_id)`` is the missing hop, and
``foldrow_<ptf_fold_results.id>`` is the handle it hands back. Against HEAD this
module does not import: neither the methods nor the helpers exist.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from pytest_httpx import HTTPXMock

from ligandai import LigandAI
from ligandai.resources.folds import (
    FOLD_DESIGN_HANDLE_PREFIX,
    _resolve_fold_result_id,
    fold_design_handle,
    fold_result_id_from_handle,
    is_fold_design_handle,
)

BASE = "http://api.ligandai.test"

#: A real multi-design batch job id, measured on production 2026-09-23.
BATCH_JOB = "smartfold_session_parallel_1777525211377_e5e64a6e_AQP2_1777525246917"


@pytest.fixture
def client() -> LigandAI:
    return LigandAI(api_key="lgai_pro_test123", base_url=BASE, max_retries=1)


def _design(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "fold_handle": "foldrow_216530",
        "fold_result_id": 216530,
        "fold_job_id": BATCH_JOB,
        "sample_idx": 0,
        "gene": "AQP2",
        "sequence": "ACDEFGHIKLMNPQRSTVWY",
        "peptide_length": 20,
        "peptide_length_source": "scored_sequence",
        "peptide_ipsae": 0.8777092,
        "peptide_ipsae_status": "ok",
        "peptide_ipsae_reason": "measured",
        "peptide_ipsae_reason_text": "peptide_ipsae was computed for this fold",
        "peptide_ipsae_attribution": "fold_result_id",
        "iptm": 0.81,
        "plddt_mean": 0.86,
        "coord_degenerate": False,
        "has_structure": True,
        "delta_g": -9.1,
        "kd_nm": 86.197,
        "kd_status": "ok",
        "kd_reason": None,
        "kd_scorer": "deltaforge_v10_2",
        "kd_scorer_version": "10.2.0",
        "kd_aggregate_method": "boltzmann_parallel",
        "kd_ipsae_gate": 0.67,
    }
    row.update(over)
    return row


def _listing(designs: list[dict[str, Any]] | None = None, **over: Any) -> dict[str, Any]:
    rows = designs if designs is not None else [_design()]
    out: dict[str, Any] = {
        "fold_job_id": BATCH_JOB,
        "attribution": "gpu_job_id",
        "is_batch": True,
        "design_count": len(rows),
        "row_count": len(rows),
        "returned": len(rows),
        "limit": 200,
        "offset": 0,
        "has_more": False,
        "kd_ipsae_gate": 0.67,
        "metric_notes": {},
        "designs": rows,
    }
    out.update(over)
    return out


# ── The handle codec ─────────────────────────────────────────────────────────


def test_handle_round_trips_a_fold_row_primary_key() -> None:
    assert FOLD_DESIGN_HANDLE_PREFIX == "foldrow_"
    assert fold_design_handle(216530) == "foldrow_216530"
    assert fold_result_id_from_handle("foldrow_216530") == 216530
    assert is_fold_design_handle("foldrow_216530") is True


def test_a_bare_integer_is_not_a_design_handle() -> None:
    # ⛔ THE POINT OF THE NAMESPACE. ``fold_job_ids=`` is a string-typed JOB id
    # parameter. A bare integer in it would be disambiguated only by "no job id
    # happens to be all digits" — true of all 11,177 gpu_jobs and 5,026
    # folding_jobs ids on 2026-09-23, and guaranteed by nothing. The day that
    # stops holding, the request returns ANOTHER DESIGN'S numbers with a 200.
    assert fold_result_id_from_handle("216530") is None
    assert is_fold_design_handle("216530") is False
    assert is_fold_design_handle(216530) is False


@pytest.mark.parametrize(
    "bad",
    [
        "foldrow_", "foldrow_0", "foldrow_-1", "foldrow_1.5", "foldrow_1e3",
        "FOLDROW_1", "xfoldrow_1", "foldrow_1/../2", "",
        "fold_1790178651800_e0jnusm4j", BATCH_JOB, None, 1.5, {},
    ],
)
def test_handle_decoder_refuses_every_near_miss(bad: Any) -> None:
    assert fold_result_id_from_handle(bad) is None


def test_handle_encoder_refuses_a_non_positive_id() -> None:
    for bad in (0, -1, "nope", None):
        with pytest.raises(ValueError):
            fold_design_handle(bad)  # type: ignore[arg-type]


# ── The PAE round trip: listing -> handle -> matrix ──────────────────────────


def test_resolve_fold_result_id_accepts_a_design_handle() -> None:
    # Closes the round trip without weakening the guard below it.
    assert _resolve_fold_result_id("foldrow_216530") == 216530
    assert _resolve_fold_result_id(216530) == 216530


def test_resolve_fold_result_id_still_refuses_a_job_id() -> None:
    # ⛔ The guard this must not weaken: a job id parsed as a row id returns a
    # DIFFERENT fold's matrix with a 200.
    with pytest.raises(ValueError, match="positive integer"):
        _resolve_fold_result_id("fold_1790178651800_e0jnusm4j")


def test_download_pae_accepts_the_handle_the_listing_emitted(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/folds/216530/pae",
        method="GET",
        content=b"\x00\x01\x02",
        headers={"X-Pae-Scale-Angstrom": "0.12549019607843137"},
    )
    out = client.folds.download_pae("foldrow_216530", decode=False)
    assert out == b"\x00\x01\x02"
    assert httpx_mock.get_requests()[-1].url.path == "/api/v1/folds/216530/pae"


# ── list_designs: the discovery hop ─────────────────────────────────────────


def test_list_designs_gets_the_batch_and_forwards_paging(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"{re.escape(BASE)}/api/v1/folds/{re.escape(BATCH_JOB)}/designs.*"),
        json=_listing(),
    )
    out = client.folds.list_designs(BATCH_JOB, limit=50, offset=100)

    req = httpx_mock.get_requests()[-1]
    assert req.url.path == f"/api/v1/folds/{BATCH_JOB}/designs"
    assert req.url.params["limit"] == "50"
    assert req.url.params["offset"] == "100"
    assert out["is_batch"] is True
    assert out["designs"][0]["fold_handle"] == "foldrow_216530"


def test_list_designs_refuses_a_design_handle_before_spending_a_round_trip() -> None:
    # ⛔ No httpx_mock: a request here would be a failure. Laundering a design
    # handle into the job-id space is exactly the confusion the namespace exists
    # to prevent, and the SDK must not do it on the customer's behalf.
    c = LigandAI(api_key="lgai_pro_test123", base_url=BASE, max_retries=1)
    with pytest.raises(ValueError, match="per-design fold handle"):
        c.folds.list_designs("foldrow_216530")
    c.close()


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"limit": 0}, "limit must be"),
        ({"limit": 501}, "limit must be"),
        ({"offset": -1}, "offset must be"),
    ],
)
def test_list_designs_validates_paging_client_side(kwargs: Any, match: str) -> None:
    c = LigandAI(api_key="lgai_pro_test123", base_url=BASE, max_retries=1)
    with pytest.raises(ValueError, match=match):
        c.folds.list_designs(BATCH_JOB, **kwargs)
    c.close()


def test_list_designs_requires_a_job_id() -> None:
    c = LigandAI(api_key="lgai_pro_test123", base_url=BASE, max_retries=1)
    with pytest.raises(ValueError, match="job_id is required"):
        c.folds.list_designs("   ")
    c.close()


def test_iter_designs_pages_until_has_more_is_false(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    page1 = _listing(
        [_design(fold_handle="foldrow_1", fold_result_id=1),
         _design(fold_handle="foldrow_2", fold_result_id=2)],
        has_more=True, row_count=3, design_count=3, offset=0,
    )
    page2 = _listing(
        [_design(fold_handle="foldrow_3", fold_result_id=3)],
        has_more=False, row_count=3, design_count=3, offset=2,
    )
    for payload in (page1, page2):
        httpx_mock.add_response(
            method="GET",
            url=re.compile(rf"{re.escape(BASE)}/api/v1/folds/.*/designs.*"),
            json=payload,
        )
    handles = [d["fold_handle"] for d in client.folds.iter_designs(BATCH_JOB, page_size=2)]
    assert handles == ["foldrow_1", "foldrow_2", "foldrow_3"]
    assert httpx_mock.get_requests()[-1].url.params["offset"] == "2"


def test_iter_designs_stops_on_an_empty_page(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    # A server that keeps saying has_more must not spin this forever.
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"{re.escape(BASE)}/api/v1/folds/.*/designs.*"),
        json=_listing([], has_more=True),
    )
    assert list(client.folds.iter_designs(BATCH_JOB)) == []


# ── Metric discipline on what comes back ────────────────────────────────────


def test_peptide_ipsae_is_bounded_and_a_computed_zero_survives(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    rows = [
        _design(fold_handle="foldrow_1", fold_result_id=1, peptide_ipsae=0.8777092),
        # ⛔ A COMPUTED 0.0 IS A MEASUREMENT ("no interface"), not a gap —
        # 14,351 of 120,890 non-null values in this column are exactly 0. Any
        # truthiness test downstream erases every one of them.
        _design(
            fold_handle="foldrow_2", fold_result_id=2, peptide_ipsae=0.0,
            kd_status="below_ipsae_gate", kd_nm=None, delta_g=None,
            kd_scorer=None, kd_scorer_version=None, kd_aggregate_method=None,
            kd_reason="peptide_ipsae 0 is below the interface gate 0.67",
        ),
    ]
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"{re.escape(BASE)}/api/v1/folds/.*/designs.*"),
        json=_listing(rows),
    )
    out = client.folds.list_designs(BATCH_JOB)
    values = [d["peptide_ipsae"] for d in out["designs"]]

    # peptide_ipsae is the A-B interface metric and is bounded at 1.0. A value
    # above it would mean the pre-fold PREDICTED column had been substituted —
    # that one emits > 1.0.
    assert max(values) <= 1.0
    zero_row = out["designs"][1]
    assert zero_row["peptide_ipsae"] == 0.0
    assert zero_row["peptide_ipsae"] is not None
    assert zero_row["peptide_ipsae_status"] == "ok"


def test_a_gated_out_design_is_labelled_not_blanked(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    # ⛔ "below the interface gate" is a REFUSAL TO QUOTE. DeltaForge scores an
    # interface; below the gate there is none to score. It must never arrive as
    # a missing or zero affinity, which a selection loop reads as "weak binder".
    row = _design(
        peptide_ipsae=0.41, kd_status="below_ipsae_gate", kd_nm=None, delta_g=None,
        kd_reason="peptide_ipsae 0.41 is below the interface gate 0.67",
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"{re.escape(BASE)}/api/v1/folds/.*/designs.*"),
        json=_listing([row]),
    )
    d = client.folds.list_designs(BATCH_JOB)["designs"][0]
    assert d["kd_status"] == "below_ipsae_gate"
    assert d["kd_nm"] is None and d["delta_g"] is None
    assert d["kd_nm"] != 0
    assert "gate" in (d["kd_reason"] or "")


def test_peptide_length_travels_beside_every_quoted_kd(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    # The deployed DeltaForge scorer has NO measured-affinity evaluation, so a
    # Kd ordering is not a validated affinity ordering and length is its most
    # obvious confound. It must be present on every row a caller could rank.
    rows = [
        _design(fold_handle="foldrow_1", fold_result_id=1, kd_nm=12.0, peptide_length=18),
        _design(fold_handle="foldrow_2", fold_result_id=2, kd_nm=340.0, peptide_length=31),
    ]
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"{re.escape(BASE)}/api/v1/folds/.*/designs.*"),
        json=_listing(rows),
    )
    out = client.folds.list_designs(BATCH_JOB)
    ranked = sorted(out["designs"], key=lambda d: d["kd_nm"])
    assert [d["peptide_length"] for d in ranked] == [18, 31]
    assert all(d.get("peptide_length") is not None for d in out["designs"] if d.get("kd_nm"))


def test_overall_ipsae_is_not_served_beside_the_binder_metric(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    # A detached peptide can read overall ipsae 0.93 at peptide_ipsae 0.00.
    # Publishing both invites the substitution, so the listing carries only the
    # binder metric.
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"{re.escape(BASE)}/api/v1/folds/.*/designs.*"),
        json=_listing(),
    )
    d = client.folds.list_designs(BATCH_JOB)["designs"][0]
    assert "peptide_ipsae" in d
    assert "ipsae" not in d


# ── The round trip the bead asked for ───────────────────────────────────────


def test_a_batch_handle_is_accepted_by_filter_folds(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    # batch job id -> designs -> per-design handle -> that design's metrics.
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"{re.escape(BASE)}/api/v1/folds/.*/designs.*"),
        json=_listing(),
    )
    handle = client.folds.list_designs(BATCH_JOB)["designs"][0]["fold_handle"]

    httpx_mock.add_response(
        method="POST",
        url=f"{BASE}/api/v1/folds/filter-by-criteria",
        json={
            "total": 1, "passing": 1, "failing": 0,
            "results": [{
                "fold_job_id": handle, "passes": True,
                "metrics": {
                    "fold_handle": handle, "fold_result_id": 216530,
                    "peptide_ipsae": 0.8777092, "peptide_ipsae_status": "ok",
                    "peptide_ipsae_reason": "measured",
                },
            }],
        },
    )
    out = client.evaluator.filter_folds([handle], criteria={})
    import json as _json

    body = _json.loads(httpx_mock.get_requests()[-1].read())
    assert body["fold_job_ids"] == [handle]
    assert out["results"][0]["metrics"]["fold_handle"] == handle


# ── Sync/async parity ───────────────────────────────────────────────────────


def test_async_mirrors_the_sync_signatures() -> None:
    import inspect

    from ligandai.resources.folds import AsyncFolds, Folds

    for name in ("list_designs", "iter_designs"):
        sync_fn = getattr(Folds, name)
        async_fn = getattr(AsyncFolds, name)
        assert (
            list(inspect.signature(sync_fn).parameters)
            == list(inspect.signature(async_fn).parameters)
        ), name
    assert inspect.iscoroutinefunction(AsyncFolds.list_designs)
    assert inspect.isasyncgenfunction(AsyncFolds.iter_designs)
