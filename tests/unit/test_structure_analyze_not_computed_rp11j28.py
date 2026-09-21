# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""`POST /api/structure/analyze` must not be able to answer with nothing.
[bd-dre-rp11j.28]

THE DEFECT, measured 2026-09-21 against the tree at d38e71a:

    >>> StructureAnalysis.model_validate({"gene": "EGFR"})
    StructureAnalysis(gene='EGFR', pocket_count=None, recommended_pocket=None,
                      pockets=None, vacancy_score=None, surface_features=None)

That object validated cleanly, and `Structures.analyze()` MANUFACTURED it:

    StructureAnalysis.model_validate(
        self._transport.request("POST", "/api/structure/analyze", json=body)
        or {"gene": gene}
    )

`gene` is the only required field on the model, so `or {"gene": gene}` supplied
exactly the field whose absence would have failed validation — and supplied it
from the REQUEST, not the response. Four layers then agreed on an answer that
was never computed: the status was 200, the transport read the body fine, the
schema validated, and the call site filled the gap. The endpoint was a total
no-op for its entire life and no layer was in a position to notice.

⛔ THE CORRECTION THIS FILE ENCODES is narrower than "reject null fields". The
route genuinely returns `pockets: null` on every correct response — it performs
target-SUITABILITY analysis, not pocket detection, and says so in its own
`pocket_analysis` block (server/sdk-alias-routes.ts, bd-dre-rp11j.31). So a
client that required a pocket field would reject every valid answer. What must
be rejected is a body carrying NO EVIDENCE THAT ANYTHING WAS COMPUTED, while an
honest empty result (`pocket_count=0`, `pockets=[]`, a suitability verdict)
keeps validating.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from pytest_httpx import HTTPXMock

from ligandai import AsyncLigandAI, LigandAI
from ligandai.errors import LigandAIAnalysisNotComputed, LigandAIUnreadableResponse
from ligandai.types import StructureAnalysis

BASE = "http://api.ligandai.test"
ANALYZE_URL = f"{BASE}/api/structure/analyze"
JSON_HEADERS = {"content-type": "application/json"}


@pytest.fixture
def pro_client() -> LigandAI:
    return LigandAI(api_key="lgai_pro_test", base_url=BASE, max_retries=1)


#: A verbatim copy of the shape the route returns on a genuine 200, from
#: server/sdk-alias-routes.ts `app.post('/api/structure/analyze', ...)`. Copied
#: rather than invented so the client contract is checked against the server's
#: real output — including the legacy pocket keys being null.
SERVER_200_BODY = {
    "gene": "BRD4",
    "canonicalGene": "BRD4",
    "uniprotId": "O60885",
    "analysis_depth": "full",
    "computed": True,
    "computed_fields": ["isGoodTarget", "suitabilityScore", "targetType"],
    "structure_used": True,
    "structure_source": "pdb",
    "suitability": {
        "isGoodTarget": True,
        "suitabilityScore": 82,
        "targetType": "monomer",
        "suggestedConfiguration": "BRD4",
        "bindingStudyCompatibility": {"recommendation": "BD1 bromodomain is tractable"},
        "warnings": [],
        "recommendations": ["Target the BD1 acetyl-lysine pocket"],
        "suggestedActions": [],
    },
    "pocketCount": None,
    "recommendedPocket": None,
    "pockets": None,
    "vacancyScore": None,
    "surfaceFeatures": None,
    "pocket_analysis": {
        "available": False,
        "reason": "this endpoint performs target-suitability analysis, not pocket detection",
        "pocket_endpoints": ["/api/receptor-intelligence/profile/{gene}"],
    },
}


# ---------------------------------------------------------------------------
# 1. The model itself — the guard cannot be bypassed by calling model_validate
# ---------------------------------------------------------------------------


def test_gene_only_body_is_refused_not_returned() -> None:
    """RED before this bead: this returned a fully-valid StructureAnalysis.

    This exact payload is what the old `or {"gene": gene}` fallback built.
    """
    with pytest.raises(LigandAIAnalysisNotComputed) as exc:
        StructureAnalysis.model_validate({"gene": "EGFR"})

    err = exc.value
    assert err.populated_fields == []
    assert err.subject == "EGFR"
    assert err.endpoint == "POST /api/structure/analyze"
    assert "computed" in err.checked_fields
    assert "suitability" in err.checked_fields
    # `gene` is echoed from the request, so it is NOT evidence and must not be
    # in the set the guard consults.
    assert "gene" not in err.checked_fields


def test_every_computed_field_null_is_refused() -> None:
    with pytest.raises(LigandAIAnalysisNotComputed):
        StructureAnalysis.model_validate(
            {
                "gene": "EGFR",
                "pocketCount": None,
                "recommendedPocket": None,
                "pockets": None,
                "vacancyScore": None,
                "surfaceFeatures": None,
            }
        )


def test_computed_false_with_empty_suitability_is_refused() -> None:
    """`computed=False` is the server saying it did not, and empty arrays are
    not evidence of an analysis."""
    with pytest.raises(LigandAIAnalysisNotComputed):
        StructureAnalysis.model_validate(
            {
                "gene": "EGFR",
                "computed": False,
                "computed_fields": [],
                "suitability": {
                    "isGoodTarget": None,
                    "suitabilityScore": None,
                    "warnings": [],
                    "recommendations": [],
                    "suggestedActions": [],
                },
            }
        )


def test_missing_gene_is_still_a_validation_error_not_the_named_error() -> None:
    """Two different faults must stay two different exceptions: a body with no
    identity at all is a schema fault, not a not-computed fault."""
    with pytest.raises(ValidationError):
        StructureAnalysis.model_validate({})
    with pytest.raises(ValidationError):
        StructureAnalysis.model_validate({"gene": None, "computed": True})


def test_the_error_message_says_what_was_attempted_and_what_to_do() -> None:
    """A named error whose text does not tell the caller what happened is just a
    differently-spelled silence."""
    with pytest.raises(LigandAIAnalysisNotComputed) as exc:
        StructureAnalysis.model_validate({"gene": "CD8A"})
    text = str(exc.value)
    assert "CD8A" in text
    assert "/api/structure/analyze" in text
    # names that this is NOT an honest negative result
    assert "NOT a finding" in text
    # names a remedy
    assert "retry" in text.lower()
    # points at the endpoint that DOES do pockets
    assert "receptor-intelligence" in text


# ---------------------------------------------------------------------------
# 2. An honest empty result, and a real answer, still work
# ---------------------------------------------------------------------------


def test_zero_pockets_is_a_measurement_and_validates() -> None:
    """`pocket_count=0` is a result, not an absence. A guard that rejected this
    would have replaced a silent no-op with a loud false alarm."""
    got = StructureAnalysis.model_validate({"gene": "EGFR", "pocketCount": 0, "pockets": []})
    assert got.pocket_count == 0
    assert got.pockets == []
    assert got.evidence_of_computation() == ["pocket_count"]


def test_the_real_server_200_body_validates_and_parses() -> None:
    got = StructureAnalysis.model_validate(SERVER_200_BODY)
    assert got.gene == "BRD4"
    assert got.uniprot_id == "O60885"
    assert got.computed is True
    assert got.structure_used is True
    assert got.suitability is not None
    assert got.suitability.suitability_score == 82
    assert got.suitability.target_type == "monomer"
    # the legacy pocket keys are null on a CORRECT response — that must not raise
    assert got.pockets is None
    assert got.pocket_count is None
    assert got.pocket_analysis is not None
    assert got.pocket_analysis["available"] is False
    assert set(got.evidence_of_computation()) == {"computed", "computed_fields", "suitability"}


def test_a_suitability_verdict_alone_is_enough_evidence() -> None:
    """An older server that answers with a verdict but no `computed` flag is
    still answering. The guard must not require the flag."""
    got = StructureAnalysis.model_validate(
        {"gene": "EGFR", "suitability": {"isGoodTarget": False, "suitabilityScore": 12}}
    )
    assert got.suitability is not None
    assert got.suitability.is_good_target is False
    assert got.evidence_of_computation() == ["suitability"]


# ---------------------------------------------------------------------------
# 3. The two call sites — sync and async must behave identically
# ---------------------------------------------------------------------------


def test_sync_analyze_raises_on_an_all_null_body(
    httpx_mock: HTTPXMock, pro_client: LigandAI
) -> None:
    httpx_mock.add_response(
        url=ANALYZE_URL,
        json={"gene": "EGFR", "pocketCount": None, "pockets": None, "vacancyScore": None},
    )
    with pytest.raises(LigandAIAnalysisNotComputed):
        pro_client.structures.analyze("EGFR")


def test_sync_analyze_raises_on_an_empty_body_with_a_DIFFERENT_error(
    httpx_mock: HTTPXMock, pro_client: LigandAI
) -> None:
    """"the platform returned nothing" and "the platform returned a body with no
    result" are different faults and must not collapse into one error.

    This is the case the transport's own rule lets through: a POST's empty 200
    is a legitimate command ack (bd-dre-rp11j.3), so the transport returns
    None. `analyze` is a query wearing a POST, so the call site has to refuse.
    """
    httpx_mock.add_response(url=ANALYZE_URL, status_code=200, content=b"", headers=JSON_HEADERS)
    with pytest.raises(LigandAIUnreadableResponse) as exc:
        pro_client.structures.analyze("EGFR")
    assert exc.value.cause == "empty_body"
    assert "EGFR" in str(exc.value)
    assert "NOTHING was measured" in str(exc.value)


def test_sync_analyze_never_reports_the_gene_it_was_asked_about_as_a_result(
    httpx_mock: HTTPXMock, pro_client: LigandAI
) -> None:
    """The specific regression: an unusable response must not come back as a
    StructureAnalysis whose `gene` was copied out of the request."""
    httpx_mock.add_response(url=ANALYZE_URL, json={})
    with pytest.raises((LigandAIAnalysisNotComputed, LigandAIUnreadableResponse, ValidationError)):
        result = pro_client.structures.analyze("KRAS")
        pytest.fail(f"analyze() manufactured a result from its own argument: {result!r}")


def test_sync_analyze_returns_a_real_answer(
    httpx_mock: HTTPXMock, pro_client: LigandAI
) -> None:
    httpx_mock.add_response(url=ANALYZE_URL, json=SERVER_200_BODY)
    got = pro_client.structures.analyze("BRD4")
    assert got.suitability is not None
    assert got.suitability.suitability_score == 82


def test_sync_analyze_returns_an_honest_empty_result(
    httpx_mock: HTTPXMock, pro_client: LigandAI
) -> None:
    httpx_mock.add_response(url=ANALYZE_URL, json={"gene": "EGFR", "pocketCount": 0, "pockets": []})
    got = pro_client.structures.analyze("EGFR")
    assert got.pocket_count == 0


async def test_async_analyze_raises_on_an_all_null_body(httpx_mock: HTTPXMock) -> None:
    """The async twin must not diverge — sync/async drift is a defect this epic
    already found twice (AsyncEnsemble.run(timeout), contrastive_references)."""
    httpx_mock.add_response(url=ANALYZE_URL, json={"gene": "EGFR", "pockets": None})
    client = AsyncLigandAI(api_key="lgai_pro_test", base_url=BASE, max_retries=1)
    try:
        with pytest.raises(LigandAIAnalysisNotComputed):
            await client.structures.analyze("EGFR")
    finally:
        await client.close()


async def test_async_analyze_raises_on_an_empty_body(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=ANALYZE_URL, status_code=200, content=b"", headers=JSON_HEADERS)
    client = AsyncLigandAI(api_key="lgai_pro_test", base_url=BASE, max_retries=1)
    try:
        with pytest.raises(LigandAIUnreadableResponse) as exc:
            await client.structures.analyze("EGFR")
        assert exc.value.cause == "empty_body"
    finally:
        await client.close()


async def test_async_analyze_returns_a_real_answer(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=ANALYZE_URL, json=SERVER_200_BODY)
    client = AsyncLigandAI(api_key="lgai_pro_test", base_url=BASE, max_retries=1)
    try:
        got = await client.structures.analyze("BRD4")
        assert got.computed is True
        assert got.suitability is not None
        assert got.suitability.suitability_score == 82
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# 4. Structural guard: the fallback must not come back
# ---------------------------------------------------------------------------


def test_no_analyze_call_site_substitutes_a_body_for_the_response() -> None:
    """Source-level guard, because this is a one-line edit away from returning.

    Reads the AST rather than grepping so a reformat cannot hide it: neither
    `analyze` body may contain a `BoolOp(or)` whose right operand is a dict
    literal. That expression IS the defect, in both twins.
    """
    import ast
    import pathlib

    src = pathlib.Path(
        pathlib.Path(__file__).resolve().parents[2] / "ligandai" / "resources" / "structures.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(src)

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != "analyze":
            continue
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.BoolOp)
                and isinstance(sub.op, ast.Or)
                and any(isinstance(v, (ast.Dict, ast.List)) for v in sub.values)
            ):
                offenders.append(f"{node.name} (line {sub.lineno})")
    assert not offenders, (
        f"analyze() substitutes a literal for the response again at {offenders}. "
        f"`transport.request(...) or {{'gene': gene}}` manufactures the ONLY required "
        f"field of StructureAnalysis out of the caller's own argument, which turns "
        f"'the platform did not answer' into a valid-looking analysis."
    )
