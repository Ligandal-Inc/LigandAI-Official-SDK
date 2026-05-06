# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Constants mirrored from the LIGANDAI server.

Source-of-truth references in the platform:

- Tier prefixes: ``server/api-key-routes.ts:17``
- API-key validation: ``server/enterprise-api-routes.ts:41``
- Tier features: ``server/enterprise-api-routes.ts`` and ``server/routes.ts``
- Generation:    ``server/routes.ts:tierLimits``
- Fold limits:   ``server/enterprise-api-routes.ts:TIER_GPU_CAP``
"""

from __future__ import annotations

from typing import Literal

# Tier identifiers from API key prefixes.
# Mirrors API_KEY_PREFIXES in api-key-routes.ts.
Tier = Literal["free", "basic", "academia", "pro", "enterprise", "superadmin"]

API_KEY_PREFIXES: dict[Tier, str] = {
    "free": "lgai_free_",
    "basic": "lgai_basic_",
    "academia": "lgai_edu_",
    "pro": "lgai_pro_",
    "enterprise": "lgai_ent_",
    "superadmin": "lgai_sa_",
}

# tierLevel ordering from server/middleware.ts.
TIER_ORDER: tuple[Tier, ...] = ("free", "basic", "academia", "pro", "enterprise", "superadmin")

# Rate limits per minute by API key tier.
TIER_RATE_LIMITS: dict[Tier, int] = {
    "free": 10,
    "basic": 20,
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
    "generate_peptides": "free",
    "predict_structure": "free",
    "predict_hotspots": "free",
    # Basic tier
    "standard_generation": "basic",
    # Academia tier
    "advanced_guidance": "academia",
    "logits_output": "academia",
    # Pro tier
    "analyze_binding": "pro",
    "bivalent_design": "pro",
    "transcriptomics_analysis": "pro",
    # Enterprise-only
    "batch_operations": "enterprise",
    "priority_queue": "enterprise",
    "custom_models": "enterprise",
    "transport_vasculome": "enterprise",
}

# Generation limits by tier-visible design count (mirrors server/routes.ts tierLimits).
# The SDK uses the tier-visible count, NEVER the backend pool size.
TIER_GENERATION_LIMITS: dict[Tier, int] = {
    "free": 10,
    "basic": 100,
    "academia": 300,
    "pro": 300,
    "enterprise": 1000,
    "superadmin": 1000,
}

# Fold limits by tier. Paid tiers do not have a lower SDK-side fold cap; the
# platform remains authoritative for credits, generation allowance, and abuse
# guards.
TIER_FOLD_LIMITS: dict[Tier, int | None] = {
    "free": 10,
    "basic": None,
    "academia": None,
    "pro": None,
    "enterprise": None,  # unlimited
    "superadmin": None,
}

# Target count limits by tier.
TIER_TARGET_LIMITS: dict[Tier, int | None] = {
    "free": 3,
    "basic": None,
    "academia": None,
    "pro": None,
    "enterprise": None,
    "superadmin": None,
}

# Default fold trajectories (Boltz-2 diffusion samples).
DEFAULT_TRAJECTORIES: int = 4

# Folding GPU caps by tier. Generation is submitted as a one-GPU job; these caps
# apply to folding and parallel fold batches.
TIER_GPU_SLOTS: dict[Tier, int] = {
    "free": 1,
    "basic": 4,
    "academia": 16,
    "pro": 25,
    "enterprise": 50,
    "superadmin": 50,
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
