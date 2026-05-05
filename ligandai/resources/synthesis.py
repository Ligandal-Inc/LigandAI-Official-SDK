# Copyright © 2025 Ligandal, Inc. All rights reserved.
"""Synthesis cart, recommendations, Adaptyv direct API."""

from __future__ import annotations

from typing import Literal

from ligandai.resources._base import AsyncResource, Resource
from ligandai.types import (
    AdaptyvExperiment,
    AdaptyvSequence,
    AdaptyvTarget,
    BindingOrientationResult,
    BiotinLinker,
    GenerationMaskGuidance,
    LinkerRecommendation,
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

    def adaptyv_create(
        self,
        name: str,
        target_id: str,
        sequences: list[AdaptyvSequence | dict],
        include_bli: bool = True,
    ) -> AdaptyvExperiment:
        """Create a new Adaptyv experiment and populate it with sequences.

        Typical flow::

            targets = client.synthesis.adaptyv_search_targets("EGFR")
            seqs = [AdaptyvSequence(name="cand1", sequence="ACDEF...")]
            exp = client.synthesis.adaptyv_create(
                name="EGFR BLI validation",
                target_id=targets[0].id,
                sequences=seqs,
                include_bli=True,
            )
        """
        normalized = [
            s.model_dump(by_alias=True) if isinstance(s, AdaptyvSequence) else s
            for s in sequences
        ]
        payload = self._transport.request(
            "POST",
            "/api/adaptyv/experiments",
            json={
                "name": name,
                "targetId": target_id,
                "sequences": normalized,
                "includeBli": include_bli,
            },
        ) or {}
        return AdaptyvExperiment.model_validate(payload)

    def adaptyv_submit(self, experiment_id: str) -> AdaptyvExperiment:
        """Submit a draft Adaptyv experiment for synthesis + BLI validation."""
        return AdaptyvExperiment.model_validate(
            self._transport.request(
                "POST", f"/api/adaptyv/experiments/{experiment_id}/submit"
            )
            or {"id": experiment_id, "status": "submitted"}
        )

    # -- BLI linker recommendation (server/linker-configuration.ts) -----------

    def linker_options(self) -> list[BiotinLinker]:
        """Return all available BLI biotinylation linker options.

        Linkers are used to tether a peptide to the BLI sensor surface for
        affinity measurement. The server returns 9 options spanning PEG, Ahx,
        and GS-type spacers at N- or C-terminus.
        """
        payload = self._transport.request("GET", "/api/synthesis-checkout/linker-options") or []
        items = payload if isinstance(payload, list) else payload.get("linkers", [])
        return [BiotinLinker.model_validate(item) for item in items]

    def recommend_linker(
        self,
        sequence: str,
        gene: str | None = None,
        pdb_job_id: str | None = None,
        intended_application: str = "bli_validation",
    ) -> LinkerRecommendation:
        """Get a server-recommended BLI biotinylation linker for a peptide sequence.

        The server picks the optimal linker based on sequence length, terminus
        composition, and the planned application (BLI validation vs. therapeutic
        conjugation). Also returns alternative linkers with reasoning.

        Args:
            sequence: Amino acid sequence to analyse.
            gene: Target gene for context (improves recommendation accuracy).
            pdb_job_id: Fold job ID — enables contact-map-based orientation
                analysis to determine which terminus binds.
            intended_application: One of ``"bli_validation"`` (default),
                ``"therapeutic"``, ``"conjugation"``, ``"research"``.
        """
        body: dict[str, object] = {
            "sequence": sequence,
            "intendedApplication": intended_application,
        }
        if gene is not None:
            body["gene"] = gene
        if pdb_job_id is not None:
            body["pdbJobId"] = pdb_job_id
        return LinkerRecommendation.model_validate(
            self._transport.request("POST", "/api/synthesis-checkout/recommend-linker", json=body)
            or {}
        )

    def binding_orientation(
        self,
        sequence: str,
        pdb_job_id: str,
        gene: str | None = None,
    ) -> BindingOrientationResult:
        """Analyse which peptide terminus contacts the target (N, C, or middle).

        The server performs contact-map analysis on the folded complex to
        determine which end should be free (binding interface) and which end
        should be biotinylated (tethered to the BLI sensor surface).

        Args:
            sequence: Peptide amino acid sequence.
            pdb_job_id: Fold job ID from :meth:`~ligandai.resources.Peptides.fold`
                (must be a complex fold including the receptor chain).
            gene: Target gene for labelling / logging.
        """
        body: dict[str, object] = {
            "sequence": sequence,
            "pdbJobId": pdb_job_id,
        }
        if gene is not None:
            body["gene"] = gene
        return BindingOrientationResult.model_validate(
            self._transport.request("POST", "/api/synthesis-checkout/binding-orientation", json=body)
            or {}
        )

    def generation_mask_guidance(
        self,
        linker_id: str,
        linker_position: str = "c_terminal",
    ) -> GenerationMaskGuidance:
        """Get the generation-time mask hint that corresponds to a planned BLI linker.

        When the linker will be attached at the C-terminus (most common), the
        generator should avoid placing key binding contacts in the last N residues
        so they remain exposed on the sensor surface. This method translates your
        linker choice into a concrete constraint that can be passed to
        :meth:`~ligandai.resources.Peptides.generate` via ``extra``.

        Args:
            linker_id: Linker ID from :meth:`linker_options` or
                :meth:`recommend_linker` (e.g. ``"peg4_c_terminal"``).
            linker_position: ``"n_terminal"`` or ``"c_terminal"`` (default).

        Example::

            guidance = client.synthesis.generation_mask_guidance("peg4_c_terminal")
            job = client.peptides.generate(
                gene="EGFR",
                extra=guidance.generation_constraints,
            )
        """
        body: dict[str, object] = {
            "linkerId": linker_id,
            "linkerPosition": linker_position,
        }
        return GenerationMaskGuidance.model_validate(
            self._transport.request("POST", "/api/synthesis-checkout/mask-guidance", json=body)
            or {"maskHint": "none"}
        )

    # -- Cost estimation -------------------------------------------------------

    def estimate_cost(
        self,
        gene: str,
        num_peptides: int = 300,
        max_folds: int = 25,
        include_bli: bool = False,
        include_deltaforge: bool = True,
    ) -> "CostEstimate":
        """Estimate credit cost for a generation + folding run before submitting.

        Args:
            gene: Target gene symbol.
            num_peptides: Planned peptide count (affects generation cost).
            max_folds: Planned fold count (affects folding cost).
            include_bli: Whether BLI synthesis is included (affects quote).
            include_deltaforge: Whether DeltaForge scoring is included.
        """
        from ligandai.types import CostEstimate
        params: dict[str, object] = {
            "gene": gene,
            "numPeptides": num_peptides,
            "maxFolds": max_folds,
            "includeBli": include_bli,
            "includeDeltaforge": include_deltaforge,
        }
        return CostEstimate.model_validate(
            self._transport.request("GET", "/api/billing/estimate", params=params)
            or {"credits": 0, "costUsd": 0.0}
        )

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

    async def adaptyv_create(
        self,
        name: str,
        target_id: str,
        sequences: list[AdaptyvSequence | dict],
        include_bli: bool = True,
    ) -> AdaptyvExperiment:
        normalized = [
            s.model_dump(by_alias=True) if isinstance(s, AdaptyvSequence) else s
            for s in sequences
        ]
        payload = await self._transport.request(
            "POST",
            "/api/adaptyv/experiments",
            json={
                "name": name,
                "targetId": target_id,
                "sequences": normalized,
                "includeBli": include_bli,
            },
        ) or {}
        return AdaptyvExperiment.model_validate(payload)

    async def adaptyv_submit(self, experiment_id: str) -> AdaptyvExperiment:
        return AdaptyvExperiment.model_validate(
            await self._transport.request(
                "POST", f"/api/adaptyv/experiments/{experiment_id}/submit"
            )
            or {"id": experiment_id, "status": "submitted"}
        )

    async def linker_options(self) -> list[BiotinLinker]:
        payload = await self._transport.request("GET", "/api/synthesis-checkout/linker-options") or []
        items = payload if isinstance(payload, list) else payload.get("linkers", [])
        return [BiotinLinker.model_validate(item) for item in items]

    async def recommend_linker(
        self,
        sequence: str,
        gene: str | None = None,
        pdb_job_id: str | None = None,
        intended_application: str = "bli_validation",
    ) -> LinkerRecommendation:
        body: dict[str, object] = {
            "sequence": sequence,
            "intendedApplication": intended_application,
        }
        if gene is not None:
            body["gene"] = gene
        if pdb_job_id is not None:
            body["pdbJobId"] = pdb_job_id
        return LinkerRecommendation.model_validate(
            await self._transport.request("POST", "/api/synthesis-checkout/recommend-linker", json=body)
            or {}
        )

    async def binding_orientation(
        self,
        sequence: str,
        pdb_job_id: str,
        gene: str | None = None,
    ) -> BindingOrientationResult:
        body: dict[str, object] = {
            "sequence": sequence,
            "pdbJobId": pdb_job_id,
        }
        if gene is not None:
            body["gene"] = gene
        return BindingOrientationResult.model_validate(
            await self._transport.request("POST", "/api/synthesis-checkout/binding-orientation", json=body)
            or {}
        )

    async def generation_mask_guidance(
        self,
        linker_id: str,
        linker_position: str = "c_terminal",
    ) -> GenerationMaskGuidance:
        body: dict[str, object] = {
            "linkerId": linker_id,
            "linkerPosition": linker_position,
        }
        return GenerationMaskGuidance.model_validate(
            await self._transport.request("POST", "/api/synthesis-checkout/mask-guidance", json=body)
            or {"maskHint": "none"}
        )

    async def estimate_cost(
        self,
        gene: str,
        num_peptides: int = 300,
        max_folds: int = 25,
        include_bli: bool = False,
        include_deltaforge: bool = True,
    ) -> "CostEstimate":
        from ligandai.types import CostEstimate
        params: dict[str, object] = {
            "gene": gene,
            "numPeptides": num_peptides,
            "maxFolds": max_folds,
            "includeBli": include_bli,
            "includeDeltaforge": include_deltaforge,
        }
        return CostEstimate.model_validate(
            await self._transport.request("GET", "/api/billing/estimate", params=params)
            or {"credits": 0, "costUsd": 0.0}
        )

    async def amide_quote(self, peptides: list[SynthesisPeptide | dict]) -> dict:
        normalized = [
            p.model_dump(by_alias=True) if isinstance(p, SynthesisPeptide) else p
            for p in peptides
        ]
        return await self._transport.request("POST", "/api/amide/quote", json={"peptides": normalized}) or {}
