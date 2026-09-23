# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""[bd-LIGANDAI_ALPHA_V2-etbo3] An ambiguous target name must be ANSWERABLE
from the SDK, without a second round trip to a different endpoint.

The customer this comes from uploaded three genuinely different conformational
states of the BRD4 ET domain and named them all ``BRD4ET``. The platform
refuses rather than guessing — a silently-picked conformer folds, scores and
reaches a results table looking exactly like a correct one. What these tests
pin is that the refusal now carries the candidates, and that a remembered
choice is a decision the customer made, visibly, and can take back.
"""
from __future__ import annotations

import pytest

from ligandai.errors import (
    LigandAIAmbiguousTarget,
    LigandAIValidationError,
    error_from_response,
)
from ligandai.resources.targets import (
    Targets,
    TargetCandidate,
    TargetNameChoice,
    TargetResolution,
)


# The real thing, measured against unpopsci@gmail.com on 2026-09-23: 41 rows
# named BRD4ET which are THREE structures and 38 duplicate re-uploads.
CANDIDATES = [
    {
        "uploadId": "userprotein_7", "store": "user_proteins", "id": 7,
        "name": "Brd4ET_closed", "matchedOn": "gene", "matchedValue": "BRD4ET",
        "customName": "Brd4ET_closed", "originalName": "Brd4ET_closed_state_fix.pdb",
        "gene": "BRD4ET", "contentSha256": "6bf672", "sizeBytes": 44388,
        "chainCount": 1, "residueCount": 67, "uploadedAt": "2026-06-23T09:49:50Z",
        "duplicateCount": 15, "duplicateIds": list(range(8, 23)),
        "aliases": ["Brd4ET_closed_minimal", "Brd4ET_closed_state_fix", "BRD4"],
        "chosen": False,
    },
    {
        "uploadId": "userprotein_42", "store": "user_proteins", "id": 42,
        "name": "Brd4ET_closed_state_form", "matchedOn": "gene", "matchedValue": "BRD4ET",
        "customName": "Brd4ET_closed_state_form", "originalName": "Brd4ET_closed_state_form.pdb",
        "gene": "BRD4ET", "contentSha256": "fd0b69", "sizeBytes": 117719,
        "chainCount": 1, "residueCount": 67, "uploadedAt": "2026-07-01T10:00:00Z",
        "duplicateCount": 23, "duplicateIds": list(range(43, 66)), "chosen": False,
    },
    {
        "uploadId": "userprotein_598", "store": "user_proteins", "id": 598,
        "name": "Brd4ET_isoform_target_state", "matchedOn": "gene", "matchedValue": "BRD4ET",
        "customName": "Brd4ET_isoform_target_state", "originalName": "Brd4ET_isoform_target_state.pdb",
        "gene": "BRD4ET", "contentSha256": "645459", "sizeBytes": 43296,
        "chainCount": 1, "residueCount": 68, "uploadedAt": "2026-09-23T11:20:00Z",
        "duplicateCount": 0, "duplicateIds": [], "chosen": False,
    },
]

CHOICE = {
    "requestedName": "BRD4ET", "normalizedName": "brd4et",
    "uploadId": "userprotein_598", "store": "user_proteins", "rowId": 598,
    "contentSha256": "645459", "source": "user",
    "chosenAt": "2026-09-23T18:00:00.000Z",
}


class FakeTransport:
    """Records the calls made, replays canned payloads keyed by (method, path)."""

    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def request(self, method, path, **kw):
        self.calls.append((method, path, kw))
        return self.payloads.get((method, path), self.payloads.get(path, {}))


# ────────────────────────────────────────────────────────────────────────────
# THE ERROR CARRIES THE ANSWER
# ────────────────────────────────────────────────────────────────────────────
def test_409_becomes_a_typed_error_with_candidates():
    err = error_from_response(
        409,
        {
            "error": "ambiguous_target_name",
            "code": "ambiguous_target_name",
            "message": '"BRD4ET" matches 3 different uploaded structure(s).',
            "targetName": "BRD4ET",
            "candidates": CANDIDATES,
            "retryable": False,
        },
    )
    assert isinstance(err, LigandAIAmbiguousTarget)
    assert err.target_name == "BRD4ET"
    assert [c.upload_id for c in err.candidates] == [
        "userprotein_7", "userprotein_42", "userprotein_598",
    ]
    assert err.upload_ids[2] == "userprotein_598"


def test_a_caller_can_tell_the_structures_apart_without_a_second_call():
    # The point of the whole change: catch, inspect, re-call. No trip to a
    # different endpoint to find out what the ids mean.
    err = error_from_response(
        409, {"error": "ambiguous_target_name", "message": "…", "candidates": CANDIDATES},
    )
    assert isinstance(err, LigandAIAmbiguousTarget)
    sizes = [c.size_bytes for c in err.candidates]
    assert sizes == [44388, 117719, 43296]
    assert [c.residue_count for c in err.candidates] == [67, 67, 68]
    assert [c.name for c in err.candidates] == [
        "Brd4ET_closed", "Brd4ET_closed_state_form", "Brd4ET_isoform_target_state",
    ]


def test_the_duplicates_are_counted_not_listed_as_choices():
    # 41 rows, 3 choices. A 41-entry list is not a choice anyone can make.
    err = error_from_response(
        409, {"error": "ambiguous_target_name", "message": "…", "candidates": CANDIDATES},
    )
    assert isinstance(err, LigandAIAmbiguousTarget)
    assert len(err.candidates) == 3
    assert sum(c.duplicate_count for c in err.candidates) == 38


def test_str_renders_something_a_human_can_act_on():
    err = error_from_response(
        409, {"error": "ambiguous_target_name", "message": "pick one", "candidates": CANDIDATES},
    )
    text = str(err)
    assert "userprotein_598" in text
    assert "Brd4ET_isoform_target_state" in text
    assert "+15 identical re-uploads" in text


def test_a_missing_metadata_field_is_none_not_zero():
    # "— residues" is true; "0 residues" is a number a customer would act on.
    c = TargetCandidate({"uploadId": "userprotein_1"})
    assert c.residue_count is None
    assert c.chain_count is None
    assert c.size_bytes is None
    assert c.duplicate_count == 0


# ────────────────────────────────────────────────────────────────────────────
# LISTING BEFORE CALLING
# ────────────────────────────────────────────────────────────────────────────
def test_candidates_lists_the_distinct_structures_for_a_name():
    t = FakeTransport({"/api/targets": {
        "name": "BRD4ET", "outcome": "ambiguous", "resolvesTo": "nothing",
        "candidates": CANDIDATES, "matchedRowCount": 41, "choice": None,
    }})
    cands = Targets(t).candidates("BRD4ET")
    assert t.calls[0][1] == "/api/targets"
    assert t.calls[0][2]["params"] == {"name": "BRD4ET"}
    assert len(cands) == 3
    assert all(isinstance(c, TargetCandidate) for c in cands)


def test_an_ambiguous_name_is_not_usable_without_a_choice():
    r = TargetResolution({
        "name": "BRD4ET", "outcome": "ambiguous", "resolvesTo": "nothing",
        "candidates": CANDIDATES, "choice": None,
    })
    assert r.usable is False
    assert r.matched_row_count is None or isinstance(r.matched_row_count, int)


def test_an_ambiguous_name_WITH_a_live_choice_is_usable():
    r = TargetResolution({
        "name": "BRD4ET", "outcome": "ambiguous", "resolvesTo": "upload",
        "candidates": CANDIDATES, "choice": CHOICE,
    })
    assert r.usable is True
    assert r.choice is not None
    assert r.choice.upload_id == "userprotein_598"
    # ⛔ Still reported as ambiguous. The name IS ambiguous; the choice is how it
    # gets answered, not evidence it never was.
    assert r.outcome == "ambiguous"


def test_a_stale_choice_is_surfaced_and_does_not_resolve():
    r = TargetResolution({
        "name": "BRD4ET", "outcome": "ambiguous", "resolvesTo": "nothing",
        "candidates": CANDIDATES, "choice": None, "staleChoice": CHOICE,
    })
    assert r.usable is False
    assert r.stale_choice is not None
    assert r.stale_choice.upload_id == "userprotein_598"


# ────────────────────────────────────────────────────────────────────────────
# THE SAVED CHOICE
# ────────────────────────────────────────────────────────────────────────────
def test_choose_puts_a_store_qualified_id():
    t = FakeTransport({("PUT", "/api/targets/choice"): {"choice": CHOICE}})
    choice = Targets(t).choose("BRD4ET", "userprotein_598")
    method, path, kw = t.calls[0]
    assert (method, path) == ("PUT", "/api/targets/choice")
    assert kw["json"] == {"name": "BRD4ET", "uploadId": "userprotein_598"}
    assert isinstance(choice, TargetNameChoice)
    assert choice.upload_id == "userprotein_598"
    assert choice.source == "user"


def test_choose_refuses_an_empty_upload_id_before_any_http():
    t = FakeTransport({})
    with pytest.raises(LigandAIValidationError):
        Targets(t).choose("BRD4ET", "")
    assert t.calls == []


def test_choose_refuses_an_empty_name_before_any_http():
    t = FakeTransport({})
    with pytest.raises(LigandAIValidationError):
        Targets(t).choose("   ", "userprotein_598")
    assert t.calls == []


def test_clear_choice_deletes_and_reports_whether_anything_went():
    t = FakeTransport({("DELETE", "/api/targets/choice"): {"ok": True, "cleared": True}})
    assert Targets(t).clear_choice("BRD4ET") is True
    method, path, kw = t.calls[0]
    assert (method, path) == ("DELETE", "/api/targets/choice")
    assert kw["params"] == {"name": "BRD4ET"}


def test_choices_lists_every_saved_pick():
    t = FakeTransport({("GET", "/api/targets/choices"): {"choices": [CHOICE]}})
    rows = Targets(t).choices()
    assert len(rows) == 1
    assert rows[0].name == "BRD4ET"
    assert rows[0].chosen_at == "2026-09-23T18:00:00.000Z"


# ────────────────────────────────────────────────────────────────────────────
# THE INDEX COLLAPSES DUPLICATES
# ────────────────────────────────────────────────────────────────────────────
def test_index_entries_carry_the_collapsed_duplicate_count():
    from ligandai.resources.targets import TargetIndex

    idx = TargetIndex({
        "targets": [{
            "store": "user_proteins", "id": 7, "uploadId": "userprotein_7",
            "names": [{"name": "Brd4ET_closed", "tier": "gene_label"},
                      {"name": "BRD4ET", "tier": "gene"}],
            "contentSha256": "6bf672", "duplicateCount": 15,
            "duplicateIds": list(range(8, 23)), "sizeBytes": 44388,
            "chainCount": 1, "residueCount": 67,
            "uploadedAt": "2026-06-23T09:49:50Z", "chosen": False,
        }],
        "count": 1,
        "ambiguousNames": ["brd4et"],
        "ambiguous": [{"name": "brd4et", "distinctCount": 3, "chosen": "userprotein_598"}],
        "choices": [CHOICE],
    })
    assert idx.targets[0].duplicate_count == 15
    assert idx.targets[0].residue_count == 67
    # Kept as plain strings for the published SDK's list[str] contract…
    assert idx.ambiguous_names == ["brd4et"]
    # …with the structured form beside it.
    assert idx.ambiguous[0]["distinctCount"] == 3
    assert idx.choices[0].upload_id == "userprotein_598"


def test_ambiguous_names_tolerates_the_object_shape_too():
    from ligandai.resources.targets import TargetIndex

    idx = TargetIndex({"targets": [], "ambiguousNames": [{"name": "brd4et"}]})
    assert idx.ambiguous_names == ["brd4et"]


def test_a_candidate_keeps_the_other_names_its_duplicates_answer_to():
    # ⛔ Collapsing 41 rows to 3 entries must not delete a working name. The
    # rows behind userprotein_7 carry three different labels on the real
    # account, and all three resolve.
    err = error_from_response(
        409, {"error": "ambiguous_target_name", "message": "…", "candidates": CANDIDATES},
    )
    assert isinstance(err, LigandAIAmbiguousTarget)
    assert "Brd4ET_closed_state_fix" in err.candidates[0].aliases
    assert err.candidates[2].aliases == []
