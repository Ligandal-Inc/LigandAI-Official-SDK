# Copyright © 2025 Ligandal, Inc. All rights reserved.
"""`bivalent.fold` must send the SEQUENCE STRINGS its route reads.

⛔ THE DEFECT THIS PINS DID NOT FAIL — IT SUCCEEDED WITH GARBAGE, WHICH IS WORSE.
`server/bivalent-routes.ts:528` reads candidates as strings:

    body.candidates.map((s) => String(s || '').toUpperCase())
                   .filter((s) => s.length >= 5)

`_norm_candidate` turns EVERY candidate into a dict — a plain `str` becomes
`{"sequence": c}` — so each one arrived as the literal `"[object Object]"`.
That is 15 characters, so it **passes** the `>= 5` filter and is then treated as
a peptide sequence. The call returned 200 and the fold descriptors were built
from a sequence that does not exist.

Measured live on 2026-10-22 after the fix: descriptors carry the real
41-residue sequence with chain A peptide / chain B receptor entities.

⛔ THE FIX IS SCOPED TO `fold` AND MUST STAY THAT WAY. `analyze_folds` also sends
`normalized`, but its route is UNRESOLVED — no handler was found in the inspected
sources — so what shape it reads is unknown and changing its payload would be a
guess. These tests assert that `analyze_folds` still sends objects.
"""

from __future__ import annotations

import inspect

import ligandai
from ligandai.resources import bivalent as module


class _Transport:
    def __init__(self):
        self.calls = []

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return {"session_id": "biv_test", "status": "folding_t1", "fold_descriptors": []}


def _bivalent():
    client = ligandai.LigandAI(api_key="test-key")
    resource = client.bivalent
    resource._transport = _Transport()
    resource._client = None
    return resource, resource._transport


SEQ_A = "AGFRFGDGGGEAVDDNCRKAIFGAGKVVKHSKDPGCQYHFG"
SEQ_B = "MKTVRQERLKSIVRILERSKE"


def test_plain_strings_are_sent_as_strings_not_objects():
    resource, transport = _bivalent()
    resource.fold(session_id="s", fold_mode="target1", candidates=[SEQ_A, SEQ_B])
    body = transport.calls[0][2]["json"]
    assert body["candidates"] == [SEQ_A, SEQ_B]


def test_dict_candidates_are_reduced_to_their_sequence():
    resource, transport = _bivalent()
    resource.fold(session_id="s", fold_mode="target1",
                  candidates=[{"sequence": SEQ_A, "name": "a", "iptm": 0.7}])
    body = transport.calls[0][2]["json"]
    assert body["candidates"] == [SEQ_A]


def test_the_objects_are_still_carried_so_nothing_is_lost():
    resource, transport = _bivalent()
    resource.fold(session_id="s", fold_mode="target1",
                  candidates=[{"sequence": SEQ_A, "name": "a"}])
    body = transport.calls[0][2]["json"]
    assert body["candidate_objects"][0]["sequence"] == SEQ_A
    assert body["candidate_objects"][0]["name"] == "a"


def test_no_candidate_serialises_to_the_object_object_string():
    """The exact failure mode: 15 chars, which passed the route's >= 5 filter."""
    resource, transport = _bivalent()
    resource.fold(session_id="s", fold_mode="target1",
                  candidates=[SEQ_A, {"sequence": SEQ_B}])
    for value in transport.calls[0][2]["json"]["candidates"]:
        assert isinstance(value, str)
        assert "object" not in value.lower()


def test_a_candidate_without_a_sequence_is_dropped_not_stringified():
    resource, transport = _bivalent()
    resource.fold(session_id="s", fold_mode="target1",
                  candidates=[{"name": "no sequence here"}, SEQ_A])
    assert transport.calls[0][2]["json"]["candidates"] == [SEQ_A]


def test_analyze_folds_is_deliberately_left_sending_objects():
    """Its route is unresolved; changing its shape would be a guess."""
    source = inspect.getsource(module)
    assert source.count('"candidates": normalized,') == 2, (
        "analyze_folds (sync and async) must still send `normalized` until its route is identified")
    assert source.count('"candidates": sequences,') == 2, (
        "fold (sync and async) must both send sequence strings")


def test_both_fold_twins_build_the_sequence_list():
    source = inspect.getsource(module)
    assert source.count("sequences = [s for s in (_candidate_sequence(c) for c in normalized) if s]") == 2
