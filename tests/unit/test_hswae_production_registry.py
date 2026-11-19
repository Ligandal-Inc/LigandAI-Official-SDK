"""Read actual production definitions and a recorded signed-boundary receipt."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ligandai.types import (
    HSWAE_CAPABILITY_DEFINITION_HASHES,
    HswaeCapabilityDiscovery,
    HswaeResolveCellTypeReceipt,
    HswaeResolveCellTypeResult,
)

FIXTURES = Path(__file__).parents[1] / "fixtures"
PREVIEW = FIXTURES / "hswae_preview_resolver_definitions_20261119.json"


def test_blue_registry_and_digest_integrity():
    record = json.loads((FIXTURES / "hswae_blue_registry_20261118.json").read_text())["registry"]
    parsed = HswaeCapabilityDiscovery.model_validate(record)
    assert len(parsed.capabilities) == 7
    assert all(x.definition_digest is None for x in parsed.capabilities)
    # Simulate corruption of a real definition, without changing the pinned hashes.
    record["capabilities"][0]["description"] += " "
    with pytest.raises(ValidationError, match="unreviewed definition"):
        HswaeCapabilityDiscovery.model_validate(record)


def test_resolver_aliases_read_the_selected_candidate():
    raw = json.loads((FIXTURES / "hswae_live_20261015/receipt_resolve_cell_type.json").read_text())
    result = HswaeResolveCellTypeReceipt.model_validate(raw).result
    assert result.canonical_cell_type == raw["result"]["canonical"]
    selected = next(
        row for row in raw["result"]["candidates"] if row["cell_type"] == result.canonical
    )
    assert result.confidence == selected["confidence"]
    assert result.source == selected["source"]
    assert result.model_dump()["canonical_cell_type"] == raw["result"]["canonical"]


def test_compiled_preview_resolver_definitions_match_reviewed_pins():
    definitions = json.loads(PREVIEW.read_text())["definitions"]
    parsed = HswaeCapabilityDiscovery.model_validate(
        {"contractVersion": "1.0.0", "capabilities": definitions}
    )
    assert {entry.operation for entry in parsed.capabilities} == {
        "hswae.v2.resolve_cell_type",
        "hswae.v2.compare_cell_types",
    }
    for entry in parsed.capabilities:
        assert entry.definition_digest == HSWAE_CAPABILITY_DEFINITION_HASHES[entry.operation]


def test_server_selection_survives_parsing_and_serialization():
    results = json.loads(PREVIEW.read_text())["resolver_results"]
    assert {r["resolution_status"] for r in results} == {"resolved", "unresolved"}
    for raw in results:
        result = HswaeResolveCellTypeResult.model_validate(raw)
        for field in ("canonical_cell_type", "confidence", "source"):
            assert field in result.model_fields_set
            assert getattr(result, field) == result.model_dump()[field] == raw[field]


def test_stated_digest_must_match_the_body_and_reviewed_pin():
    raw = json.loads((FIXTURES / "hswae_blue_registry_20261118.json").read_text())["registry"]
    definition = next(
        d for d in raw["capabilities"] if d["operation"] == "hswae.v2.annotate_dataset"
    )
    definition["definitionDigest"] = HSWAE_CAPABILITY_DEFINITION_HASHES[definition["operation"]]
    assert HswaeCapabilityDiscovery.model_validate(raw).capabilities
    definition["definitionDigest"] = HSWAE_CAPABILITY_DEFINITION_HASHES[
        "hswae.v2.resolve_cell_type"
    ]
    with pytest.raises(ValidationError, match="digest mismatch"):
        HswaeCapabilityDiscovery.model_validate(raw)
