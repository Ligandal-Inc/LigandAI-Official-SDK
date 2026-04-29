# Copyright © 2025 Ligandal, Inc. All rights reserved.
"""Synthesis cart, recommendations, Adaptyv direct API."""

from __future__ import annotations

from typing import Literal

from ligandai.resources._base import AsyncResource, Resource
from ligandai.types import (
    AdaptyvExperiment,
    AdaptyvSequence,
    AdaptyvTarget,
    SynthesisCart,
    SynthesisOptions,
    SynthesisOrder,
    SynthesisPeptide,
    SynthesisQuote,
    SynthesisRecommendation,
)


class Synthesis(Resource):
    """``/api/synthesis-checkout/*`` and ``/api/adaptyv/*``."""

    # -- Synthesis checkout --

    def options(
        self,
        include_linkers: bool = True,
        include_modifications: bool = True,
        include_pricing: bool = True,
    ) -> SynthesisOptions:
        params = {
            "includeLinkers": include_linkers,
            "includeModifications": include_modifications,
            "includePricing": include_pricing,
        }
        return SynthesisOptions.model_validate(
            self._transport.request("GET", "/api/synthesis-checkout/options", params=params) or {}
        )

    def estimate(
        self,
        peptides: list[SynthesisPeptide | dict],
        gene: str | None = None,
        session_id: str | None = None,
        include_bli: bool = False,
        include_target_expression: bool = False,
    ) -> SynthesisQuote:
        normalized = [
            p.model_dump(by_alias=True) if isinstance(p, SynthesisPeptide) else p
            for p in peptides
        ]
        body: dict[str, object] = {
            "peptides": normalized,
            "includeBli": include_bli,
            "includeTargetExpression": include_target_expression,
        }
        if gene is not None:
            body["gene"] = gene
        if session_id is not None:
            body["sessionId"] = session_id
        return SynthesisQuote.model_validate(
            self._transport.request("POST", "/api/synthesis-checkout/estimate", json=body)
            or {"totalUsd": 0.0, "lineItems": []}
        )

    def recommend(
        self,
        peptides: list[SynthesisPeptide | dict],
        gene: str | None = None,
        intent: Literal["validation", "therapeutic", "conjugation", "research"] = "validation",
        synthesis_mode: Literal["recombinant", "synthetic"] = "recombinant",
    ) -> SynthesisRecommendation:
        normalized = [
            p.model_dump(by_alias=True) if isinstance(p, SynthesisPeptide) else p
            for p in peptides
        ]
        body: dict[str, object] = {
            "peptides": normalized,
            "intent": intent,
            "synthesisMode": synthesis_mode,
        }
        if gene is not None:
            body["gene"] = gene
        return SynthesisRecommendation.model_validate(
            self._transport.request("POST", "/api/synthesis-checkout/recommend", json=body)
            or {"intent": intent, "synthesisMode": synthesis_mode, "recommendations": []}
        )

    def add_to_cart(
        self,
        session_id: str,
        gene: str,
        peptide_names: list[str] | None = None,
        peptide_sequences: list[str] | None = None,
        default_purity: str = ">95%",
        default_quantity: str = "1mg",
        include_bli: bool = False,
    ) -> SynthesisCart:
        body: dict[str, object] = {
            "sessionId": session_id,
            "gene": gene,
            "defaultPurity": default_purity,
            "defaultQuantity": default_quantity,
            "includeBli": include_bli,
        }
        if peptide_names is not None:
            body["peptideNames"] = peptide_names
        if peptide_sequences is not None:
            body["peptideSequences"] = peptide_sequences
        return SynthesisCart.model_validate(
            self._transport.request("POST", "/api/synthesis-checkout/cart", json=body)
            or {"cartId": ""}
        )

    def get_cart(self) -> SynthesisCart:
        return SynthesisCart.model_validate(
            self._transport.request("GET", "/api/synthesis-checkout/cart") or {"cartId": ""}
        )

    def list_orders(self) -> list[SynthesisOrder]:
        payload = self._transport.request("GET", "/api/synthesis-checkout/orders") or []
        items = payload if isinstance(payload, list) else payload.get("orders", [])
        return [SynthesisOrder.model_validate(o) for o in items]

    def get_order(self, order_id: str) -> SynthesisOrder:
        return SynthesisOrder.model_validate(
            self._transport.request("GET", f"/api/synthesis-checkout/orders/{order_id}")
            or {"id": order_id, "status": "unknown"}
        )

    # -- Adaptyv --

    def adaptyv_list(self, status: str | None = None, limit: int = 20) -> list[AdaptyvExperiment]:
        params: dict[str, object] = {"limit": limit}
        if status is not None:
            params["status"] = status
        payload = self._transport.request("GET", "/api/adaptyv/experiments", params=params) or []
        items = payload if isinstance(payload, list) else payload.get("experiments", [])
        return [AdaptyvExperiment.model_validate(e) for e in items]

    def adaptyv_get(self, experiment_id: str, include_quote: bool = False) -> AdaptyvExperiment:
        if include_quote:
            quote = self._transport.request("GET", f"/api/adaptyv/experiments/{experiment_id}/quote") or {}
            payload = self._transport.request("GET", f"/api/adaptyv/experiments/{experiment_id}") or {}
            payload["quoteUsd"] = quote.get("totalUsd") or quote.get("total")
            return AdaptyvExperiment.model_validate(payload)
        return AdaptyvExperiment.model_validate(
            self._transport.request("GET", f"/api/adaptyv/experiments/{experiment_id}")
            or {"id": experiment_id, "status": "unknown"}
        )

    def adaptyv_patch_sequences(
        self,
        experiment_id: str,
        sequences: list[AdaptyvSequence | dict],
    ) -> AdaptyvExperiment:
        normalized = [
            s.model_dump(by_alias=True) if isinstance(s, AdaptyvSequence) else s
            for s in sequences
        ]
        return AdaptyvExperiment.model_validate(
            self._transport.request(
                "PATCH", f"/api/adaptyv/experiments/{experiment_id}", json={"sequences": normalized}
            )
            or {"id": experiment_id, "status": "unknown"}
        )

    def adaptyv_search_targets(self, query: str, limit: int = 10) -> list[AdaptyvTarget]:
        payload = self._transport.request(
            "GET", "/api/adaptyv/targets/search", params={"query": query, "limit": limit}
        ) or []
        items = payload if isinstance(payload, list) else payload.get("results", [])
        return [AdaptyvTarget.model_validate(t) for t in items]

    def amide_quote(self, peptides: list[SynthesisPeptide | dict]) -> dict:
        """``POST /api/amide/quote`` — Amide Tech (legacy)."""
        normalized = [
            p.model_dump(by_alias=True) if isinstance(p, SynthesisPeptide) else p
            for p in peptides
        ]
        return self._transport.request("POST", "/api/amide/quote", json={"peptides": normalized}) or {}


class AsyncSynthesis(AsyncResource):
    async def options(
        self,
        include_linkers: bool = True,
        include_modifications: bool = True,
        include_pricing: bool = True,
    ) -> SynthesisOptions:
        params = {
            "includeLinkers": include_linkers,
            "includeModifications": include_modifications,
            "includePricing": include_pricing,
        }
        return SynthesisOptions.model_validate(
            await self._transport.request("GET", "/api/synthesis-checkout/options", params=params) or {}
        )

    async def estimate(
        self,
        peptides: list[SynthesisPeptide | dict],
        gene: str | None = None,
        session_id: str | None = None,
        include_bli: bool = False,
        include_target_expression: bool = False,
    ) -> SynthesisQuote:
        normalized = [
            p.model_dump(by_alias=True) if isinstance(p, SynthesisPeptide) else p
            for p in peptides
        ]
        body: dict[str, object] = {
            "peptides": normalized,
            "includeBli": include_bli,
            "includeTargetExpression": include_target_expression,
        }
        if gene is not None:
            body["gene"] = gene
        if session_id is not None:
            body["sessionId"] = session_id
        return SynthesisQuote.model_validate(
            await self._transport.request("POST", "/api/synthesis-checkout/estimate", json=body)
            or {"totalUsd": 0.0, "lineItems": []}
        )

    async def recommend(
        self,
        peptides: list[SynthesisPeptide | dict],
        gene: str | None = None,
        intent: Literal["validation", "therapeutic", "conjugation", "research"] = "validation",
        synthesis_mode: Literal["recombinant", "synthetic"] = "recombinant",
    ) -> SynthesisRecommendation:
        normalized = [
            p.model_dump(by_alias=True) if isinstance(p, SynthesisPeptide) else p
            for p in peptides
        ]
        body: dict[str, object] = {
            "peptides": normalized,
            "intent": intent,
            "synthesisMode": synthesis_mode,
        }
        if gene is not None:
            body["gene"] = gene
        return SynthesisRecommendation.model_validate(
            await self._transport.request("POST", "/api/synthesis-checkout/recommend", json=body)
            or {"intent": intent, "synthesisMode": synthesis_mode, "recommendations": []}
        )

    async def add_to_cart(
        self,
        session_id: str,
        gene: str,
        peptide_names: list[str] | None = None,
        peptide_sequences: list[str] | None = None,
        default_purity: str = ">95%",
        default_quantity: str = "1mg",
        include_bli: bool = False,
    ) -> SynthesisCart:
        body: dict[str, object] = {
            "sessionId": session_id,
            "gene": gene,
            "defaultPurity": default_purity,
            "defaultQuantity": default_quantity,
            "includeBli": include_bli,
        }
        if peptide_names is not None:
            body["peptideNames"] = peptide_names
        if peptide_sequences is not None:
            body["peptideSequences"] = peptide_sequences
        return SynthesisCart.model_validate(
            await self._transport.request("POST", "/api/synthesis-checkout/cart", json=body)
            or {"cartId": ""}
        )

    async def get_cart(self) -> SynthesisCart:
        return SynthesisCart.model_validate(
            await self._transport.request("GET", "/api/synthesis-checkout/cart") or {"cartId": ""}
        )

    async def list_orders(self) -> list[SynthesisOrder]:
        payload = await self._transport.request("GET", "/api/synthesis-checkout/orders") or []
        items = payload if isinstance(payload, list) else payload.get("orders", [])
        return [SynthesisOrder.model_validate(o) for o in items]

    async def get_order(self, order_id: str) -> SynthesisOrder:
        return SynthesisOrder.model_validate(
            await self._transport.request("GET", f"/api/synthesis-checkout/orders/{order_id}")
            or {"id": order_id, "status": "unknown"}
        )

    async def adaptyv_list(self, status: str | None = None, limit: int = 20) -> list[AdaptyvExperiment]:
        params: dict[str, object] = {"limit": limit}
        if status is not None:
            params["status"] = status
        payload = await self._transport.request("GET", "/api/adaptyv/experiments", params=params) or []
        items = payload if isinstance(payload, list) else payload.get("experiments", [])
        return [AdaptyvExperiment.model_validate(e) for e in items]

    async def adaptyv_get(self, experiment_id: str, include_quote: bool = False) -> AdaptyvExperiment:
        if include_quote:
            quote = await self._transport.request("GET", f"/api/adaptyv/experiments/{experiment_id}/quote") or {}
            payload = await self._transport.request("GET", f"/api/adaptyv/experiments/{experiment_id}") or {}
            payload["quoteUsd"] = quote.get("totalUsd") or quote.get("total")
            return AdaptyvExperiment.model_validate(payload)
        return AdaptyvExperiment.model_validate(
            await self._transport.request("GET", f"/api/adaptyv/experiments/{experiment_id}")
            or {"id": experiment_id, "status": "unknown"}
        )

    async def adaptyv_patch_sequences(
        self,
        experiment_id: str,
        sequences: list[AdaptyvSequence | dict],
    ) -> AdaptyvExperiment:
        normalized = [
            s.model_dump(by_alias=True) if isinstance(s, AdaptyvSequence) else s
            for s in sequences
        ]
        return AdaptyvExperiment.model_validate(
            await self._transport.request(
                "PATCH", f"/api/adaptyv/experiments/{experiment_id}", json={"sequences": normalized}
            )
            or {"id": experiment_id, "status": "unknown"}
        )

    async def adaptyv_search_targets(self, query: str, limit: int = 10) -> list[AdaptyvTarget]:
        payload = await self._transport.request(
            "GET", "/api/adaptyv/targets/search", params={"query": query, "limit": limit}
        ) or []
        items = payload if isinstance(payload, list) else payload.get("results", [])
        return [AdaptyvTarget.model_validate(t) for t in items]

    async def amide_quote(self, peptides: list[SynthesisPeptide | dict]) -> dict:
        normalized = [
            p.model_dump(by_alias=True) if isinstance(p, SynthesisPeptide) else p
            for p in peptides
        ]
        return await self._transport.request("POST", "/api/amide/quote", json={"peptides": normalized}) or {}
