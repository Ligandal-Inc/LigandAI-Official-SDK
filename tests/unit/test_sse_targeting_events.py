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


# ---------------------------------------------------------------------------
# Canonical `diagnostic` SSE event + job.diagnostics accumulation
# [bd-LIGANDAI_ALPHA_V2-x4vt8.3.2]
#
# The customer's exclusion silently no-op'd and NOTHING told them. These tests
# pin the three paths that now do: the canonical event, the REST diagnostics
# array, and locally-reconstructed legacy events. The single most important
# property is that an unrecognized code is never filtered out.
# ---------------------------------------------------------------------------

import json as _json  # noqa: E402

from ligandai.diagnostics import DIAGNOSTIC_EVENT  # noqa: E402


def _sse(event: str, payload: dict) -> bytes:
    return f"event: {event}\ndata: {_json.dumps(payload)}\n\n".encode()


NONE_RESOLVED = {
    "schema_version": 1,
    "code": "avoid_residues_none_resolved",
    "severity": "error",
    "stage": "generate",
    "title": "None of your excluded residues were found",
    "message": "None of the 11 residues you asked to avoid (36-46) were found on BRD4.",
    "remediation": "Re-run with residue numbers taken from the structure itself.",
    "details": {"requested": 11, "unresolved_resnums": [36, 37, 38]},
    "doc_anchor": "sdk/python/allosteric-targeting#event-avoid",
    "occurred_at": "2026-08-04T18:22:01Z",
    "subject_id": "BRD4",
}


def test_canonical_diagnostic_event_lands_on_job_diagnostics(
    httpx_mock: HTTPXMock, transport: HTTPTransport
) -> None:
    httpx_mock.add_response(url=SSE_URL, stream=IteratorStream([_sse("diagnostic", NONE_RESOLVED)]))
    httpx_mock.add_response(url=STATUS_URL, json={"id": "sid_test", "status": "running"})
    job = _make_job(transport)
    with pytest.warns(LigandAITargetingWarning, match=r"None of the 11 residues"):
        events = list(job.stream())

    assert len(job.diagnostics) == 1
    diag = job.diagnostics[0]
    assert diag.code == "avoid_residues_none_resolved"
    assert diag.severity == "error"
    assert diag.stage == "generate"
    assert diag.subject_id == "BRD4"
    assert diag.details["unresolved_resnums"] == [36, 37, 38]
    assert job.worst_diagnostic_severity == "error"
    assert job.diagnostics_at_least("error") == [diag]

    # The frame is ALSO a normal stream event, named for what it is.
    diagnostic_events = [e for e in events if e.event_type == DIAGNOSTIC_EVENT]
    assert len(diagnostic_events) == 1
    assert diagnostic_events[0].payload["code"] == "avoid_residues_none_resolved"
    # ...while the envelope's own `stage` (WHERE it happened) is preserved.
    assert diagnostic_events[0].stage == "generate"


def test_unknown_diagnostic_code_is_never_filtered(
    httpx_mock: HTTPXMock, transport: HTTPTransport
) -> None:
    """THE forward-compat requirement: a code this SDK build has never heard of
    must reach the caller in full, with its unknown envelope keys intact."""
    future = {
        "schema_version": 3,
        "code": "solvent_shell_conflict_v4",
        "severity": "error",
        "stage": "fold",
        "message": "a thing this SDK has never heard of went wrong",
        "details": {"n": 3},
        "future_field": {"nested": True},
    }
    httpx_mock.add_response(url=SSE_URL, stream=IteratorStream([_sse("diagnostic", future)]))
    httpx_mock.add_response(url=STATUS_URL, json={"id": "sid_test", "status": "running"})
    job = _make_job(transport)
    with pytest.warns(LigandAITargetingWarning, match=r"never heard of"):
        list(job.stream())

    assert len(job.diagnostics) == 1
    diag = job.diagnostics[0]
    assert diag.code == "solvent_shell_conflict_v4"
    assert diag.is_known_code is False
    assert diag.severity == "error"
    assert diag.stage == "fold"
    assert diag.raw["future_field"] == {"nested": True}
    assert diag.to_dict()["future_field"] == {"nested": True}


def test_info_severity_diagnostic_is_recorded_without_warning(
    httpx_mock: HTTPXMock, transport: HTTPTransport
) -> None:
    clean = {
        "schema_version": 1,
        "code": "avoid_conformance_clean",
        "severity": "info",
        "stage": "fold",
        "message": "all 25 designs avoided the excluded residues",
        "details": {},
    }
    httpx_mock.add_response(url=SSE_URL, stream=IteratorStream([_sse("diagnostic", clean)]))
    httpx_mock.add_response(url=STATUS_URL, json={"id": "sid_test", "status": "running"})
    job = _make_job(transport)
    with warnings.catch_warnings():
        warnings.simplefilter("error", LigandAITargetingWarning)
        list(job.stream())
    assert [d.code for d in job.diagnostics] == ["avoid_conformance_clean"]
    assert job.worst_diagnostic_severity == "info"
    assert job.diagnostics_at_least("warning") == []


def test_repeated_identical_diagnostics_are_deduplicated(
    httpx_mock: HTTPXMock, transport: HTTPTransport
) -> None:
    httpx_mock.add_response(
        url=SSE_URL,
        stream=IteratorStream([_sse("diagnostic", NONE_RESOLVED), _sse("diagnostic", NONE_RESOLVED)]),
    )
    httpx_mock.add_response(url=STATUS_URL, json={"id": "sid_test", "status": "running"})
    job = _make_job(transport)
    with pytest.warns(LigandAITargetingWarning):
        list(job.stream())
    assert len(job.diagnostics) == 1


def test_multiple_distinct_diagnostics_all_accumulate(
    httpx_mock: HTTPXMock, transport: HTTPTransport
) -> None:
    second = dict(NONE_RESOLVED, code="param_ignored", severity="warning", stage="request",
                  message="`foo` was accepted but had no effect", details={"param": "foo"})
    httpx_mock.add_response(
        url=SSE_URL,
        stream=IteratorStream([_sse("diagnostic", NONE_RESOLVED), _sse("diagnostic", second)]),
    )
    httpx_mock.add_response(url=STATUS_URL, json={"id": "sid_test", "status": "running"})
    job = _make_job(transport)
    with pytest.warns(LigandAITargetingWarning):
        list(job.stream())
    assert [d.code for d in job.diagnostics] == ["avoid_residues_none_resolved", "param_ignored"]


def test_diagnostics_property_returns_a_snapshot(
    httpx_mock: HTTPXMock, transport: HTTPTransport
) -> None:
    httpx_mock.add_response(url=SSE_URL, stream=IteratorStream([_sse("diagnostic", NONE_RESOLVED)]))
    httpx_mock.add_response(url=STATUS_URL, json={"id": "sid_test", "status": "running"})
    job = _make_job(transport)
    with pytest.warns(LigandAITargetingWarning):
        list(job.stream())
    snapshot = job.diagnostics
    snapshot.clear()
    assert len(job.diagnostics) == 1


# ---------------------------------------------------------------------------
# REST surface — wait()-only callers must see the same thing
# ---------------------------------------------------------------------------


def test_rest_diagnostics_reach_a_wait_only_caller(
    httpx_mock: HTTPXMock, transport: HTTPTransport
) -> None:
    httpx_mock.add_response(
        url=STATUS_URL,
        json={"id": "sid_test", "status": "completed", "diagnostics": [NONE_RESOLVED]},
    )
    job = _make_job(transport)
    with pytest.warns(LigandAITargetingWarning, match=r"None of the 11 residues"):
        job.refresh()
    assert [d.code for d in job.diagnostics] == ["avoid_residues_none_resolved"]


def test_rest_diagnostics_nested_in_result(
    httpx_mock: HTTPXMock, transport: HTTPTransport
) -> None:
    httpx_mock.add_response(
        url=STATUS_URL,
        json={"id": "sid_test", "status": "completed", "result": {"diagnostics": [NONE_RESOLVED]}},
    )
    job = _make_job(transport)
    with pytest.warns(LigandAITargetingWarning):
        job.refresh()
    assert [d.code for d in job.diagnostics] == ["avoid_residues_none_resolved"]


def test_repeat_polling_does_not_re_warn(httpx_mock: HTTPXMock, transport: HTTPTransport) -> None:
    """A wait() loop polls every few seconds. It must warn ONCE."""
    body = {"id": "sid_test", "status": "running", "diagnostics": [NONE_RESOLVED]}
    httpx_mock.add_response(url=STATUS_URL, json=body, is_reusable=True)
    job = _make_job(transport)
    with pytest.warns(LigandAITargetingWarning):
        job.refresh()
    with warnings.catch_warnings():
        warnings.simplefilter("error", LigandAITargetingWarning)
        job.refresh()
        job.refresh()
    assert len(job.diagnostics) == 1


def test_initial_submit_payload_diagnostics_are_absorbed(transport: HTTPTransport) -> None:
    """Request-stage diagnostics ride the submit response, before any poll."""
    with pytest.warns(LigandAITargetingWarning, match=r"had no effect"):
        job = Job(
            transport,
            "sid_test",
            job_type="generation",
            parser=lambda payload: payload,
            status_path="/api/ptf/parallel/{job_id}/status",
            initial={
                "id": "sid_test",
                "status": "queued",
                "diagnostics": [
                    {
                        "code": "param_ignored",
                        "severity": "warning",
                        "stage": "request",
                        "message": "`avoid_shell_angstrom` was accepted but had no effect",
                        "details": {"param": "avoid_shell_angstrom"},
                    }
                ],
            },
        )
    assert [d.code for d in job.diagnostics] == ["param_ignored"]


def test_job_with_no_diagnostics_has_an_empty_list(transport: HTTPTransport) -> None:
    job = _make_job(transport)
    assert job.diagnostics == []
    assert job.worst_diagnostic_severity is None
    assert job.diagnostics_at_least("info") == []


# ---------------------------------------------------------------------------
# Legacy targeting events -> canonical envelopes (old server, new SDK)
# ---------------------------------------------------------------------------


def test_legacy_avoid_event_with_nothing_resolved_becomes_an_error_diagnostic(
    httpx_mock: HTTPXMock, transport: HTTPTransport
) -> None:
    """The exact customer failure, on a server whose Express normalizer has not
    landed yet: every named residue unresolved, zero excluded."""
    httpx_mock.add_response(
        url=SSE_URL,
        stream=IteratorStream([
            _sse(
                "avoid_residues_applied",
                {
                    "gene": "BRD4",
                    "pocket_before": 42,
                    "pocket_after": 42,
                    "removed_resnums": [],
                    "named_excluded": 0,
                    "shell_excluded": 0,
                    "unresolved_resnums": [36, 37, 38, 39],
                    "degenerate": False,
                },
            )
        ]),
    )
    httpx_mock.add_response(url=STATUS_URL, json={"id": "sid_test", "status": "running"})
    job = _make_job(transport)
    with pytest.warns(LigandAITargetingWarning, match=r"NO EFFECT"):
        list(job.stream())
    assert len(job.diagnostics) == 1
    diag = job.diagnostics[0]
    assert diag.code == "avoid_residues_none_resolved"
    assert diag.severity == "error"
    assert diag.subject_id == "BRD4"
    assert diag.details["source"] == "legacy_sse_event"
    assert diag.details["unresolved_resnums"] == [36, 37, 38, 39]


def test_legacy_avoid_event_partially_resolved_is_a_warning(
    httpx_mock: HTTPXMock, transport: HTTPTransport
) -> None:
    httpx_mock.add_response(
        url=SSE_URL,
        stream=IteratorStream([
            _sse(
                "avoid_residues_applied",
                {
                    "gene": "EGFR",
                    "removed_resnums": [36, 37],
                    "named_excluded": 2,
                    "unresolved_resnums": [900],
                },
            )
        ]),
    )
    httpx_mock.add_response(url=STATUS_URL, json={"id": "sid_test", "status": "running"})
    job = _make_job(transport)
    with pytest.warns(LigandAITargetingWarning):
        list(job.stream())
    assert [d.code for d in job.diagnostics] == ["avoid_residues_unresolved"]
    assert job.diagnostics[0].severity == "warning"
    assert job.diagnostics[0].details["resolved_count"] == 2


def test_legacy_target_event_none_resolved(
    httpx_mock: HTTPXMock, transport: HTTPTransport
) -> None:
    httpx_mock.add_response(
        url=SSE_URL,
        stream=IteratorStream([
            _sse(
                "target_residues_applied",
                {"gene": "EGFR", "resolved_resnums": [], "unresolved_resnums": [1, 2, 3]},
            )
        ]),
    )
    httpx_mock.add_response(url=STATUS_URL, json={"id": "sid_test", "status": "running"})
    job = _make_job(transport)
    with pytest.warns(LigandAITargetingWarning, match=r"unfiltered pocket"):
        list(job.stream())
    assert [d.code for d in job.diagnostics] == ["target_residues_none_resolved"]


def test_clean_legacy_events_produce_no_diagnostics(
    httpx_mock: HTTPXMock, transport: HTTPTransport
) -> None:
    """A run where everything resolved must stay silent — inventing a
    diagnostic for a clean run is how a diagnostics channel becomes noise."""
    httpx_mock.add_response(
        url=SSE_URL,
        stream=IteratorStream([
            _sse(
                "avoid_residues_applied",
                {"gene": "EGFR", "removed_resnums": [36], "named_excluded": 1,
                 "unresolved_resnums": [], "degenerate": False},
            ),
            _sse(
                "target_residues_applied",
                {"gene": "EGFR", "resolved_resnums": [45, 47], "unresolved_resnums": []},
            ),
        ]),
    )
    httpx_mock.add_response(url=STATUS_URL, json={"id": "sid_test", "status": "running"})
    job = _make_job(transport)
    with warnings.catch_warnings():
        warnings.simplefilter("error", LigandAITargetingWarning)
        list(job.stream())
    assert job.diagnostics == []


def test_legacy_targeting_warning_is_recorded_as_a_diagnostic_too(
    httpx_mock: HTTPXMock, transport: HTTPTransport
) -> None:
    httpx_mock.add_response(
        url=SSE_URL,
        stream=IteratorStream([
            _sse(
                "targeting_warning",
                {"gene": "EGFR", "code": "degenerate_pocket_after_avoid",
                 "pocket_residue_count": 1, "message": "pocket nearly empty after avoid"},
            )
        ]),
    )
    httpx_mock.add_response(url=STATUS_URL, json={"id": "sid_test", "status": "running"})
    job = _make_job(transport)
    with pytest.warns(LigandAITargetingWarning, match=r"pocket nearly empty after avoid"):
        list(job.stream())
    assert [d.code for d in job.diagnostics] == ["degenerate_pocket_after_avoid"]
    assert job.diagnostics[0].severity == "error"


def test_legacy_and_canonical_for_the_same_issue_warn_only_once(
    httpx_mock: HTTPXMock, transport: HTTPTransport
) -> None:
    """A fully-upgraded server emits BOTH. The user must not be told twice."""
    httpx_mock.add_response(
        url=SSE_URL,
        stream=IteratorStream([
            _sse("diagnostic", NONE_RESOLVED),
            _sse(
                "avoid_residues_applied",
                {"gene": "BRD4", "removed_resnums": [], "named_excluded": 0,
                 "unresolved_resnums": [36, 37, 38]},
            ),
        ]),
    )
    httpx_mock.add_response(url=STATUS_URL, json={"id": "sid_test", "status": "running"})
    job = _make_job(transport)
    with pytest.warns(LigandAITargetingWarning) as record:
        list(job.stream())
    targeting = [w for w in record if issubclass(w.category, LigandAITargetingWarning)]
    assert len(targeting) == 1
    # Both are still RECORDED — provenance is visible, nothing is dropped.
    assert [d.code for d in job.diagnostics] == [
        "avoid_residues_none_resolved",
        "avoid_residues_none_resolved",
    ]
    assert job.diagnostics[1].details["source"] == "legacy_sse_event"


def test_unknown_event_carrying_degenerate_flag_still_warns(
    httpx_mock: HTTPXMock, transport: HTTPTransport
) -> None:
    """Unchanged pre-existing behaviour, now also producing a diagnostic."""
    httpx_mock.add_response(
        url=SSE_URL,
        stream=IteratorStream([
            _sse("some_new_targeting_signal",
                 {"gene": "EGFR", "code": "brand_new_code_v2", "degenerate": True})
        ]),
    )
    httpx_mock.add_response(url=STATUS_URL, json={"id": "sid_test", "status": "running"})
    job = _make_job(transport)
    with pytest.warns(LigandAITargetingWarning):
        list(job.stream())
    assert [d.code for d in job.diagnostics] == ["brand_new_code_v2"]
    assert job.diagnostics[0].severity == "warning"
