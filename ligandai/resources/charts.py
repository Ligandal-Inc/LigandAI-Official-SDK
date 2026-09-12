# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Chart generation endpoints (matplotlib-rendered server-side).

⛔ The request body here is **snake_case on purpose**. ``POST /api/charts/generate``
destructures ``chart_type`` / ``save_to_program`` (server/chart-routes.ts) and reads
no camelCase alias, so the previous ``chartType`` / ``saveToProgram`` body was
rejected with HTTP 400 ``"chart_type is required"`` on *every* call — the request
never reached the renderer. [bd-dre-mc8jm]

The response is equally unforgiving: ``generate`` returns
``{success, chart_id, url, svg_url, file_path}``, ``GET /api/charts/{id}`` returns
``{chartId, url, format}``, and a ``/api/charts/list`` row carries
``{chart_id, chart_type, title, formats, created_at, ...}``. None of the three
carry ``id``, so :class:`~ligandai.types.Chart` accepts ``chart_id`` / ``chartId``
as aliases for it. ``chart_type`` and ``title`` are absent from the generate
response, so they are back-filled here from the request the caller just made
rather than left ``None``.
"""

from __future__ import annotations

from typing import Any, Literal

from ligandai.resources._base import AsyncResource, Resource
from ligandai.types import Chart

ChartFormat = Literal["png", "svg", "both"]


def _generate_body(
    chart_type: str,
    title: str,
    data: dict[str, Any],
    style: dict[str, Any] | None,
    save_to_program: int | None,
    fmt: str | None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "chart_type": chart_type,
        "title": title,
        "data": data,
    }
    if style is not None:
        body["style"] = style
    if save_to_program is not None:
        body["save_to_program"] = save_to_program
    if fmt is not None:
        body["format"] = fmt
    return body


def _echo_request_fields(
    payload: Any,
    chart_type: str,
    title: str,
) -> dict[str, Any]:
    """Merge the caller's own chart_type/title into a generate response.

    The server does not echo either one, and they are the two things a caller
    most obviously expects to read back off the returned object.
    """
    out: dict[str, Any] = dict(payload) if isinstance(payload, dict) else {}
    out.setdefault("chart_type", chart_type)
    out.setdefault("title", title)
    return out


class Charts(Resource):
    """``/api/charts/*``."""

    def generate(
        self,
        chart_type: str,
        title: str,
        data: dict[str, Any],
        style: dict[str, Any] | None = None,
        save_to_program: int | None = None,
        format: ChartFormat | None = None,
    ) -> Chart:
        """``POST /api/charts/generate``.

        Args:
            chart_type: One of the renderer's supported types (``line``, ``bar``,
                ``scatter``, ``heatmap``, ``stacked_bar``, ``volcano``, ``umap``).
                An unknown type is rejected server-side with the supported list.
            title: Chart title, rendered into the figure.
            data: Chart-type-specific series payload.
            style: Renderer style overrides (e.g. ``{"theme": "dark", "dpi": 150}``).
                DPI is floored at 150.
            save_to_program: Program id to file the chart under. Omitting it
                leaves the chart untagged and it will not appear in a
                program-filtered listing.
            format: ``"png"`` (server default), ``"svg"``, or ``"both"``.
                :attr:`Chart.svg_url` is only populated when SVG was rendered.
        """
        body = _generate_body(chart_type, title, data, style, save_to_program, format)
        payload = self._transport.request("POST", "/api/charts/generate", json=body) or {}
        return Chart.model_validate(_echo_request_fields(payload, chart_type, title))

    def get(self, chart_id: str) -> Chart:
        """``GET /api/charts/{chart_id}`` — metadata for one previously generated chart."""
        return Chart.model_validate(
            self._transport.request("GET", f"/api/charts/{chart_id}") or {}
        )

    def list(self, program_id: str | int | None = None) -> list[Chart]:
        """``GET /api/charts/list`` — charts belonging to the authenticated caller.

        Args:
            program_id: Optional program scope. A caller-supplied ``user_id`` is
                ignored by the server unless the caller is a superadmin, so this
                listing is always self-scoped.
        """
        params: dict[str, Any] = {}
        if program_id is not None:
            params["program_id"] = str(program_id)
        payload = self._transport.request(
            "GET", "/api/charts/list", params=params or None
        ) or {}
        rows = payload.get("charts", []) if isinstance(payload, dict) else (payload or [])
        return [Chart.model_validate(r) for r in rows]


class AsyncCharts(AsyncResource):
    async def generate(
        self,
        chart_type: str,
        title: str,
        data: dict[str, Any],
        style: dict[str, Any] | None = None,
        save_to_program: int | None = None,
        format: ChartFormat | None = None,
    ) -> Chart:
        body = _generate_body(chart_type, title, data, style, save_to_program, format)
        payload = (
            await self._transport.request("POST", "/api/charts/generate", json=body)
        ) or {}
        return Chart.model_validate(_echo_request_fields(payload, chart_type, title))

    async def get(self, chart_id: str) -> Chart:
        return Chart.model_validate(
            await self._transport.request("GET", f"/api/charts/{chart_id}") or {}
        )

    async def list(self, program_id: str | int | None = None) -> list[Chart]:
        params: dict[str, Any] = {}
        if program_id is not None:
            params["program_id"] = str(program_id)
        payload = (
            await self._transport.request("GET", "/api/charts/list", params=params or None)
        ) or {}
        rows = payload.get("charts", []) if isinstance(payload, dict) else (payload or [])
        return [Chart.model_validate(r) for r in rows]
