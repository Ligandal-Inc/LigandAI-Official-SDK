# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""
18 — Error handling + tier-gating decision tree.

The SDK raises three distinct exception classes you should catch separately:

  LigandAIForbidden       — auth-level rejection (revoked/invalid key, 403 ToS,
                            rate-limit hit). Don't retry; ask the user to
                            re-auth or accept ToS.
  LigandAITierError       — endpoint requires a paid tier (402-style).
                            Surface the upgrade signal.
  LigandAIUpgradeRequired — same as TierError, but the user IS paid — they
                            need a HIGHER tier (e.g. enterprise-only feature
                            on a pro key).
  LigandAIError           — base class; catch last as a fallback.

This example walks the decision tree so application code knows which branch
to take. Use as a template for your own retry/upgrade-prompt flows.

Run with:
    LIGANDAI_API_KEY=lgai_free_... python 18_error_handling_tier_gating.py
    LIGANDAI_API_KEY=lgai_pro_...  python 18_error_handling_tier_gating.py

Different tiers will exit with different return codes; that's the point.
"""

from __future__ import annotations

import os
import sys

from ligandai import LigandAI
from ligandai.errors import (
    LigandAIError,
    LigandAIForbidden,
    LigandAITierError,
    LigandAIUpgradeRequired,
)


def main() -> int:
    key = os.environ.get("LIGANDAI_API_KEY")
    if not key:
        print("LIGANDAI_API_KEY env var is required", file=sys.stderr)
        return 1

    client = LigandAI(api_key=key)

    # 1) Always-allowed read: account.me() never gates, so it's a clean probe
    try:
        me = client.account.me()
        print(f"Authenticated as {me.email} (tier={me.subscription_tier})")
    except LigandAIForbidden as e:
        print(f"AUTH FAIL: {e}")
        print("→ Check your API key, accept ToS at https://ligandai.com/terms.")
        return 4
    except LigandAIError as e:
        print(f"Connectivity issue: {e}")
        return 2

    # 2) Try a paid endpoint and route on the exception type
    try:
        # adaptyv_search_targets requires Pro+ — free/basic should hit
        # LigandAITierError; pro/academia/enterprise will succeed.
        result = client.synthesis.adaptyv_search_targets("EGFR", limit=1)
        print(f"adaptyv_search_targets succeeded: {len(result)} hits")
        return 0

    except LigandAIUpgradeRequired as e:
        # Right-decision path: prompt user to upgrade
        print(f"UPGRADE REQUIRED → {e}")
        print(f"  Required tier: {getattr(e, 'required_tier', 'higher tier')}")
        print(f"  Current tier : {me.subscription_tier}")
        print("  Visit https://ligandai.com/pricing")
        return 3

    except LigandAITierError as e:
        # Free/basic users get this; surface the message + pricing link
        print(f"TIER-LOCKED → {e}")
        print(f"  Required tier: {getattr(e, 'required_tier', 'pro+')}")
        print(f"  Current tier : {me.subscription_tier}")
        print("  Visit https://ligandai.com/pricing")
        return 3

    except LigandAIForbidden as e:
        print(f"FORBIDDEN → {e}")
        print("  Likely causes: revoked key, ToS not accepted, rate-limit.")
        return 4

    except LigandAIError as e:
        # Generic fallback — log + show; don't break the user's flow
        print(f"API error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
