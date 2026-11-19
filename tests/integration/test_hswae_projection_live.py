"""Opt-in mutation acceptance. Run against an authorized server with a curator credential.

Set HSWAE_RUN_PROJECTION_ACCEPTANCE=1 and LIGANDAI_API_KEY through the secret provider.
The two names below occur verbatim in the public HPA v25 atlas. The test refuses to
remove a pre-existing entry. Use an exclusive curator window for the named items.
"""

import os

import pytest

from ligandai import LigandAI
from ligandai.errors import LigandAIError

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("HSWAE_RUN_PROJECTION_ACCEPTANCE") != "1",
        reason="requires live curator approval",
    ),
]


@pytest.mark.parametrize(
    ("kind", "name"),
    [
        ("tissue", "retina"),
        ("cell_type", "retinal bipolar cells"),
    ],
)
def test_live_projection_round_trip(kind, name):
    with LigandAI(api_key=os.environ["LIGANDAI_API_KEY"]) as client:
        discovery = client.discovery
        ops = {entry.operation: entry for entry in discovery.capabilities().capabilities}
        for op in ("hswae.v2.add_projection_item", "hswae.v2.remove_projection_item"):
            assert ops[op].definition_digest
        listing = discovery.tissues if kind == "tissue" else discovery.projection_cell_types

        def exact():
            response = discovery.invoke_capability("hswae.v1.semantic_search", {"query": name})
            return any(
                m.match_type == "exact"
                and m.source_entity == name
                and m.score == 1.0
                and (m.cell_type == name if kind == "cell_type" else m.cell_type is None)
                for m in response.result.matches
            )

        assert name not in listing() and not exact(), "acceptance item is already present"
        added = discovery.add_projection_item(kind, name)
        assert added.result.changed and added.result.present
        try:
            assert exact()
            assert name in listing()
        finally:
            removed = discovery.remove_projection_item(kind, name)
            assert removed.result.changed and not removed.result.present
        assert not exact()
        assert name not in listing()


def test_empty_reference_documentation_matches_live_comparison():
    with LigandAI(api_key=os.environ["LIGANDAI_API_KEY"]) as client:
        discovery = client.discovery
        assert "no_references_resolved" in discovery.get_hswae2_cell_type_markers.__doc__
        empty = discovery.get_hswae2_cell_type_markers("cardiomyocytes", reference_cell_types=[])
        assert (
            empty["differential"] == [] and empty["differential_status"] == "no_references_resolved"
        )
        compared = discovery.invoke_capability(
            "hswae.v2.compare_cell_types",
            {
                "target_cell_type": "cardiomyocytes",
                "reference_cell_types": ["astrocytes"],
                "top_n": 1,
            },
        )
        assert compared.result.differential_status == "computed" and compared.result.differential


@pytest.mark.skipif(
    not os.getenv("HSWAE_READ_ONLY_API_KEY"), reason="requires an authorized read-only credential"
)
def test_live_projection_curator_entitlement():
    with LigandAI(api_key=os.environ["HSWAE_READ_ONLY_API_KEY"]) as client:
        discovery = client.discovery
        assert "hswae.v2.remove_projection_item" not in {
            d.operation for d in discovery.capabilities().capabilities
        }
        # A real atlas item already present. Even an unexpected grant cannot remove a public entry.
        with pytest.raises(LigandAIError) as denied:
            discovery.add_projection_item("tissue", "eye")
        assert denied.value.status_code == 403
        assert denied.value.response.get("code") == "entitlement_required"
