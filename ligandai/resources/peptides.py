# Copyright © 2025 Ligandal, Inc. All rights reserved.
"""Peptide generation, folding, and scoring.

Public methods that submit GPU work return :class:`Job` (or :class:`AsyncJob`)
instances. Use ``.wait()`` to block until completion.

Endpoint mapping (server source-of-truth):

- :meth:`Peptides.generate`               → ``POST /api/ptf/parallel/generate``
- :meth:`Peptides.fold`                   → ``POST /api/folding/predict``
- :meth:`Peptides.fold_custom_mutation`   → ``POST /api/ptf/fold-custom-mutation`` (or boltz2/modified-fold)
- :meth:`Peptides.continue_folding`       → ``POST /api/ptf/parallel/{sid}/continue``
- :meth:`Peptides.score_complex`          → ``POST /api/binder-scoring/fold-and-score``
- :meth:`Peptides.score_with_ligandiq`    → ``POST /api/ptf/parallel/{sid}/ligandiq-score``
- :meth:`Peptides.analyze_solubility`     → ``POST /api/peptide-features/solubility``
- :meth:`Peptides.search`                 → ``GET  /api/ptf/genes/summary`` + filter
- :meth:`Peptides.search_by_pocket`       → ``GET  /api/ptf/peptides/by-pocket``
- :meth:`Peptides.get_elite`              → ``GET  /api/ptf/parallel/{sid}/elite``
"""

from __future__ import annotations

from typing import Any, Literal

from ligandai.errors import LigandAIError
from ligandai.jobs import AsyncJob, Job
from ligandai.resources._base import AsyncResource, Resource
from ligandai.types import (
    DeltaForgeScore,
    FoldResult,
    GenerationResult,
    LigandIQScore,
    Peptide,
    PeptideInput,
    ResidueRange,
    Sequence,
    SolubilityResult,
)

_TargetingStrategy = Literal["full_surface", "pocket_targeted"]


def _generation_target(
    gene: str,
    target_residues: list[ResidueRange] | None = None,
    targeting_strategy: _TargetingStrategy = "full_surface",
    variant_id: int | None = None,
) -> dict[str, Any]:
    """Build a single PTF target spec for the parallel generate endpoint."""
    target: dict[str, Any] = {"gene": gene, "targetingStrategy": targeting_strategy}
    if target_residues is not None:
        target["targetResidues"] = [
            r.model_dump(by_alias=True) if isinstance(r, ResidueRange) else r
            for r in target_residues
        ]
    if variant_id is not None:
        target["variantId"] = variant_id
    return target


def _generation_body(
    *,
    gene: str,
    num_peptides: int | None,
    length_range: tuple[int, int],
    target_residues: list[ResidueRange] | None,
    targeting_strategy: _TargetingStrategy,
    auto_fold: bool,
    top_n_fold: int | None,
    ec_domain_trimming: bool,
    deimmunize_mode: bool,
    variant_id: int | None,
    gen_gpus: int,
    fold_gpus: int,
    program_id: int | None,
    extra: dict[str, Any] | None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "targets": [_generation_target(gene, target_residues, targeting_strategy, variant_id)],
        "lengthRange": list(length_range),
        "autoFoldEnabled": auto_fold,
        "ecDomainTrimming": ec_domain_trimming,
        "deimmunizeMode": deimmunize_mode,
        "genParallelCount": gen_gpus,
        "foldingGpus": fold_gpus,
    }
    if num_peptides is not None:
        body["peptidesPerTarget"] = num_peptides
    if top_n_fold is not None:
        body["maxFoldsPerTarget"] = top_n_fold
    if program_id is not None:
        body["programId"] = program_id
    if extra:
        body.update(extra)
    return body


def _fold_body(
    sequences: list[Sequence | str | dict[str, Any]],
    *,
    auto_score: bool = True,
    template_mode: bool = False,
    msa_enabled: bool | None = None,
    target_gene: str | None = None,
    glycosylation: bool | None = None,
    pegylation: bool | None = None,
    gpu_count: int = 1,
    diffusion_samples: int = 4,
) -> dict[str, Any]:
    """Build the body for ``POST /api/folding/predict``.

    Single sequence → ``{model, sequence}``. Multiple → ``{model, entities}``.
    """
    normalized = [_norm_seq(s) for s in sequences]
    body: dict[str, Any] = {
        "model": "boltz2",
        "gpuCount": gpu_count,
        "diffusionSamples": diffusion_samples,
        "templateMode": template_mode,
        "autoScore": auto_score,
    }
    if target_gene is not None:
        body["targetGeneName"] = target_gene
    if msa_enabled is not None:
        body["msaEnabled"] = msa_enabled
    if glycosylation:
        body["glycosylation"] = {"enabled": True}
    if pegylation:
        body["pegylation"] = {"enabled": True}

    if len(normalized) == 1 and not normalized[0].get("chainId"):
        body["sequence"] = normalized[0]["sequence"]
        if "name" in normalized[0]:
            body["name"] = normalized[0]["name"]
    else:
        body["entities"] = [
            {
                "type": s.get("type", "protein"),
                "chainId": s.get("chainId") or chr(ord("A") + i),
                "sequence": s["sequence"],
                **({"name": s["name"]} if "name" in s else {}),
                **({"geneName": s["geneName"]} if "geneName" in s else {}),
            }
            for i, s in enumerate(normalized)
        ]
    return body


def _norm_seq(s: Sequence | str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(s, str):
        return {"sequence": s}
    if isinstance(s, Sequence):
        out: dict[str, Any] = {"sequence": s.sequence}
        if s.name:
            out["name"] = s.name
        if s.target_gene:
            out["geneName"] = s.target_gene
        if s.target_chain:
            out["chainId"] = s.target_chain
        return out
    return dict(s)


def _parse_generation(payload: dict[str, Any]) -> GenerationResult:
    """Coerce a server result payload into :class:`GenerationResult`."""
    out: dict[str, Any] = {
        "jobId": payload.get("jobId") or payload.get("id") or payload.get("session_id") or "",
        "sessionId": payload.get("sessionId") or payload.get("session_id"),
        "gene": payload.get("gene") or _first_target_gene(payload) or "",
        "peptides": _extract_peptides(payload),
        "totalGenerated": payload.get("totalGenerated") or payload.get("total"),
        "parameters": payload.get("parameters") or payload.get("config"),
    }
    return GenerationResult.model_validate(out)


def _parse_fold(payload: dict[str, Any]) -> FoldResult:
    return FoldResult.model_validate(
        {
            "jobId": payload.get("jobId") or payload.get("id") or "",
            "pdbUrl": payload.get("pdbUrl") or payload.get("pdb_url"),
            "pdbData": payload.get("pdbData") or payload.get("pdb_data") or payload.get("pdb"),
            "iptm": payload.get("iptm") or payload.get("ipTM"),
            "ipsae": payload.get("ipsae"),
            "plddt": payload.get("plddt"),
            "ptm": payload.get("ptm"),
            "chainPairIptm": payload.get("chainPairIptm") or payload.get("chain_pair_iptm"),
        }
    )


def _first_target_gene(payload: dict[str, Any]) -> str | None:
    targets = payload.get("targets")
    if isinstance(targets, list) and targets:
        first = targets[0]
        if isinstance(first, dict):
            return first.get("gene")
    return None


def _set_if_missing(out: dict[str, Any], key: str, value: Any) -> None:
    if value is not None and out.get(key) is None:
        out[key] = value


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _flatten_peptide(raw: dict[str, Any], gene: str | None = None) -> dict[str, Any]:
    """Promote quality_scores sub-fields to top level for Peptide mapping."""
    if not isinstance(raw, dict):
        return {"sequence": str(raw), **({"targetGene": gene} if gene else {})}
    qs = raw.get("quality_scores") or {}
    out = dict(raw)
    if gene and not out.get("targetGene") and not out.get("target_gene"):
        out["targetGene"] = gene

    _set_if_missing(out, "ligandiq", _first_present(raw.get("ligandiq_score"), qs.get("ligandiq_score")))
    _set_if_missing(out, "predictedIpsae", _first_present(raw.get("predicted_ipsae"), qs.get("predicted_ipsae")))
    predicted_iptm = _first_present(
        raw.get("predicted_iptm"),
        raw.get("pred_iptm"),
        raw.get("ligandiq_pred_iptm"),
        qs.get("predicted_iptm"),
        qs.get("pred_iptm"),
        qs.get("ligandiq_pred_iptm"),
    )
    legacy_predicted_ptm = _first_present(raw.get("predicted_ptm"), qs.get("predicted_ptm"))
    # Legacy production LigandIQ payloads normalize Modal's pred_iptm head into
    # quality_scores.predicted_ptm. Expose it only as predicted_iptm; current
    # LigandIQ does not emit a distinct predicted pTM head.
    if predicted_iptm is None and (
        raw.get("ligandiq_score") is not None
        or qs.get("ligandiq_score") is not None
        or raw.get("predicted_ipsae") is not None
        or qs.get("predicted_ipsae") is not None
    ):
        predicted_iptm = legacy_predicted_ptm
    out.pop("predicted_ptm", None)
    out.pop("predictedPtm", None)
    _set_if_missing(out, "predictedIptm", predicted_iptm)
    _set_if_missing(out, "predictedPlddt", _first_present(raw.get("predicted_plddt"), qs.get("predicted_plddt")))
    _set_if_missing(out, "binderProb", _first_present(raw.get("binder_prob"), qs.get("binder_prob")))

    # Stability / immuno (pro+ tier, may be None)
    if not out.get("stability_grade") and raw.get("stability_scores"):
        out["stabilityGrade"] = raw["stability_scores"].get("stability_grade")
    if not out.get("immunogenicity_score") and raw.get("immuno_scores"):
        out["immunogenicityScore"] = raw["immuno_scores"].get("immunogenicityScore")
    return out


def _extract_peptides(payload: dict[str, Any]) -> list[dict[str, Any]]:
    pep = payload.get("peptides")
    if isinstance(pep, list):
        return [_flatten_peptide(p) for p in pep]
    # Dict keyed by gene (session detail format) → flatten all genes
    if isinstance(pep, dict):
        flat: list[dict[str, Any]] = []
        for gene, gene_peps in pep.items():
            if isinstance(gene_peps, list):
                flat.extend(_flatten_peptide(p, gene=str(gene)) for p in gene_peps)
        return flat
    nested = payload.get("results")
    if isinstance(nested, dict) and isinstance(nested.get("peptides"), list):
        return [_flatten_peptide(p) for p in nested["peptides"]]
    if isinstance(nested, list):
        return [_flatten_peptide(p) for p in nested]
    return []


def _has_generation_peptides(payload: dict[str, Any]) -> bool:
    return bool(_extract_peptides(payload))


def _unwrap_session_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the session object from the common session endpoint envelopes."""
    if not isinstance(payload, dict):
        return {}
    session = payload.get("session")
    if isinstance(session, dict):
        return session
    data = payload.get("data")
    if isinstance(data, dict):
        nested = data.get("session")
        if isinstance(nested, dict):
            return nested
        if "peptides" in data:
            return data
    return payload


def _session_id_from_payload(payload: dict[str, Any]) -> str | None:
    sid = (
        payload.get("sessionId")
        or payload.get("session_id")
        or payload.get("sessionID")
        or payload.get("id")
    )
    return sid if isinstance(sid, str) else None


def _generation_result_from_session(
    payload: dict[str, Any],
    session_response: dict[str, Any],
    *,
    fallback_session_id: str | None,
    fallback_gene: str | None,
) -> dict[str, Any]:
    session = _unwrap_session_response(session_response)
    result = dict(payload)

    session_id = (
        _session_id_from_payload(result)
        or _session_id_from_payload(session)
        or fallback_session_id
    )
    if session_id:
        result.setdefault("sessionId", session_id)
        result.setdefault("jobId", session_id)

    gene = (
        result.get("gene")
        or session.get("gene")
        or _first_target_gene(result)
        or _first_target_gene(session)
        or fallback_gene
    )
    if gene:
        result.setdefault("gene", gene)

    if not _has_generation_peptides(result) and session.get("peptides") is not None:
        result["peptides"] = session["peptides"]

    if result.get("totalGenerated") is None:
        result["totalGenerated"] = (
            session.get("totalGenerated")
            or session.get("total_generated")
            or session.get("total")
            or len(_extract_peptides(result))
            or None
        )

    if result.get("parameters") is None:
        result["parameters"] = session.get("parameters") or session.get("config")

    return result


def _load_generation_result(
    transport: Any,
    info: Any,
    *,
    fallback_session_id: str | None,
    fallback_gene: str | None,
) -> dict[str, Any] | None:
    payload = dict(info.result or {})
    if _has_generation_peptides(payload):
        return payload
    session_id = _session_id_from_payload(payload) or fallback_session_id or getattr(info, "id", None)
    if not session_id:
        return payload
    session_response = transport.request("GET", f"/api/ptf/sessions/{session_id}") or {}
    return _generation_result_from_session(
        payload,
        session_response,
        fallback_session_id=session_id,
        fallback_gene=fallback_gene,
    )


async def _aload_generation_result(
    transport: Any,
    info: Any,
    *,
    fallback_session_id: str | None,
    fallback_gene: str | None,
) -> dict[str, Any] | None:
    payload = dict(info.result or {})
    if _has_generation_peptides(payload):
        return payload
    session_id = _session_id_from_payload(payload) or fallback_session_id or getattr(info, "id", None)
    if not session_id:
        return payload
    session_response = await transport.request("GET", f"/api/ptf/sessions/{session_id}") or {}
    return _generation_result_from_session(
        payload,
        session_response,
        fallback_session_id=session_id,
        fallback_gene=fallback_gene,
    )


# -- Sync resource ----------------------------------------------------------


class Peptides(Resource):
    """Generation, folding, scoring, and search."""

    def generate(
        self,
        gene: str,
        num_peptides: int | None = None,
        length_range: tuple[int, int] = (20, 70),
        target_residues: list[ResidueRange] | None = None,
        targeting_strategy: _TargetingStrategy = "full_surface",
        auto_fold: bool = True,
        top_n_fold: int | None = None,
        ec_domain_trimming: bool = True,
        deimmunize_mode: bool = False,
        variant_id: int | None = None,
        gen_gpus: int = 1,
        fold_gpus: int = 5,
        program_id: int | None = None,
        **extra: Any,
    ) -> Job[GenerationResult]:
        """Submit a peptide generation job. Returns a :class:`Job`."""
        if self._client is not None:
            self._client._require_feature("generate_peptides")
        body = _generation_body(
            gene=gene,
            num_peptides=num_peptides,
            length_range=length_range,
            target_residues=target_residues,
            targeting_strategy=targeting_strategy,
            auto_fold=auto_fold,
            top_n_fold=top_n_fold,
            ec_domain_trimming=ec_domain_trimming,
            deimmunize_mode=deimmunize_mode,
            variant_id=variant_id,
            gen_gpus=gen_gpus,
            fold_gpus=fold_gpus,
            program_id=program_id,
            extra=extra,
        )
        payload = self._transport.request("POST", "/api/ptf/parallel/generate", json=body) or {}
        job_id = payload.get("sessionId") or payload.get("jobId") or payload.get("session_id") or ""
        if not job_id:
            raise LigandAIError(
                "Server did not return a session_id/jobId for generation",
                response=payload,
            )
        return Job(
            self._transport,
            job_id,
            job_type="generation",
            parser=_parse_generation,
            status_path="/api/ptf/parallel/{job_id}/status",
            cancel_path="/api/ptf/parallel/{job_id}/cancel",
            sse_path="/api/ptf/parallel/{job_id}/stream",
            initial={"id": job_id, "type": "generation", "status": "queued", **payload},
            result_loader=lambda info: _load_generation_result(
                self._transport,
                info,
                fallback_session_id=job_id,
                fallback_gene=gene,
            ),
        )

    def fold(
        self,
        sequences: list[Sequence | str | dict[str, Any]],
        target_gene: str | None = None,
        auto_score: bool = True,
        template_mode: bool = False,
        msa_enabled: bool | None = None,
        glycosylation: bool | None = None,
        pegylation: bool | None = None,
        gpu_count: int = 1,
        diffusion_samples: int = 4,
    ) -> Job[FoldResult]:
        """Submit a Boltz-2 folding job (monomer or multimer)."""
        if self._client is not None:
            self._client._require_feature("predict_structure")
        body = _fold_body(
            sequences,
            auto_score=auto_score,
            template_mode=template_mode,
            msa_enabled=msa_enabled,
            target_gene=target_gene,
            glycosylation=glycosylation,
            pegylation=pegylation,
            gpu_count=gpu_count,
            diffusion_samples=diffusion_samples,
        )
        payload = self._transport.request("POST", "/api/folding/predict", json=body) or {}
        job_id = payload.get("jobId") or payload.get("id") or ""
        if not job_id:
            raise LigandAIError("Server did not return a jobId for fold", response=payload)
        return Job(
            self._transport,
            job_id,
            job_type="folding",
            parser=_parse_fold,
            status_path="/api/folding/jobs/{job_id}",
            cancel_path="/api/folding/jobs/{job_id}",
            sse_path="/api/jobs/{job_id}/sse",
            initial={"id": job_id, "type": "folding", "status": "queued", **payload},
        )

    def fold_custom_mutation(
        self,
        gene: str,
        mutations: list[str],
        alias: str | None = None,
    ) -> Job[FoldResult]:
        """``POST /api/ptf/fold-custom-mutation`` — fold a mutated variant."""
        if self._client is not None:
            self._client._require_feature("predict_structure")
        body: dict[str, Any] = {"gene": gene, "mutations": mutations}
        if alias is not None:
            body["alias"] = alias
        payload = self._transport.request("POST", "/api/ptf/fold-custom-mutation", json=body) or {}
        job_id = payload.get("jobId") or payload.get("id") or ""
        if not job_id:
            raise LigandAIError("Server did not return a jobId for custom mutation fold", response=payload)
        return Job(
            self._transport,
            job_id,
            job_type="folding",
            parser=_parse_fold,
            status_path="/api/folding/jobs/{job_id}",
            cancel_path="/api/folding/jobs/{job_id}",
            initial={"id": job_id, "type": "folding", "status": "queued", **payload},
        )

    def continue_folding(
        self,
        session_id: str | None = None,
        gene: str | None = None,
        top_n: int = 25,
        gpu_count: int = 5,
        template_mode: bool = False,
    ) -> Job[GenerationResult]:
        """``POST /api/ptf/parallel/{sid}/continue`` — fold more peptides from an existing session."""
        if not session_id and not gene:
            raise ValueError("Pass session_id= or gene=")
        if not session_id:
            assert gene is not None
            from_session = self._transport.request("GET", f"/api/ptf/sessions/by-gene/{gene}") or {}
            session_id = from_session.get("id")
            if not session_id:
                raise LigandAIError(f"No active session found for gene {gene!r}")
        body = {
            "topN": top_n,
            "gpuCount": gpu_count,
            "templateMode": template_mode,
        }
        payload = (
            self._transport.request("POST", f"/api/ptf/parallel/{session_id}/continue", json=body) or {}
        )
        job_id = payload.get("jobId") or session_id
        return Job(
            self._transport,
            job_id,
            job_type="generation",
            parser=_parse_generation,
            status_path="/api/ptf/parallel/{job_id}/status",
            cancel_path="/api/ptf/parallel/{job_id}/cancel",
            sse_path="/api/ptf/parallel/{job_id}/stream",
            initial={"id": job_id, "type": "generation", "status": "running", **payload},
            result_loader=lambda info: _load_generation_result(
                self._transport,
                info,
                fallback_session_id=session_id,
                fallback_gene=gene,
            ),
        )

    def score_complex(
        self,
        binder_sequence: str,
        target_sequence: str,
        binder_name: str = "binder",
        target_name: str = "target",
    ) -> Job[DeltaForgeScore]:
        """``POST /api/binder-scoring/fold-and-score`` — submit a fold + DeltaForge scoring job.

        Returns a :class:`Job[DeltaForgeScore]`. Poll with ``.wait()`` and read
        the parsed ``DeltaForgeScore`` from ``.results``.
        """
        body = {
            "binderSequence": binder_sequence,
            "targetSequence": target_sequence,
            "binderName": binder_name,
            "targetName": target_name,
        }
        payload = self._transport.request("POST", "/api/binder-scoring/fold-and-score", json=body) or {}
        job_id = payload.get("jobId") or payload.get("id") or ""
        if not job_id:
            raise LigandAIError("Server did not return a jobId", response=payload)

        def parse(data: dict[str, Any]) -> DeltaForgeScore:
            scoring = data.get("scoring") or data.get("deltaforge") or data
            return DeltaForgeScore.model_validate(
                {
                    "dg": scoring.get("dg") or scoring.get("delta_g"),
                    "kd": scoring.get("kd"),
                    "contacts": scoring.get("contacts") or scoring.get("contact_count"),
                    "interfaceResidues": scoring.get("interface_residues"),
                    "metadata": scoring.get("metadata"),
                }
            )

        return Job(
            self._transport,
            job_id,
            job_type="scoring",
            parser=parse,
            status_path="/api/binder-scoring/job/{job_id}",
            initial={"id": job_id, "type": "scoring", "status": "submitted"},
        )

    def score_with_ligandiq(
        self,
        session_id: str | None = None,
        gene: str | None = None,
        top_n: int = 20,
    ) -> list[LigandIQScore]:
        """LigandIQ scoring on a session's peptides — synchronous (CPU-only)."""
        if not session_id and not gene:
            raise ValueError("Pass session_id= or gene=")
        if not session_id:
            from_session = self._transport.request("GET", f"/api/ptf/sessions/by-gene/{gene}") or {}
            session_id = from_session.get("id")
            if not session_id:
                raise LigandAIError(f"No active session found for gene {gene!r}")
        body = {"topN": top_n}
        payload = (
            self._transport.request(
                "POST", f"/api/ptf/parallel/{session_id}/ligandiq-score", json=body
            )
            or {}
        )
        items = payload.get("scores") or payload.get("results") or []
        return [LigandIQScore.model_validate(s) for s in items]

    def analyze_solubility(
        self,
        peptides: list[PeptideInput | dict[str, Any] | str],
        gravy_threshold: float = 0.0,
        flag_multi_cys: bool = True,
    ) -> list[SolubilityResult]:
        """``POST /api/peptide-features/solubility`` — GRAVY + cysteine + disulfide check."""
        normalized = [
            (p.model_dump(by_alias=True) if isinstance(p, PeptideInput) else
             {"sequence": p} if isinstance(p, str) else p)
            for p in peptides
        ]
        body = {
            "peptides": normalized,
            "gravyThreshold": gravy_threshold,
            "flagMultiCys": flag_multi_cys,
        }
        payload = (
            self._transport.request("POST", "/api/peptide-features/solubility", json=body)
            or {}
        )
        items = payload.get("results") or payload.get("solubility") or []
        return [SolubilityResult.model_validate(s) for s in items]

    def search(
        self,
        gene: str | None = None,
        classification: str | None = None,
        min_ipsae: float | None = None,
        limit: int = 20,
    ) -> list[Peptide]:
        """Search existing peptides by gene/classification/score."""
        if gene is None:
            raise ValueError("Pass gene= for now (search by classification-only is not supported yet)")
        params: dict[str, Any] = {"limit": limit}
        if classification is not None:
            params["classification"] = classification
        if min_ipsae is not None:
            params["min_ipsae"] = min_ipsae
        payload = self._transport.request(
            "GET", f"/api/ptf/generated-peptides/by-gene/{gene}", params=params
        ) or []
        items = payload if isinstance(payload, list) else payload.get("peptides", [])
        return [Peptide.model_validate(p) for p in items]

    def search_by_pocket(
        self,
        gene: str,
        chain: str | None = None,
        start_residue: int | None = None,
        end_residue: int | None = None,
        targeted_only: bool = True,
    ) -> list[Peptide]:
        """``GET /api/ptf/peptides/by-pocket`` — find prior peptides targeting a pocket."""
        params: dict[str, Any] = {"gene": gene, "targeted_only": targeted_only}
        if chain is not None:
            params["chain"] = chain
        if start_residue is not None:
            params["start_residue"] = start_residue
        if end_residue is not None:
            params["end_residue"] = end_residue
        payload = self._transport.request("GET", "/api/ptf/peptides/by-pocket", params=params) or []
        items = payload if isinstance(payload, list) else payload.get("peptides", [])
        return [Peptide.model_validate(p) for p in items]

    def get_elite(
        self,
        session_id: str | None = None,
        gene: str | None = None,
    ) -> list[Peptide]:
        """``GET /api/ptf/parallel/{sid}/elite`` — elite peptides for a session."""
        if not session_id and not gene:
            raise ValueError("Pass session_id= or gene=")
        if not session_id:
            from_session = self._transport.request("GET", f"/api/ptf/sessions/by-gene/{gene}") or {}
            session_id = from_session.get("id")
            if not session_id:
                raise LigandAIError(f"No active session for gene {gene!r}")
        payload = self._transport.request("GET", f"/api/ptf/parallel/{session_id}/elite") or []
        items = payload if isinstance(payload, list) else payload.get("peptides", [])
        return [Peptide.model_validate(p) for p in items]


# -- Async resource ---------------------------------------------------------


class AsyncPeptides(AsyncResource):
    async def generate(
        self,
        gene: str,
        num_peptides: int | None = None,
        length_range: tuple[int, int] = (20, 70),
        target_residues: list[ResidueRange] | None = None,
        targeting_strategy: _TargetingStrategy = "full_surface",
        auto_fold: bool = True,
        top_n_fold: int | None = None,
        ec_domain_trimming: bool = True,
        deimmunize_mode: bool = False,
        variant_id: int | None = None,
        gen_gpus: int = 1,
        fold_gpus: int = 5,
        program_id: int | None = None,
        **extra: Any,
    ) -> AsyncJob[GenerationResult]:
        if self._client is not None:
            self._client._require_feature("generate_peptides")
        body = _generation_body(
            gene=gene,
            num_peptides=num_peptides,
            length_range=length_range,
            target_residues=target_residues,
            targeting_strategy=targeting_strategy,
            auto_fold=auto_fold,
            top_n_fold=top_n_fold,
            ec_domain_trimming=ec_domain_trimming,
            deimmunize_mode=deimmunize_mode,
            variant_id=variant_id,
            gen_gpus=gen_gpus,
            fold_gpus=fold_gpus,
            program_id=program_id,
            extra=extra,
        )
        payload = await self._transport.request("POST", "/api/ptf/parallel/generate", json=body) or {}
        job_id = payload.get("sessionId") or payload.get("jobId") or payload.get("session_id") or ""
        if not job_id:
            raise LigandAIError("Server did not return a session_id/jobId", response=payload)
        return AsyncJob(
            self._transport,
            job_id,
            job_type="generation",
            parser=_parse_generation,
            status_path="/api/ptf/parallel/{job_id}/status",
            cancel_path="/api/ptf/parallel/{job_id}/cancel",
            sse_path="/api/ptf/parallel/{job_id}/stream",
            initial={"id": job_id, "type": "generation", "status": "queued", **payload},
            result_loader=lambda info: _aload_generation_result(
                self._transport,
                info,
                fallback_session_id=job_id,
                fallback_gene=gene,
            ),
        )

    async def fold(
        self,
        sequences: list[Sequence | str | dict[str, Any]],
        target_gene: str | None = None,
        auto_score: bool = True,
        template_mode: bool = False,
        msa_enabled: bool | None = None,
        glycosylation: bool | None = None,
        pegylation: bool | None = None,
        gpu_count: int = 1,
        diffusion_samples: int = 4,
    ) -> AsyncJob[FoldResult]:
        if self._client is not None:
            self._client._require_feature("predict_structure")
        body = _fold_body(
            sequences,
            auto_score=auto_score,
            template_mode=template_mode,
            msa_enabled=msa_enabled,
            target_gene=target_gene,
            glycosylation=glycosylation,
            pegylation=pegylation,
            gpu_count=gpu_count,
            diffusion_samples=diffusion_samples,
        )
        payload = await self._transport.request("POST", "/api/folding/predict", json=body) or {}
        job_id = payload.get("jobId") or payload.get("id") or ""
        if not job_id:
            raise LigandAIError("Server did not return a jobId for fold", response=payload)
        return AsyncJob(
            self._transport,
            job_id,
            job_type="folding",
            parser=_parse_fold,
            status_path="/api/folding/jobs/{job_id}",
            cancel_path="/api/folding/jobs/{job_id}",
            sse_path="/api/jobs/{job_id}/sse",
            initial={"id": job_id, "type": "folding", "status": "queued", **payload},
        )

    async def fold_custom_mutation(
        self,
        gene: str,
        mutations: list[str],
        alias: str | None = None,
    ) -> AsyncJob[FoldResult]:
        if self._client is not None:
            self._client._require_feature("predict_structure")
        body: dict[str, Any] = {"gene": gene, "mutations": mutations}
        if alias is not None:
            body["alias"] = alias
        payload = await self._transport.request("POST", "/api/ptf/fold-custom-mutation", json=body) or {}
        job_id = payload.get("jobId") or payload.get("id") or ""
        if not job_id:
            raise LigandAIError("Server did not return a jobId", response=payload)
        return AsyncJob(
            self._transport,
            job_id,
            job_type="folding",
            parser=_parse_fold,
            status_path="/api/folding/jobs/{job_id}",
            cancel_path="/api/folding/jobs/{job_id}",
            initial={"id": job_id, "type": "folding", "status": "queued", **payload},
        )

    async def continue_folding(
        self,
        session_id: str | None = None,
        gene: str | None = None,
        top_n: int = 25,
        gpu_count: int = 5,
        template_mode: bool = False,
    ) -> AsyncJob[GenerationResult]:
        if not session_id and not gene:
            raise ValueError("Pass session_id= or gene=")
        if not session_id:
            assert gene is not None
            from_session = await self._transport.request("GET", f"/api/ptf/sessions/by-gene/{gene}") or {}
            session_id = from_session.get("id")
            if not session_id:
                raise LigandAIError(f"No active session for gene {gene!r}")
        body = {
            "topN": top_n,
            "gpuCount": gpu_count,
            "templateMode": template_mode,
        }
        payload = (
            await self._transport.request("POST", f"/api/ptf/parallel/{session_id}/continue", json=body) or {}
        )
        job_id = payload.get("jobId") or session_id
        return AsyncJob(
            self._transport,
            job_id,
            job_type="generation",
            parser=_parse_generation,
            status_path="/api/ptf/parallel/{job_id}/status",
            cancel_path="/api/ptf/parallel/{job_id}/cancel",
            sse_path="/api/ptf/parallel/{job_id}/stream",
            initial={"id": job_id, "type": "generation", "status": "running", **payload},
            result_loader=lambda info: _aload_generation_result(
                self._transport,
                info,
                fallback_session_id=session_id,
                fallback_gene=gene,
            ),
        )

    async def score_complex(
        self,
        binder_sequence: str,
        target_sequence: str,
        binder_name: str = "binder",
        target_name: str = "target",
    ) -> AsyncJob[DeltaForgeScore]:
        body = {
            "binderSequence": binder_sequence,
            "targetSequence": target_sequence,
            "binderName": binder_name,
            "targetName": target_name,
        }
        payload = await self._transport.request("POST", "/api/binder-scoring/fold-and-score", json=body) or {}
        job_id = payload.get("jobId") or payload.get("id") or ""
        if not job_id:
            raise LigandAIError("Server did not return a jobId", response=payload)

        def parse(data: dict[str, Any]) -> DeltaForgeScore:
            scoring = data.get("scoring") or data.get("deltaforge") or data
            return DeltaForgeScore.model_validate(
                {
                    "dg": scoring.get("dg") or scoring.get("delta_g"),
                    "kd": scoring.get("kd"),
                    "contacts": scoring.get("contacts") or scoring.get("contact_count"),
                    "interfaceResidues": scoring.get("interface_residues"),
                    "metadata": scoring.get("metadata"),
                }
            )

        return AsyncJob(
            self._transport,
            job_id,
            job_type="scoring",
            parser=parse,
            status_path="/api/binder-scoring/job/{job_id}",
            initial={"id": job_id, "type": "scoring", "status": "submitted"},
        )

    async def score_with_ligandiq(
        self,
        session_id: str | None = None,
        gene: str | None = None,
        top_n: int = 20,
    ) -> list[LigandIQScore]:
        if not session_id and not gene:
            raise ValueError("Pass session_id= or gene=")
        if not session_id:
            from_session = await self._transport.request("GET", f"/api/ptf/sessions/by-gene/{gene}") or {}
            session_id = from_session.get("id")
            if not session_id:
                raise LigandAIError(f"No active session for gene {gene!r}")
        body = {"topN": top_n}
        payload = (
            await self._transport.request(
                "POST", f"/api/ptf/parallel/{session_id}/ligandiq-score", json=body
            )
            or {}
        )
        items = payload.get("scores") or payload.get("results") or []
        return [LigandIQScore.model_validate(s) for s in items]

    async def analyze_solubility(
        self,
        peptides: list[PeptideInput | dict[str, Any] | str],
        gravy_threshold: float = 0.0,
        flag_multi_cys: bool = True,
    ) -> list[SolubilityResult]:
        normalized = [
            (p.model_dump(by_alias=True) if isinstance(p, PeptideInput) else
             {"sequence": p} if isinstance(p, str) else p)
            for p in peptides
        ]
        body = {
            "peptides": normalized,
            "gravyThreshold": gravy_threshold,
            "flagMultiCys": flag_multi_cys,
        }
        payload = (
            await self._transport.request("POST", "/api/peptide-features/solubility", json=body)
            or {}
        )
        items = payload.get("results") or payload.get("solubility") or []
        return [SolubilityResult.model_validate(s) for s in items]

    async def search(
        self,
        gene: str | None = None,
        classification: str | None = None,
        min_ipsae: float | None = None,
        limit: int = 20,
    ) -> list[Peptide]:
        if gene is None:
            raise ValueError("Pass gene=")
        params: dict[str, Any] = {"limit": limit}
        if classification is not None:
            params["classification"] = classification
        if min_ipsae is not None:
            params["min_ipsae"] = min_ipsae
        payload = await self._transport.request(
            "GET", f"/api/ptf/generated-peptides/by-gene/{gene}", params=params
        ) or []
        items = payload if isinstance(payload, list) else payload.get("peptides", [])
        return [Peptide.model_validate(p) for p in items]

    async def search_by_pocket(
        self,
        gene: str,
        chain: str | None = None,
        start_residue: int | None = None,
        end_residue: int | None = None,
        targeted_only: bool = True,
    ) -> list[Peptide]:
        params: dict[str, Any] = {"gene": gene, "targeted_only": targeted_only}
        if chain is not None:
            params["chain"] = chain
        if start_residue is not None:
            params["start_residue"] = start_residue
        if end_residue is not None:
            params["end_residue"] = end_residue
        payload = await self._transport.request("GET", "/api/ptf/peptides/by-pocket", params=params) or []
        items = payload if isinstance(payload, list) else payload.get("peptides", [])
        return [Peptide.model_validate(p) for p in items]

    async def get_elite(
        self,
        session_id: str | None = None,
        gene: str | None = None,
    ) -> list[Peptide]:
        if not session_id and not gene:
            raise ValueError("Pass session_id= or gene=")
        if not session_id:
            from_session = await self._transport.request("GET", f"/api/ptf/sessions/by-gene/{gene}") or {}
            session_id = from_session.get("id")
            if not session_id:
                raise LigandAIError(f"No active session for gene {gene!r}")
        payload = await self._transport.request("GET", f"/api/ptf/parallel/{session_id}/elite") or []
        items = payload if isinstance(payload, list) else payload.get("peptides", [])
        return [Peptide.model_validate(p) for p in items]
