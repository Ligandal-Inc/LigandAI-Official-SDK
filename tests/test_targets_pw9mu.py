# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""[bd-LIGANDAI_ALPHA_V2-pw9mu] Named targets — a customer's own name for
their own uploaded structure.

The failure these encode: `generate(gene="BRD4ET")` returned
`503 upstream_unavailable: resolver_error:ValueError`, because "BRD4ET" is the
~68-residue ET sub-domain of BRD4 and has no gene symbol of its own. Nothing
in the resolution path had ever looked at the caller's own uploads.
"""
from __future__ import annotations

import pytest

from ligandai.errors import LigandAIValidationError
from ligandai.resources.targets import (
    NAME_MATCH_TIERS,
    NamedTarget,
    TargetIndex,
    TargetResolution,
    Targets,
)


class FakeTransport:
    """Records the calls made, replays a canned payload."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def request(self, method, path, **kw):
        self.calls.append((method, path, kw))
        return self.payload


def test_tiers_are_in_strength_order():
    assert NAME_MATCH_TIERS == ("gene_label", "gene", "filename_stem")


def test_list_hits_the_index_endpoint_and_parses_targets():
    payload = {
        "targets": [
            {
                "store": "user_proteins",
                "id": 26,
                "uploadId": "userprotein_26",
                "names": [
                    {"name": "Brd4ET_closed_COMP_sa", "tier": "gene_label"},
                    {"name": "BRD4ET", "tier": "gene"},
                ],
                "contentSha256": None,
            }
        ],
        "count": 1,
        "ambiguousNames": [],
    }
    t = FakeTransport(payload)
    idx = Targets(t).list()
    assert t.calls[0][0] == "GET"
    assert t.calls[0][1] == "/api/targets"
    assert len(idx) == 1
    assert idx.targets[0].upload_id == "userprotein_26"
    assert "BRD4ET" in idx.names()


def test_primary_name_takes_the_strongest_tier_not_list_order():
    # `gene` is listed FIRST here; `gene_label` is the stronger tier and must
    # still win. Reading position instead of tier would make renaming a file
    # change which structure a campaign targets.
    t = NamedTarget(
        {
            "uploadId": "userpdb_9",
            "names": [
                {"name": "inferred", "tier": "gene"},
                {"name": "TYPED", "tier": "gene_label"},
                {"name": "stem", "tier": "filename_stem"},
            ],
        }
    )
    assert t.primary_name == "TYPED"


def test_primary_name_falls_back_to_first_entry_for_an_unknown_tier():
    t = NamedTarget({"names": [{"name": "odd", "tier": "something_new"}]})
    assert t.primary_name == "odd"


def test_primary_name_is_none_with_no_names():
    assert NamedTarget({"names": []}).primary_name is None


def test_ambiguous_names_are_surfaced_from_the_index():
    idx = TargetIndex({"targets": [], "count": 0, "ambiguousNames": ["brd4"]})
    assert idx.ambiguous_names == ["brd4"]


def test_resolve_sends_the_name_as_a_query_param():
    t = FakeTransport({"name": "BRD4ET", "resolvesTo": "upload", "outcome": "resolved"})
    Targets(t).resolve("  BRD4ET  ")
    method, path, kw = t.calls[0]
    assert (method, path) == ("GET", "/api/targets")
    assert kw["params"] == {"name": "BRD4ET"}  # trimmed


@pytest.mark.parametrize("bad", ["", "   ", None, 7])
def test_resolve_refuses_an_empty_name_before_any_http_call(bad):
    t = FakeTransport({})
    with pytest.raises(LigandAIValidationError):
        Targets(t).resolve(bad)
    # An empty name is not a wildcard — nothing should have gone out.
    assert t.calls == []


def test_resolution_of_an_upload_is_usable_and_exposes_the_upload_id():
    r = TargetResolution(
        {
            "name": "BRD4ET",
            "resolvesTo": "upload",
            "gene": None,
            "outcome": "resolved",
            "upload": {
                "store": "user_proteins",
                "id": 26,
                "matchedOn": "gene",
                "uploadId": "userprotein_26",
            },
            "duplicatesFolded": [],
        }
    )
    assert r.usable is True
    assert r.upload_id == "userprotein_26"


def test_a_real_gene_still_wins_and_the_upload_collision_is_reported():
    # Naming an upload "BRD4" must not redefine BRD4 for the account.
    r = TargetResolution(
        {
            "name": "BRD4",
            "resolvesTo": "gene",
            "gene": "BRD4",
            "outcome": "ambiguous",
            "reason": '"BRD4" matches 2 of this account\'s uploaded structures',
            "candidates": [
                {"store": "user_proteins", "id": 17, "uploadId": "userprotein_17"},
                {"store": "user_proteins", "id": 588, "uploadId": "userprotein_588"},
            ],
        }
    )
    assert r.resolves_to == "gene"
    assert r.gene == "BRD4"
    assert len(r.candidates) == 2


def test_ambiguous_is_not_usable_even_though_something_matched():
    # The whole point of refusing: a wrong structure folds, scores, and reaches
    # a results table looking exactly like a real answer.
    r = TargetResolution(
        {
            "name": "DUP",
            "resolvesTo": "upload",
            "outcome": "ambiguous",
            "reason": "2 distinct by content",
            "candidates": [{"uploadId": "userpdb_1"}, {"uploadId": "userpdb_2"}],
        }
    )
    assert r.usable is False
    assert r.reason


def test_no_match_is_not_usable_and_carries_a_reason():
    r = TargetResolution(
        {"name": "NOPE", "resolvesTo": "nothing", "outcome": "no_match", "reason": "none named that"}
    )
    assert r.usable is False
    assert r.reason == "none named that"


def test_byte_identical_duplicates_are_folded_not_refused():
    r = TargetResolution(
        {
            "name": "BRD4ET",
            "resolvesTo": "upload",
            "outcome": "resolved",
            "upload": {"uploadId": "userpdb_60"},
            "duplicatesFolded": [61],
        }
    )
    assert r.usable is True
    assert r.duplicates_folded == [61]


def test_empty_payloads_parse_without_throwing():
    # A server that predates this route returns something unhelpful; the client
    # must degrade to "nothing named" rather than raise.
    assert len(TargetIndex({})) == 0
    assert TargetResolution({}).usable is False


def test_client_exposes_targets_on_both_sync_and_async():
    from ligandai import AsyncLigandAI, LigandAI

    c = LigandAI("lgai_test_key")
    assert hasattr(c.targets, "list") and hasattr(c.targets, "resolve")
    a = AsyncLigandAI("lgai_test_key")
    assert hasattr(a.targets, "list") and hasattr(a.targets, "resolve")
