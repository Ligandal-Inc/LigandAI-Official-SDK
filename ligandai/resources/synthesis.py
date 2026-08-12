# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Synthesis cart, recommendations, Adaptyv direct API."""

from __future__ import annotations

from collections.abc import Mapping, Sequence as _Sequence
from typing import Any, Literal

from ligandai.resources._base import AsyncResource, Resource
from ligandai.types import (
    AdaptyvExperiment,
    AdaptyvSequence,
    AdaptyvTarget,
    BindingOrientationResult,
    BiotinLinker,
    CostEstimate,
    GenerationMaskGuidance,
    LinkerRecommendation,
    SynthesisCart,
    SynthesisOptions,
    SynthesisOrder,
    SynthesisPeptide,
    SynthesisQuote,
    SynthesisRecommendation,
)

# ── Adaptyv experiment body construction ────────────────────────────────────
#
# ⛔ WIRE SHAPE. ``POST /api/adaptyv/experiments`` requires ``{name,
# experiment_spec}`` (server/adaptyv-routes.ts:297) and answers anything else
# with ``400 {"error": "name and experiment_spec are required"}``. Releases up
# to and including 0.9.0 posted ``{name, targetId, sequences, includeBli}``, so
# every ``adaptyv_create()`` call ever made against production failed. The
# server contract is the correct one — it mirrors Adaptyv Foundry's own
# ``/experiments`` schema, which the route proxies to — so the fix belongs
# here, on the client.
#
# Inside the spec, ``sequences`` is a JSON OBJECT keyed by design name, NOT a
# list::
#
#     {"cand-1": {"aa_string": "...", "control": false,
#                 "metadata": {"tag_location": "N"}}}
#
# ⛔ ``aa_string`` is the BARE BINDER CORE. Adaptyv expresses in E. coli
# cell-free and appends its own TwinStrep+splitGFP assay tag per design; a
# sequence submitted with a tag already attached gets a second one.

_ADAPTYV_DEFAULT_ANTIGEN_CONCENTRATIONS: list[float] = [1000.0, 316.2, 100.0, 31.6, 0.0]


def _adaptyv_tag_location(tag_position: str | None) -> str:
    """``n_term``/``c_term`` → the ``tag_location`` letter Adaptyv expects."""
    return "C" if str(tag_position or "").lower().startswith("c") else "N"


def _adaptyv_sequence_entry(
    item: AdaptyvSequence | Mapping[str, Any] | str,
    index: int,
    tag_location: str,
    key_hint: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Normalise one caller-supplied sequence into ``(name, spec_entry)``.

    Accepts an :class:`~ligandai.types.AdaptyvSequence`, a mapping using either
    the SDK's ``sequence`` key or the wire's ``aa_string`` key, or a bare
    amino-acid string.
    """
    if isinstance(item, AdaptyvSequence):
        raw: dict[str, Any] = item.model_dump(exclude_none=True)
    elif isinstance(item, Mapping):
        raw = dict(item)
    elif isinstance(item, str):
        raw = {"sequence": item}
    else:
        raise TypeError(
            f"sequences[{index}] must be an AdaptyvSequence, a mapping, or a string; "
            f"got {type(item).__name__}"
        )

    aa = raw.get("aa_string") or raw.get("aaString") or raw.get("sequence")
    aa = str(aa or "").strip().upper()
    if not aa:
        raise ValueError(
            f"sequences[{index}] has no amino-acid sequence. Provide 'sequence' "
            "(SDK form) or 'aa_string' (wire form)."
        )

    name = key_hint or raw.get("name") or raw.get("id") or f"seq-{index + 1}"
    name = str(name).strip() or f"seq-{index + 1}"

    entry: dict[str, Any] = {
        "aa_string": aa,
        "control": bool(raw.get("control", False)),
        "metadata": dict(raw.get("metadata") or {"tag_location": tag_location}),
    }
    # Anything else the caller attached (quantity, notes, ...) rides along
    # untouched rather than being silently dropped by an allow-list.
    for k, v in raw.items():
        if k not in {
            "aa_string", "aaString", "sequence", "name", "id", "control", "metadata"
        } and v is not None:
            entry.setdefault(k, v)
    return name, entry


def _adaptyv_sequences_object(
    sequences: Any, tag_location: str
) -> dict[str, dict[str, Any]]:
    """Build the ``experiment_spec.sequences`` OBJECT from any accepted form."""
    out: dict[str, dict[str, Any]] = {}
    items: list[tuple[str | None, Any]]
    if isinstance(sequences, Mapping):
        items = [(str(k), v) for k, v in sequences.items()]
    elif isinstance(sequences, _Sequence) and not isinstance(sequences, (str, bytes)):
        items = [(None, v) for v in sequences]
    else:
        raise TypeError(
            "sequences must be a list of AdaptyvSequence/dict/str, or a dict keyed "
            f"by design name; got {type(sequences).__name__}"
        )
    if not items:
        raise ValueError("sequences is empty — Adaptyv rejects an experiment with no designs.")

    for i, (key_hint, item) in enumerate(items):
        name, entry = _adaptyv_sequence_entry(item, i, tag_location, key_hint)
        if name in out:
            # Silently overwriting would drop a design from a real, paid order.
            raise ValueError(
                f"duplicate sequence name {name!r}. Adaptyv keys designs by name, so "
                "every entry needs a distinct one."
            )
        out[name] = entry
    return out


def _build_adaptyv_experiment_body(
    *,
    name: str,
    target_id: str | None,
    sequences: Any,
    include_bli: bool,
    method: str | None,
    experiment_type: str | None,
    n_replicates: int,
    antigen_concentrations: _Sequence[float] | None,
    synthesis_mode: str,
    tag_position: str,
    parameters: Mapping[str, Any] | None,
    experiment_spec: Mapping[str, Any] | None,
    extra: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Assemble the ``{name, experiment_spec, ...}`` body the route requires.

    ``experiment_spec``, when given, is used verbatim as the base and the
    derived keys only fill gaps — the documented escape hatch for spec fields
    this signature does not model.
    """
    if not str(name or "").strip():
        raise ValueError("name is required.")

    tag_location = _adaptyv_tag_location(tag_position)
    spec: dict[str, Any] = dict(experiment_spec or {})

    if sequences is not None:
        spec["sequences"] = _adaptyv_sequences_object(sequences, tag_location)
    if not spec.get("sequences"):
        raise ValueError(
            "sequences is required — pass the designs, or an experiment_spec that "
            "already carries a 'sequences' object."
        )

    # include_bli=True  → a binding-affinity run measured by BLI.
    # include_bli=False → expression/screening only; no assay method is sent,
    #                     because 'method' is what tells Adaptyv to run BLI.
    spec.setdefault("experiment_type", experiment_type or ("affinity" if include_bli else "screening"))
    resolved_method = method or ("bli" if include_bli else None)
    if resolved_method:
        spec.setdefault("method", resolved_method)
    if target_id:
        spec.setdefault("target_id", target_id)
    spec.setdefault("n_replicates", int(n_replicates))
    if spec["experiment_type"] == "affinity" or resolved_method:
        spec.setdefault(
            "antigen_concentrations",
            list(antigen_concentrations)
            if antigen_concentrations is not None
            else list(_ADAPTYV_DEFAULT_ANTIGEN_CONCENTRATIONS),
        )
    if parameters:
        merged = dict(parameters)
        merged.update(spec.get("parameters") or {})
        spec["parameters"] = merged

    body: dict[str, Any] = {
        "name": str(name).strip(),
        "experiment_spec": spec,
        "synthesis_mode": synthesis_mode,
        "tag_position": tag_position,
    }
    if extra:
        body.update({k: v for k, v in extra.items() if v is not None})
    return body


_ADAPTYV_CREATE_DOC = """Create a draft Adaptyv experiment and populate it with sequences.

        Creates a DRAFT. Nothing is quoted or charged until
        :meth:`adaptyv_submit` is called separately.

        ⛔ ``sequences`` must carry BARE BINDER CORES. Adaptyv expresses in
        E. coli cell-free and appends its own TwinStrep+splitGFP assay tag to
        each design; submitting an already-tagged construct produces a
        double-tagged protein.

        :param name:      Human-readable experiment name.
        :param target_id: Adaptyv target UUID, from :meth:`adaptyv_search_targets`.
        :param sequences: Designs, as a list of
            :class:`~ligandai.types.AdaptyvSequence` / dicts / bare strings, or
            a dict keyed by design name. Each is normalised into the wire's
            ``{name: {aa_string, control, metadata}}`` object.
        :param include_bli: ``True`` (default) requests a BLI affinity run.
            ``False`` requests expression/screening only.
        :param method: Assay method — ``"bli"`` or ``"spr"``. Defaults to
            ``"bli"`` when ``include_bli``.
        :param experiment_type: Defaults to ``"affinity"`` when ``include_bli``,
            else ``"screening"``.
        :param n_replicates: Replicates per design (default 3).
        :param antigen_concentrations: Titration series in nM. Defaults to
            ``[1000.0, 316.2, 100.0, 31.6, 0.0]``.
        :param synthesis_mode: ``"recombinant"`` (default) or ``"chemical"``.
        :param tag_position: ``"n_term"`` (default) or ``"c_term"``. Drives the
            per-sequence ``metadata.tag_location``.
        :param parameters: Extra ``experiment_spec.parameters`` entries.
        :param experiment_spec: A pre-built spec used as the base, for fields
            this signature does not model. Derived keys only fill gaps.
        :param extra: Extra top-level body keys (e.g. ``webhook_url``,
            ``skip_draft``, ``orientation_recommendations``).

        Typical flow::

            targets = client.synthesis.adaptyv_search_targets("EGFR")
            exp = client.synthesis.adaptyv_create(
                name="EGFR BLI validation",
                target_id=targets[0].id,
                sequences=[AdaptyvSequence(name="cand1", sequence="ACDEF...")],
            )
            # Separate, deliberate step — this one costs money:
            # client.synthesis.adaptyv_submit(exp.id)
        """


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
        target_id: str | None = None,
        sequences: list[AdaptyvSequence | dict | str] | dict[str, Any] | None = None,
        include_bli: bool = True,
        *,
        method: str | None = None,
        experiment_type: str | None = None,
        n_replicates: int = 3,
        antigen_concentrations: _Sequence[float] | None = None,
        synthesis_mode: str = "recombinant",
        tag_position: str = "n_term",
        parameters: Mapping[str, Any] | None = None,
        experiment_spec: Mapping[str, Any] | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> AdaptyvExperiment:
        body = _build_adaptyv_experiment_body(
            name=name,
            target_id=target_id,
            sequences=sequences,
            include_bli=include_bli,
            method=method,
            experiment_type=experiment_type,
            n_replicates=n_replicates,
            antigen_concentrations=antigen_concentrations,
            synthesis_mode=synthesis_mode,
            tag_position=tag_position,
            parameters=parameters,
            experiment_spec=experiment_spec,
            extra=extra,
        )
        payload = self._transport.request(
            "POST", "/api/adaptyv/experiments", json=body
        ) or {}
        return AdaptyvExperiment.model_validate(payload)

    adaptyv_create.__doc__ = _ADAPTYV_CREATE_DOC

    def adaptyv_submit(self, experiment_id: str) -> AdaptyvExperiment:
        """Submit a draft Adaptyv experiment for synthesis + BLI validation."""
        return AdaptyvExperiment.model_validate(
            self._transport.request(
                "POST", f"/api/adaptyv/experiments/{experiment_id}/submit"
            )
            or {"id": experiment_id, "status": "submitted"}
        )

    # -- BLI linker recommendation -------------------------------------------

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
        sampling_steps: int = 50,
        fold_trajectories: int = 1,
    ) -> CostEstimate:
        """Estimate credit cost for a generation + folding run before submitting.

        Args:
            gene: Target gene symbol.
            num_peptides: Planned peptide count (affects generation cost).
            max_folds: Planned fold count (affects folding cost).
            include_bli: Whether BLI synthesis is included (affects quote).
            include_deltaforge: Whether DeltaForge scoring is included.
            sampling_steps: Diffusion sampling steps per fold (15-200). Steps <=50
                cost 1x base rate; each additional 50 steps adds another 1x.
                E.g. 100 steps = 2x, 150 steps = 3x, 200 steps = 4x base rate.
            fold_trajectories: Number of trajectory repeats per fold (this
                parameter's default is 1). Note the platform's own tier
                default is 4 for academia/pro/pro_beta/pro_commercial/
                enterprise/discovery_partner (free/basic stay at 1) — pass
                ``fold_trajectories=4`` explicitly to match the platform
                default on a paid tier.
        """
        params: dict[str, object] = {
            "gene": gene,
            "numPeptides": num_peptides,
            "maxFolds": max_folds,
            "includeBli": include_bli,
            "includeDeltaforge": include_deltaforge,
            "sampling_steps": sampling_steps,
            "fold_trajectories": fold_trajectories,
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
        target_id: str | None = None,
        sequences: list[AdaptyvSequence | dict | str] | dict[str, Any] | None = None,
        include_bli: bool = True,
        *,
        method: str | None = None,
        experiment_type: str | None = None,
        n_replicates: int = 3,
        antigen_concentrations: _Sequence[float] | None = None,
        synthesis_mode: str = "recombinant",
        tag_position: str = "n_term",
        parameters: Mapping[str, Any] | None = None,
        experiment_spec: Mapping[str, Any] | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> AdaptyvExperiment:
        body = _build_adaptyv_experiment_body(
            name=name,
            target_id=target_id,
            sequences=sequences,
            include_bli=include_bli,
            method=method,
            experiment_type=experiment_type,
            n_replicates=n_replicates,
            antigen_concentrations=antigen_concentrations,
            synthesis_mode=synthesis_mode,
            tag_position=tag_position,
            parameters=parameters,
            experiment_spec=experiment_spec,
            extra=extra,
        )
        payload = await self._transport.request(
            "POST", "/api/adaptyv/experiments", json=body
        ) or {}
        return AdaptyvExperiment.model_validate(payload)

    adaptyv_create.__doc__ = _ADAPTYV_CREATE_DOC

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
        sampling_steps: int = 50,
        fold_trajectories: int = 1,
    ) -> CostEstimate:
        """Estimate credit cost for a generation + folding run before submitting.

        Args:
            gene: Target gene symbol.
            num_peptides: Planned peptide count (affects generation cost).
            max_folds: Planned fold count (affects folding cost).
            include_bli: Whether BLI synthesis is included (affects quote).
            include_deltaforge: Whether DeltaForge scoring is included.
            sampling_steps: Diffusion sampling steps per fold (15-200). Steps <=50
                cost 1x base rate; each additional 50 steps adds another 1x.
                E.g. 100 steps = 2x, 150 steps = 3x, 200 steps = 4x base rate.
            fold_trajectories: Number of trajectory repeats per fold (this
                parameter's default is 1). Note the platform's own tier
                default is 4 for academia/pro/pro_beta/pro_commercial/
                enterprise/discovery_partner (free/basic stay at 1) — pass
                ``fold_trajectories=4`` explicitly to match the platform
                default on a paid tier.
        """
        params: dict[str, object] = {
            "gene": gene,
            "numPeptides": num_peptides,
            "maxFolds": max_folds,
            "includeBli": include_bli,
            "includeDeltaforge": include_deltaforge,
            "sampling_steps": sampling_steps,
            "fold_trajectories": fold_trajectories,
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
