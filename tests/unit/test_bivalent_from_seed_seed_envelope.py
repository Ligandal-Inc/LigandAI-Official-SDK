# Copyright © 2025 Ligandal, Inc. All rights reserved.
"""`bivalent.from_seed` must send the `seed` object the route actually reads.

⛔ THE DEFECT THIS PINS: UNREACHABLE BY EVERY POSSIBLE ARGUMENT.
The method sent `binder_t1_sequence` as a top-level key. `server/bivalent-routes.ts:827`
reads `body.seed.sequence`, `body.seed.smiles` and `body.seed.pdb_content`, and nothing
server-side ever reads the flat key. So a caller supplying a perfectly valid target1,
target2, linker and sequence got, measured live on 2026-10-22:

    400  seed must include sequence, smiles, or pdb_content

for every input. There was no argument that could succeed — the same family as the
campaign `quoteToken` contradiction, where omitting the field was refused by Express and
including it was refused by FastAPI.

After the fix the same call returned a real session (`biv_mvjw8dhj7qqncr2t`, status
`run1_done`, both job ids null — a planner, not a fold dispatch).

The flat key is deliberately RETAINED alongside `seed`, so any other consumer of this
payload shape is unaffected; these tests assert both are present.
"""

from __future__ import annotations

import ligandai


class _Transport:
    def __init__(self):
        self.calls = []

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return {"session_id": "biv_test", "status": "run1_done"}


def _bivalent():
    client = ligandai.LigandAI(api_key="test-key")
    resource = client.bivalent
    transport = _Transport()
    resource._transport = transport
    resource._client = None          # skip the feature gate; this tests the body only
    return resource, transport


TARGET1 = {"gene": "KDM1A"}
TARGET2 = {"gene": "PDL1"}
LINKER = {"position": "C", "length_min": 6, "length_max": 12}
SEQUENCE = "AGFRFGDGGGEAVDDNCRKAIFGAGKVVKHSKDPGCQYHFG"


def test_body_carries_the_seed_object_the_route_reads():
    resource, transport = _bivalent()
    resource.from_seed(target1=TARGET1, target2=TARGET2, linker=LINKER,
                       binder_t1_sequence=SEQUENCE)
    _, path, kwargs = transport.calls[0]
    assert path == "/api/ligandforge/bivalent/from-seed"
    body = kwargs["json"]
    assert body["seed"] == {"sequence": SEQUENCE}


def test_the_flat_key_is_retained_for_other_consumers():
    resource, transport = _bivalent()
    resource.from_seed(target1=TARGET1, target2=TARGET2, linker=LINKER,
                       binder_t1_sequence=SEQUENCE)
    body = transport.calls[0][2]["json"]
    assert body["binder_t1_sequence"] == SEQUENCE


def test_targets_and_linker_still_round_trip():
    resource, transport = _bivalent()
    resource.from_seed(target1=TARGET1, target2=TARGET2, linker=LINKER,
                       binder_t1_sequence=SEQUENCE, num_designs=4)
    body = transport.calls[0][2]["json"]
    assert body["target1"]["gene"] == "KDM1A"
    assert body["target2"]["gene"] == "PDL1"
    assert body["linker"]["position"] == "C"
    assert body["num_designs"] == 4


def test_the_async_twin_sends_the_same_seed_object():
    """A fix applied to one twin and not the other is how this class of defect returns."""
    import inspect

    from ligandai.resources import bivalent as module

    source = inspect.getsource(module)
    # Both the sync and the async from_seed must build the seed envelope.
    assert source.count('"seed": {"sequence": binder_t1_sequence}') == 2
