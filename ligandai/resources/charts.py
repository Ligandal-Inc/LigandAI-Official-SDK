# Copyright © 2025 Ligandal, Inc. All rights reserved.
"""Chart generation endpoints (matplotlib-rendered server-side)."""

from __future__ import annotations

from typing import Any

from ligandai.resources._base import AsyncResource, Resource
from ligandai.types import Chart


class Charts(Resource):
    """``/api/charts/*``."""

    def generate(
        self,
        chart_type: str,
        title: str,
        data: dict[str, Any],
        style: dict[str, Any] | None = None,
        save_to_program: int | None = None,
    ) -> Chart:
        body: dict[str, Any] = {
            "chartType": chart_type,
            "title": title,
            "data": data,
        }
        if style is not None:
            body["style"] = style
        if save_to_program is not None:
            body["saveToProgram"] = save_to_program
        return Chart.model_validate(
            self._transport.request("POST", "/api/charts/generate", json=body) or {}
        )

    def get(self, chart_id: str) -> Chart:
        return Chart.model_validate(
            self._transport.request("GET", f"/api/charts/{chart_id}") or {}
        )


class AsyncCharts(AsyncResource):
    async def generate(
        self,
        chart_type: str,
        title: str,
        data: dict[str, Any],
        style: dict[str, Any] | None = None,
        save_to_program: int | None = None,
    ) -> Chart:
        body: dict[str, Any] = {
            "chartType": chart_type,
            "title": title,
            "data": data,
        }
        if style is not None:
            body["style"] = style
        if save_to_program is not None:
            body["saveToProgram"] = save_to_program
        return Chart.model_validate(
            await self._transport.request("POST", "/api/charts/generate", json=body) or {}
        )

    async def get(self, chart_id: str) -> Chart:
        return Chart.model_validate(
            await self._transport.request("GET", f"/api/charts/{chart_id}") or {}
        )
