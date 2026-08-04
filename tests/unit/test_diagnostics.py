# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""The diagnostics envelope: parsing, forward-compat, and registry parity.

[bd-LIGANDAI_ALPHA_V2-x4vt8.3.2]

The customer bug this closes was a SILENCE bug: an exclusion silently no-op'd
and nothing told the user. So the single most important property under test
here is that NOTHING IS EVER DROPPED — not an unknown code, not an unknown
severity, not an unknown envelope key. A layer that filters to a known-code
allowlist reintroduces the exact defect.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ligandai.diagnostics import (
    DIAGNOSTIC_CODES,
    DIAGNOSTIC_EVENT,
    DIAGNOSTIC_SCHEMA_VERSION,
    Diagnostic,
    diagnostic_from_payload,
    diagnostics_from_container,
    is_diagnostic_envelope,
    known_codes,
    make_diagnostic,
    merge_diagnostics,
    severity_rank,
    spec_for,
    worst_severity,
)

# ---------------------------------------------------------------------------
# Registry parity — the SDK copy must not drift from shared/warnings.json
# ---------------------------------------------------------------------------

#: Codes this SDK emits BEFORE any HTTP call. They are not in the shared
#: registry (proposed additions) so parity ignores them; every other code must
#: match the platform file byte-for-byte.
CLIENT_ONLY_CODES = frozenset(
    {
        "targeting_region_swapped",
        "targeting_chain_ambiguous",
        "avoid_shell_disabled",
        "degenerate_pocket_risk",
        "targeting_contradiction",
    }
)

_SHARED_REGISTRY_CANDIDATES = (
    os.environ.get("LIGANDAI_SHARED_WARNINGS_JSON") or "",
    "/mnt/backup/LIGANDAI_ALPHA_V2/shared/warnings.json",
)


def _shared_registry() -> dict[str, dict[str, str]] | None:
    for candidate in _SHARED_REGISTRY_CANDIDATES:
        if not candidate:
            continue
        path = Path(candidate)
        if path.is_file():
            with path.open("r", encoding="utf-8") as fh:
                return dict(json.load(fh).get("codes") or {})
    return None


def test_registry_is_byte_identical_to_shared_warnings_json() -> None:
    """The SDK copies the taxonomy rather than importing it, so a drift guard
    is the only thing keeping the two in sync. Skips where the server repo
    isn't checked out (published sdist, CI runner)."""
    shared = _shared_registry()
    if shared is None:
        pytest.skip("shared/warnings.json not available on this machine")

    sdk_shared_codes = {k: v for k, v in DIAGNOSTIC_CODES.items() if k not in CLIENT_ONLY_CODES}
    assert set(sdk_shared_codes) == set(shared), "code set drifted from shared/warnings.json"
    for code, spec in shared.items():
        for field in ("severity", "stage", "title", "remediation", "doc_anchor"):
            assert sdk_shared_codes[code][field] == spec[field], f"{code}.{field} drifted"


def test_client_only_codes_are_flagged_as_such() -> None:
    shared = _shared_registry()
    if shared is None:
        pytest.skip("shared/warnings.json not available on this machine")
    for code in CLIENT_ONLY_CODES:
        assert code in DIAGNOSTIC_CODES
        assert code not in shared
        assert DIAGNOSTIC_CODES[code]["stage"] == "client"


def test_every_registry_entry_is_well_formed() -> None:
    for code, spec in DIAGNOSTIC_CODES.items():
        assert spec["severity"] in ("info", "warning", "error"), code
        assert spec["stage"] in ("request", "generate", "fold", "results", "client"), code
        assert spec["title"], code
        assert not spec["doc_anchor"].startswith("/"), f"{code}: anchors are relative"


def test_known_codes_and_spec_for() -> None:
    assert known_codes() == sorted(DIAGNOSTIC_CODES)
    assert spec_for("avoid_residues_none_resolved")["severity"] == "error"
    assert spec_for("no_such_code_anywhere") == {}


def test_sdk_version_codes_have_no_doc_anchor() -> None:
    """Deliberate: they apply to BOTH SDKs, so a python-only docs link would be
    wrong. The actionable part lives in details['upgrade_command']."""
    for code in ("sdk_outdated", "sdk_unsupported", "sdk_version_unknown"):
        assert DIAGNOSTIC_CODES[code]["doc_anchor"] == ""


# ---------------------------------------------------------------------------
# make_diagnostic — the emitter path
# ---------------------------------------------------------------------------


def test_make_diagnostic_enriches_from_registry() -> None:
    d = make_diagnostic("avoid_residues_none_resolved", "11 of 11 not found", {"requested": 11})
    assert d.severity == "error"
    assert d.stage == "generate"
    assert d.title == "None of your excluded residues were found"
    assert "PDB author numbering" in d.remediation or "numbering mismatch" in d.remediation
    assert d.details == {"requested": 11}
    assert d.occurred_at.endswith("Z")
    assert d.schema_version == DIAGNOSTIC_SCHEMA_VERSION
    assert d.is_known_code is True


def test_make_diagnostic_works_for_unregistered_code() -> None:
    d = make_diagnostic("a_code_from_the_future", "something happened")
    assert d.code == "a_code_from_the_future"
    assert d.severity == "warning"
    assert d.stage == "results"
    assert d.title == "a_code_from_the_future"
    assert d.is_known_code is False


def test_make_diagnostic_coerces_invalid_severity_and_stage() -> None:
    d = make_diagnostic("param_ignored", "x", severity="catastrophic", stage="nowhere")
    assert d.severity == "warning"
    assert d.stage == "results"


def test_explicit_severity_overrides_registry_default() -> None:
    """Contract rule 3: the envelope's own severity is authoritative."""
    d = make_diagnostic("avoid_conformance_clean", "escalated", severity="error")
    assert d.severity == "error"


# ---------------------------------------------------------------------------
# diagnostic_from_payload — the RECEIVER path. Never filters.
# ---------------------------------------------------------------------------


def test_round_trip_of_a_full_envelope() -> None:
    payload = {
        "schema_version": 1,
        "code": "avoid_residues_none_resolved",
        "severity": "error",
        "stage": "generate",
        "title": "None of your excluded residues were found",
        "message": "11 of 12 residues you asked to avoid were not found",
        "remediation": "Use PDB author numbering.",
        "details": {"requested": 12, "unresolved_resnums": [36, 37]},
        "doc_anchor": "sdk/python/allosteric-targeting#event-avoid",
        "occurred_at": "2026-08-04T18:22:01Z",
        "subject_id": "BRD4",
    }
    d = diagnostic_from_payload(payload)
    assert d is not None
    assert d.to_dict() == payload


def test_unknown_code_passes_through_untouched() -> None:
    """THE forward-compat requirement. A code this build has never seen must
    surface in full rather than be filtered away."""
    d = diagnostic_from_payload(
        {
            "schema_version": 2,
            "code": "some_code_invented_next_year",
            "severity": "error",
            "stage": "fold",
            "message": "the new thing went wrong",
            "details": {"n": 3},
        }
    )
    assert d is not None
    assert d.code == "some_code_invented_next_year"
    assert d.is_known_code is False
    assert d.severity == "error"
    assert d.stage == "fold"
    assert d.message == "the new thing went wrong"
    assert d.details == {"n": 3}
    assert d.schema_version == 2


def test_unknown_severity_is_preserved_and_ranks_as_at_least_warning() -> None:
    """A future severity must never be silently DEMOTED below the warn
    threshold — that would re-hide exactly what it was invented to surface."""
    d = diagnostic_from_payload({"code": "x", "severity": "catastrophic", "message": "m"})
    assert d is not None
    assert d.severity == "catastrophic"
    assert d.at_least("warning") is True
    assert severity_rank("catastrophic") == severity_rank("warning")


def test_unknown_envelope_keys_survive_a_round_trip() -> None:
    d = diagnostic_from_payload({"code": "param_ignored", "message": "m", "future_field": {"a": 1}})
    assert d is not None
    assert d.raw["future_field"] == {"a": 1}
    assert d.to_dict()["future_field"] == {"a": 1}


def test_missing_fields_are_backfilled_from_the_registry() -> None:
    """An older server that emits a bare {code, message} still yields a fully
    formed, actionable diagnostic."""
    d = diagnostic_from_payload({"code": "degenerate_pocket_after_avoid", "message": "boom"})
    assert d is not None
    assert d.severity == "error"
    assert d.stage == "generate"
    assert d.title == "Exclusion left almost no pocket to steer with"
    assert d.remediation
    assert d.occurred_at


def test_camelcase_envelope_is_tolerated() -> None:
    d = diagnostic_from_payload(
        {
            "code": "param_ignored",
            "message": "m",
            "docAnchor": "sdk/python/api-reference",
            "occurredAt": "2026-08-04T00:00:00Z",
            "subjectId": "job_1",
            "schemaVersion": 1,
        }
    )
    assert d is not None
    assert d.doc_anchor == "sdk/python/api-reference"
    assert d.occurred_at == "2026-08-04T00:00:00Z"
    assert d.subject_id == "job_1"
    # camelCase spellings must not leak back out as duplicate keys
    assert "docAnchor" not in d.to_dict()


def test_payload_with_no_code_still_yields_a_diagnostic() -> None:
    d = diagnostic_from_payload({"message": "something"})
    assert d is not None
    assert d.code == "unspecified"
    assert d.message == "something"


def test_non_mapping_payload_returns_none() -> None:
    assert diagnostic_from_payload("nope") is None
    assert diagnostic_from_payload(None) is None
    assert diagnostic_from_payload([1, 2]) is None


def test_non_dict_details_is_wrapped_not_discarded() -> None:
    d = diagnostic_from_payload({"code": "x", "message": "m", "details": [1, 2, 3]})
    assert d is not None
    assert d.details == {"value": [1, 2, 3]}


# ---------------------------------------------------------------------------
# envelope detection + container extraction
# ---------------------------------------------------------------------------


def test_is_diagnostic_envelope() -> None:
    assert is_diagnostic_envelope({"code": "x", "severity": "warning"}) is True
    assert is_diagnostic_envelope({"code": "x", "schema_version": 1}) is True
    # A legacy targeting event carries `code` but none of the envelope markers.
    assert is_diagnostic_envelope({"gene": "EGFR", "code": "x", "degenerate": True}) is False
    assert is_diagnostic_envelope({"severity": "warning"}) is False
    assert is_diagnostic_envelope("nope") is False


def test_diagnostics_from_container_top_level_and_nested() -> None:
    payload = {
        "id": "job_1",
        "diagnostics": [{"code": "param_ignored", "message": "top"}],
        "result": {"diagnostics": [{"code": "avoid_conformance_violation", "message": "nested"}]},
    }
    found = diagnostics_from_container(payload)
    assert [d.message for d in found] == ["top", "nested"]


def test_diagnostics_from_container_is_empty_not_none_when_absent() -> None:
    assert diagnostics_from_container({"id": "job_1"}) == []
    assert diagnostics_from_container(None) == []
    assert diagnostics_from_container({"diagnostics": "not-a-list"}) == []


# ---------------------------------------------------------------------------
# merge / dedupe / severity ordering
# ---------------------------------------------------------------------------


def test_merge_drops_exact_repeats_and_reports_new_ones() -> None:
    a = make_diagnostic("param_ignored", "same", {"param": "x"})
    b = make_diagnostic("param_ignored", "same", {"param": "x"})
    c = make_diagnostic("param_ignored", "other", {"param": "y"})
    merged, added = merge_diagnostics([a], [b, c])
    assert len(merged) == 2
    assert [d.message for d in added] == ["other"]


def test_merge_preserves_order() -> None:
    first = make_diagnostic("param_ignored", "1")
    second = make_diagnostic("param_dropped_in_transport", "2")
    merged, _ = merge_diagnostics([first], [second])
    assert [d.code for d in merged] == ["param_ignored", "param_dropped_in_transport"]


def test_worst_severity() -> None:
    assert worst_severity([]) is None
    diags = [
        make_diagnostic("avoid_conformance_clean", "i"),
        make_diagnostic("param_ignored", "w"),
        make_diagnostic("avoid_residues_none_resolved", "e"),
    ]
    assert worst_severity(diags) == "error"


def test_severity_rank_ordering() -> None:
    assert severity_rank("info") < severity_rank("warning") < severity_rank("error")
    assert severity_rank(None) == severity_rank("warning")


# ---------------------------------------------------------------------------
# formatting + constants
# ---------------------------------------------------------------------------


def test_format_includes_the_actionable_parts() -> None:
    d = make_diagnostic("avoid_residues_none_resolved", "11 of 11 missing", {"requested": 11})
    text = d.format()
    assert "avoid_residues_none_resolved" in text
    assert "11 of 11 missing" in text
    assert "NO EFFECT" in text  # the remediation
    assert "requested" in text
    assert str(d) == text


def test_canonical_event_name_is_diagnostic() -> None:
    assert DIAGNOSTIC_EVENT == "diagnostic"


def test_diagnostic_is_constructible_directly() -> None:
    d = Diagnostic(code="x", message="m")
    assert d.severity == "warning"
    assert d.details == {}
    assert d.to_dict()["code"] == "x"
