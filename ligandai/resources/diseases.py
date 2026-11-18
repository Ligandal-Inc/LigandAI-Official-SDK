# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Disease Atlas biological resolution and superadmin intelligence endpoints."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, overload
from urllib.parse import quote

from ligandai._constants import (
    DEFAULT_ATLAS_DATA_VERSION,
    SUPERADMIN_API_KEY_PREFIXES,
    AtlasDataVersion,
)
from ligandai.errors import (
    LigandAIAuthError,
    LigandAIForbidden,
    LigandAIInvalidConfig,
    LigandAIUnreadableResponse,
)
from ligandai.resources._base import AsyncResource, Resource, unwrap_rows
from ligandai.types import (
    AdminBasketDetailResponse,
    AdminBasketsResponse,
    AdminBenchmarksResponse,
    AdminDiseaseIntelligence,
    AdminGeoResponse,
    AdminLadderEntry,
    AdminLaddersResponse,
    AdminMatrixResponse,
    AdminPopulationRecord,
    AdminPopulationsResponse,
    AtlasDiseaseDetail,
    AtlasDiseaseSummary,
    AtlasDiseaseTargetsResponse,
    AtlasDiseaseTissue,
    AtlasDiseaseTissuesResponse,
    AtlasMeta,
    AtlasRankedTarget,
    AtlasSearchResponse,
    DiseaseMap,
    MatrixWeights,
    Mutation,
)

if TYPE_CHECKING:
    from ligandai._http import AsyncHTTPTransport, HTTPTransport
    from ligandai.client import AsyncLigandAI, LigandAI

_ATLAS_SEARCH_KEYS = ("items", "diseases", "results", "data")
_ATLAS_TISSUE_KEYS = ("tissues", "items", "results", "data")
_ATLAS_TARGET_KEYS = ("targets", "items", "results", "data")
_MUTATION_KEYS = ("mutations", "results", "data")


def _gates_file_passed(gates_path: str | Path) -> bool:
    """Return True only when a v3 ``gates.json`` file exists, is post-Addendum-7 fresh, and passes."""
    p = Path(gates_path)
    try:
        gates_mtime = p.stat().st_mtime_ns
        for add7_file in p.parent.glob("add7-*.json"):
            if add7_file.stat().st_mtime_ns > gates_mtime:
                return False
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(raw, Mapping):
        return False
    if (
        str(raw.get("overall") or "").lower() == "fail"
        or raw.get("passed") is False
        or raw.get("all_passed") is False
        or raw.get("allPassed") is False
        or str(raw.get("status") or "").lower() == "fail"
    ):
        return False

    gates = raw.get("gates") if "gates" in raw else raw.get("checks")
    if isinstance(gates, Mapping) and gates:
        all_ok = all(
            (v is True)
            or (isinstance(v, str) and v.lower() in {"pass", "passed"})
            or (
                isinstance(v, Mapping)
                and (
                    v.get("passed") is True
                    or v.get("ok") is True
                    or str(v.get("status") or "").lower() in {"pass", "passed"}
                )
            )
            for v in gates.values()
        )
        if not all_ok:
            return False
        basket_gate = gates.get("Basket replay and drivers")
        if isinstance(basket_gate, Mapping):
            obs = basket_gate.get("observed")
            if isinstance(obs, Mapping):
                unpriced = obs.get("unpriced")
                if isinstance(unpriced, list) and len(unpriced) > 0:
                    return False
        prev_gate = gates.get("US point prevalence concentration")
        if isinstance(prev_gate, Mapping):
            obs = prev_gate.get("observed")
            if isinstance(obs, Mapping):
                num_id = obs.get("numeric_identities")
                tot_id = obs.get("total_identities")
                if (
                    isinstance(num_id, (int, float))
                    and isinstance(tot_id, (int, float))
                    and tot_id > 0
                    and (num_id / tot_id) < 0.95
                ):
                    return False
        return True
    if isinstance(gates, list) and gates:
        return all(
            (item is True)
            or (isinstance(item, str) and item.lower() in {"pass", "passed"})
            or (
                isinstance(item, Mapping)
                and (
                    item.get("passed") is True
                    or item.get("ok") is True
                    or str(item.get("status") or "").lower() in {"pass", "passed"}
                )
            )
            for item in gates
        )
    if (
        raw.get("passed") is True
        or raw.get("all_passed") is True
        or raw.get("allPassed") is True
    ):
        return True
    if str(raw.get("overall") or "").lower() in {"pass", "passed"}:
        return True
    if str(raw.get("status") or "").lower() in {"pass", "passed"}:
        return True
    return False


def resolve_atlas_data_version(
    requested: str | None = None,
    *,
    gates_path: str | Path | None = None,
) -> AtlasDataVersion:
    """Resolve the active Disease Atlas dataset version (``"v2"`` or ``"v3"``).

    Defaults to :data:`DEFAULT_ATLAS_DATA_VERSION` (``"v2"``), overridable via
    the ``requested`` argument or ``ATLAS_DATA_VERSION`` /
    ``LIGANDAI_ATLAS_DATA_VERSION``. When ``"v3"`` is requested alongside a
    ``gates_path`` (or ``ATLAS_GATES_PATH``), ``gates.json`` must report passing
    gates or the resolver falls back safely to ``"v2"``.
    """
    raw = (
        requested
        or os.environ.get("ATLAS_DATA_VERSION")
        or os.environ.get("LIGANDAI_ATLAS_DATA_VERSION")
        or DEFAULT_ATLAS_DATA_VERSION
    )
    cleaned = str(raw).strip().lower()
    if cleaned not in ("v2", "v3"):
        raise LigandAIInvalidConfig(
            f"Unsupported Disease Atlas data version {raw!r}; expected 'v2' or 'v3'.",
            field="data_version",
            value=raw,
            allowed=("v2", "v3"),
        )
    effective_gates = gates_path or os.environ.get("ATLAS_GATES_PATH")
    if cleaned == "v3" and effective_gates is not None:
        if not _gates_file_passed(effective_gates):
            return "v2"
    return "v3" if cleaned == "v3" else "v2"


def _atlas_headers(data_version: str | None) -> dict[str, str]:
    explicit = (
        data_version
        or os.environ.get("ATLAS_DATA_VERSION")
        or os.environ.get("LIGANDAI_ATLAS_DATA_VERSION")
    )
    if not explicit:
        return {}
    resolved = resolve_atlas_data_version(explicit)
    return {"X-Atlas-Data-Version": resolved}


def _header_kwargs(data_version: str | None) -> dict[str, Any]:
    hdrs = _atlas_headers(data_version)
    return {"headers": hdrs} if hdrs else {}


def _encode_id(value: str | int) -> str:
    text = str(value).strip()
    if not text:
        raise LigandAIInvalidConfig(
            "Disease identifier must be a non-empty string or integer.",
            field="disease_id",
            value=value,
            allowed="non-empty disease identifier",
        )
    return quote(text, safe="")


def _require_mapping(payload: Any, *, what: str) -> Mapping[str, Any]:
    if isinstance(payload, Mapping):
        return payload
    if payload is None:
        raise LigandAIUnreadableResponse(
            f"{what} returned an empty body instead of a JSON object.",
            cause="empty_body",
        )
    raise LigandAIUnreadableResponse(
        f"{what} returned {type(payload).__name__} instead of a JSON object.",
        cause="unrecognised_envelope",
    )


def _require_superadmin_role(
    client: LigandAI | AsyncLigandAI | None,
    transport: HTTPTransport | AsyncHTTPTransport,
) -> None:
    """Enforce superadmin API-key role client-side before sending any network request."""
    api_key = (
        (client.api_key if client is not None else None)
        or getattr(transport, "api_key", None)
    )
    if api_key is None or not str(api_key).strip():
        raise LigandAIAuthError(
            "Superadmin Disease Atlas intelligence endpoints require a superadmin API key "
            "(lgai_sa_* or lgai_superadmin_*).",
            code="superadmin_api_key_required",
            status_code=401,
        )
    cleaned = str(api_key).strip()
    tier = client.tier if client is not None else None
    is_superadmin = tier == "superadmin" or any(
        cleaned.startswith(prefix) for prefix in SUPERADMIN_API_KEY_PREFIXES
    )
    if not is_superadmin:
        raise LigandAIForbidden(
            "Superadmin Disease Atlas intelligence endpoints require superadmin role; "
            "non-superadmin API keys are not permitted.",
            reason="forbidden_role",
            code="forbidden_role",
            status_code=403,
        )


def _serialize_weights(
    weights: MatrixWeights | Mapping[str, float] | None,
) -> dict[str, float] | None:
    if weights is None:
        return None
    if isinstance(weights, MatrixWeights):
        return weights.model_dump()
    if isinstance(weights, Mapping):
        return {str(k): float(v) for k, v in weights.items()}
    raise LigandAIInvalidConfig(
        "Matrix weights must be a MatrixWeights instance or mapping of component weights.",
        field="weights",
        value=weights,
        allowed="MatrixWeights | dict[str, float]",
    )


class AdminDiseases(Resource):
    """Superadmin-only Disease Atlas intelligence endpoints (``/api/admin/atlas/*``).

    Every method verifies that the client holds a superadmin API key
    (``lgai_sa_*`` or ``lgai_superadmin_*``) before dispatching any HTTP call.
    """

    def _check_role(self) -> None:
        _require_superadmin_role(self._client, self._transport)

    def matrix(
        self,
        *,
        q: str | None = None,
        organ: str | None = None,
        modality: str | None = None,
        rarity: str | None = None,
        competition_level: str | None = None,
        designation: str | None = None,
        ladder_id: str | None = None,
        exclude_imputed: bool = False,
        min_score: float | None = None,
        sort_by: str = "score",
        sort_dir: str = "desc",
        limit: int = 50,
        cursor: str | None = None,
        weights: MatrixWeights | Mapping[str, float] | None = None,
        data_version: str | None = None,
    ) -> AdminMatrixResponse:
        """Query or dynamically rerank the superadmin decision matrix."""
        self._check_role()
        weights_dict = _serialize_weights(weights)
        extra = _header_kwargs(data_version)
        if weights_dict is not None:
            body: dict[str, Any] = {
                "q": q,
                "organ": organ,
                "modality": modality,
                "rarity": rarity,
                "competition_level": competition_level,
                "designation": designation,
                "ladder_id": ladder_id,
                "exclude_imputed": exclude_imputed,
                "min_score": min_score,
                "sort_by": sort_by,
                "sort_dir": sort_dir,
                "limit": limit,
                "cursor": cursor,
                "weights": weights_dict,
            }
            payload = self._transport.request(
                "POST",
                "/api/admin/atlas/matrix/rerank",
                json={k: v for k, v in body.items() if v is not None},
                **extra,
            )
            what = "POST /api/admin/atlas/matrix/rerank"
        else:
            params: dict[str, Any] = {
                "q": q,
                "organ": organ,
                "modality": modality,
                "rarity": rarity,
                "competition_level": competition_level,
                "designation": designation,
                "ladder_id": ladder_id,
                "exclude_imputed": exclude_imputed if exclude_imputed else None,
                "min_score": min_score,
                "sort_by": sort_by,
                "sort_dir": sort_dir,
                "limit": limit,
                "cursor": cursor,
            }
            payload = self._transport.request(
                "GET",
                "/api/admin/atlas/matrix",
                params={k: v for k, v in params.items() if v is not None},
                **extra,
            )
            what = "GET /api/admin/atlas/matrix"
        return AdminMatrixResponse.model_validate(_require_mapping(payload, what=what))

    def intelligence(
        self,
        disease_id: str,
        *,
        data_version: str | None = None,
    ) -> AdminDiseaseIntelligence:
        """Fetch the full superadmin commercial, population, and regulatory dossier."""
        self._check_role()
        encoded = _encode_id(disease_id)
        path = f"/api/admin/atlas/diseases/{encoded}/intelligence"
        payload = self._transport.request("GET", path, **_header_kwargs(data_version))
        return AdminDiseaseIntelligence.model_validate(
            _require_mapping(payload, what=f"GET {path}")
        )

    @overload
    def baskets(
        self,
        organ: None = None,
        *,
        data_version: str | None = None,
    ) -> AdminBasketsResponse: ...

    @overload
    def baskets(
        self,
        organ: str,
        *,
        data_version: str | None = None,
    ) -> AdminBasketDetailResponse: ...

    def baskets(
        self,
        organ: str | None = None,
        *,
        data_version: str | None = None,
    ) -> AdminBasketsResponse | AdminBasketDetailResponse:
        """Fetch all organ rNPV baskets or one organ's contributions and cash flows."""
        self._check_role()
        extra = _header_kwargs(data_version)
        if organ is not None:
            encoded = _encode_id(organ)
            path = f"/api/admin/atlas/baskets/{encoded}"
            payload = self._transport.request("GET", path, **extra)
            return AdminBasketDetailResponse.model_validate(
                _require_mapping(payload, what=f"GET {path}")
            )
        path = "/api/admin/atlas/baskets"
        payload = self._transport.request("GET", path, **extra)
        return AdminBasketsResponse.model_validate(
            _require_mapping(payload, what=f"GET {path}")
        )

    @overload
    def populations(
        self,
        disease_id: None = None,
        *,
        q: str | None = None,
        organ: str | None = None,
        exclude_imputed: bool = False,
        limit: int = 50,
        cursor: str | None = None,
        data_version: str | None = None,
    ) -> AdminPopulationsResponse: ...

    @overload
    def populations(
        self,
        disease_id: str,
        *,
        q: str | None = None,
        organ: str | None = None,
        exclude_imputed: bool = False,
        limit: int = 50,
        cursor: str | None = None,
        data_version: str | None = None,
    ) -> AdminPopulationRecord: ...

    def populations(
        self,
        disease_id: str | None = None,
        *,
        q: str | None = None,
        organ: str | None = None,
        exclude_imputed: bool = False,
        limit: int = 50,
        cursor: str | None = None,
        data_version: str | None = None,
    ) -> AdminPopulationsResponse | AdminPopulationRecord:
        """List patient population estimates or fetch one disease's population dossier."""
        self._check_role()
        extra = _header_kwargs(data_version)
        if disease_id is not None:
            encoded = _encode_id(disease_id)
            path = f"/api/admin/atlas/populations/{encoded}"
            payload = self._transport.request("GET", path, **extra)
            return AdminPopulationRecord.model_validate(
                _require_mapping(payload, what=f"GET {path}")
            )
        params: dict[str, Any] = {
            "q": q,
            "organ": organ,
            "exclude_imputed": exclude_imputed if exclude_imputed else None,
            "limit": limit,
            "cursor": cursor,
        }
        path = "/api/admin/atlas/populations"
        payload = self._transport.request(
            "GET",
            path,
            params={k: v for k, v in params.items() if v is not None},
            **extra,
        )
        return AdminPopulationsResponse.model_validate(
            _require_mapping(payload, what=f"GET {path}")
        )

    @overload
    def ladders(
        self,
        ladder_id: None = None,
        *,
        data_version: str | None = None,
    ) -> AdminLaddersResponse: ...

    @overload
    def ladders(
        self,
        ladder_id: str,
        *,
        data_version: str | None = None,
    ) -> AdminLadderEntry: ...

    def ladders(
        self,
        ladder_id: str | None = None,
        *,
        data_version: str | None = None,
    ) -> AdminLaddersResponse | AdminLadderEntry:
        """List rare-to-common expansion ladders or fetch one ladder's ordered cohorts."""
        self._check_role()
        extra = _header_kwargs(data_version)
        if ladder_id is not None:
            encoded = _encode_id(ladder_id)
            path = f"/api/admin/atlas/ladders/{encoded}"
            payload = self._transport.request("GET", path, **extra)
            return AdminLadderEntry.model_validate(
                _require_mapping(payload, what=f"GET {path}")
            )
        path = "/api/admin/atlas/ladders"
        payload = self._transport.request("GET", path, **extra)
        return AdminLaddersResponse.model_validate(
            _require_mapping(payload, what=f"GET {path}")
        )

    def geo(
        self,
        disease_id: str,
        *,
        data_version: str | None = None,
    ) -> AdminGeoResponse:
        """Fetch superadmin disease-tissue GEO cohorts and coverage."""
        self._check_role()
        encoded = _encode_id(disease_id)
        path = f"/api/admin/atlas/geo/{encoded}"
        payload = self._transport.request("GET", path, **_header_kwargs(data_version))
        return AdminGeoResponse.model_validate(
            _require_mapping(payload, what=f"GET {path}")
        )

    def benchmarks(
        self,
        *,
        data_version: str | None = None,
    ) -> AdminBenchmarksResponse:
        """Fetch superadmin US sales benchmarks and regulatory designation criteria."""
        self._check_role()
        path = "/api/admin/atlas/benchmarks"
        payload = self._transport.request("GET", path, **_header_kwargs(data_version))
        return AdminBenchmarksResponse.model_validate(
            _require_mapping(payload, what=f"GET {path}")
        )


class AsyncAdminDiseases(AsyncResource):
    """Async counterpart to :class:`AdminDiseases`."""

    def _check_role(self) -> None:
        _require_superadmin_role(self._client, self._transport)

    async def matrix(
        self,
        *,
        q: str | None = None,
        organ: str | None = None,
        modality: str | None = None,
        rarity: str | None = None,
        competition_level: str | None = None,
        designation: str | None = None,
        ladder_id: str | None = None,
        exclude_imputed: bool = False,
        min_score: float | None = None,
        sort_by: str = "score",
        sort_dir: str = "desc",
        limit: int = 50,
        cursor: str | None = None,
        weights: MatrixWeights | Mapping[str, float] | None = None,
        data_version: str | None = None,
    ) -> AdminMatrixResponse:
        self._check_role()
        weights_dict = _serialize_weights(weights)
        extra = _header_kwargs(data_version)
        if weights_dict is not None:
            body: dict[str, Any] = {
                "q": q,
                "organ": organ,
                "modality": modality,
                "rarity": rarity,
                "competition_level": competition_level,
                "designation": designation,
                "ladder_id": ladder_id,
                "exclude_imputed": exclude_imputed,
                "min_score": min_score,
                "sort_by": sort_by,
                "sort_dir": sort_dir,
                "limit": limit,
                "cursor": cursor,
                "weights": weights_dict,
            }
            payload = await self._transport.request(
                "POST",
                "/api/admin/atlas/matrix/rerank",
                json={k: v for k, v in body.items() if v is not None},
                **extra,
            )
            what = "POST /api/admin/atlas/matrix/rerank"
        else:
            params: dict[str, Any] = {
                "q": q,
                "organ": organ,
                "modality": modality,
                "rarity": rarity,
                "competition_level": competition_level,
                "designation": designation,
                "ladder_id": ladder_id,
                "exclude_imputed": exclude_imputed if exclude_imputed else None,
                "min_score": min_score,
                "sort_by": sort_by,
                "sort_dir": sort_dir,
                "limit": limit,
                "cursor": cursor,
            }
            payload = await self._transport.request(
                "GET",
                "/api/admin/atlas/matrix",
                params={k: v for k, v in params.items() if v is not None},
                **extra,
            )
            what = "GET /api/admin/atlas/matrix"
        return AdminMatrixResponse.model_validate(_require_mapping(payload, what=what))

    async def intelligence(
        self,
        disease_id: str,
        *,
        data_version: str | None = None,
    ) -> AdminDiseaseIntelligence:
        self._check_role()
        encoded = _encode_id(disease_id)
        path = f"/api/admin/atlas/diseases/{encoded}/intelligence"
        payload = await self._transport.request(
            "GET", path, **_header_kwargs(data_version)
        )
        return AdminDiseaseIntelligence.model_validate(
            _require_mapping(payload, what=f"GET {path}")
        )

    async def baskets(
        self,
        organ: str | None = None,
        *,
        data_version: str | None = None,
    ) -> AdminBasketsResponse | AdminBasketDetailResponse:
        self._check_role()
        extra = _header_kwargs(data_version)
        if organ is not None:
            encoded = _encode_id(organ)
            path = f"/api/admin/atlas/baskets/{encoded}"
            payload = await self._transport.request("GET", path, **extra)
            return AdminBasketDetailResponse.model_validate(
                _require_mapping(payload, what=f"GET {path}")
            )
        path = "/api/admin/atlas/baskets"
        payload = await self._transport.request("GET", path, **extra)
        return AdminBasketsResponse.model_validate(
            _require_mapping(payload, what=f"GET {path}")
        )

    async def populations(
        self,
        disease_id: str | None = None,
        *,
        q: str | None = None,
        organ: str | None = None,
        exclude_imputed: bool = False,
        limit: int = 50,
        cursor: str | None = None,
        data_version: str | None = None,
    ) -> AdminPopulationsResponse | AdminPopulationRecord:
        self._check_role()
        extra = _header_kwargs(data_version)
        if disease_id is not None:
            encoded = _encode_id(disease_id)
            path = f"/api/admin/atlas/populations/{encoded}"
            payload = await self._transport.request("GET", path, **extra)
            return AdminPopulationRecord.model_validate(
                _require_mapping(payload, what=f"GET {path}")
            )
        params: dict[str, Any] = {
            "q": q,
            "organ": organ,
            "exclude_imputed": exclude_imputed if exclude_imputed else None,
            "limit": limit,
            "cursor": cursor,
        }
        path = "/api/admin/atlas/populations"
        payload = await self._transport.request(
            "GET",
            path,
            params={k: v for k, v in params.items() if v is not None},
            **extra,
        )
        return AdminPopulationsResponse.model_validate(
            _require_mapping(payload, what=f"GET {path}")
        )

    async def ladders(
        self,
        ladder_id: str | None = None,
        *,
        data_version: str | None = None,
    ) -> AdminLaddersResponse | AdminLadderEntry:
        self._check_role()
        extra = _header_kwargs(data_version)
        if ladder_id is not None:
            encoded = _encode_id(ladder_id)
            path = f"/api/admin/atlas/ladders/{encoded}"
            payload = await self._transport.request("GET", path, **extra)
            return AdminLadderEntry.model_validate(
                _require_mapping(payload, what=f"GET {path}")
            )
        path = "/api/admin/atlas/ladders"
        payload = await self._transport.request("GET", path, **extra)
        return AdminLaddersResponse.model_validate(
            _require_mapping(payload, what=f"GET {path}")
        )

    async def geo(
        self,
        disease_id: str,
        *,
        data_version: str | None = None,
    ) -> AdminGeoResponse:
        self._check_role()
        encoded = _encode_id(disease_id)
        path = f"/api/admin/atlas/geo/{encoded}"
        payload = await self._transport.request(
            "GET", path, **_header_kwargs(data_version)
        )
        return AdminGeoResponse.model_validate(
            _require_mapping(payload, what=f"GET {path}")
        )

    async def benchmarks(
        self,
        *,
        data_version: str | None = None,
    ) -> AdminBenchmarksResponse:
        self._check_role()
        path = "/api/admin/atlas/benchmarks"
        payload = await self._transport.request(
            "GET", path, **_header_kwargs(data_version)
        )
        return AdminBenchmarksResponse.model_validate(
            _require_mapping(payload, what=f"GET {path}")
        )


class Diseases(Resource):
    """Disease Atlas biological resolution (``/api/atlas/*``) and DiseaseViewer endpoints.

    Public biological methods (:meth:`meta`, :meth:`search`, :meth:`get`,
    :meth:`tissues`, :meth:`targets`, and one-call :meth:`map`) never return
    commercial, valuation, population, or regulatory fields. Superadmin-only
    intelligence methods live on ``client.diseases.admin`` (and are also
    delegated on ``client.diseases`` with the same client-side superadmin API-key
    role check).
    """

    def __init__(
        self,
        transport: HTTPTransport,
        client: LigandAI | None = None,
    ) -> None:
        super().__init__(transport, client=client)
        self.admin: AdminDiseases = AdminDiseases(transport, client=client)

    def meta(self, *, data_version: str | None = None) -> AtlasMeta:
        """Fetch Disease Atlas metadata, counts, organs, and featured examples."""
        payload = self._transport.request(
            "GET", "/api/atlas/meta", **_header_kwargs(data_version)
        )
        return AtlasMeta.model_validate(
            _require_mapping(payload, what="GET /api/atlas/meta")
        )

    def search(
        self,
        q: str = "",
        *,
        organ: str | None = None,
        tissue: str | None = None,
        rarity: str | None = None,
        gene: str | None = None,
        limit: int = 25,
        cursor: str | None = None,
        data_version: str | None = None,
    ) -> list[AtlasDiseaseSummary]:
        """Search diseases by name, clinical abbreviation, gene symbol, or ontology ID."""
        params: dict[str, Any] = {
            "q": q,
            "organ": organ,
            "tissue": tissue,
            "rarity": rarity,
            "gene": gene,
            "limit": limit,
            "cursor": cursor,
        }
        payload = self._transport.request(
            "GET",
            "/api/atlas/diseases/search",
            params={k: v for k, v in params.items() if v is not None},
            **_header_kwargs(data_version),
        )
        items = unwrap_rows(
            payload,
            _ATLAS_SEARCH_KEYS,
            what=f"GET /api/atlas/diseases/search for {q!r}",
        )
        return [AtlasDiseaseSummary.model_validate(row) for row in items]

    def search_page(
        self,
        q: str = "",
        *,
        organ: str | None = None,
        tissue: str | None = None,
        rarity: str | None = None,
        gene: str | None = None,
        limit: int = 25,
        cursor: str | None = None,
        data_version: str | None = None,
    ) -> AtlasSearchResponse:
        """Search diseases and return the full paginated envelope (``next_cursor``, ``total_estimate``)."""
        params: dict[str, Any] = {
            "q": q,
            "organ": organ,
            "tissue": tissue,
            "rarity": rarity,
            "gene": gene,
            "limit": limit,
            "cursor": cursor,
        }
        payload = self._transport.request(
            "GET",
            "/api/atlas/diseases/search",
            params={k: v for k, v in params.items() if v is not None},
            **_header_kwargs(data_version),
        )
        mapping = _require_mapping(
            payload, what=f"GET /api/atlas/diseases/search for {q!r}"
        )
        return AtlasSearchResponse.model_validate(mapping)

    def get(
        self,
        disease_id: str | int,
        *,
        data_version: str | None = None,
    ) -> AtlasDiseaseDetail:
        """Fetch public biological detail for one disease by ID, alias, or name."""
        encoded = _encode_id(disease_id)
        path = f"/api/atlas/diseases/{encoded}"
        payload = self._transport.request("GET", path, **_header_kwargs(data_version))
        mapping = _require_mapping(payload, what=f"GET {path}")
        inner = (
            mapping["disease"]
            if isinstance(mapping.get("disease"), Mapping)
            else mapping
        )
        return AtlasDiseaseDetail.model_validate(inner)

    def tissues(
        self,
        disease_id: str | int,
        *,
        role: str | None = None,
        include_cells: bool | None = None,
        data_version: str | None = None,
    ) -> list[AtlasDiseaseTissue]:
        """Fetch disease-associated organs, GTEx/HPA tissues, single-cell types, and healthy GEO series."""
        encoded = _encode_id(disease_id)
        path = f"/api/atlas/diseases/{encoded}/tissues"
        params: dict[str, Any] = {
            "role": role,
            "include_cells": include_cells,
        }
        payload = self._transport.request(
            "GET",
            path,
            params={k: v for k, v in params.items() if v is not None},
            **_header_kwargs(data_version),
        )
        items = unwrap_rows(payload, _ATLAS_TISSUE_KEYS, what=f"GET {path}")
        return [AtlasDiseaseTissue.model_validate(row) for row in items]

    def tissues_response(
        self,
        disease_id: str | int,
        *,
        role: str | None = None,
        include_cells: bool | None = None,
        data_version: str | None = None,
    ) -> AtlasDiseaseTissuesResponse:
        """Fetch the full tissues response envelope (including ``matched_tissue_hint`` and ``took_ms``)."""
        encoded = _encode_id(disease_id)
        path = f"/api/atlas/diseases/{encoded}/tissues"
        params: dict[str, Any] = {
            "role": role,
            "include_cells": include_cells,
        }
        payload = self._transport.request(
            "GET",
            path,
            params={k: v for k, v in params.items() if v is not None},
            **_header_kwargs(data_version),
        )
        return AtlasDiseaseTissuesResponse.model_validate(
            _require_mapping(payload, what=f"GET {path}")
        )

    def targets(
        self,
        disease_id: str | int,
        tissue: str | None = None,
        *,
        cell_type: str | None = None,
        surface_only: bool | None = None,
        top_k: int = 25,
        strict_targets: bool | None = None,
        data_version: str | None = None,
    ) -> list[AtlasRankedTarget]:
        """Fetch ranked biological targets and delivery receptors for a disease (optionally scoped to a tissue)."""
        encoded = _encode_id(disease_id)
        path = f"/api/atlas/diseases/{encoded}/targets"
        params: dict[str, Any] = {
            "tissue": tissue,
            "cell_type": cell_type,
            "surface_only": surface_only,
            "top_k": top_k,
            "strict_targets": strict_targets,
        }
        payload = self._transport.request(
            "GET",
            path,
            params={k: v for k, v in params.items() if v is not None},
            **_header_kwargs(data_version),
        )
        items = unwrap_rows(payload, _ATLAS_TARGET_KEYS, what=f"GET {path}")
        return [AtlasRankedTarget.model_validate(row) for row in items]

    def targets_response(
        self,
        disease_id: str | int,
        tissue: str | None = None,
        *,
        cell_type: str | None = None,
        surface_only: bool | None = None,
        top_k: int = 25,
        strict_targets: bool | None = None,
        data_version: str | None = None,
    ) -> AtlasDiseaseTargetsResponse:
        """Fetch the full targets response envelope (including ``status``, ``empty_reason``, and ``filters``)."""
        encoded = _encode_id(disease_id)
        path = f"/api/atlas/diseases/{encoded}/targets"
        params: dict[str, Any] = {
            "tissue": tissue,
            "cell_type": cell_type,
            "surface_only": surface_only,
            "top_k": top_k,
            "strict_targets": strict_targets,
        }
        payload = self._transport.request(
            "GET",
            path,
            params={k: v for k, v in params.items() if v is not None},
            **_header_kwargs(data_version),
        )
        return AtlasDiseaseTargetsResponse.model_validate(
            _require_mapping(payload, what=f"GET {path}")
        )

    def map(
        self,
        disease: str,
        *,
        tissue: str | None = None,
        cell_type: str | None = None,
        surface_only: bool = False,
        top_k: int = 25,
        use_get: bool = False,
        data_version: str | None = None,
    ) -> DiseaseMap:
        """Resolve a disease name, abbreviation, ID, or compound tissue phrase in one call.

        Returns ``tissues``, ``cells``, ``receptors``, ``targets``, ``evidence``,
        and ``start_campaign`` ready for binder design workflows.
        """
        cleaned = str(disease).strip()
        if not cleaned:
            raise LigandAIInvalidConfig(
                "Disease query must be a non-empty string.",
                field="disease",
                value=disease,
                allowed="non-empty disease query",
            )
        extra = _header_kwargs(data_version)
        if use_get:
            encoded = _encode_id(cleaned)
            path = f"/api/atlas/diseases/{encoded}/map"
            params: dict[str, Any] = {
                "tissue": tissue,
                "cell_type": cell_type,
                "surface_only": surface_only if surface_only else None,
                "top_k": top_k,
            }
            payload = self._transport.request(
                "GET",
                path,
                params={k: v for k, v in params.items() if v is not None},
                **extra,
            )
            return DiseaseMap.model_validate(
                _require_mapping(payload, what=f"GET {path}")
            )
        body: dict[str, Any] = {
            "disease": cleaned,
            "tissue": tissue,
            "cellType": cell_type,
            "surfaceOnly": surface_only,
            "topK": top_k,
        }
        payload = self._transport.request(
            "POST",
            "/api/atlas/map",
            json=body,
            **extra,
        )
        return DiseaseMap.model_validate(
            _require_mapping(payload, what="POST /api/atlas/map")
        )

    # -- Superadmin-gated convenience delegates -------------------------------

    def matrix(self, **kwargs: Any) -> AdminMatrixResponse:
        """Superadmin-only decision matrix query/rerank (delegates to ``diseases.admin.matrix``)."""
        return self.admin.matrix(**kwargs)

    def intelligence(
        self, disease_id: str, *, data_version: str | None = None
    ) -> AdminDiseaseIntelligence:
        """Superadmin-only disease intelligence dossier (delegates to ``diseases.admin.intelligence``)."""
        return self.admin.intelligence(disease_id, data_version=data_version)

    def baskets(
        self, organ: str | None = None, *, data_version: str | None = None
    ) -> AdminBasketsResponse | AdminBasketDetailResponse:
        """Superadmin-only organ rNPV baskets (delegates to ``diseases.admin.baskets``)."""
        return self.admin.baskets(organ, data_version=data_version)

    def populations(
        self, disease_id: str | None = None, **kwargs: Any
    ) -> AdminPopulationsResponse | AdminPopulationRecord:
        """Superadmin-only patient populations (delegates to ``diseases.admin.populations``)."""
        return self.admin.populations(disease_id, **kwargs)

    def ladders(
        self, ladder_id: str | None = None, *, data_version: str | None = None
    ) -> AdminLaddersResponse | AdminLadderEntry:
        """Superadmin-only expansion ladders (delegates to ``diseases.admin.ladders``)."""
        return self.admin.ladders(ladder_id, data_version=data_version)

    def geo(
        self, disease_id: str, *, data_version: str | None = None
    ) -> AdminGeoResponse:
        """Superadmin-only disease-tissue GEO cohorts (delegates to ``diseases.admin.geo``)."""
        return self.admin.geo(disease_id, data_version=data_version)

    def benchmarks(self, *, data_version: str | None = None) -> AdminBenchmarksResponse:
        """Superadmin-only US sales benchmarks and designation criteria."""
        return self.admin.benchmarks(data_version=data_version)

    # -- Legacy DiseaseViewer endpoints (preserved for backwards compatibility) -

    def mutations(self, disease_id: int) -> list[Mutation]:
        payload = self._transport.request(
            "GET", f"/api/disease-viewer/mutations/{disease_id}"
        )
        items = unwrap_rows(
            payload,
            _MUTATION_KEYS,
            what=f"GET /api/disease-viewer/mutations/{disease_id}",
        )
        return [Mutation.model_validate(m) for m in items]

    def gene_mutations(self, gene: str) -> list[Mutation]:
        payload = self._transport.request(
            "GET", f"/api/disease-viewer/gene-mutations/{gene}"
        )
        items = unwrap_rows(
            payload,
            _MUTATION_KEYS,
            what=f"GET /api/disease-viewer/gene-mutations/{gene}",
        )
        return [Mutation.model_validate(m) for m in items]

    def categories(self) -> list[str]:
        payload = self._transport.request("GET", "/api/disease-viewer/categories")
        return list(
            unwrap_rows(
                payload,
                ("categories", "results"),
                what="GET /api/disease-viewer/categories",
            )
        )


class AsyncDiseases(AsyncResource):
    """Async counterpart to :class:`Diseases`."""

    def __init__(
        self,
        transport: AsyncHTTPTransport,
        client: AsyncLigandAI | None = None,
    ) -> None:
        super().__init__(transport, client=client)
        self.admin: AsyncAdminDiseases = AsyncAdminDiseases(transport, client=client)

    async def meta(self, *, data_version: str | None = None) -> AtlasMeta:
        payload = await self._transport.request(
            "GET", "/api/atlas/meta", **_header_kwargs(data_version)
        )
        return AtlasMeta.model_validate(
            _require_mapping(payload, what="GET /api/atlas/meta")
        )

    async def search(
        self,
        q: str = "",
        *,
        organ: str | None = None,
        tissue: str | None = None,
        rarity: str | None = None,
        gene: str | None = None,
        limit: int = 25,
        cursor: str | None = None,
        data_version: str | None = None,
    ) -> list[AtlasDiseaseSummary]:
        params: dict[str, Any] = {
            "q": q,
            "organ": organ,
            "tissue": tissue,
            "rarity": rarity,
            "gene": gene,
            "limit": limit,
            "cursor": cursor,
        }
        payload = await self._transport.request(
            "GET",
            "/api/atlas/diseases/search",
            params={k: v for k, v in params.items() if v is not None},
            **_header_kwargs(data_version),
        )
        items = unwrap_rows(
            payload,
            _ATLAS_SEARCH_KEYS,
            what=f"GET /api/atlas/diseases/search for {q!r}",
        )
        return [AtlasDiseaseSummary.model_validate(row) for row in items]

    async def search_page(
        self,
        q: str = "",
        *,
        organ: str | None = None,
        tissue: str | None = None,
        rarity: str | None = None,
        gene: str | None = None,
        limit: int = 25,
        cursor: str | None = None,
        data_version: str | None = None,
    ) -> AtlasSearchResponse:
        params: dict[str, Any] = {
            "q": q,
            "organ": organ,
            "tissue": tissue,
            "rarity": rarity,
            "gene": gene,
            "limit": limit,
            "cursor": cursor,
        }
        payload = await self._transport.request(
            "GET",
            "/api/atlas/diseases/search",
            params={k: v for k, v in params.items() if v is not None},
            **_header_kwargs(data_version),
        )
        mapping = _require_mapping(
            payload, what=f"GET /api/atlas/diseases/search for {q!r}"
        )
        return AtlasSearchResponse.model_validate(mapping)

    async def get(
        self,
        disease_id: str | int,
        *,
        data_version: str | None = None,
    ) -> AtlasDiseaseDetail:
        encoded = _encode_id(disease_id)
        path = f"/api/atlas/diseases/{encoded}"
        payload = await self._transport.request(
            "GET", path, **_header_kwargs(data_version)
        )
        mapping = _require_mapping(payload, what=f"GET {path}")
        inner = (
            mapping["disease"]
            if isinstance(mapping.get("disease"), Mapping)
            else mapping
        )
        return AtlasDiseaseDetail.model_validate(inner)

    async def tissues(
        self,
        disease_id: str | int,
        *,
        role: str | None = None,
        include_cells: bool | None = None,
        data_version: str | None = None,
    ) -> list[AtlasDiseaseTissue]:
        encoded = _encode_id(disease_id)
        path = f"/api/atlas/diseases/{encoded}/tissues"
        params: dict[str, Any] = {
            "role": role,
            "include_cells": include_cells,
        }
        payload = await self._transport.request(
            "GET",
            path,
            params={k: v for k, v in params.items() if v is not None},
            **_header_kwargs(data_version),
        )
        items = unwrap_rows(payload, _ATLAS_TISSUE_KEYS, what=f"GET {path}")
        return [AtlasDiseaseTissue.model_validate(row) for row in items]

    async def tissues_response(
        self,
        disease_id: str | int,
        *,
        role: str | None = None,
        include_cells: bool | None = None,
        data_version: str | None = None,
    ) -> AtlasDiseaseTissuesResponse:
        encoded = _encode_id(disease_id)
        path = f"/api/atlas/diseases/{encoded}/tissues"
        params: dict[str, Any] = {
            "role": role,
            "include_cells": include_cells,
        }
        payload = await self._transport.request(
            "GET",
            path,
            params={k: v for k, v in params.items() if v is not None},
            **_header_kwargs(data_version),
        )
        return AtlasDiseaseTissuesResponse.model_validate(
            _require_mapping(payload, what=f"GET {path}")
        )

    async def targets(
        self,
        disease_id: str | int,
        tissue: str | None = None,
        *,
        cell_type: str | None = None,
        surface_only: bool | None = None,
        top_k: int = 25,
        strict_targets: bool | None = None,
        data_version: str | None = None,
    ) -> list[AtlasRankedTarget]:
        encoded = _encode_id(disease_id)
        path = f"/api/atlas/diseases/{encoded}/targets"
        params: dict[str, Any] = {
            "tissue": tissue,
            "cell_type": cell_type,
            "surface_only": surface_only,
            "top_k": top_k,
            "strict_targets": strict_targets,
        }
        payload = await self._transport.request(
            "GET",
            path,
            params={k: v for k, v in params.items() if v is not None},
            **_header_kwargs(data_version),
        )
        items = unwrap_rows(payload, _ATLAS_TARGET_KEYS, what=f"GET {path}")
        return [AtlasRankedTarget.model_validate(row) for row in items]

    async def targets_response(
        self,
        disease_id: str | int,
        tissue: str | None = None,
        *,
        cell_type: str | None = None,
        surface_only: bool | None = None,
        top_k: int = 25,
        strict_targets: bool | None = None,
        data_version: str | None = None,
    ) -> AtlasDiseaseTargetsResponse:
        encoded = _encode_id(disease_id)
        path = f"/api/atlas/diseases/{encoded}/targets"
        params: dict[str, Any] = {
            "tissue": tissue,
            "cell_type": cell_type,
            "surface_only": surface_only,
            "top_k": top_k,
            "strict_targets": strict_targets,
        }
        payload = await self._transport.request(
            "GET",
            path,
            params={k: v for k, v in params.items() if v is not None},
            **_header_kwargs(data_version),
        )
        return AtlasDiseaseTargetsResponse.model_validate(
            _require_mapping(payload, what=f"GET {path}")
        )

    async def map(
        self,
        disease: str,
        *,
        tissue: str | None = None,
        cell_type: str | None = None,
        surface_only: bool = False,
        top_k: int = 25,
        use_get: bool = False,
        data_version: str | None = None,
    ) -> DiseaseMap:
        cleaned = str(disease).strip()
        if not cleaned:
            raise LigandAIInvalidConfig(
                "Disease query must be a non-empty string.",
                field="disease",
                value=disease,
                allowed="non-empty disease query",
            )
        extra = _header_kwargs(data_version)
        if use_get:
            encoded = _encode_id(cleaned)
            path = f"/api/atlas/diseases/{encoded}/map"
            params: dict[str, Any] = {
                "tissue": tissue,
                "cell_type": cell_type,
                "surface_only": surface_only if surface_only else None,
                "top_k": top_k,
            }
            payload = await self._transport.request(
                "GET",
                path,
                params={k: v for k, v in params.items() if v is not None},
                **extra,
            )
            return DiseaseMap.model_validate(
                _require_mapping(payload, what=f"GET {path}")
            )
        body: dict[str, Any] = {
            "disease": cleaned,
            "tissue": tissue,
            "cellType": cell_type,
            "surfaceOnly": surface_only,
            "topK": top_k,
        }
        payload = await self._transport.request(
            "POST",
            "/api/atlas/map",
            json=body,
            **extra,
        )
        return DiseaseMap.model_validate(
            _require_mapping(payload, what="POST /api/atlas/map")
        )

    # -- Superadmin-gated convenience delegates -------------------------------

    async def matrix(self, **kwargs: Any) -> AdminMatrixResponse:
        return await self.admin.matrix(**kwargs)

    async def intelligence(
        self, disease_id: str, *, data_version: str | None = None
    ) -> AdminDiseaseIntelligence:
        return await self.admin.intelligence(disease_id, data_version=data_version)

    async def baskets(
        self, organ: str | None = None, *, data_version: str | None = None
    ) -> AdminBasketsResponse | AdminBasketDetailResponse:
        return await self.admin.baskets(organ, data_version=data_version)

    async def populations(
        self, disease_id: str | None = None, **kwargs: Any
    ) -> AdminPopulationsResponse | AdminPopulationRecord:
        return await self.admin.populations(disease_id, **kwargs)

    async def ladders(
        self, ladder_id: str | None = None, *, data_version: str | None = None
    ) -> AdminLaddersResponse | AdminLadderEntry:
        return await self.admin.ladders(ladder_id, data_version=data_version)

    async def geo(
        self, disease_id: str, *, data_version: str | None = None
    ) -> AdminGeoResponse:
        return await self.admin.geo(disease_id, data_version=data_version)

    async def benchmarks(
        self, *, data_version: str | None = None
    ) -> AdminBenchmarksResponse:
        return await self.admin.benchmarks(data_version=data_version)

    # -- Legacy DiseaseViewer endpoints (preserved for backwards compatibility) -

    async def mutations(self, disease_id: int) -> list[Mutation]:
        payload = await self._transport.request(
            "GET", f"/api/disease-viewer/mutations/{disease_id}"
        )
        items = unwrap_rows(
            payload,
            _MUTATION_KEYS,
            what=f"GET /api/disease-viewer/mutations/{disease_id}",
        )
        return [Mutation.model_validate(m) for m in items]

    async def gene_mutations(self, gene: str) -> list[Mutation]:
        payload = await self._transport.request(
            "GET", f"/api/disease-viewer/gene-mutations/{gene}"
        )
        items = unwrap_rows(
            payload,
            _MUTATION_KEYS,
            what=f"GET /api/disease-viewer/gene-mutations/{gene}",
        )
        return [Mutation.model_validate(m) for m in items]

    async def categories(self) -> list[str]:
        payload = await self._transport.request("GET", "/api/disease-viewer/categories")
        return list(
            unwrap_rows(
                payload,
                ("categories", "results"),
                what="GET /api/disease-viewer/categories",
            )
        )
