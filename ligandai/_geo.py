# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Shared validation for the public GEO discovery/import boundary."""

from __future__ import annotations

import json
import math
import re
from typing import Any

from ligandai.errors import LigandAIError
from ligandai.types import GeoImportJob, GeoInspection, GeoSearchResponse

GEO_PATH = "/api/datasets/geo"
GEO_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "canceled"})


class GeoImportTimeout(TimeoutError):
    """Polling deadline reached; the last actual server receipt remains available."""

    def __init__(self, job_id: str, last_job: GeoImportJob | None) -> None:
        super().__init__(f"GEO import {job_id} did not finish before the polling deadline")
        self.job_id = job_id
        self.last_job = last_job


def geo_accession(accession: str) -> str:
    if not isinstance(accession, str) or not re.fullmatch(r"GSE[0-9]+", accession.strip(), re.I):
        raise ValueError("GEO accession must be GSE followed by digits")
    return accession.strip().upper()


def geo_job_id(job_id: str) -> str:
    if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_:-]{1,200}", job_id):
        raise ValueError("GEO job_id must be a nonempty identifier, not a URL or path")
    return job_id


def geo_search_params(query: str, limit: int) -> dict[str, Any]:
    if not isinstance(query, str) or not query.strip():
        raise ValueError("GEO search query must not be empty")
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("GEO search limit must be an integer between 1 and 50")
    return {"query": query.strip(), "limit": limit}


def geo_import_selection(selection: dict[str, Any] | None) -> dict[str, Any] | None:
    """Preserve explicit selection; the server validates published names and parser schema."""
    if selection is None:
        return None
    if not isinstance(selection, dict) or not all(isinstance(key, str) for key in selection):
        raise ValueError("selection must be a JSON object")
    try:
        encoded = json.dumps(selection, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("selection must contain finite JSON values") from exc
    if len(encoded.encode("utf-8")) > 1024 * 1024:
        raise ValueError("selection exceeds the one MiB limit")
    return selection


def parse_geo_search(payload: Any) -> GeoSearchResponse:
    if isinstance(payload, list):
        payload = {"results": payload}
    elif isinstance(payload, dict):
        if payload.get("success") is False:
            raise LigandAIError(str(payload.get("error") or "GEO search failed"))
        payload = dict(payload)
        if "results" not in payload and (
            "curatedSuggestions" in payload or "ncbiResults" in payload
        ):
            curated = payload.get("curatedSuggestions", [])
            ncbi = payload.get("ncbiResults", [])
            if not isinstance(curated, list) or not isinstance(ncbi, list):
                raise ValueError("GEO search groups must be lists")
            # Older servers did not normalize results. Keep both source records;
            # do not overwrite conflicting metadata for a repeated accession.
            payload["results"] = [*curated, *ncbi]
    return GeoSearchResponse.model_validate(payload)


def parse_geo_job(
    payload: Any, *, job_id: str | None = None, accession: str | None = None,
) -> GeoImportJob:
    job = GeoImportJob.model_validate(payload)
    if job_id is not None and job.job_id != job_id:
        raise ValueError("GEO import response jobId does not match the requested job")
    if accession is not None and job.accession != accession:
        raise ValueError("GEO import response accession does not match the requested dataset")
    return job


def parse_geo_inspection(payload: Any, accession: str) -> GeoInspection:
    inspection = GeoInspection.model_validate(payload)
    if inspection.accession != accession:
        raise ValueError("GEO inspection accession does not match the requested dataset")
    return inspection


def geo_poll_parameters(timeout: float, poll_interval: float) -> None:
    for name, value in (("timeout", timeout), ("poll_interval", poll_interval)):
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be a positive finite number")
