# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""
17 — Peptide synthesis + Adaptyv BLI/SPR integration.

Covers:
  client.synthesis.estimate(peptides, gene)
  client.synthesis.estimate_cost(gene, num_peptides, ...)
  client.synthesis.recommend(peptides, gene, intent)
  client.synthesis.linker_options() / .options()
  client.synthesis.recommend_linker(sequence, gene, intended_application)
  client.synthesis.add_to_cart / .get_cart
  client.synthesis.adaptyv_search_targets(query)
  client.synthesis.adaptyv_create / .adaptyv_get / .adaptyv_list / .adaptyv_submit
  client.synthesis.binding_orientation(sequence, pdb_job_id)

Tier-gated: synthesis.options + estimates are open to Basic+;
adaptyv_* + add_to_cart require Pro/academia/enterprise. Free users get
LigandAITierError when they call paid methods — see the bottom of the file
for the right-decision exception-handling pattern.

Run with:
    LIGANDAI_API_KEY=lgai_pro_... python 17_synthesis_adaptyv.py
"""

from __future__ import annotations

import os
import sys

from ligandai import LigandAI
from ligandai.errors import LigandAIError, LigandAITierError, LigandAIUpgradeRequired


def main() -> int:
    key = os.environ.get("LIGANDAI_API_KEY")
    if not key:
        print("LIGANDAI_API_KEY env var is required", file=sys.stderr)
        return 1

    client = LigandAI(api_key=key)

    try:
        # 1) Linker + modification options (read-only, basic+)
        print("== synthesis.linker_options() ==")
        linkers = client.synthesis.linker_options()
        for L in linkers[:5]:
            print(f"  - {L.id}  cleavable={L.is_cleavable}  thiol={L.is_thiol_reactive}")

        # 2) Quick cost estimate for a peptide list
        peptides = [
            {"sequence": "PSGYIPVHIFLA", "name": "alpha"},
            {"sequence": "WELRGTPMACFG", "name": "beta"},
        ]
        print("\n== synthesis.estimate(peptides) ==")
        est = client.synthesis.estimate(peptides=peptides, gene="EGFR")
        print(f"  unit_cost_usd: {getattr(est, 'unit_cost_usd', 'n/a')}")
        print(f"  total_usd    : {getattr(est, 'total_cost_usd', 'n/a')}")

        # 3) Linker recommendation for a single sequence + intended application
        print("\n== synthesis.recommend_linker('PSGYIPVHIFLA', "
              "intended_application='ELISA') ==")
        rec = client.synthesis.recommend_linker(
            sequence="PSGYIPVHIFLA",
            gene="EGFR",
            intended_application="elisa",
        )
        print(f"  recommended_linker: {rec.linker_id}  "
              f"position={rec.terminus}  rationale={rec.rationale[:60]}…")

        # 4) Adaptyv target search
        print("\n== synthesis.adaptyv_search_targets('EGFR') ==")
        try:
            adaptyv_targets = client.synthesis.adaptyv_search_targets("EGFR", limit=3)
            for t in adaptyv_targets:
                print(f"  - {t.id}  {t.name}  status={t.availability_status}")
        except LigandAITierError as e:
            print(f"  TIER-GATED: {e}")
            print(f"  Required: pro+; current: {e.current_tier}")
            return 0   # Right-decision path: stop, don't try adaptyv_create.

        # 5) Adaptyv experiment listing
        print("\n== synthesis.adaptyv_list(limit=3) ==")
        experiments = client.synthesis.adaptyv_list(limit=3)
        for x in experiments:
            print(f"  - {x.id}  status={x.status}  sequences={x.sequence_count}")

        # NOTE: We do NOT auto-call adaptyv_create here — it commits real spend.
        # Pattern for production:
        #   exp = client.synthesis.adaptyv_create(
        #       name="my_run", target_id=target.id,
        #       sequences=[{"sequence": ..., "name": ...} for p in top_peps],
        #       include_bli=True,
        #   )
        #   client.synthesis.adaptyv_submit(exp.id)

    except LigandAIUpgradeRequired as e:
        print(f"UPGRADE REQUIRED: {e}")
        print(f"Current tier: {e.current_tier}, need: {e.required_tier}")
        print("Visit https://ligandai.com/pricing to upgrade.")
        return 3
    except LigandAITierError as e:
        print(f"TIER-LOCKED: {e}")
        print(f"This API requires {getattr(e, 'required_tier', 'pro+')}.")
        return 3
    except LigandAIError as e:
        print(f"API error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
