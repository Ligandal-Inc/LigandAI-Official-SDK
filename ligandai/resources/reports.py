# Copyright © 2025 Ligandal, Inc. All rights reserved.
"""Report generation endpoints (PDF reports)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from ligandai.resources._base import AsyncResource, Resource
from ligandai.types import Report, ReportSection


class Reports(Resource):
    """``/api/reports/*``."""

    def generate(
        self,
        title: str,
        sections: list[ReportSection | dict[str, Any]],
        report_type: str = "custom",
        metadata: dict[str, Any] | None = None,
        theme: Literal["light", "dark"] = "light",
    ) -> Report:
        normalized_sections = [
            s.model_dump(by_alias=True) if isinstance(s, ReportSection) else s
            for s in sections
        ]
        body: dict[str, Any] = {
            "title": title,
            "sections": normalized_sections,
            "reportType": report_type,
            "theme": theme,
        }
        if metadata is not None:
            body["metadata"] = metadata
        return Report.model_validate(
            self._transport.request("POST", "/api/reports/generate", json=body) or {}
        )

    def download(self, report_id: str, dest: Path | str) -> Path:
        """Download the rendered PDF and save it to ``dest``. Returns the path."""
        dest_path = Path(dest)
        resp = self._transport.request(
            "GET", f"/api/reports/{report_id}/download", expect_json=False
        )
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(resp.content)
        return dest_path


class AsyncReports(AsyncResource):
    async def generate(
        self,
        title: str,
        sections: list[ReportSection | dict[str, Any]],
        report_type: str = "custom",
        metadata: dict[str, Any] | None = None,
        theme: Literal["light", "dark"] = "light",
    ) -> Report:
        normalized_sections = [
            s.model_dump(by_alias=True) if isinstance(s, ReportSection) else s
            for s in sections
        ]
        body: dict[str, Any] = {
            "title": title,
            "sections": normalized_sections,
            "reportType": report_type,
            "theme": theme,
        }
        if metadata is not None:
            body["metadata"] = metadata
        return Report.model_validate(
            await self._transport.request("POST", "/api/reports/generate", json=body) or {}
        )

    async def download(self, report_id: str, dest: Path | str) -> Path:
        dest_path = Path(dest)
        resp = await self._transport.request(
            "GET", f"/api/reports/{report_id}/download", expect_json=False
        )
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(resp.content)
        return dest_path
