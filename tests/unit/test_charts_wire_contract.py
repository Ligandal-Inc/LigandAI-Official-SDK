# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Wire-contract tests for ``client.charts`` — bd-dre-mc8jm.

Two independent defects made every ``client.charts.*`` call fail, and neither
was caught because nothing here asserted against the SHAPES the platform
actually sends and receives:

1. **Request**: the body was built with ``chartType`` / ``saveToProgram``.
   ``POST /api/charts/generate`` (server/chart-routes.ts) destructures
   ``chart_type`` / ``save_to_program`` and reads no camelCase alias, so the
   server answered HTTP 400 ``"chart_type is required"`` before the renderer
   was ever invoked.

2. **Response**: ``Chart`` required ``id`` / ``chartType`` / ``title``. No
   server surface emits any of those three — ``generate`` returns
   ``chart_id`` + ``url``, ``GET /api/charts/{id}`` returns ``chartId`` +
   ``url``, and a ``/api/charts/list`` row returns ``chart_id`` +
   ``chart_type``. Every response raised ``pydantic.ValidationError``.

The fixtures below are copied from the real emitters — ``server/chart_worker.py``
``_generate_chart`` / ``_register_chart`` and the ``/api/charts/:chartId`` alias
in ``server/sdk-alias-routes.ts`` — not from the SDK's own idea of them.
"""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from ligandai import LigandAI

BASE = "http://api.ligandai.test"
CHART_ID = "a1b2c3d4e5f6a7b8"

# server/chart_worker.py::_generate_chart, after chart-routes.ts rewrites the
# /chart/ prefix to /api/charts/.
GENERATE_RESPONSE = {
    "success": True,
    "chart_id": CHART_ID,
    "url": f"/api/charts/{CHART_ID}.png",
    "svg_url": f"/api/charts/{CHART_ID}.svg",
    "file_path": f"/data/charts/u/{CHART_ID}.png",
}

# server/sdk-alias-routes.ts::GET /api/charts/:chartId
GET_RESPONSE = {
    "chartId": CHART_ID,
    "url": f"/api/charts/{CHART_ID}.png",
    "format": "png",
}

# server/chart_worker.py::_register_chart, surfaced by GET /api/charts/list
LIST_RESPONSE = {
    "success": True,
    "count": 1,
    "charts": [
        {
            "chart_id": CHART_ID,
            "user_id": "u@example.com",
            "chart_type": "bar",
            "title": "Top Markers by SI",
            "formats": ["png", "svg"],
            "file_paths": {"png": f"/data/charts/u/{CHART_ID}.png"},
            "program_id": "142",
            "session_id": None,
            "created_at": "2026-09-12T04:05:06Z",
        }
    ],
}


@pytest.fixture
def client() -> LigandAI:
    return LigandAI(api_key="lgai_pro_test123", base_url=BASE, max_retries=1)


# --------------------------------------------------------------------------- #
# 1. Request body must be snake_case — the server reads nothing else.          #
# --------------------------------------------------------------------------- #


def test_generate_sends_snake_case_body(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/charts/generate", method="POST", json=GENERATE_RESPONSE
    )

    client.charts.generate(
        chart_type="line",
        title="CD8A Expression",
        data={"x": ["T1", "T2"], "y": [1.0, 2.0]},
        style={"theme": "dark", "dpi": 150},
        save_to_program=142,
        format="both",
    )

    body = httpx_mock.get_requests()[0].read().decode()
    import json as _json

    sent = _json.loads(body)

    # The keys the server destructures.
    assert sent["chart_type"] == "line"
    assert sent["save_to_program"] == 142
    assert sent["title"] == "CD8A Expression"
    assert sent["style"] == {"theme": "dark", "dpi": 150}
    assert sent["format"] == "both"

    # The keys that silently did nothing. Their presence is the bug.
    assert "chartType" not in sent
    assert "saveToProgram" not in sent


def test_generate_omits_unset_optionals(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/charts/generate", method="POST", json=GENERATE_RESPONSE
    )

    client.charts.generate(chart_type="bar", title="t", data={"x": [], "y": []})

    import json as _json

    sent = _json.loads(httpx_mock.get_requests()[0].read().decode())
    assert set(sent) == {"chart_type", "title", "data"}


# --------------------------------------------------------------------------- #
# 2. Every real response shape must validate.                                  #
# --------------------------------------------------------------------------- #


def test_generate_parses_the_real_response(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/charts/generate", method="POST", json=GENERATE_RESPONSE
    )

    chart = client.charts.generate(
        chart_type="line", title="CD8A Expression", data={"x": [], "y": []}
    )

    # chart_id -> id
    assert chart.id == CHART_ID
    # url -> image_url, and the SVG sibling is not dropped
    assert chart.image_url == f"/api/charts/{CHART_ID}.png"
    assert chart.svg_url == f"/api/charts/{CHART_ID}.svg"
    # the server echoes neither of these, so they come from the request
    assert chart.chart_type == "line"
    assert chart.title == "CD8A Expression"


def test_get_parses_the_alias_route_response(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/charts/{CHART_ID}", method="GET", json=GET_RESPONSE
    )

    chart = client.charts.get(CHART_ID)

    assert chart.id == CHART_ID  # from chartId
    assert chart.image_url == f"/api/charts/{CHART_ID}.png"
    # The alias route carries no type/title, and nothing may invent them.
    assert chart.chart_type is None
    assert chart.title is None


def test_list_parses_index_rows(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/charts/list?program_id=142", method="GET", json=LIST_RESPONSE
    )

    charts = client.charts.list(program_id=142)

    assert len(charts) == 1
    row = charts[0]
    assert row.id == CHART_ID
    assert row.chart_type == "bar"
    assert row.title == "Top Markers by SI"
    assert row.created_at is not None
    # extra="allow" keeps the fields the model does not name
    assert row.model_extra is not None
    assert row.model_extra.get("formats") == ["png", "svg"]


def test_list_without_program_sends_no_params(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/charts/list", method="GET", json={"success": True, "charts": []}
    )

    assert client.charts.list() == []
    assert str(httpx_mock.get_requests()[0].url.query, "utf-8") == ""
