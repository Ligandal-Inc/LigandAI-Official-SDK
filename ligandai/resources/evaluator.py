# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Post-fold structural evaluation for SDK customers.  [bd-LIGANDAI_ALPHA_V2-6bybp]

Three capabilities that build on an existing ``fold_batch`` run:

Endpoint mapping:

- :meth:`Evaluator.filter_folds`      → ``POST /api/v1/folds/filter-by-criteria``
- :meth:`Evaluator.empirical_evaluate`→ ``POST /api/v1/folds/empirical-evaluate``
- :meth:`Evaluator.chat`              → ``POST /api/v1/folds/evaluator-chat``

Typical workflow::

    import ligandai

    client = ligandai.LigandAI()

    # 1. Submit a large fold batch (existing SDK surface)
    batch = client.peptides.fold_batch(
        peptides=my_peptides,
        target_gene="EGFR",
        diffusion_samples=4,
        use_potentials=True,
    )
    fold_ids = [job["id"] for job in batch.jobs]

    # 2. (Optional) Score with DeltaForge for thermodynamic affinity
    dg_scores = client.deltaforge.batch_score_fold(fold_ids)

    # 3. Filter by customer-defined structural criteria
    filtered = client.evaluator.filter_folds(
        fold_job_ids=fold_ids,
        criteria={
            "min_peptide_ipsae": 0.6,    # THE binder metric
            "max_delta_g": -5.0,         # kcal/mol (requires step 2)
            "min_contacts": 5,
        },
        deltaforge_scores=dg_scores.get("results", []),
    )
    elite_ids = [r["fold_job_id"] for r in filtered["results"] if r["passes"]]
    print(f"{filtered['passing']} / {filtered['total']} passed criteria")

    # 4. Empirical evaluator: data-first, no narrative
    report = client.evaluator.empirical_evaluate(
        fold_job_ids=fold_ids,
        criteria={"min_peptide_ipsae": 0.6},
        deltaforge_scores=dg_scores.get("results", []),
    )
    print(report["distributions"]["peptide_ipsae"])
    print(report["empirical_findings"]["binding_quality_assessment"])

    # 5. Grounded AI chat — interrogate the data with natural language
    answer = client.evaluator.chat(
        message="Which three peptides have the best binding profile and why?",
        empirical_report=report,
    )
    print(answer["response"])

    # 6. Multi-turn conversation
    history = []
    for question in ["What is the median peptide_ipsae?",
                      "How many folds passed delta_g < -5?",
                      "List the top 5 by ipsae"]:
        r = client.evaluator.chat(
            message=question,
            empirical_report=report,
            history=history,
        )
        history.append({"role": "user", "content": question})
        history.append({"role": "assistant", "content": r["response"]})
        print(f"Q: {question}\\nA: {r['response']}\\n")

Domain rules enforced throughout:

- ``peptide_ipsae`` (interface iPSAE) is THE binder metric.  We never
  substitute overall ``ipsae`` — it is dominated by receptor self-confidence on
  multimeric targets and is not a reliable binding indicator.
- ``delta_g`` and ``kd_nm`` are available only after
  ``client.deltaforge.batch_score_fold`` is run.  Passing them in via
  ``deltaforge_scores=`` merges them into the filter/report without requiring a
  second DeltaForge call.
- H-bond metric counts require explicit hydrogen placement; the server surfaces
  them when available in ``fold_metric_details``.  If absent, they are reported
  as ``null``, not estimated.
- MSA is enforced upstream by ``fold_batch`` for receptor chains — this module
  is post-fold only.
"""

from __future__ import annotations

from typing import Any

from ligandai.resources._base import AsyncResource, Resource

# ─── Endpoint constants ──────────────────────────────────────────────────────

_FILTER_BY_CRITERIA  = "/api/v1/folds/filter-by-criteria"
_EMPIRICAL_EVALUATE  = "/api/v1/folds/empirical-evaluate"
_EVALUATOR_CHAT      = "/api/v1/folds/evaluator-chat"


# ─── Body builders ──────────────────────────────────────────────────────────

def _filter_body(
    fold_job_ids: list[str],
    criteria: dict[str, Any],
    deltaforge_scores: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    if not isinstance(fold_job_ids, list) or not fold_job_ids:
        raise ValueError("fold_job_ids must be a non-empty list of strings")
    body: dict[str, Any] = {
        "fold_job_ids": [str(x) for x in fold_job_ids],
        "criteria": criteria,
    }
    if deltaforge_scores is not None:
        body["deltaforge_scores"] = deltaforge_scores
    return body


def _empirical_body(
    fold_job_ids: list[str],
    criteria: dict[str, Any] | None,
    deltaforge_scores: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    if not isinstance(fold_job_ids, list) or not fold_job_ids:
        raise ValueError("fold_job_ids must be a non-empty list of strings")
    body: dict[str, Any] = {"fold_job_ids": [str(x) for x in fold_job_ids]}
    if criteria is not None:
        body["criteria"] = criteria
    if deltaforge_scores is not None:
        body["deltaforge_scores"] = deltaforge_scores
    return body


def _chat_body(
    message: str,
    empirical_report: dict[str, Any],
    history: list[dict[str, str]] | None,
) -> dict[str, Any]:
    if not message or not isinstance(message, str):
        raise ValueError("message must be a non-empty string")
    if not isinstance(empirical_report, dict):
        raise ValueError(
            "empirical_report must be the dict returned by evaluator.empirical_evaluate()"
        )
    body: dict[str, Any] = {
        "message": message,
        "empirical_report": empirical_report,
    }
    if history:
        body["history"] = history
    return body


# ─── Sync resource ───────────────────────────────────────────────────────────

class Evaluator(Resource):
    """Sync ``client.evaluator`` — post-fold structural criteria filtering and
    AI-grounded analysis for SDK customers.

    All methods in this resource operate on fold jobs that were already
    submitted via ``client.peptides.fold_batch()``.  They read real structural
    metrics from the server — they never synthesise or estimate missing values.
    """

    def filter_folds(
        self,
        fold_job_ids: list[str],
        *,
        criteria: dict[str, Any] | None = None,
        deltaforge_scores: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Filter a completed fold batch by customer-defined structural criteria.

        ``POST /api/v1/folds/filter-by-criteria``

        Parameters
        ----------
        fold_job_ids
            List of fold job ids from a completed ``fold_batch`` run.
            Maximum 500 per call.

        criteria
            A dict expressing the structural thresholds to apply.  All keys
            are optional — omitting a key means that criterion is not applied::

                criteria = {
                    # ⛔ THE binder metric — interface iPSAE, not overall ipsae
                    "min_peptide_ipsae": 0.6,

                    # Thermodynamic affinity (requires deltaforge_scores)
                    "max_delta_g": -5.0,   # kcal/mol
                    "max_kd_nm": 100.0,    # nanomolar

                    # Structural confidence
                    "min_plddt": 70.0,

                    # Interface geometry
                    "min_contacts": 5,

                    # Receptor residues the peptide must contact (at least one)
                    "required_interface_residues": [
                        {"chain": "A", "residue": 148},
                        {"chain": "A", "residue": 162},
                    ],

                    # Customer-defined provenance tags echoed on passing folds
                    "provenance": {"campaign": "v3", "target": "EGFR"},
                }

        deltaforge_scores
            Pre-computed DeltaForge scores from ``client.deltaforge.batch_score_fold()``.
            Pass the ``results`` list directly::

                dg = client.deltaforge.batch_score_fold(fold_job_ids)
                filtered = client.evaluator.filter_folds(
                    fold_job_ids,
                    criteria={"max_delta_g": -5.0},
                    deltaforge_scores=dg["results"],
                )

            If omitted, ``delta_g`` / ``kd_nm`` criteria are not evaluated.

        Returns
        -------
        dict with keys:
            ``total``, ``passing``, ``failing``,
            ``results``: list of per-fold dicts with ``passes``, ``metrics``,
            ``failed_criteria``.

        Examples
        --------
        ::

            filtered = client.evaluator.filter_folds(
                fold_ids,
                criteria={
                    "min_peptide_ipsae": 0.6,
                    "min_contacts": 5,
                    "required_interface_residues": [{"chain": "A", "residue": 148}],
                },
            )
            elite = [r["fold_job_id"] for r in filtered["results"] if r["passes"]]
        """
        body = _filter_body(
            fold_job_ids=fold_job_ids,
            criteria=criteria or {},
            deltaforge_scores=deltaforge_scores,
        )
        return self._transport.request("POST", _FILTER_BY_CRITERIA, json=body) or {}

    def empirical_evaluate(
        self,
        fold_job_ids: list[str],
        *,
        criteria: dict[str, Any] | None = None,
        deltaforge_scores: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Run the data-first empirical evaluator agent on a fold batch.

        ``POST /api/v1/folds/empirical-evaluate``

        **Stage A** of the two-stage AI evaluation workflow (data before narrative).
        The agent reads every fold's real structural metrics from the database and
        produces:

        - Score distributions across the whole fold set (peptide_ipsae, iptm,
          plddt, delta_g when DeltaForge scores are supplied)
        - Criteria summary: how many folds pass / fail / have not_computed metrics
        - Per-fold pass/fail table
        - Empirical findings: AI-generated quantitative description of what the
          data actually shows (no recommendations, no suggestions)
        - Raw metrics list for direct programmatic consumption

        The returned dict is intended to be passed directly to
        :meth:`chat` for grounded conversational follow-up (Stage B).

        Parameters
        ----------
        fold_job_ids
            List of fold job ids.  Maximum 500.
        criteria
            Optional structural criteria.  If omitted, a default elite gate of
            ``min_peptide_ipsae >= 0.6`` is applied.
        deltaforge_scores
            Pre-computed DeltaForge scores to merge in for delta_g / kd_nm
            distributions and criteria evaluation.

        Returns
        -------
        dict with keys:
            ``fold_count``, ``structured_at``, ``distributions``,
            ``criteria_summary``, ``per_fold``, ``empirical_findings``,
            ``raw_metrics``

        Examples
        --------
        ::

            report = client.evaluator.empirical_evaluate(
                fold_ids,
                criteria={"min_peptide_ipsae": 0.6, "max_delta_g": -5.0},
                deltaforge_scores=dg_result["results"],
            )
            dist = report["distributions"]["peptide_ipsae"]
            print(f"peptide_ipsae: mean={dist['mean']}, p90={dist['p90']}")
            print(report["empirical_findings"]["binding_quality_assessment"])
        """
        body = _empirical_body(
            fold_job_ids=fold_job_ids,
            criteria=criteria,
            deltaforge_scores=deltaforge_scores,
        )
        return self._transport.request("POST", _EMPIRICAL_EVALUATE, json=body) or {}

    def chat(
        self,
        message: str,
        *,
        empirical_report: dict[str, Any],
        history: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Conversational AI grounded in empirical fold evaluation data.

        ``POST /api/v1/folds/evaluator-chat``

        **Stage B** of the two-stage AI evaluation workflow.  The assistant is
        grounded in the report produced by :meth:`empirical_evaluate` — every
        answer is traceable to the actual fold data, not to general protein
        biology knowledge.

        Parameters
        ----------
        message
            The customer's natural-language question.
        empirical_report
            The dict returned by :meth:`empirical_evaluate`.  Pass it back
            verbatim; the server extracts the relevant data blocks as context.
        history
            Optional conversation history as a list of
            ``{"role": "user"|"assistant", "content": str}`` dicts.
            Pass the growing conversation to support multi-turn dialogue.

        Returns
        -------
        dict with keys:
            ``response`` (str) — the AI's answer
            ``grounded_in`` (list[str]) — data fields from the report used

        Examples
        --------
        Single question::

            r = client.evaluator.chat(
                message="Which peptide has the highest binding confidence?",
                empirical_report=report,
            )
            print(r["response"])

        Multi-turn::

            history = []
            for q in ["What is the median ipsae?",
                       "How many folds passed criteria?",
                       "List top 5 by ipsae"]:
                r = client.evaluator.chat(
                    message=q,
                    empirical_report=report,
                    history=history,
                )
                history.append({"role": "user", "content": q})
                history.append({"role": "assistant", "content": r["response"]})
                print(f"Q: {q}\\nA: {r['response']}\\n")
        """
        body = _chat_body(
            message=message,
            empirical_report=empirical_report,
            history=history,
        )
        return self._transport.request("POST", _EVALUATOR_CHAT, json=body) or {}


# ─── Async resource ──────────────────────────────────────────────────────────

class AsyncEvaluator(AsyncResource):
    """Async ``client.evaluator`` — same API as :class:`Evaluator`, awaitable."""

    async def filter_folds(
        self,
        fold_job_ids: list[str],
        *,
        criteria: dict[str, Any] | None = None,
        deltaforge_scores: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Async variant of :meth:`Evaluator.filter_folds`."""
        body = _filter_body(
            fold_job_ids=fold_job_ids,
            criteria=criteria or {},
            deltaforge_scores=deltaforge_scores,
        )
        return await self._transport.request("POST", _FILTER_BY_CRITERIA, json=body) or {}

    async def empirical_evaluate(
        self,
        fold_job_ids: list[str],
        *,
        criteria: dict[str, Any] | None = None,
        deltaforge_scores: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Async variant of :meth:`Evaluator.empirical_evaluate`."""
        body = _empirical_body(
            fold_job_ids=fold_job_ids,
            criteria=criteria,
            deltaforge_scores=deltaforge_scores,
        )
        return await self._transport.request("POST", _EMPIRICAL_EVALUATE, json=body) or {}

    async def chat(
        self,
        message: str,
        *,
        empirical_report: dict[str, Any],
        history: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Async variant of :meth:`Evaluator.chat`."""
        body = _chat_body(
            message=message,
            empirical_report=empirical_report,
            history=history,
        )
        return await self._transport.request("POST", _EVALUATOR_CHAT, json=body) or {}
