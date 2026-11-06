"""Regression: compare-groups group-shape gate (Tom discovery ask D, item 2).

The live server rejects a GTEx TARGET (and a non-custom target in focus
mode) with a bare 400; the SDK docstrings previously advertised GTEx targets.
The SDK now matches live truth: it refuses locally with guidance BEFORE the
request, and the docstrings say what the server actually accepts.
[bd-LIGANDAI_ALPHA_V2-tom-D2]
"""

from __future__ import annotations

import pytest

from ligandai.errors import LigandAIError  # noqa: F401  (transport errors stay loud)
from ligandai.resources.discovery import _check_compare_group_shapes
from ligandai.types import ReferenceGroup, TargetGroup


def _t(**kw):
    return TargetGroup.model_validate({"name": "t", **kw})


def test_gtex_target_refused_before_request():
    with pytest.raises(ValueError, match="custom or geneset targetGroup"):
        _check_compare_group_shapes(_t(type="gtex", tissue="liver"), [], "compare")


def test_gtex_target_refused_for_global():
    with pytest.raises(ValueError, match="global"):
        _check_compare_group_shapes(_t(type="gtex", tissue="liver"), None, "global")


def test_focus_requires_custom_target():
    with pytest.raises(ValueError, match="focus.*requires a custom"):
        _check_compare_group_shapes(_t(type="geneset", genes=["TFRC"]), None, "focus")


def test_compare_requires_at_least_one_reference():
    tgt = _t(type="geneset", genes=["TFRC"])
    with pytest.raises(ValueError, match="at least one referenceGroup"):
        _check_compare_group_shapes(tgt, [], "compare")
    with pytest.raises(ValueError, match="at least one referenceGroup"):
        _check_compare_group_shapes(tgt, None, "compare")


def test_accepted_shapes_pass_the_gate():
    geneset = _t(type="geneset", genes=["CD177", "FCGRT", "TFRC"])
    custom = _t(type="custom", datasetId=7, cellTypes=["astrocyte"])
    gtex_ref = ReferenceGroup(name="liver", type="gtex", tissue="liver")
    # Tom's known-working reproduction: geneset target vs gtex references.
    _check_compare_group_shapes(geneset, [gtex_ref], "compare")
    _check_compare_group_shapes(custom, None, "global")
    _check_compare_group_shapes(custom, [gtex_ref], "focus")


def test_gate_raises_before_transport(httpx_mock) -> None:
    """A refused call must not hit the network."""
    from ligandai import LigandAI

    client = LigandAI(api_key="lgai_pro_test", base_url="http://testserver", max_retries=1)
    with pytest.raises(ValueError):
        client.discovery.compare_groups(
            target_group=TargetGroup(name="t", type="gtex", tissue="liver"),
            reference_groups=[ReferenceGroup(name="r", type="gtex", tissue="spleen")],
        )
    assert httpx_mock.get_requests() == []  # zero requests — refused locally
