# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""The H-SWAE projection contract, in both directions.

0.15.0 through 0.16.4 shipped the response models as ``extra="forbid"`` and so
raised ``ValidationError`` on every well-formed production payload that carried
a field the SDK did not declare. Measured against live production on
2026-10-15, the published 0.16.4 wheel raised on 5 of 6 operations — 452
errors, every one ``extra_forbidden``, zero type errors, zero missing fields
[bd-dre-pjhrp.18]. The response models are now ``extra="allow"``.

⛔ THAT FIX REMOVED THE ONLY DETECTOR OF THE OPPOSITE DEFECT, which is why this
file exists. ``extra="forbid"`` was, accidentally, the thing that noticed a
field the SDK had failed to declare: it raised. With the models open, such a
field lands silently in ``model_extra`` and never reaches the typed surface —
the receipt parses, any drift audit reads clean, and the caller still cannot
see the value. That is exactly the shape of bd-dre-pjhrp.19, where three
fields were *declared* by the capability boundary and never *projected*:
``getHswaeContractDrift()`` answers "what is the backend sending that I do not
declare", NOT "what do I declare and still drop", and the second class is
invisible to it.

So parsing is not enough and is not what this file tests. It pins:

1. every property the live signed ``outputSchema`` declares is declared by the
   SDK — the schema is the authority, not the payload, because a field absent
   from today's payload is still part of the contract;
2. ``model_dump(by_alias=True)`` reproduces every key and value the server
   sent, so nothing is parsed and then dropped on the way out;
3. the type discipline ``extra="allow"`` did NOT relax — a wrong type, a
   missing required field and a non-object where a model belongs still raise;
4. caller INPUT stays closed, so a typo still fails before the request is sent.

The fixtures in ``tests/fixtures/hswae_live_20261015/`` are real production
responses recorded on 2026-10-15, with only account identity (``owner.userId``,
``owner.orgId``, ``requestId``) replaced by synthetic placeholders.
"""

from __future__ import annotations

import json
import pathlib
import re
import typing
from typing import Any

import pydantic
import pytest

from ligandai.types import (
    HswaeAnnotateDatasetReceipt,
    HswaeCompareCellTypesReceipt,
    HswaeComplexSpecificityReceipt,
    HswaeIsoformExpressionReceipt,
    HswaeResolveCellTypeReceipt,
    HswaeSemanticSearchReceipt,
    HswaeSurfaceMarkersReceipt,
    validate_hswae_capability_input,
)

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "hswae_live_20261015"

RECEIPT_MODELS: dict[str, type[pydantic.BaseModel]] = {
    "hswae.v1.semantic_search": HswaeSemanticSearchReceipt,
    "hswae.v1.surface_markers": HswaeSurfaceMarkersReceipt,
    "hswae.v2.resolve_cell_type": HswaeResolveCellTypeReceipt,
    "hswae.v2.compare_cell_types": HswaeCompareCellTypesReceipt,
    "hswae.v2.isoform_expression": HswaeIsoformExpressionReceipt,
    "hswae.v2.annotate_dataset": HswaeAnnotateDatasetReceipt,
    "predictive_interactomics.complex_specificity": HswaeComplexSpecificityReceipt,
}

#: Recorded receipts, by the fixture's operation. ``hswae.v2.annotate_dataset``
#: has no recorded payload because it needs a caller-owned dataset; its schema
#: parity is still pinned below, which is the half that catches a dropped field.
RECORDED = [
    "hswae.v1.semantic_search",
    "hswae.v1.surface_markers",
    "hswae.v2.resolve_cell_type",
    "hswae.v2.compare_cell_types",
    "hswae.v2.isoform_expression",
    "predictive_interactomics.complex_specificity",
]

FIXTURE_FILES = {
    "hswae.v1.semantic_search": "receipt_semantic_search.json",
    "hswae.v1.surface_markers": "receipt_surface_markers.json",
    "hswae.v2.resolve_cell_type": "receipt_resolve_cell_type.json",
    "hswae.v2.compare_cell_types": "receipt_compare_cell_types.json",
    "hswae.v2.isoform_expression": "receipt_isoform_expression.json",
    "predictive_interactomics.complex_specificity": "receipt_complex_specificity.json",
}

#: Fields the SDK declares AHEAD of the backend deploying them, so they reach
#: callers the moment it does. Each is emitted by server commit 90aeca34ff
#: (bd-dre-pjhrp.19) and was verified absent from both the live signed schema
#: and live payloads on 2026-10-15.
#:
#: ⛔ This is an allowlist, not a licence. Anything here is invisible to check
#: (1) — it cannot be read off the schema — so a NEW entry must be justified
#: against the server source that emits it, and an entry should be DELETED once
#: the schema carries it, which turns it back into a pinned contract.
FORWARD_DECLARED: frozenset[str] = frozenset(
    {
        "result.expression_floor_basis",
        "result.candidates[].corroborated_qualifiers",
        "result.candidates[].match_detail",
        "result.candidates[].match_detail.matched_term",
        "result.candidates[].match_detail.jaccard",
        "result.candidates[].match_detail.containment_canonical",
        "result.candidates[].match_detail.containment_query",
        "result.candidates[].match_detail.ratio",
        "result.candidates[].match_detail.shared_tokens",
    }
)


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _receipt(operation: str) -> Any:
    return _load(FIXTURE_FILES[operation])


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _normalise(paths: typing.Iterable[str]) -> set[str]:
    """Compare on field identity, not on camelCase-vs-snake_case spelling.

    ⛔ KNOWN AND DELIBERATE BLIND SPOT, measured 2026-10-15. Because this folds
    ``floorNote`` onto ``floor_note``, the schema-parity checks below CANNOT
    see a field whose alias has the wrong casing — and that is the silent
    failure now that the models are open: the server's key goes unconsumed to
    ``model_extra``, the declared field reads ``None`` forever, and nothing
    raises. Verified by rebuilding a mis-aliased model against a recorded
    payload: parity passed, and the real ``floor_note`` string sat in
    ``model_extra``.

    That case is covered by
    :func:`test_nothing_the_backend_sends_is_stranded_in_model_extra`, which
    works on the unconsumed WIRE key and so does not normalise. The two checks
    are not redundant: parity sees fields absent from today's payload, the
    stranded check sees names that do not match. Removing either one opens a
    hole the other does not close.
    """
    return {".".join(_snake(part) for part in path.split(".")) for path in paths}


def _nested_models(annotation: Any) -> list[tuple[Any, str]]:
    out: list[tuple[Any, str]] = []
    if isinstance(annotation, type) and issubclass(annotation, pydantic.BaseModel):
        return [(annotation, "")]
    origin = typing.get_origin(annotation)
    for arg in typing.get_args(annotation):
        if arg is type(None):
            continue
        for nested, suffix in _nested_models(arg):
            prefix = "[]" if origin in (list, set, tuple, frozenset) else ""
            out.append((nested, prefix + suffix))
    return out


def _model_properties(model: Any, where: str = "", depth: int = 0) -> set[str]:
    """Every property path the SDK model tree declares, by wire name."""
    found: set[str] = set()
    if depth > 8 or not (isinstance(model, type) and issubclass(model, pydantic.BaseModel)):
        return found
    for name, field in model.model_fields.items():
        wire = field.alias or name
        path = f"{where}.{wire}" if where else wire
        found.add(path)
        for nested, suffix in _nested_models(field.annotation):
            found |= _model_properties(nested, f"{path}{suffix}", depth + 1)
    return found


def _schema_properties(node: Any, where: str = "") -> set[str]:
    """Every property path the signed JSON schema declares."""
    found: set[str] = set()
    if not isinstance(node, dict):
        return found
    properties = node.get("properties")
    if isinstance(properties, dict):
        for name, child in properties.items():
            path = f"{where}.{name}" if where else name
            found.add(path)
            found |= _schema_properties(child, path)
    items = node.get("items")
    if isinstance(items, dict):
        found |= _schema_properties(items, f"{where}[]")
    for keyword in ("anyOf", "oneOf", "allOf"):
        for child in node.get(keyword, []) or []:
            found |= _schema_properties(child, where)
    return found


def _undeclared(node: Any, where: str = "receipt") -> dict[str, list[str]]:
    """Keys the server sent that no model declares — they reached model_extra."""
    found: dict[str, list[str]] = {}
    if isinstance(node, pydantic.BaseModel):
        extra = node.model_extra or {}
        if extra:
            found[f"{where} [{type(node).__name__}]"] = sorted(extra)
        for name in type(node).model_fields:
            found.update(_undeclared(getattr(node, name, None), f"{where}.{name}"))
    elif isinstance(node, (list, tuple)):
        for index, item in enumerate(node):
            found.update(_undeclared(item, f"{where}[{index}]"))
    return found


def _losses(sent: Any, dumped: Any, where: str = "") -> list[str]:
    """Every key/value the server sent that model_dump fails to reproduce."""
    out: list[str] = []
    if isinstance(sent, dict):
        if not isinstance(dumped, dict):
            return [f"{where}: dumped as {type(dumped).__name__}, expected object"]
        for key, value in sent.items():
            if key not in dumped:
                out.append(f"{where}.{key}: DROPPED by model_dump")
                continue
            out.extend(_losses(value, dumped[key], f"{where}.{key}"))
    elif isinstance(sent, list):
        if not isinstance(dumped, list):
            return [f"{where}: dumped as {type(dumped).__name__}, expected array"]
        if len(sent) != len(dumped):
            out.append(f"{where}: length {len(sent)} became {len(dumped)}")
        for index, (value, other) in enumerate(zip(sent, dumped)):
            out.extend(_losses(value, other, f"{where}[{index}]"))
    elif sent != dumped:
        out.append(f"{where}: {sent!r} became {dumped!r}")
    return out


# --------------------------------------------------------------------------
# 1. the outage itself: a real payload must parse, and nothing may be stranded
# --------------------------------------------------------------------------


@pytest.mark.parametrize("operation", RECORDED)
def test_recorded_production_receipt_parses(operation: str) -> None:
    """This is the regression. Every one of these raised in 0.15.0-0.16.4."""
    receipt = RECEIPT_MODELS[operation].model_validate(_receipt(operation))
    assert receipt.operation == operation  # type: ignore[attr-defined]


@pytest.mark.parametrize("operation", RECORDED)
def test_nothing_the_backend_sends_is_stranded_in_model_extra(operation: str) -> None:
    """``extra="allow"`` keeps an undeclared field — it does not SURFACE it.

    A key in ``model_extra`` is readable but untyped, undocumented and absent
    from the IDE, so it is only a soft landing, never the destination. This is
    the check that fails when the backend ships a field we have not declared.
    """
    receipt = RECEIPT_MODELS[operation].model_validate(_receipt(operation))
    stranded = _undeclared(receipt)
    assert not stranded, (
        f"{operation}: the backend sent fields no model declares, so they are "
        f"reachable only through model_extra and never reach a caller's typed "
        f"surface:\n"
        + "\n".join(f"    {where}: {keys}" for where, keys in sorted(stranded.items()))
        + "\n\nDeclare them on the matching model."
    )


# --------------------------------------------------------------------------
# 2. the mirror image: declared inbound, dropped outbound
# --------------------------------------------------------------------------


@pytest.mark.parametrize("operation", RECORDED)
def test_projection_outward_is_lossless(operation: str) -> None:
    """Parsed is not the same as projected — bd-dre-pjhrp.19 was exactly this gap."""
    sent = _receipt(operation)
    receipt = RECEIPT_MODELS[operation].model_validate(sent)
    dumped = receipt.model_dump(by_alias=True, mode="json")
    losses = _losses(sent, dumped, operation)
    assert not losses, (
        f"{operation}: the SDK parsed these and then failed to project them "
        f"back out, so a caller re-serialising the receipt loses them:\n"
        + "\n".join(f"    {item}" for item in losses[:40])
    )


def test_signed_output_schema_is_fully_declared_by_the_sdk() -> None:
    """The SCHEMA is the authority, not the payload.

    A field absent from today's response is still part of the contract, and
    once the backend starts sending it an undeclared field goes silently to
    ``model_extra``. The schema diff is the only thing that sees that coming.
    """
    discovery = _load("capability_output_schemas.json")
    missing: dict[str, list[str]] = {}
    for capability in discovery["capabilities"]:
        operation = capability["operation"]
        model = RECEIPT_MODELS.get(operation)
        assert model is not None, f"no SDK receipt model for {operation}"
        gap = _normalise(_schema_properties(capability["outputSchema"])) - _normalise(
            _model_properties(model)
        )
        if gap:
            missing[operation] = sorted(gap)
    assert not missing, (
        "the signed capability output schema declares properties this SDK does "
        "not, so they will land in model_extra instead of reaching callers:\n"
        + "\n".join(
            f"    {operation}: {fields}" for operation, fields in sorted(missing.items())
        )
    )


def test_sdk_declares_nothing_beyond_the_schema_except_forward_declarations() -> None:
    """The other side of the same diff, so the allowlist cannot grow silently.

    An SDK-only field is either a forward declaration with a named server
    commit behind it, or a field whose wire name is WRONG — and a wrong wire
    name is invisible now that the models are open: it simply parses as
    ``None`` forever while the real value sits in ``model_extra``.
    """
    discovery = _load("capability_output_schemas.json")
    unexpected: dict[str, list[str]] = {}
    for capability in discovery["capabilities"]:
        operation = capability["operation"]
        model = RECEIPT_MODELS[operation]
        gap = (
            _normalise(_model_properties(model))
            - _normalise(_schema_properties(capability["outputSchema"]))
            - _normalise(FORWARD_DECLARED)
        )
        if gap:
            unexpected[operation] = sorted(gap)
    assert not unexpected, (
        "the SDK declares properties the signed schema does not, and they are "
        "not in FORWARD_DECLARED. Either the wire name is wrong (it will parse "
        "as None forever) or a forward declaration needs recording:\n"
        + "\n".join(
            f"    {operation}: {fields}" for operation, fields in sorted(unexpected.items())
        )
    )


def test_forward_declared_fields_are_still_absent_from_the_schema() -> None:
    """Delete an entry from FORWARD_DECLARED once the backend ships it.

    Left in place, the allowlist would permanently exempt a real field from the
    parity check above — the allowlist has to shrink to zero, or it becomes the
    blind spot it was added to cover.
    """
    discovery = _load("capability_output_schemas.json")
    declared_by_server: set[str] = set()
    for capability in discovery["capabilities"]:
        declared_by_server |= _normalise(_schema_properties(capability["outputSchema"]))
    landed = sorted(_normalise(FORWARD_DECLARED) & declared_by_server)
    assert not landed, (
        "these FORWARD_DECLARED fields are now IN the signed schema, so the "
        "forward declaration served its purpose and the exemption must go — "
        "remove them from FORWARD_DECLARED and re-record the fixtures so they "
        "are pinned by the parity check instead of exempted from it:\n"
        + "\n".join(f"    {field}" for field in landed)
    )


# --------------------------------------------------------------------------
# 3. mutation checks: prove extra="allow" did not relax the type discipline
# --------------------------------------------------------------------------


def test_an_added_backend_field_is_kept_rather_than_refused() -> None:
    """The actual 0.15.0-0.16.4 failure, reproduced forward."""
    payload = _receipt("hswae.v1.surface_markers")
    payload["result"]["a_field_the_platform_adds_next_quarter"] = {"nested": [1, 2, 3]}
    receipt = HswaeSurfaceMarkersReceipt.model_validate(payload)
    assert (receipt.result.model_extra or {})["a_field_the_platform_adds_next_quarter"] == {
        "nested": [1, 2, 3]
    }


@pytest.mark.parametrize(
    ("sent", "stage"),
    [
        ({}, "a stage that measures nothing"),
        ({"matched_term": "hepatocyte"}, "ontology_synonym"),
        (
            {
                "jaccard": 1.0,
                "containment_canonical": 1.0,
                "containment_query": 1.0,
                "shared_tokens": ["proximal", "tubule"],
            },
            "token_set",
        ),
        ({"ratio": 0.8571}, "fuzzy"),
        ({"jaccard": None}, "a MEASURED null, which must survive"),
        ({"ratio": 0.5, "a_fifth_stage_member": 7}, "an undeclared member"),
    ],
)
def test_match_detail_projects_back_exactly_the_members_it_received(
    sent: dict[str, Any], stage: str
) -> None:
    """``match_detail`` is the one model here that omits unsent members.

    The signed schema permits a real ``null`` for every member, so dumping an
    unmeasured member as ``None`` would claim a stage reported a number it
    never computed — and these are audit receipts. The backend omits for that
    reason and so does this. A member that was sent AS null is kept, because
    that one IS a measurement.
    """
    from ligandai.types import HswaeMatchDetail

    assert HswaeMatchDetail.model_validate(sent).model_dump() == sent, stage


def test_a_field_whose_alias_has_the_wrong_casing_is_caught() -> None:
    """Pins the division of labour documented on :func:`_normalise`.

    The parity checks normalise camelCase to snake_case and so cannot see this;
    the stranded-field check works on the unconsumed wire key and can. If a
    refactor ever makes BOTH normalise, this is the test that goes red.
    """
    from pydantic import Field as _Field

    from ligandai.types import _HswaeNullableNote, _HswaeOpenModel, _HswaePublicString

    class MisAliased(_HswaeOpenModel):
        primary_tissue: _HswaePublicString
        # the server sends `floor_note`; this declares `floorNote`
        floor_note: _HswaeNullableNote = _Field(default=None, alias="floorNote")

    result = _receipt("hswae.v1.surface_markers")["result"]
    parsed = MisAliased.model_validate(result)
    assert parsed.floor_note is None, "precondition: the mis-aliased field reads as absent"
    assert "floor_note" in (parsed.model_extra or {}), "precondition: value is stranded"
    stranded = _undeclared(parsed)
    assert any("floor_note" in keys for keys in stranded.values()), (
        "the stranded-field detector no longer catches a mis-aliased field, so "
        "nothing in this suite does — see the note on _normalise()"
    )


def test_a_wrong_type_on_a_declared_field_still_raises() -> None:
    payload = _receipt("hswae.v1.surface_markers")
    payload["result"]["timing_ms"] = "fast"
    with pytest.raises(pydantic.ValidationError) as caught:
        HswaeSurfaceMarkersReceipt.model_validate(payload)
    assert not any(e["type"] == "extra_forbidden" for e in caught.value.errors())


def test_a_missing_required_field_still_raises() -> None:
    payload = _receipt("hswae.v1.surface_markers")
    del payload["result"]["primary_tissue"]
    with pytest.raises(pydantic.ValidationError):
        HswaeSurfaceMarkersReceipt.model_validate(payload)


def test_a_non_object_where_a_model_belongs_still_raises() -> None:
    payload = _receipt("hswae.v1.surface_markers")
    payload["result"] = "surface markers, trust me"
    with pytest.raises(pydantic.ValidationError):
        HswaeSurfaceMarkersReceipt.model_validate(payload)


def test_the_operation_discriminator_is_not_interchangeable() -> None:
    payload = _receipt("hswae.v1.surface_markers")
    payload["operation"] = "hswae.v1.semantic_search"
    with pytest.raises(pydantic.ValidationError):
        HswaeSurfaceMarkersReceipt.model_validate(payload)


# --------------------------------------------------------------------------
# 4. caller input must stay CLOSED — the asymmetry is the whole design
# --------------------------------------------------------------------------


def test_a_typo_in_caller_input_still_fails_before_http() -> None:
    """Open responses, closed requests. A dropped input is silent server-side."""
    with pytest.raises(pydantic.ValidationError) as caught:
        validate_hswae_capability_input("hswae.v1.surface_markers", {"context": "colon", "top_nn": 5})
    assert any(e["type"] == "extra_forbidden" for e in caught.value.errors())


def test_valid_caller_input_projects_every_field_it_declared() -> None:
    """The input side of the same projection bug: declared, then not sent."""
    sent = validate_hswae_capability_input(
        "hswae.v1.surface_markers",
        {"context": "colon", "top_n": 5, "exclude_contexts": ["brain"]},
    )
    assert sent == {"context": "colon", "top_n": 5, "exclude_contexts": ["brain"]}
