# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""The SDK surfaces a receptor-MSA refusal as an object, not a string.

[bd-LIGANDAI_ALPHA_V2-7r1sb, follows bd-dre-7ae4n]

Andre's requirement: "the SDK should have capabilities to tag this and indicate
the flaw and failure reason with what's required to fix it."

Three things have to hold, and only the first was true before:

  1. A failed job's structured payload must SURVIVE. Every LigandAIJobError
     raise site used to pass only message/job_id/job_status, so `.response` was
     always None for a failed job while `self._info.result` — the dict carrying
     the answer — sat unused in scope. The sibling timeout path already kept it
     as `server_state`; job failures did not.
  2. A receptor-MSA refusal must arrive as its OWN type carrying the tag, the
     flaw, the per-rung receipts, the ordered fixes and the retryable verdict.
  3. A diagnosis that is NOT a refusal must NOT change the error class. A fold
     that self-healed its MSA and then failed for some other reason must not be
     reported as an MSA failure — that sends the caller after the wrong cause.
"""

from __future__ import annotations

import pytest

from ligandai.diagnostics import DIAGNOSTIC_CODES
from ligandai.errors import (
    RECEPTOR_MSA_TAG_SELF_HEALED,
    RECEPTOR_MSA_TAG_UNAVAILABLE,
    LigandAIError,
    LigandAIJobError,
    LigandAIReceptorMsaUnavailable,
    error_from_response,
    job_error_from_info,
    receptor_msa_diagnosis,
)


# The platform's real emitted shape (fastapi_backend/services/msa_diagnosis.py,
# schema msa-diagnosis/1), trimmed to the fields a caller acts on.
REFUSAL = {
    "schema": "msa-diagnosis/1",
    "tag": RECEPTOR_MSA_TAG_UNAVAILABLE,
    "failure_class": RECEPTOR_MSA_TAG_UNAVAILABLE,
    "severity": "error",
    "retryable": False,
    "job_id": "job-abc",
    "gene": "BRD4ET",
    "uniprot_id": None,
    "receptor_chains": ["A", "B"],
    "primary_chains": ["B"],
    "recovered_by": {},
    "missing_chains": ["A"],
    "rungs": [
        {
            "rung": "local_msa_service", "order": 1, "ok": False,
            "cause": "orphan_sequence", "error": "HTTP 422",
            "endpoint": "http://127.0.0.1:5080", "elapsed_ms": 1204,
            "chains_recovered": [], "realized_depth": {},
            "attempts": [{"endpoint": "http://127.0.0.1:5080", "ok": True}],
            "detail": {},
        },
        {
            "rung": "modal_volume_cache", "order": 2, "ok": False,
            "cause": "cache_miss", "error": "no entry for 2 probed key(s)",
            "endpoint": "modal-volume://ligandal-boltz2-models/msa_cache",
            "elapsed_ms": 318, "chains_recovered": [], "realized_depth": {},
            "attempts": [], "detail": {"probes": [{"path": "msa_cache/deadbeef/A.csv", "result": "miss"}]},
        },
        {
            "rung": "uniprot_alignment", "order": 3, "ok": False,
            "cause": "uniprot_accession_unresolved",
            "error": "no UniProt accession for gene='BRD4ET'",
            "endpoint": None, "elapsed_ms": 12,
            "chains_recovered": [], "realized_depth": {}, "attempts": [], "detail": {},
        },
    ],
    "rung_order": ["local_msa_service", "modal_volume_cache", "uniprot_alignment"],
    "required_fixes": [
        {
            "id": "receptor_has_no_detectable_homologs", "order": 1,
            "who": "caller", "automatable": False,
            "action": "The MSA service answered correctly and this sequence has no detectable homologs...",
            "because": ["local_msa_service: orphan_sequence"],
            "verify": "...",
        },
        {
            "id": "supply_uniprot_accession", "order": 2,
            "who": "caller", "automatable": False,
            "action": "Give the platform the receptor's UniProt accession ... pass uniprotId / msaSourceGene ...",
            "because": ["uniprot_alignment: uniprot_accession_unresolved"],
            "verify": "...",
        },
    ],
    "flaw": "Receptor chain(s) ['A'] have no usable alignment from any source ...",
    "human": "MSA FAILURE — receptor fold refused rather than degraded.\n  FIXES, in order:\n    1. ...",
    "elapsed_ms": 1534,
    "beads": ["bd-LIGANDAI_ALPHA_V2-7r1sb", "bd-dre-7ae4n"],
}


def test_extractor_finds_the_diagnosis_and_ignores_junk():
    assert receptor_msa_diagnosis({"retry_msa_diagnosis": REFUSAL}) is REFUSAL
    assert receptor_msa_diagnosis({"msa_diagnosis": REFUSAL}) is REFUSAL
    # Absent, wrong type, or missing the tag => absent. A half-written value
    # must read as "no diagnosis", never as a malformed one.
    assert receptor_msa_diagnosis(None) is None
    assert receptor_msa_diagnosis({}) is None
    assert receptor_msa_diagnosis({"retry_msa_diagnosis": "nope"}) is None
    assert receptor_msa_diagnosis({"retry_msa_diagnosis": {"schema": "x"}}) is None


def test_a_refusal_becomes_its_own_typed_error_with_the_whole_receipt():
    exc = job_error_from_info(
        "Job job-abc ended with status failed",
        job_id="job-abc",
        job_status="failed",
        result={"retry_msa_diagnosis": REFUSAL, "fold_retry_count": 1},
    )

    assert isinstance(exc, LigandAIReceptorMsaUnavailable)
    # Still catchable by the classes callers already write.
    assert isinstance(exc, LigandAIJobError)
    assert isinstance(exc, LigandAIError)

    # THE TAG — a stable machine identifier, suitable for branching and metrics.
    assert exc.tag == RECEPTOR_MSA_TAG_UNAVAILABLE
    assert exc.code == RECEPTOR_MSA_TAG_UNAVAILABLE
    assert exc.schema == "msa-diagnosis/1"

    # THE FLAW — one sentence, plus the chains it is about.
    assert "no usable alignment" in exc.flaw
    assert exc.missing_chains == ["A"]
    assert exc.receptor_chains == ["A", "B"]

    # THE FAILURE REASON — per-rung, with the endpoint and the wall time, so an
    # operator can tell "the service was down" from "the service said no".
    assert [r["rung"] for r in exc.rungs] == exc.diagnosis["rung_order"]
    local = exc.rung("local_msa_service")
    assert local["cause"] == "orphan_sequence"
    assert local["endpoint"] == "http://127.0.0.1:5080"
    assert local["elapsed_ms"] == 1204
    assert exc.rung("no_such_rung") is None

    # WHAT IS REQUIRED TO FIX IT — ordered, and actionable without prose parsing.
    assert [f["id"] for f in exc.required_fixes] == [
        "receptor_has_no_detectable_homologs", "supply_uniprot_accession"]
    assert all("action" in f and "who" in f for f in exc.required_fixes)
    assert "uniprotId" in exc.fixes_text()

    # RETRYABLE — false here, and that is load-bearing: retrying an orphan
    # receptor with no resolvable accession burns budget on a fold that cannot
    # change.
    assert exc.retryable is False

    # The prose half is still reachable, and is the fallback for an unknown
    # schema major.
    assert "FIXES, in order:" in exc.human_text()

    # repr is greppable rather than a wall of JSON.
    assert "receptor_msa_unavailable" in repr(exc)
    assert "retryable=False" in repr(exc)


def test_the_structured_payload_survives_on_a_plain_job_failure():
    """The gap that made this necessary: `.response` used to always be None."""
    result = {"error": "modal timeout", "call_id": "fc-123", "fold_retry_count": 2}
    exc = job_error_from_info(
        "Job job-xyz ended with status failed",
        job_id="job-xyz", job_status="failed", result=result,
    )
    assert type(exc) is LigandAIJobError
    assert exc.response == result          # <- was None before
    assert exc.job_id == "job-xyz"


def test_a_self_healed_diagnosis_does_not_relabel_an_unrelated_failure():
    """A compensated MSA is not a reason to blame MSA for the failure."""
    healed = dict(REFUSAL)
    healed["tag"] = RECEPTOR_MSA_TAG_SELF_HEALED
    healed["missing_chains"] = []
    healed["recovered_by"] = {"A": "uniprot_alignment"}

    exc = job_error_from_info(
        "Job job-def ended with status failed",
        job_id="job-def", job_status="failed",
        result={"retry_msa_diagnosis": healed, "error": "GPU OOM"},
    )
    assert type(exc) is LigandAIJobError
    assert not isinstance(exc, LigandAIReceptorMsaUnavailable)
    # ...but the diagnosis is still reachable, so a caller can see the fold's
    # alignment came from a fallback source when interpreting its ipSAE.
    assert receptor_msa_diagnosis(exc.response)["recovered_by"] == {"A": "uniprot_alignment"}


def test_an_unknown_schema_major_still_yields_the_human_text():
    future = dict(REFUSAL)
    future["schema"] = "msa-diagnosis/99"
    exc = job_error_from_info("x", job_id="j", job_status="failed",
                              result={"retry_msa_diagnosis": future})
    assert isinstance(exc, LigandAIReceptorMsaUnavailable)
    assert exc.schema == "msa-diagnosis/99"
    assert "FIXES, in order:" in exc.human_text()


@pytest.mark.parametrize("status", [409, 424, 500])
def test_an_http_error_carrying_the_diagnosis_maps_to_the_typed_class(status):
    """/api/retry-fold answers 200 with status='refused' so the body survives,
    but a route that surfaces it as an HTTP error must not lose the type."""
    exc = error_from_response(
        status,
        {"message": "receptor MSA unavailable", "job_id": "job-abc",
         "status": "refused", "msa_diagnosis": REFUSAL},
        request_id="req-1",
    )
    assert isinstance(exc, LigandAIReceptorMsaUnavailable)
    assert exc.status_code == status
    assert exc.request_id == "req-1"
    assert exc.missing_chains == ["A"]


def test_a_bare_diagnosis_body_is_recognised_too():
    exc = error_from_response(424, dict(REFUSAL))
    assert isinstance(exc, LigandAIReceptorMsaUnavailable)
    assert exc.tag == RECEPTOR_MSA_TAG_UNAVAILABLE


def test_the_condition_is_in_the_diagnostics_registry():
    """So a caller watching `job.diagnostics` sees it without catching."""
    for code, expected_severity in (
        ("receptor_msa_self_healed", "warning"),
        ("receptor_msa_unavailable", "error"),
        ("receptor_msa_parent_not_sliceable", "warning"),
    ):
        entry = DIAGNOSTIC_CODES[code]
        assert entry["severity"] == expected_severity
        assert entry["stage"] == "fold"
        assert entry["title"] and entry["remediation"]
        # ⛔ Degrading to an MSA-free fold is never the remedy.
        low = entry["remediation"].lower()
        assert "single-sequence" not in low
        assert "without an msa" not in low
    # The refusal's remediation must point at the structured fields, not prose.
    assert "required_fixes" in DIAGNOSTIC_CODES["receptor_msa_unavailable"]["remediation"]
