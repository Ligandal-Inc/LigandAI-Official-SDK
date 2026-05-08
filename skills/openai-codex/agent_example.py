# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""
LigandAI tool dispatcher for OpenAI Codex / GPT tool-calling agents.

Wire-up:

    1. pip install openai ligandai>=0.5.0
    2. export OPENAI_API_KEY=...
    3. export LIGANDAI_API_KEY=lgai_pro_...
    4. python agent_example.py "Design 25 EGFR binders with iPSAE>0.7"

The script loads ``tools.json`` from the same directory, hands those tool
definitions to ``client.responses.create`` (OpenAI Responses API), and
dispatches every tool call to a Python handler that shells through the
``ligandai`` SDK. No mock data, no fake responses — every call hits
https://ligandai.com.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from ligandai import LigandAI
from ligandai.errors import LigandAIError


HERE = Path(__file__).resolve().parent


def _client() -> LigandAI:
    key = os.environ.get("LIGANDAI_API_KEY")
    if not key:
        raise SystemExit("LIGANDAI_API_KEY is required")
    return LigandAI(api_key=key)


# ---------------------------------------------------------------------------
# Tool handlers (one per name in tools.json)
# ---------------------------------------------------------------------------

def design_peptide(args: dict[str, Any]) -> dict[str, Any]:
    c = _client()
    job = c.peptides.generate(
        gene=args["gene"],
        num_peptides=args.get("num_peptides", 10),
        length_range=(args.get("length_min", 20), args.get("length_max", 70)),
        auto_fold=args.get("auto_fold", True),
        max_folds_per_target=args.get("max_folds", 5),
        num_trajectories=args.get("num_trajectories", 1),
        sampling_steps=args.get("sampling_steps", 50),
        fold_strategy=args.get("fold_strategy", "quality_ranked"),
        target_chains=args.get("target_chains"),
        variant_id=args.get("variant_id"),
        quality_guided=args.get("quality_guided", False),
        immunogenicity=args.get("immunogenicity", False),
        serum_stability=args.get("serum_stability", False),
        cyclic_mode=args.get("cyclic_mode"),
        program_id=args.get("program_id"),
    )
    return {"session_id": job.session_id, "status": job.status}


def get_job(args: dict[str, Any]) -> dict[str, Any]:
    c = _client()
    info = c.jobs.get(args["session_id"])
    return {"id": info.id, "status": info.status, "progress": getattr(info, "progress", None)}


def estimate_cost(args: dict[str, Any]) -> dict[str, Any]:
    c = _client()
    est = c.peptides.estimate_cost(
        gene=args["gene"],
        num_peptides=args.get("num_peptides", 10),
        auto_fold=True,
        fold_top_n=args.get("max_folds", 5),
    )
    return {"credits": est.credits, "cost_usd": float(est.cost_usd)}


def get_credits(_args: dict[str, Any]) -> dict[str, Any]:
    c = _client()
    bal = c.account.get_balance()
    return {
        "credits": bal.credits,
        "tier": bal.tier,
        "burn_rate_30d": getattr(bal, "burn_rate_30d", None),
        "days_remaining": getattr(bal, "days_remaining", None),
    }


def resolve_pdb(args: dict[str, Any]) -> dict[str, Any]:
    c = _client()
    s = c.structures.resolve(
        gene=args.get("gene"),
        pdb_id=args.get("pdb_id"),
        uniprot_id=args.get("uniprot_id"),
    )
    return {
        "gene": s.gene,
        "source": s.source,
        "chains": [getattr(ch, "id", None) for ch in (s.chains or [])],
    }


def upload_pdb(args: dict[str, Any]) -> dict[str, Any]:
    c = _client()
    up = c.proteins.upload_pdb(
        file=Path(args["file_path"]),
        gene=args["gene"],
        custom_name=args.get("custom_name"),
    )
    return {"variant_id": up.id, "gene": up.gene}


def search_peptides(args: dict[str, Any]) -> dict[str, Any]:
    c = _client()
    res = c.peptides.search(
        program_id=args.get("program_id"),
        session_id=args.get("session_id"),
        gene=args.get("gene"),
        ipsae_min=args.get("ipsae_min"),
        iptm_min=args.get("iptm_min"),
        kd_max=args.get("kd_max_nm"),
        is_elite=args.get("is_elite"),
        super_elite=args.get("super_elite"),
        hotspot_residues=args.get("hotspot_residues"),
        limit=args.get("limit", 50),
    )
    return {
        "total": res.total,
        "tier_redacted": getattr(res, "tier_redacted", False),
        "peptides": [
            {"id": p.id, "gene": p.gene, "ipsae": p.ipsae, "kd": getattr(p, "predicted_kd", None)}
            for p in res.peptides[:25]
        ],
    }


def recommend_linker(args: dict[str, Any]) -> dict[str, Any]:
    c = _client()
    rec = c.synthesis.recommend_linker(
        sequence=args["sequence"],
        gene=args.get("gene"),
        pdb_job_id=args.get("pdb_job_id"),
        intended_application=args.get("intended_application", "bli_validation"),
    )
    return {
        "recommended_linker": rec.recommended_linker,
        "binding_orientation": getattr(rec, "binding_orientation", None),
    }


def adaptyv_submit(args: dict[str, Any]) -> dict[str, Any]:
    from ligandai.types import AdaptyvSequence

    c = _client()
    targets = c.synthesis.adaptyv_search_targets(args["gene"])
    if not targets:
        return {"error": f"No Adaptyv targets for gene {args['gene']}"}
    seqs = [AdaptyvSequence(name=s["name"], sequence=s["sequence"]) for s in args["sequences"]]
    exp = c.synthesis.adaptyv_create(
        name=args["experiment_name"],
        target_id=targets[0].id,
        sequences=seqs,
        include_bli=args.get("include_bli", True),
    )
    submitted = c.synthesis.adaptyv_submit(exp.id)
    return {"experiment_id": submitted.id, "status": submitted.status}


HANDLERS = {
    "ligandai_design_peptide": design_peptide,
    "ligandai_get_job": get_job,
    "ligandai_estimate_cost": estimate_cost,
    "ligandai_get_credits": get_credits,
    "ligandai_resolve_pdb": resolve_pdb,
    "ligandai_upload_pdb": upload_pdb,
    "ligandai_search_peptides": search_peptides,
    "ligandai_recommend_linker": recommend_linker,
    "ligandai_adaptyv_submit": adaptyv_submit,
}


# ---------------------------------------------------------------------------
# Entry point — minimal Responses API loop
# ---------------------------------------------------------------------------

def main(user_msg: str) -> int:
    try:
        from openai import OpenAI
    except ImportError:
        print("Install openai: pip install openai", file=sys.stderr)
        return 1

    tools = json.loads((HERE / "tools.json").read_text())
    oai = OpenAI()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": (
            "You are a peptide-design agent backed by LigandAI. Use the tools "
            "to design, fold, score, and order peptides. NEVER fabricate "
            "results — always call a tool. Surface 402 (paid required) and "
            "403 (tier required) responses verbatim to the user."
        )},
        {"role": "user", "content": user_msg},
    ]

    while True:
        resp = oai.chat.completions.create(
            model=os.environ.get("OPENAI_MODEL", "gpt-5"),
            messages=messages,
            tools=tools,
        )
        choice = resp.choices[0]
        if choice.finish_reason == "tool_calls":
            messages.append(choice.message.model_dump())
            for call in choice.message.tool_calls or []:
                args = json.loads(call.function.arguments or "{}")
                handler = HANDLERS.get(call.function.name)
                if handler is None:
                    out: Any = {"error": f"unknown tool {call.function.name}"}
                else:
                    try:
                        out = handler(args)
                    except LigandAIError as e:
                        out = {"error": type(e).__name__, "message": str(e)}
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(out),
                })
            continue
        print(choice.message.content)
        return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: agent_example.py '<your message>'", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(" ".join(sys.argv[1:])))
