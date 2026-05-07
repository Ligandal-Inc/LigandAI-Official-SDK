# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""
09 — Account, quota, and tier inspection.

Covers every method on `client.account`. Useful as a CLI health-check before
launching jobs: tells you who you are, how much credit you have, and what
your tier limits are. Run with:

    LIGANDAI_API_KEY=lgai_pro_... python 09_account_quota_tier.py
"""

from __future__ import annotations

import os
import sys

from ligandai import LigandAI
from ligandai.errors import LigandAIError


def main() -> int:
    key = os.environ.get("LIGANDAI_API_KEY")
    if not key:
        print("LIGANDAI_API_KEY env var is required", file=sys.stderr)
        return 1

    client = LigandAI(api_key=key)
    try:
        # Identity
        me = client.account.me()
        print(f"User       : {me.email} (id={me.id})")
        print(f"Tier       : {me.subscription_tier}")
        print(f"Org        : {getattr(me, 'organization_name', None) or '—'}")
        print()

        # Credits
        credits = client.account.credits()
        print(f"Credits    : balance={credits.balance:>10,}  "
              f"trial={getattr(credits, 'trial_balance', 0):>6,}")

        # Tier limits
        limits = client.account.tier_limits()
        print(f"Tier max   : peptides={limits.max_peptides_per_generation}  "
              f"folds={limits.max_folds_per_session}  "
              f"GPUs={limits.max_parallel_gpus}")

        # Usage summary
        usage = client.account.usage()
        print()
        print("Usage (this month):")
        print(f"  generations  : {usage.generations_count}")
        print(f"  folds        : {usage.folds_count}")
        print(f"  credits used : {usage.credits_used:,}")

        # Recent transactions
        history = client.account.credit_history(limit=5)
        print()
        print(f"Last {len(history)} credit transactions:")
        for tx in history:
            print(f"  {tx.created_at.strftime('%Y-%m-%d')}  "
                  f"{tx.transaction_type:<10}  "
                  f"{tx.delta_credits:>+8,}  {tx.reason}")

    except LigandAIError as e:
        print(f"API error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
