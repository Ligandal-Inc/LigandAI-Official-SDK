# Copyright © 2025 Ligandal, Inc. All rights reserved.
"""``Job.stream()`` SSE handling — unknown-event forwarding + targeting events.

[bd-LIGANDAI_ALPHA_V2-x4vt8]

The server (``fastapi_backend/routers/generate.py``'s ``_sse_event()``
helper) formats every message as a STANDARD two-line SSE block::

    event: target_residues_applied
    data: {"gene": "EGFR", ...}

``event:`` and ``data:`` are separate lines, and the event name is NOT
repeated inside the JSON body. Before this fix, ``Job.stream()`` only ever
read ``data:`` lines — the ``event:`` line (and therefore the actual event
name) was silently discarded, and ``_normalize_event_payload()`` fell back
to the generic ``"message"`` type for every such event. This collapsed
``target_residues_applied`` / ``avoid_residues_applied`` / ``targeting_warning``
(and any future, currently-unknown event type) into indistinguishable
``"message"`` events — exactly the "unknown event type gets swallowed"
failure mode item D exists to close.
"""

from __future__ import annotations

import warnings

import pytest
from pytest_httpx import HTTPXMock, IteratorStream

from ligandai._http import HTTPTransport
from ligandai.errors import LigandAITargetingWarning
from ligandai.jobs import Job

BASE = "http://api.ligandai.test"
SSE_URL = f"{BASE}/api/ptf/parallel/sid_test/stream"
STATUS_URL = f"{BASE}/api/ptf/parallel/sid_test/status"


def _make_job(transport: HTTPTransport) -> Job:
    return Job(
        transport,
        "sid_test",
        job_type="generation",
        parser=lambda payload: payload,
        status_path="/api/ptf/parallel/{job_id}/status",
        sse_path="/api/ptf/parallel/{job_id}/stream",
        initial={"id": "sid_test", "type": "generation", "status": "running"},
    )


@pytest.fixture
def transport() -> HTTPTransport:
    t = HTTPTransport(api_key="lgai_pro_test", base_url=BASE, max_retries=1)
    yield t
    t.close()


def test_sse_event_line_name_is_not_dropped(httpx_mock: HTTPXMock, transport: HTTPTransport) -> None:
    """A payload with NO type/event/stage key in its JSON must still resolve
    event_type from the standalone `event:` line — the core x4vt8 SSE fix."""
    httpx_mock.add_response(
        url=SSE_URL,
        stream=IteratorStream([
            b'event: target_residues_applied\n'
            b'data: {"gene": "EGFR", "resolved_resnums": [45,47], '
            b'"unresolved_resnums": [], "selected_residue_count": 2, '
            b'"total_residue_count": 10}\n\n',
        ]),
    )
    httpx_mock.add_response(url=STATUS_URL, json={"id": "sid_test", "status": "running"})
    job = _make_job(transport)
    events = list(job.stream())
    named = [e for e in events if e.event_type == "target_residues_applied"]
    assert len(named) == 1
    assert named[0].payload["gene"] == "EGFR"
    assert named[0].payload["resolved_resnums"] == [45, 47]
    # Must NOT have collapsed to the generic fallback.
    assert all(e.event_type != "message" for e in named)


def test_sse_avoid_residues_applied_event_surfaced(httpx_mock: HTTPXMock, transport: HTTPTransport) -> None:
    httpx_mock.add_response(
        url=SSE_URL,
        stream=IteratorStream([
            b'event: avoid_residues_applied\n'
            b'data: {"gene": "EGFR", "pocket_before": 42, "pocket_after": 30, '
            b'"removed_resnums": [36,37,38], "named_excluded": 3, '
            b'"shell_excluded": 8, "shell_angstrom": 8.0, '
            b'"unresolved_resnums": [], "remaining_resnums": [], '
            b'"degenerate": false}\n\n',
        ]),
    )
    httpx_mock.add_response(url=STATUS_URL, json={"id": "sid_test", "status": "running"})
    job = _make_job(transport)
    events = [e for e in job.stream() if e.event_type == "avoid_residues_applied"]
    assert len(events) == 1
    assert events[0].payload["pocket_before"] == 42
    assert events[0].payload["pocket_after"] == 30


def test_sse_unknown_future_event_type_is_forwarded_not_dropped(
    httpx_mock: HTTPXMock, transport: HTTPTransport
) -> None:
    """A completely novel, never-seen event type must still reach the caller
    with its real name — 'unknown events are forwarded, not discarded'."""
    httpx_mock.add_response(
        url=SSE_URL,
        stream=IteratorStream([
            b'event: some_future_event_nobody_has_seen_yet\n'
            b'data: {"foo": "bar", "n": 7}\n\n',
        ]),
    )
    httpx_mock.add_response(url=STATUS_URL, json={"id": "sid_test", "status": "running"})
    job = _make_job(transport)
    events = list(job.stream())
    matching = [e for e in events if e.event_type == "some_future_event_nobody_has_seen_yet"]
    assert len(matching) == 1
    # payload carries the original JSON keys, PLUS the merged-in `event` key
    # (since the JSON body had no type/event/stage key of its own) — nothing
    # from the original body is lost.
    assert matching[0].payload["foo"] == "bar"
    assert matching[0].payload["n"] == 7


def test_sse_payload_with_own_type_key_is_not_overridden(
    httpx_mock: HTTPXMock, transport: HTTPTransport
) -> None:
    """When the JSON body already carries its own type/event/stage key, the
    standalone `event:` line must not clobber it."""
    httpx_mock.add_response(
        url=SSE_URL,
        stream=IteratorStream([
            b'event: outer_name\n'
            b'data: {"type": "inner_name", "x": 1}\n\n',
        ]),
    )
    httpx_mock.add_response(url=STATUS_URL, json={"id": "sid_test", "status": "running"})
    job = _make_job(transport)
    all_events = list(job.stream())
    assert any(e.event_type == "inner_name" for e in all_events)
    assert not any(e.event_type == "outer_name" for e in all_events)


def test_sse_multiple_events_in_one_stream_each_get_correct_name(
    httpx_mock: HTTPXMock, transport: HTTPTransport
) -> None:
    httpx_mock.add_response(
        url=SSE_URL,
        stream=IteratorStream([
            b'event: target_residues_applied\ndata: {"gene": "EGFR", "n": 1}\n\n',
            b'event: avoid_residues_applied\ndata: {"gene": "EGFR", "n": 2}\n\n',
            b'event: progress\ndata: {"pct": 50}\n\n',
        ]),
    )
    httpx_mock.add_response(url=STATUS_URL, json={"id": "sid_test", "status": "running"})
    job = _make_job(transport)
    events = list(job.stream())
    types = [e.event_type for e in events if e.event_type in
             ("target_residues_applied", "avoid_residues_applied", "progress")]
    assert types == ["target_residues_applied", "avoid_residues_applied", "progress"]


# ---------------------------------------------------------------------------
# targeting_warning — dual channel: JobEvent stream AND warnings.warn
# ---------------------------------------------------------------------------


def test_targeting_warning_event_also_emits_sdk_warning(
    httpx_mock: HTTPXMock, transport: HTTPTransport
) -> None:
    httpx_mock.add_response(
        url=SSE_URL,
        stream=IteratorStream([
            b'event: targeting_warning\n'
            b'data: {"gene": "EGFR", "code": "degenerate_pocket_after_avoid", '
            b'"pocket_residue_count": 1, "message": "pocket nearly empty after avoid"}\n\n',
        ]),
    )
    httpx_mock.add_response(url=STATUS_URL, json={"id": "sid_test", "status": "running"})
    job = _make_job(transport)
    with pytest.warns(LigandAITargetingWarning, match=r"pocket nearly empty after avoid"):
        events = [e for e in job.stream() if e.event_type == "targeting_warning"]
    assert len(events) == 1
    assert events[0].payload["code"] == "degenerate_pocket_after_avoid"


def test_degenerate_flag_true_warns_even_with_unrecognized_event_name(
    httpx_mock: HTTPXMock, transport: HTTPTransport
) -> None:
    """Contract §3: 'code is an open enum — treat unknown codes as warnings.'
    A payload independently carrying degenerate=true must warn even if the
    event name/code itself is one the SDK has never seen."""
    httpx_mock.add_response(
        url=SSE_URL,
        stream=IteratorStream([
            b'event: some_new_targeting_signal\n'
            b'data: {"gene": "EGFR", "code": "brand_new_code_v2", "degenerate": true}\n\n',
        ]),
    )
    httpx_mock.add_response(url=STATUS_URL, json={"id": "sid_test", "status": "running"})
    job = _make_job(transport)
    with pytest.warns(LigandAITargetingWarning):
        list(job.stream())


def test_non_targeting_events_do_not_emit_warnings(httpx_mock: HTTPXMock, transport: HTTPTransport) -> None:
    httpx_mock.add_response(
        url=SSE_URL,
        stream=IteratorStream([
            b'event: progress\ndata: {"pct": 50}\n\n',
        ]),
    )
    httpx_mock.add_response(url=STATUS_URL, json={"id": "sid_test", "status": "running"})
    job = _make_job(transport)
    with warnings.catch_warnings():
        warnings.simplefilter("error", LigandAITargetingWarning)
        list(job.stream())
