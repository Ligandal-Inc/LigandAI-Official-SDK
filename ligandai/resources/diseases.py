# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Disease Atlas biological resolution and superadmin intelligence endpoints."""

from __future__ import annotations

import csv
import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, overload
from urllib.parse import quote

from ligandai._constants import (
    BASKET_RNPV_PROVISIONAL_LABEL,
    DEFAULT_ATLAS_DATA_VERSION,
    SUPERADMIN_API_KEY_PREFIXES,
    AtlasDataVersion,
)
from ligandai.errors import (
    LigandAIAuthError,
    LigandAIError,
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
    DiseaseTargetGraph,
    GraphCellTypeNode,
    GraphEdgeEvidence,
    GraphTissueNode,
    MarkerValueRanking,
    MatrixWeights,
    Mutation,
    PerOrganTopMarkersEntry,
    RankedMarkerCandidate,
)

if TYPE_CHECKING:
    from ligandai._http import AsyncHTTPTransport, HTTPTransport
    from ligandai.client import AsyncLigandAI, LigandAI

_ATLAS_SEARCH_KEYS = ("items", "diseases", "results", "data")
_ATLAS_TISSUE_KEYS = ("tissues", "items", "results", "data")
_ATLAS_TARGET_KEYS = ("targets", "items", "results", "data")
_MUTATION_KEYS = ("mutations", "results", "data")


_FORBIDDEN_PUBLIC_KEYS: frozenset[str] = frozenset(
    {
        "commercial",
        "competition",
        "designations",
        "pricing",
        "valuationInputs",
        "valuation_inputs",
        "rnpvUsd",
        "rnpv_usd",
        "oneDrugPeakSalesUsd",
        "one_drug_peak_sales_usd",
        "annualDrugPriceUsd",
        "annual_drug_price_usd",
        "annualServiceFeeUsd",
        "annual_service_fee_usd",
        "priceAnchors",
        "price_anchors",
        "rnpvProvisional",
        "rnpv_provisional",
        "rnpvProvisionalLabel",
        "rnpv_provisional_label",
        "rnpvLowUsdM",
        "rnpvBaseUsdM",
        "rnpvHighUsdM",
        "rnpv_low_usd_m",
        "rnpv_base_usd_m",
        "rnpv_high_usd_m",
        "usMarketBenchmarkUsd",
        "us_market_benchmark_usd",
        "observedUsComparator",
        "observed_us_comparator",
        "observedUsComparatorSalesM",
        "observed_us_comparator_sales_m",
        "populations",
        "population",
        "basketContributions",
        "basketCashFlows",
        "salesBenchmarks",
        "designationCriteria",
        "expansionLadder",
        "activeWeights",
        "customScore",
    }
)


def sanitize_public_atlas_payload(value: Any) -> Any:
    """Recursively strip superadmin commercial, pricing, rNPV, and population fields."""
    if isinstance(value, list):
        return [sanitize_public_atlas_payload(item) for item in value]
    if isinstance(value, Mapping):
        return {
            str(k): sanitize_public_atlas_payload(v)
            for k, v in value.items()
            if str(k) not in _FORBIDDEN_PUBLIC_KEYS
        }
    return value


def is_basket_rnpv_provisional(version: str | None) -> bool:
    """Return True when basket / lead-portfolio / organ-stress rNPV is provisional (not ``v4``)."""
    return str(version or DEFAULT_ATLAS_DATA_VERSION).strip().lower() != "v4"


def _apply_basket_provisional(
    response: AdminBasketsResponse | AdminBasketDetailResponse,
    requested_version: str | None,
) -> AdminBasketsResponse | AdminBasketDetailResponse:
    effective = str(response.data_version or requested_version or DEFAULT_ATLAS_DATA_VERSION).strip().lower()
    provisional = is_basket_rnpv_provisional(effective)
    label = BASKET_RNPV_PROVISIONAL_LABEL if provisional else None
    response.rnpv_provisional = provisional
    response.rnpv_provisional_label = label
    if isinstance(response, AdminBasketsResponse):
        for b in response.baskets:
            b.rnpv_provisional = provisional
            b.rnpv_provisional_label = label
        if response.totals is not None:
            response.totals.rnpv_provisional = provisional
            response.totals.rnpv_provisional_label = label
    else:
        response.basket.rnpv_provisional = provisional
        response.basket.rnpv_provisional_label = label
    return response


def _gate_entry_ok(entry: Any) -> bool:
    if entry is True:
        return True
    if isinstance(entry, str):
        return entry.lower() in {"pass", "passed"}
    if isinstance(entry, Mapping):
        if entry.get("passed") is True or entry.get("ok") is True:
            return True
        return str(entry.get("status") or "").lower() in {"pass", "passed"}
    return False


def _gates_file_passed(
    gates_path: str | Path,
    *,
    spec_path: str | Path | None = None,
    require_addendum8_spec: bool = False,
) -> bool:
    """Return True only when ``gates.json`` exists, is fresh against inputs/spec, and passes all gates."""
    p = Path(gates_path)
    try:
        gates_mtime = p.stat().st_mtime_ns
        for add7_file in p.parent.glob("add7-*.json"):
            if add7_file.name == "add7-5-release.json":
                continue
            if add7_file.stat().st_mtime_ns > gates_mtime:
                return False
        if p.parent.name == "validation":
            version_dir = p.parent.parent
            for csv_file in version_dir.glob("*.csv"):
                if csv_file.stat().st_mtime_ns > gates_mtime:
                    return False
            effective_spec = (
                Path(spec_path)
                if spec_path is not None
                else (
                    version_dir.parent / "TASK_ADDENDUM8_LABEL_EXPANSION.md"
                    if require_addendum8_spec or version_dir.name == "v4"
                    else None
                )
            )
            if effective_spec is not None and effective_spec.is_file():
                if effective_spec.stat().st_mtime_ns > gates_mtime:
                    return False
        elif spec_path is not None:
            sp = Path(spec_path)
            if sp.is_file() and sp.stat().st_mtime_ns > gates_mtime:
                return False
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(raw, Mapping):
        return False
    overall = str(raw.get("overall") or "").strip().lower()
    top_status = str(raw.get("status") or "").strip().lower()
    if (overall and overall not in {"pass", "passed"}) or (
        top_status and top_status not in {"pass", "passed"}
    ):
        return False
    if (
        raw.get("passed") is False
        or raw.get("all_passed") is False
        or raw.get("allPassed") is False
    ):
        return False

    v4_counts = raw.get("v4_counts")
    if isinstance(v4_counts, Mapping):
        for k, count in v4_counts.items():
            if str(k).lower() in {"pass", "passed"}:
                continue
            if isinstance(count, (int, float)) and count > 0:
                return False

    v3_gates = raw.get("v3_gates")
    if isinstance(v3_gates, Mapping):
        if not v3_gates or not all(_gate_entry_ok(v) for v in v3_gates.values()):
            return False

    gates = raw.get("gates") if "gates" in raw else raw.get("checks")
    if isinstance(gates, Mapping) and gates:
        if not all(_gate_entry_ok(v) for v in gates.values()):
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
        return all(_gate_entry_ok(item) for item in gates)
    if (
        raw.get("passed") is True
        or raw.get("all_passed") is True
        or raw.get("allPassed") is True
    ):
        return True
    if overall in {"pass", "passed"} or top_status in {"pass", "passed"}:
        return True
    return False


def resolve_atlas_data_version(
    requested: str | None = None,
    *,
    gates_path: str | Path | None = None,
    spec_path: str | Path | None = None,
) -> AtlasDataVersion:
    """Resolve the active Disease Atlas dataset version (``"v2"``, ``"v3"``, or ``"v4"``).

    Defaults to :data:`DEFAULT_ATLAS_DATA_VERSION` (``"v3"``), overridable via
    ``requested`` or ``ATLAS_DATA_VERSION`` / ``LIGANDAI_ATLAS_DATA_VERSION``.
    When ``"v4"`` is requested alongside ``gates_path`` (or ``ATLAS_V4_GATES_PATH`` /
    ``ATLAS_GATES_PATH``), ``v4/validation/gates.json`` must report all gates passing
    and postdate ``TASK_ADDENDUM8_LABEL_EXPANSION.md``; otherwise the resolver fails
    closed to ``"v3"`` (or ``"v2"`` if sibling ``v3`` gates also fail).
    """
    raw = (
        requested
        or os.environ.get("ATLAS_DATA_VERSION")
        or os.environ.get("LIGANDAI_ATLAS_DATA_VERSION")
        or DEFAULT_ATLAS_DATA_VERSION
    )
    cleaned = str(raw).strip().lower()
    if cleaned not in ("v2", "v3", "v4"):
        raise LigandAIInvalidConfig(
            f"Unsupported Disease Atlas data version {raw!r}; expected 'v2', 'v3', or 'v4'.",
            field="data_version",
            value=raw,
            allowed=("v2", "v3", "v4"),
        )
    if cleaned == "v4":
        effective_v4_gates = (
            gates_path
            or os.environ.get("ATLAS_V4_GATES_PATH")
            or os.environ.get("ATLAS_GATES_PATH")
        )
        if effective_v4_gates is not None:
            if _gates_file_passed(
                effective_v4_gates,
                spec_path=spec_path,
                require_addendum8_spec=True,
            ):
                return "v4"
            p = Path(effective_v4_gates)
            sibling_v3 = (
                p.parent.parent.parent / "v3" / "validation" / "gates.json"
                if p.parent.name == "validation"
                else None
            )
            if sibling_v3 is not None and sibling_v3.is_file():
                return "v3" if _gates_file_passed(sibling_v3) else "v2"
            return "v3"
        return "v4"
    effective_gates = gates_path or os.environ.get("ATLAS_GATES_PATH")
    if cleaned == "v3" and effective_gates is not None:
        if not _gates_file_passed(effective_gates, spec_path=spec_path):
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
            parsed_detail = AdminBasketDetailResponse.model_validate(
                _require_mapping(payload, what=f"GET {path}")
            )
            return _apply_basket_provisional(parsed_detail, data_version)  # type: ignore[return-value]
        path = "/api/admin/atlas/baskets"
        payload = self._transport.request("GET", path, **extra)
        parsed_list = AdminBasketsResponse.model_validate(
            _require_mapping(payload, what=f"GET {path}")
        )
        return _apply_basket_provisional(parsed_list, data_version)  # type: ignore[return-value]

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
            parsed_detail = AdminBasketDetailResponse.model_validate(
                _require_mapping(payload, what=f"GET {path}")
            )
            return _apply_basket_provisional(parsed_detail, data_version)  # type: ignore[return-value]
        path = "/api/admin/atlas/baskets"
        payload = await self._transport.request("GET", path, **extra)
        parsed_list = AdminBasketsResponse.model_validate(
            _require_mapping(payload, what=f"GET {path}")
        )
        return _apply_basket_provisional(parsed_list, data_version)  # type: ignore[return-value]

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


_MARKER_PLANNING_NOTE = (
    "Internal planning assumption (with sensitivity): we model cross-disease platform "
    "reusability across roughly 50 to 100 indications per validated targeting capability "
    "on our platform (over 6.5 million peptides screened; +/-40% composite-weight "
    "sensitivity across selectivity, indication count, patient population, and v4 "
    "price-ladder anchors). Sourced GTEx, HPA, GEO, and v4 ledger inputs remain unadjusted."
)


def _float_or_zero(value: Any) -> float:
    try:
        if value is None or value == "":
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _int_or_zero(value: Any) -> int:
    try:
        if value is None or value == "":
            return 0
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _canonicalize_markers(
    markers: list[RankedMarkerCandidate],
    *,
    top_k: int,
    platform_only: bool = False,
) -> list[RankedMarkerCandidate]:
    filtered = [
        m
        for m in markers
        if not platform_only or m.is_platform_marker or m.n_diseases >= 2
    ]
    filtered.sort(key=lambda m: (-round(float(m.score), 9), m.marker))
    return filtered[:top_k]


def _compute_marker_digest(
    markers: list[RankedMarkerCandidate], seed: int
) -> str:
    canonical = json.dumps(
        {
            "seed": int(seed),
            "markers": [
                [m.marker, round(float(m.score), 6), int(m.n_diseases)]
                for m in markers
            ],
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_top_k(top_k: int) -> int:
    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k < 1 or top_k > 200:
        raise LigandAIInvalidConfig(
            f"top_k must be an integer between 1 and 200 (got {top_k!r}).",
            field="top_k",
            value=top_k,
            allowed="1..200",
        )
    return top_k


def _is_route_missing_error(exc: BaseException) -> bool:
    if isinstance(exc, LigandAIUnreadableResponse):
        return True
    if isinstance(exc, LigandAIError):
        if exc.code in {"unknown_disease", "ambiguous_disease", "validation_error"}:
            return False
        return exc.status_code in (404, 405, 501)
    return False


def _project_map_to_target_graph(
    mapped: DiseaseMap,
    *,
    query: str,
    top_k: int,
    seed: int,
) -> DiseaseTargetGraph:
    tissues: list[GraphTissueNode] = [
        GraphTissueNode(
            fine_basket_id=t.tissue_key or t.label,
            label=t.label,
            level="tissue",
            derived_coarse_system=t.organ,
            role=t.role,
            evidence=t.evidence[0].summary if t.evidence else t.role_description,
            citation=t.evidence[0].source if t.evidence else "GTEx v8 / HPA v24",
            geo_healthy_additions=[
                {
                    "gse": h.accession,
                    "organism": "Homo sapiens",
                    "evidence": h.title,
                    "url": h.url,
                }
                for h in t.healthy_reference_datasets
            ],
            geo_disease_datasets=[],
        )
        for t in mapped.tissues
    ]
    parent_fine_id = tissues[0].fine_basket_id if tissues else "tissue:primary"
    cell_types: list[GraphCellTypeNode] = [
        GraphCellTypeNode(
            cell_key=c.cell_key,
            label=c.label,
            cl_id=c.cl_id,
            hpa_cell_type=c.hpa_cell_type,
            tissue_fine_basket_id=parent_fine_id,
            evidence=c.role_in_disease or f"HPA single-cell expression ({c.label})",
            citation=f"Human Protein Atlas v24 ({c.cl_id or 'CL:0000000'})",
        )
        for c in mapped.cells
    ]
    seen_genes: set[str] = set()
    candidates: list[RankedMarkerCandidate] = []
    for tgt in [*mapped.receptors, *mapped.targets]:
        if not tgt.gene or tgt.gene in seen_genes:
            continue
        seen_genes.add(tgt.gene)
        ev_summary = (
            tgt.evidence[0].summary
            if tgt.evidence
            else tgt.recommended_approach.summary or tgt.role_label
        )
        citations = [ev.source for ev in tgt.evidence if ev.source]
        selectivity = (
            float(tgt.score_breakdown.tissue_expression_specificity)
            if tgt.score_breakdown.tissue_expression_specificity > 0
            else (0.85 if tgt.is_receptor else 0.7)
        )
        candidates.append(
            RankedMarkerCandidate(
                marker=tgt.gene,
                protein_name=tgt.protein_name,
                uniprot_id=tgt.uniprot_id,
                is_receptor=tgt.is_receptor,
                is_surface_exposed=tgt.is_surface_exposed,
                target_role=tgt.target_role,
                is_platform_marker=tgt.is_receptor,
                selectivity=round(selectivity, 6),
                n_diseases=1,
                patient_pop=0.0,
                price_per_drug=0.0,
                score=round(float(tgt.overall_score), 6),
                fine_tissues=[t.fine_basket_id for t in tissues[:5]],
                derived_coarse_systems_rollup=(
                    [mapped.disease.therapeutic_primary_organ]
                    if mapped.disease.therapeutic_primary_organ
                    else []
                ),
                addressed_indication_ids=(
                    [mapped.disease.identity_id] if mapped.disease.identity_id else []
                ),
                evidence=ev_summary,
                evidence_items=[ev.summary for ev in tgt.evidence if ev.summary],
                citations=citations,
            )
        )
    ordered = _canonicalize_markers(candidates, top_k=top_k)
    digest = _compute_marker_digest(ordered, seed)
    return DiseaseTargetGraph(
        data_version=mapped.data_version or "v3",
        surface="ligandai.com Studio dropdown",
        query=query,
        disease={
            "identityId": mapped.disease.identity_id,
            "name": mapped.disease.name,
            "derivedCoarseSystem": (
                mapped.disease.therapeutic_primary_organ or mapped.disease.primary_organ
            ),
        },
        tissues=tissues,
        cell_types=cell_types,
        markers=ordered,
        edges=[],
        ranking_stage={
            "executed": True,
            "stage": "final_pipeline_stage",
            "algorithm": "ligandai_marker_value_v4",
            "seed": seed,
            "deterministic_digest": digest,
        },
        planning_assumption_note=_MARKER_PLANNING_NOTE,
        took_ms=mapped.took_ms,
    )


def _finalize_target_graph(
    raw_payload: Mapping[str, Any],
    *,
    query: str,
    top_k: int,
    seed: int,
) -> DiseaseTargetGraph:
    cleaned = sanitize_public_atlas_payload(raw_payload)
    graph = DiseaseTargetGraph.model_validate(cleaned)
    if not graph.query:
        graph.query = query
    graph.markers = _canonicalize_markers(graph.markers, top_k=top_k)
    digest = _compute_marker_digest(graph.markers, seed)
    stage = dict(graph.ranking_stage) if graph.ranking_stage else {}
    stage.setdefault("executed", True)
    stage.setdefault("stage", "final_pipeline_stage")
    stage.setdefault("algorithm", "ligandai_marker_value_v4")
    stage["seed"] = int(stage.get("seed", seed))
    stage.setdefault("deterministic_digest", digest)
    graph.ranking_stage = stage
    if not graph.planning_assumption_note:
        graph.planning_assumption_note = _MARKER_PLANNING_NOTE
    return graph


def _finalize_marker_ranking(
    raw_payload: Mapping[str, Any],
    *,
    organ: str | None,
    tissue: str | None,
    platform_only: bool,
    top_k: int,
    seed: int,
) -> MarkerValueRanking:
    cleaned = sanitize_public_atlas_payload(raw_payload)
    ranking = MarkerValueRanking.model_validate(cleaned)
    ranking.markers = _canonicalize_markers(
        ranking.markers, top_k=top_k, platform_only=platform_only
    )
    if ranking.platform_markers:
        ranking.platform_markers = _canonicalize_markers(
            ranking.platform_markers, top_k=top_k, platform_only=True
        )
    else:
        ranking.platform_markers = [
            m for m in ranking.markers if m.is_platform_marker or m.n_diseases >= 2
        ]
    digest = _compute_marker_digest(ranking.markers, seed)
    if not ranking.filter:
        ranking.filter = {
            "organ": organ,
            "tissue": tissue,
            "platform_only": platform_only,
            "top_k": top_k,
        }
    repro = dict(ranking.reproducibility) if ranking.reproducibility else {}
    repro["seed"] = int(repro.get("seed", seed))
    repro.setdefault("deterministic_digest", digest)
    ranking.reproducibility = repro
    if not ranking.planning_assumption_note:
        ranking.planning_assumption_note = _MARKER_PLANNING_NOTE
    return ranking


def _load_target_graph_from_csv_dir(
    graph_dir: Path,
    *,
    disease: str,
    tissue: str | None,
    cell_type: str | None,
    top_k: int,
    seed: int,
) -> DiseaseTargetGraph:
    csv_path = graph_dir / "disease_marker_graph.csv"
    needle = disease.strip().lower()
    tissue_needle = tissue.strip().lower() if tissue else ""
    cell_needle = cell_type.strip().lower() if cell_type else ""
    tissues_map: dict[str, GraphTissueNode] = {}
    cells_map: dict[str, GraphCellTypeNode] = {}
    markers_map: dict[str, RankedMarkerCandidate] = {}
    edges: list[GraphEdgeEvidence] = []
    matched_id = ""
    matched_name = ""
    matched_system = ""

    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            ident = (row.get("identity_id") or "").strip()
            dname = (row.get("disease") or row.get("disease_name") or "").strip()
            if needle not in ident.lower() and needle not in dname.lower():
                continue
            t_id = (
                row.get("fine_basket_id")
                or row.get("tissue_id")
                or row.get("tissue")
                or ""
            ).strip()
            t_label = (row.get("tissue_label") or row.get("tissue") or t_id).strip()
            coarse = (
                row.get("derived_coarse_system")
                or row.get("coarse_organ")
                or row.get("organ")
                or ""
            ).strip()
            if tissue_needle and (
                tissue_needle not in t_id.lower()
                and tissue_needle not in t_label.lower()
                and tissue_needle not in coarse.lower()
            ):
                continue
            c_key = (row.get("cell_key") or row.get("cell_type") or "").strip()
            c_label = (row.get("cell_label") or c_key).strip()
            if cell_needle and c_key and (
                cell_needle not in c_key.lower() and cell_needle not in c_label.lower()
            ):
                continue
            if not matched_id:
                matched_id = ident or disease
                matched_name = dname or disease
                matched_system = coarse
            ev = (row.get("evidence") or "").strip()
            cit = (row.get("citation") or "").strip()
            if t_id and t_id not in tissues_map:
                tissues_map[t_id] = GraphTissueNode(
                    fine_basket_id=t_id,
                    label=t_label or t_id,
                    level=(row.get("level") or "tissue").strip(),
                    derived_coarse_system=coarse,
                    role=(row.get("role") or "primary").strip(),
                    evidence=ev,
                    citation=cit,
                )
            if c_key and c_key not in cells_map:
                cells_map[c_key] = GraphCellTypeNode(
                    cell_key=c_key,
                    label=c_label or c_key,
                    cl_id=(row.get("cl_id") or "").strip() or None,
                    hpa_cell_type=(row.get("hpa_cell_type") or "").strip() or None,
                    tissue_fine_basket_id=t_id,
                    evidence=ev,
                    citation=cit,
                )
            m_name = (row.get("marker") or row.get("gene") or "").strip()
            if m_name and m_name not in markers_map:
                n_dis = _int_or_zero(row.get("n_diseases")) or 1
                markers_map[m_name] = RankedMarkerCandidate(
                    marker=m_name,
                    protein_name=(row.get("protein_name") or "").strip(),
                    uniprot_id=(row.get("uniprot_id") or "").strip() or None,
                    is_receptor=str(row.get("is_receptor") or "true").lower()
                    in {"1", "true", "yes"},
                    is_surface_exposed=str(
                        row.get("is_surface_exposed") or "true"
                    ).lower()
                    in {"1", "true", "yes"},
                    target_role=(row.get("target_role") or "delivery_receptor").strip(),
                    is_platform_marker=n_dis >= 2,
                    selectivity=_float_or_zero(row.get("selectivity")),
                    n_diseases=n_dis,
                    patient_pop=_float_or_zero(row.get("patient_pop")),
                    price_per_drug=_float_or_zero(row.get("price_per_drug")),
                    score=_float_or_zero(row.get("score")),
                    fine_tissues=[t_id] if t_id else [],
                    derived_coarse_systems_rollup=[coarse] if coarse else [],
                    addressed_indication_ids=[matched_id] if matched_id else [],
                    evidence=ev,
                    evidence_items=[ev] if ev else [],
                    citations=[cit] if cit else [],
                )
                edges.append(
                    GraphEdgeEvidence(
                        edge_type="cell_to_marker",
                        from_node=c_key or t_id or matched_id,
                        to_node=m_name,
                        evidence=ev,
                        citation=cit,
                    )
                )

    if not matched_id and not markers_map:
        raise LigandAIError(
            f"No marker graph rows matched {disease!r}.",
            code="unknown_disease",
            status_code=404,
        )

    ordered = _canonicalize_markers(list(markers_map.values()), top_k=top_k)
    digest = _compute_marker_digest(ordered, seed)
    return DiseaseTargetGraph(
        data_version="v4",
        surface="ligandai.com Studio dropdown",
        query=disease,
        disease={
            "identityId": matched_id,
            "name": matched_name,
            "derivedCoarseSystem": matched_system,
        },
        tissues=list(tissues_map.values()),
        cell_types=list(cells_map.values()),
        markers=ordered,
        edges=edges,
        ranking_stage={
            "executed": True,
            "stage": "final_pipeline_stage",
            "algorithm": "ligandai_marker_value_v4",
            "seed": seed,
            "deterministic_digest": digest,
        },
        planning_assumption_note=_MARKER_PLANNING_NOTE,
        took_ms=0.0,
    )


def _parse_csv_marker_row(row: Mapping[str, str]) -> RankedMarkerCandidate:
    fine_raw = (row.get("fine_tissues") or row.get("fine_basket_id") or "").strip()
    fine_list = [
        tok.strip()
        for tok in fine_raw.replace("|", ";").split(";")
        if tok.strip()
    ]
    coarse_raw = (
        row.get("derived_coarse_systems_rollup")
        or row.get("derived_coarse_system")
        or row.get("organ")
        or ""
    ).strip()
    coarse_list = [
        tok.strip()
        for tok in coarse_raw.replace("|", ";").split(";")
        if tok.strip()
    ]
    ev = (row.get("evidence") or "").strip()
    cit = (row.get("citation") or row.get("citations") or "").strip()
    n_dis = _int_or_zero(row.get("n_diseases"))
    is_plat_raw = str(row.get("is_platform_marker") or "").strip().lower()
    is_plat = is_plat_raw in {"1", "true", "yes"} if is_plat_raw else (n_dis >= 2)
    return RankedMarkerCandidate(
        marker=(row.get("marker") or row.get("gene") or "").strip(),
        protein_name=(row.get("protein_name") or "").strip(),
        uniprot_id=(row.get("uniprot_id") or "").strip() or None,
        is_receptor=str(row.get("is_receptor") or "true").lower()
        in {"1", "true", "yes"},
        is_surface_exposed=str(row.get("is_surface_exposed") or "true").lower()
        in {"1", "true", "yes"},
        target_role=(row.get("target_role") or "delivery_receptor").strip(),
        is_platform_marker=is_plat,
        selectivity=_float_or_zero(row.get("selectivity")),
        n_diseases=n_dis,
        patient_pop=_float_or_zero(row.get("patient_pop")),
        price_per_drug=_float_or_zero(row.get("price_per_drug")),
        score=_float_or_zero(row.get("score")),
        fine_tissues=fine_list,
        derived_coarse_systems_rollup=coarse_list,
        evidence=ev,
        evidence_items=[ev] if ev else [],
        citations=[cit] if cit else [],
    )


def _load_marker_ranking_from_csv_dir(
    graph_dir: Path,
    *,
    organ: str | None,
    tissue: str | None,
    platform_only: bool,
    top_k: int,
    seed: int,
) -> MarkerValueRanking:
    ranking_csv = graph_dir / "marker_value_ranking.csv"
    filter_term = (tissue or organ or "").strip().lower()
    all_markers: list[RankedMarkerCandidate] = []
    with ranking_csv.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            entry = _parse_csv_marker_row(row)
            if not entry.marker:
                continue
            if filter_term:
                haystack = " ".join(
                    [*entry.fine_tissues, *entry.derived_coarse_systems_rollup]
                ).lower()
                if filter_term not in haystack:
                    continue
            all_markers.append(entry)

    ordered = _canonicalize_markers(
        all_markers, top_k=top_k, platform_only=platform_only
    )

    platform_csv = graph_dir / "platform_markers.csv"
    if platform_csv.is_file():
        plat_raw: list[RankedMarkerCandidate] = []
        with platform_csv.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                entry = _parse_csv_marker_row(row)
                if entry.marker:
                    plat_raw.append(entry)
        platform_markers = _canonicalize_markers(
            plat_raw, top_k=top_k, platform_only=True
        )
    else:
        platform_markers = _canonicalize_markers(
            all_markers, top_k=top_k, platform_only=True
        )

    per_organ_csv = graph_dir / "per_organ_top_markers.csv"
    per_organ_entries: list[PerOrganTopMarkersEntry] = []
    if per_organ_csv.is_file():
        grouped: dict[str, PerOrganTopMarkersEntry] = {}
        with per_organ_csv.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                fid = (
                    row.get("fine_basket_id") or row.get("organ") or "tissue:all"
                ).strip()
                label = (row.get("fine_tissue_label") or row.get("label") or fid).strip()
                rollup = (
                    row.get("derived_coarse_system_rollup")
                    or row.get("derived_coarse_system")
                    or ""
                ).strip()
                if filter_term and (
                    filter_term not in fid.lower()
                    and filter_term not in label.lower()
                    and filter_term not in rollup.lower()
                ):
                    continue
                bucket = grouped.get(fid)
                if bucket is None:
                    bucket = PerOrganTopMarkersEntry(
                        fine_basket_id=fid,
                        fine_tissue_label=label,
                        level=(row.get("level") or "tissue").strip(),
                        derived_coarse_system_rollup=rollup,
                        top_markers=[],
                    )
                    grouped[fid] = bucket
                bucket.top_markers.append(
                    {
                        "marker": (row.get("marker") or "").strip(),
                        "score": _float_or_zero(row.get("score")),
                        "selectivity": _float_or_zero(row.get("selectivity")),
                        "n_diseases": _int_or_zero(row.get("n_diseases")),
                        "patient_pop": _float_or_zero(row.get("patient_pop")),
                        "price_per_drug": _float_or_zero(row.get("price_per_drug")),
                    }
                )
        per_organ_entries = list(grouped.values())

    digest = _compute_marker_digest(ordered, seed)
    return MarkerValueRanking(
        data_version="v4",
        surface="ligandai.com Studio dropdown",
        filter={
            "organ": organ,
            "tissue": tissue,
            "platform_only": platform_only,
            "top_k": top_k,
        },
        weights={
            "selectivity": 0.35,
            "disease_breadth": 0.25,
            "patient_pop": 0.2,
            "price_per_drug": 0.2,
        },
        markers=ordered,
        per_organ_top=per_organ_entries,
        platform_markers=platform_markers,
        dashboard_clusters=[],
        reproducibility={"seed": seed, "deterministic_digest": digest},
        planning_assumption_note=_MARKER_PLANNING_NOTE,
        took_ms=0.0,
    )


class Markers(Resource):
    """Cross-disease and fine-tissue marker-value ranking surface (Addendum 8 §J, Gate 30)."""

    def ranking(
        self,
        organ: str | None = None,
        platform_only: bool = False,
        *,
        tissue: str | None = None,
        top_k: int = 25,
        seed: int = 42,
        data_version: str | None = None,
        marker_graph_dir: str | Path | None = None,
    ) -> MarkerValueRanking:
        """Return scored, explainable marker rankings with deterministic seed reproducibility."""
        checked_k = _validate_top_k(top_k)
        local_dir = marker_graph_dir or os.environ.get("LIGANDAI_MARKER_GRAPH_DIR")
        if local_dir is not None:
            gdir = Path(local_dir)
            if (gdir / "marker_value_ranking.csv").is_file():
                return _load_marker_ranking_from_csv_dir(
                    gdir,
                    organ=organ,
                    tissue=tissue,
                    platform_only=platform_only,
                    top_k=checked_k,
                    seed=seed,
                )
        params: dict[str, Any] = {
            "organ": organ,
            "tissue": tissue,
            "platform_only": "true" if platform_only else None,
            "top_k": checked_k,
            "seed": seed,
        }
        payload = self._transport.request(
            "GET",
            "/api/atlas/markers/ranking",
            params={k: v for k, v in params.items() if v is not None},
            **_header_kwargs(data_version),
        )
        return _finalize_marker_ranking(
            _require_mapping(payload, what="GET /api/atlas/markers/ranking"),
            organ=organ,
            tissue=tissue,
            platform_only=platform_only,
            top_k=checked_k,
            seed=seed,
        )


class AsyncMarkers(AsyncResource):
    """Async counterpart to :class:`Markers`."""

    async def ranking(
        self,
        organ: str | None = None,
        platform_only: bool = False,
        *,
        tissue: str | None = None,
        top_k: int = 25,
        seed: int = 42,
        data_version: str | None = None,
        marker_graph_dir: str | Path | None = None,
    ) -> MarkerValueRanking:
        checked_k = _validate_top_k(top_k)
        local_dir = marker_graph_dir or os.environ.get("LIGANDAI_MARKER_GRAPH_DIR")
        if local_dir is not None:
            gdir = Path(local_dir)
            if (gdir / "marker_value_ranking.csv").is_file():
                return _load_marker_ranking_from_csv_dir(
                    gdir,
                    organ=organ,
                    tissue=tissue,
                    platform_only=platform_only,
                    top_k=checked_k,
                    seed=seed,
                )
        params: dict[str, Any] = {
            "organ": organ,
            "tissue": tissue,
            "platform_only": "true" if platform_only else None,
            "top_k": checked_k,
            "seed": seed,
        }
        payload = await self._transport.request(
            "GET",
            "/api/atlas/markers/ranking",
            params={k: v for k, v in params.items() if v is not None},
            **_header_kwargs(data_version),
        )
        return _finalize_marker_ranking(
            _require_mapping(payload, what="GET /api/atlas/markers/ranking"),
            organ=organ,
            tissue=tissue,
            platform_only=platform_only,
            top_k=checked_k,
            seed=seed,
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
        self.markers: Markers = Markers(transport, client=client)

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

    def map_disease_to_targets(
        self,
        disease: str,
        tissue: str | None = None,
        *,
        cell_type: str | None = None,
        top_k: int = 25,
        seed: int = 42,
        data_version: str | None = None,
        marker_graph_dir: str | Path | None = None,
    ) -> DiseaseTargetGraph:
        """Resolve a disease into ``{tissues, cell_types, markers}`` (Addendum 8 §J, Gate 29)."""
        cleaned = str(disease).strip()
        if not cleaned:
            raise LigandAIInvalidConfig(
                "Disease query must be a non-empty string.",
                field="disease",
                value=disease,
                allowed="non-empty disease query",
            )
        checked_k = _validate_top_k(top_k)
        local_dir = marker_graph_dir or os.environ.get("LIGANDAI_MARKER_GRAPH_DIR")
        if local_dir is not None:
            gdir = Path(local_dir)
            if (gdir / "disease_marker_graph.csv").is_file():
                return _load_target_graph_from_csv_dir(
                    gdir,
                    disease=cleaned,
                    tissue=tissue,
                    cell_type=cell_type,
                    top_k=checked_k,
                    seed=seed,
                )

        extra = _header_kwargs(data_version)
        encoded = _encode_id(cleaned)
        get_path = f"/api/atlas/diseases/{encoded}/targets-graph"
        params: dict[str, Any] = {
            "tissue": tissue,
            "cell_type": cell_type,
            "top_k": checked_k,
            "seed": seed,
        }
        try:
            payload = self._transport.request(
                "GET",
                get_path,
                params={k: v for k, v in params.items() if v is not None},
                **extra,
            )
            return _finalize_target_graph(
                _require_mapping(payload, what=f"GET {get_path}"),
                query=cleaned,
                top_k=checked_k,
                seed=seed,
            )
        except Exception as primary_err:
            if not _is_route_missing_error(primary_err):
                raise
            try:
                post_body: dict[str, Any] = {
                    "disease": cleaned,
                    "tissue": tissue,
                    "cellType": cell_type,
                    "topK": checked_k,
                    "seed": seed,
                }
                payload = self._transport.request(
                    "POST",
                    "/api/atlas/map-targets",
                    json={k: v for k, v in post_body.items() if v is not None},
                    **extra,
                )
                return _finalize_target_graph(
                    _require_mapping(payload, what="POST /api/atlas/map-targets"),
                    query=cleaned,
                    top_k=checked_k,
                    seed=seed,
                )
            except Exception as post_err:
                if not _is_route_missing_error(post_err):
                    raise
                try:
                    mapped = self.map(
                        cleaned,
                        tissue=tissue,
                        cell_type=cell_type,
                        top_k=checked_k,
                        data_version=data_version,
                    )
                    return _project_map_to_target_graph(
                        mapped, query=cleaned, top_k=checked_k, seed=seed
                    )
                except Exception:
                    raise primary_err from None

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
        self.markers: AsyncMarkers = AsyncMarkers(transport, client=client)

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

    async def map_disease_to_targets(
        self,
        disease: str,
        tissue: str | None = None,
        *,
        cell_type: str | None = None,
        top_k: int = 25,
        seed: int = 42,
        data_version: str | None = None,
        marker_graph_dir: str | Path | None = None,
    ) -> DiseaseTargetGraph:
        """Async counterpart to :meth:`Diseases.map_disease_to_targets`."""
        cleaned = str(disease).strip()
        if not cleaned:
            raise LigandAIInvalidConfig(
                "Disease query must be a non-empty string.",
                field="disease",
                value=disease,
                allowed="non-empty disease query",
            )
        checked_k = _validate_top_k(top_k)
        local_dir = marker_graph_dir or os.environ.get("LIGANDAI_MARKER_GRAPH_DIR")
        if local_dir is not None:
            gdir = Path(local_dir)
            if (gdir / "disease_marker_graph.csv").is_file():
                return _load_target_graph_from_csv_dir(
                    gdir,
                    disease=cleaned,
                    tissue=tissue,
                    cell_type=cell_type,
                    top_k=checked_k,
                    seed=seed,
                )

        extra = _header_kwargs(data_version)
        encoded = _encode_id(cleaned)
        get_path = f"/api/atlas/diseases/{encoded}/targets-graph"
        params: dict[str, Any] = {
            "tissue": tissue,
            "cell_type": cell_type,
            "top_k": checked_k,
            "seed": seed,
        }
        try:
            payload = await self._transport.request(
                "GET",
                get_path,
                params={k: v for k, v in params.items() if v is not None},
                **extra,
            )
            return _finalize_target_graph(
                _require_mapping(payload, what=f"GET {get_path}"),
                query=cleaned,
                top_k=checked_k,
                seed=seed,
            )
        except Exception as primary_err:
            if not _is_route_missing_error(primary_err):
                raise
            try:
                post_body: dict[str, Any] = {
                    "disease": cleaned,
                    "tissue": tissue,
                    "cellType": cell_type,
                    "topK": checked_k,
                    "seed": seed,
                }
                payload = await self._transport.request(
                    "POST",
                    "/api/atlas/map-targets",
                    json={k: v for k, v in post_body.items() if v is not None},
                    **extra,
                )
                return _finalize_target_graph(
                    _require_mapping(payload, what="POST /api/atlas/map-targets"),
                    query=cleaned,
                    top_k=checked_k,
                    seed=seed,
                )
            except Exception as post_err:
                if not _is_route_missing_error(post_err):
                    raise
                try:
                    mapped = await self.map(
                        cleaned,
                        tissue=tissue,
                        cell_type=cell_type,
                        top_k=checked_k,
                        data_version=data_version,
                    )
                    return _project_map_to_target_graph(
                        mapped, query=cleaned, top_k=checked_k, seed=seed
                    )
                except Exception:
                    raise primary_err from None

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
