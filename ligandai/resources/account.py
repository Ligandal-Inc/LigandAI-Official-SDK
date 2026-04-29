# Copyright © 2025 Ligandal, Inc. All rights reserved.
"""Account, credits, and tier limits."""

from __future__ import annotations

from typing import Any

from ligandai.resources._base import AsyncResource, Resource
from ligandai.types import (
    Credits,
    CreditTransaction,
    TierLimits,
    UsageSummary,
    User,
)


class Account(Resource):
    """User profile, credits, and tier limit endpoints."""

    def me(self) -> User:
        """``GET /api/auth/user`` — current authenticated user."""
        return User.model_validate(
            self._transport.request("GET", "/api/auth/user") or {}
        )

    def credits(self) -> Credits:
        """``GET /api/user-credits`` — current credit balance."""
        return Credits.model_validate(
            self._transport.request("GET", "/api/user-credits") or {}
        )

    def credit_history(self, limit: int = 50) -> list[CreditTransaction]:
        """``GET /api/user-credits/history``."""
        payload = self._transport.request(
            "GET", "/api/user-credits/history", params={"limit": limit}
        )
        items = payload if isinstance(payload, list) else (payload or {}).get("transactions", [])
        return [CreditTransaction.model_validate(it) for it in items]

    def tier_limits(self) -> TierLimits:
        """``GET /api/user-tier-limits``."""
        return TierLimits.model_validate(
            self._transport.request("GET", "/api/user-tier-limits") or {}
        )

    def update_profile(self, **fields: Any) -> User:
        """``PATCH /api/user-profile`` — update mutable profile fields."""
        return User.model_validate(
            self._transport.request("PATCH", "/api/user-profile", json=fields) or {}
        )

    def usage(self) -> UsageSummary:
        """``GET /api/assistant/usage`` — today's AI token usage vs limit."""
        return UsageSummary.model_validate(
            self._transport.request("GET", "/api/assistant/usage") or {}
        )


class AsyncAccount(AsyncResource):
    async def me(self) -> User:
        return User.model_validate(
            await self._transport.request("GET", "/api/auth/user") or {}
        )

    async def credits(self) -> Credits:
        return Credits.model_validate(
            await self._transport.request("GET", "/api/user-credits") or {}
        )

    async def credit_history(self, limit: int = 50) -> list[CreditTransaction]:
        payload = await self._transport.request(
            "GET", "/api/user-credits/history", params={"limit": limit}
        )
        items = payload if isinstance(payload, list) else (payload or {}).get("transactions", [])
        return [CreditTransaction.model_validate(it) for it in items]

    async def tier_limits(self) -> TierLimits:
        return TierLimits.model_validate(
            await self._transport.request("GET", "/api/user-tier-limits") or {}
        )

    async def update_profile(self, **fields: Any) -> User:
        return User.model_validate(
            await self._transport.request("PATCH", "/api/user-profile", json=fields) or {}
        )

    async def usage(self) -> UsageSummary:
        return UsageSummary.model_validate(
            await self._transport.request("GET", "/api/assistant/usage") or {}
        )
