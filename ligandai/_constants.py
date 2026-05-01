# Copyright © 2025 Ligandal, Inc. All rights reserved.
"""Constants mirrored from the LIGANDAI server.

Source-of-truth references in the platform:

- Tier prefixes: ``server/middleware/api-key-validator.ts:16``
- Rate limits:   ``server/middleware/api-key-validator.ts:27-33``
- Tier features: ``server/middleware/api-key-validator.ts:45-82``
- Generation:    ``shared/schema.ts:TIER_GENERATION_LIMITS``
- Fold limits:   ``shared/schema.ts:TIER_FOLD_LIMITS_BY_TARGET``
"""

from __future__ import annotations

from typing import Literal

# Tier identifiers from API key prefixes.
# Mirrors API_KEY_PREFIXES in api-key-validator.ts.
Tier = Literal["free", "academia", "pro", "enterprise", "superadmin"]

API_KEY_PREFIXES: dict[Tier, str] = {
    "free": "lgai_free_",
    "academia": "lgai_edu_",
    "pro": "lgai_pro_",
    "enterprise": "lgai_ent_",
    "superadmin": "lgai_sa_",
}

# tierLevel ordering from server/middleware.ts:115 (without basic/pro_commercial/discovery_partner
# which are session-tier only — API keys map to the five canonical tiers).
TIER_ORDER: tuple[Tier, ...] = ("free", "academia", "pro", "enterprise", "superadmin")

# Rate limits per minute by API key tier.
TIER_RATE_LIMITS: dict[Tier, int] = {
    "free": 10,
    "academia": 30,
    "pro": 60,
    "enterprise": 300,
    "superadmin": 1000,
}

# Feature → minimum tier mapping (mirrors TIER_FEATURES in api-key-validator.ts).
# This lets the SDK raise LigandAITierError client-side without a round-trip
# when a method is called against a key that doesn't have the required feature.
FEATURE_MIN_TIER: dict[str, Tier] = {
    # Free tier
    "search_receptors": "free",
    "view_structure": "free",
    "get_job_status": "free",
    # Academia
    "generate_peptides": "academia",
    "predict_structure": "academia",
    "analyze_binding": "academia",
    "predict_hotspots": "academia",
    # Enterprise-only
    "batch_operations": "enterprise",
    "priority_queue": "enterprise",
    "custom_models": "enterprise",
    "transport_vasculome": "enterprise",
}

# Generation limits by tier (mirrors TIER_GENERATION_LIMITS in shared/schema.ts).
# The SDK uses the tier-visible count, NEVER the backend pool size.
TIER_GENERATION_LIMITS: dict[Tier, int] = {
    "free": 100,
    "academia": 300,
    "pro": 1000,
    "enterprise": 5000,
    "superadmin": 5000,
}

# Fold limits per target by tier (mirrors TIER_FOLD_LIMITS_BY_TARGET).
TIER_FOLD_LIMITS: dict[Tier, int | None] = {
    "free": 1,
    "academia": 50,
    "pro": 100,
    "enterprise": None,  # unlimited
    "superadmin": None,
}

# Default fold trajectories (Boltz-2 diffusion samples).
DEFAULT_TRAJECTORIES: int = 4

# Concurrent GPU slot limits (mirrors USER_GPU_LIMITS in gpu-quota-manager.ts).
TIER_GPU_SLOTS: dict[Tier, int] = {
    "free": 2,
    "academia": 12,
    "pro": 24,
    "enterprise": 96,
    "superadmin": 96,
}

# Default URLs.
DEFAULT_BASE_URL: str = "https://ligandai.com"
DEFAULT_RECEPTORDB_URL: str = "https://receptordb.com"

# HTTP defaults.
DEFAULT_TIMEOUT_SECS: float = 60.0
DEFAULT_MAX_RETRIES: int = 5
DEFAULT_RETRY_BASE_DELAY: float = 1.0
DEFAULT_RETRY_MAX_DELAY: float = 30.0

# Job polling defaults.
DEFAULT_POLL_INTERVAL_SECS: float = 2.0
DEFAULT_JOB_TIMEOUT_SECS: float = 1800.0  # 30 minutes — generation+fold can take this long
