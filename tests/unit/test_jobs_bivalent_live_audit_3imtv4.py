# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""``client.jobs.stream`` and ``client.bivalent`` sessions against blue's real bodies.

Found by running PUBLISHED ligandai 0.16.0 against production blue on 2026-10-02:

* ``jobs.stream(id)`` called ``GET /api/jobs/{id}/sse``, a route the server does
  not have (its job stream is ``/api/jobs/:id/stream``; a ``session_parallel_*``
  id streams from ``/api/ptf/parallel/:id/stream``), so every call was a 404
  "Not found" while ``Job.stream()`` on the same job worked;
* ``bivalent.list_sessions()`` / ``get_session()`` raised ``ValidationError`` on
  ``created_at: "2026-06-11 16:03:02.793753+00"`` -- raw Postgres text with a
  two-digit UTC offset -- and, separately, on a row whose ``target2_gene`` is
  ``null`` (the second target is not chosen yet): 14 of 14 rows on the dev
  account, so the whole listing was lost.

Row bodies are trimmed copies of the live reads. [bd-dre-3imtv.4]
"""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from ligandai import AsyncLigandAI, LigandAI
from ligandai.types import BivalentSession, Session

BASE = "http://api.ligandai.test"

# GET /api/ligandforge/bivalent -- list rows carry NO linker and NO target object.
BIVALENT_LIST = {
    "sessions": [
        {
            "id": "biv_mq9osetlshdgb3oe",
            "target1_gene": "TRIM21",
            "target2_gene": "UL265_8BQ1",
            "status": "folding_t1",
            "loop_count": 0,
            "fold_loop_count": 0,
            "created_at": "2026-06-11 16:03:02.793753+00",
            "updated_at": "2026-06-11 16:17:48.052315+00",
            "design_mode": "mode2",
        },
        {
            "id": "biv_x",
            "target1_gene": "ACE2",
            "target2_gene": None,  # second target not chosen yet
            "status": "folding_t1",
            "created_at": "2026-05-05 03:01:21.070571+00",
        },
    ]
}
# GET /api/ligandforge/bivalent/{id} -- the full record.
BIVALENT_ONE = {
    "id": "biv_mq9osetlshdgb3oe",
    "user_id": "dev-local-dre",
    "target1_gene": "TRIM21",
    "target1_chain": "B",
    "target2_gene": "UL265_8BQ1",
    "target2_chain": "A",
    "linker_position": "C",
    "linker_length_min": 3,
    "linker_length_max": 6,
    "run1_job_id": None,
    "status": "folding_t1",
    "created_at": "2026-06-11 16:03:02.793753+00",
}


def _mk() -> LigandAI:
    return LigandAI(api_key="lgai_pro_testkey0123456789ABCDEF", base_url=BASE, max_retries=1)


_SSE = b'data: {"type": "progress", "progress": 40, "stage": "folding"}\n\n'
_SSE_HEADERS = {"content-type": "text/event-stream"}


def test_jobs_stream_uses_the_route_the_server_has(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/jobs/fold_1790967983869_4cnphbrdh/stream", content=_SSE, headers=_SSE_HEADERS
    )
    (ev,) = list(_mk().jobs.stream("fold_1790967983869_4cnphbrdh"))
    assert ev.stage == "folding"
    assert ev.progress == 40


def test_jobs_stream_sends_a_parallel_generation_session_to_its_own_stream(
    httpx_mock: HTTPXMock,
) -> None:
    sid = "session_parallel_1790964869524_fbc1a237"
    httpx_mock.add_response(
        url=f"{BASE}/api/ptf/parallel/{sid}/stream", content=_SSE, headers=_SSE_HEADERS
    )
    assert len(list(_mk().jobs.stream(sid))) == 1


async def test_async_jobs_stream_uses_the_same_routes(
    httpx_mock: HTTPXMock, async_client: AsyncLigandAI
) -> None:
    httpx_mock.add_response(url=f"{BASE}/api/jobs/fold_abc/stream", content=_SSE, headers=_SSE_HEADERS)
    httpx_mock.add_response(
        url=f"{BASE}/api/ptf/parallel/session_parallel_1_a/stream", content=_SSE, headers=_SSE_HEADERS
    )
    got = [e async for e in async_client.jobs.stream("fold_abc")]
    got += [e async for e in async_client.jobs.stream("session_parallel_1_a")]
    assert len(got) == 2


def test_postgres_two_digit_offset_parses_in_a_datetime_field() -> None:
    s = Session.model_validate({"id": "a", "created_at": "2026-06-11 16:03:02.793753+00"})
    assert s.created_at is not None
    assert s.created_at.utcoffset().total_seconds() == 0  # type: ignore[union-attr]
    assert (s.created_at.year, s.created_at.microsecond) == (2026, 793753)
    # and the ordinary ISO form is untouched
    assert Session.model_validate({"id": "a", "createdAt": "2026-10-02T18:14:29.555Z"}).created_at


def test_a_str_field_keeps_a_timestamp_looking_value_verbatim() -> None:
    # `status` is typed str: only datetime-typed fields are repaired.
    s = Session.model_validate({"id": "a", "status": "2026-06-11 16:03:02+00"})
    assert s.status == "2026-06-11 16:03:02+00"


def test_bivalent_list_sessions_survives_a_row_with_no_second_target(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(url=f"{BASE}/api/ligandforge/bivalent", json=BIVALENT_LIST)
    one, two = _mk().bivalent.list_sessions()
    assert one.id == "biv_mq9osetlshdgb3oe"
    assert one.target1 is not None and one.target1.gene == "TRIM21"
    assert one.target2 is not None and one.target2.gene == "UL265_8BQ1"
    assert one.created_at is not None and one.created_at.year == 2026
    # a list row carries no linker: absent is None, not a fabricated default
    assert one.linker is None
    # the unchosen second target is None -- not a target whose gene is "unknown"
    assert two.target2 is None


def test_bivalent_get_session_builds_the_linker_from_the_flat_columns(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(url=f"{BASE}/api/ligandforge/bivalent/biv_mq9osetlshdgb3oe", json=BIVALENT_ONE)
    s = _mk().bivalent.get_session("biv_mq9osetlshdgb3oe")
    assert (s.target1.gene, s.target1.chain) == ("TRIM21", "B")  # type: ignore[union-attr]
    assert (s.linker.length_min, s.linker.length_max) == (3, 6)  # type: ignore[union-attr]
    assert s.created_at is not None


async def test_async_bivalent_list_sessions(
    httpx_mock: HTTPXMock, async_client: AsyncLigandAI
) -> None:
    httpx_mock.add_response(url=f"{BASE}/api/ligandforge/bivalent", json=BIVALENT_LIST)
    rows = await async_client.bivalent.list_sessions()
    assert [r.id for r in rows] == ["biv_mq9osetlshdgb3oe", "biv_x"]


def test_bivalent_session_with_only_an_id_and_status_validates() -> None:
    s = BivalentSession.model_validate({"id": "b", "status": "queued"})
    assert s.target1 is None and s.target2 is None and s.linker is None
