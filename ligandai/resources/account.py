# Copyright © 2025 Ligandal, Inc. All rights reserved.
"""Account, credits, tier limits, and billing."""

from __future__ import annotations

from typing import Any, Literal

from ligandai.resources._base import AsyncResource, Resource
from ligandai.types import (
    AccountBalance,
    AutoTopupConfig,
    Credits,
    CreditTransaction,
    TierLimits,
    TopUpResult,
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

    # ------------------------------------------------------------------
    # v0.3.0 billing surface
    # ------------------------------------------------------------------

    def get_balance(self) -> AccountBalance:
        """``GET /api/billing/account-summary`` — current balance with burn-rate.

        Returns:
            :class:`~ligandai.types.AccountBalance` with credits, burn_rate_30d,
            days_remaining, tier, and auto_topup_enabled.
        """
        payload = self._transport.request("GET", "/api/billing/account-summary") or {}
        return AccountBalance.model_validate(payload)

    def billing_usage(
        self,
        period: Literal["7d", "30d", "90d"] = "30d",
    ) -> list[CreditTransaction]:
        """``GET /api/billing/account-summary?period=X`` — recent credit transactions.

        Args:
            period: Lookback window — ``"7d"``, ``"30d"`` (default), or ``"90d"``.

        Returns:
            List of :class:`~ligandai.types.CreditTransaction` rows, newest first.
        """
        payload = self._transport.request(
            "GET", "/api/billing/account-summary", params={"period": period}
        ) or {}
        items = (
            payload.get("recent_transactions")
            or payload.get("recentTransactions")
            or payload.get("transactions")
            or []
        )
        return [CreditTransaction.model_validate(it) for it in items]

    def top_up(
        self,
        amount_usd: int,
        save_card: bool = False,
        payment_method_id: str | None = None,
    ) -> TopUpResult:
        """``POST /api/billing/topup`` — add credits to the account.

        When ``payment_method_id`` is provided (or a card is already saved on
        file), the charge is processed immediately off-session. Otherwise the
        server returns a ``checkout_url`` for the browser-based Stripe flow.

        Args:
            amount_usd: Dollar amount to top up (integer, e.g. ``200``).
            save_card: Whether to save the payment method for future use.
            payment_method_id: Stripe payment method ID for off-session charge.

        Returns:
            :class:`~ligandai.types.TopUpResult` with success status, credits
            added, new balance, and optionally a checkout_url.
        """
        body: dict[str, Any] = {"amountUsd": amount_usd, "saveCard": save_card}
        if payment_method_id is not None:
            body["paymentMethodId"] = payment_method_id
        payload = self._transport.request("POST", "/api/billing/topup", json=body) or {}
        return TopUpResult.model_validate(payload)

    def configure_auto_topup(
        self,
        enabled: bool,
        threshold_credits: int = 10000,
        amount_usd: Literal[50, 100, 200, 500, 1000, 2000] = 200,
    ) -> AutoTopupConfig:
        """``POST /api/billing/auto-topup/configure`` — configure automatic top-ups.

        When enabled, the platform automatically charges ``amount_usd`` whenever
        the account credit balance drops below ``threshold_credits``.

        Args:
            enabled: Enable or disable auto top-up.
            threshold_credits: Credit balance that triggers a top-up (default 10 000).
            amount_usd: Dollar amount to charge per auto top-up event.
                Allowed values: 50, 100, 200 (default), 500, 1000, 2000.

        Returns:
            :class:`~ligandai.types.AutoTopupConfig` reflecting the new configuration.
        """
        body: dict[str, Any] = {
            "enabled": enabled,
            "thresholdCredits": threshold_credits,
            "amountUsd": amount_usd,
        }
        payload = (
            self._transport.request(
                "POST", "/api/billing/auto-topup/configure", json=body
            )
            or {}
        )
        return AutoTopupConfig.model_validate(payload)


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

    # ------------------------------------------------------------------
    # v0.3.0 billing surface (async)
    # ------------------------------------------------------------------

    async def get_balance(self) -> AccountBalance:
        """Async variant of :meth:`Account.get_balance`."""
        payload = await self._transport.request("GET", "/api/billing/account-summary") or {}
        return AccountBalance.model_validate(payload)

    async def billing_usage(
        self,
        period: Literal["7d", "30d", "90d"] = "30d",
    ) -> list[CreditTransaction]:
        """Async variant of :meth:`Account.billing_usage`."""
        payload = (
            await self._transport.request(
                "GET", "/api/billing/account-summary", params={"period": period}
            )
            or {}
        )
        items = (
            payload.get("recent_transactions")
            or payload.get("recentTransactions")
            or payload.get("transactions")
            or []
        )
        return [CreditTransaction.model_validate(it) for it in items]

    async def top_up(
        self,
        amount_usd: int,
        save_card: bool = False,
        payment_method_id: str | None = None,
    ) -> TopUpResult:
        """Async variant of :meth:`Account.top_up`."""
        body: dict[str, Any] = {"amountUsd": amount_usd, "saveCard": save_card}
        if payment_method_id is not None:
            body["paymentMethodId"] = payment_method_id
        payload = (
            await self._transport.request("POST", "/api/billing/topup", json=body) or {}
        )
        return TopUpResult.model_validate(payload)

    async def configure_auto_topup(
        self,
        enabled: bool,
        threshold_credits: int = 10000,
        amount_usd: Literal[50, 100, 200, 500, 1000, 2000] = 200,
    ) -> AutoTopupConfig:
        """Async variant of :meth:`Account.configure_auto_topup`."""
        body: dict[str, Any] = {
            "enabled": enabled,
            "thresholdCredits": threshold_credits,
            "amountUsd": amount_usd,
        }
        payload = (
            await self._transport.request(
                "POST", "/api/billing/auto-topup/configure", json=body
            )
            or {}
        )
        return AutoTopupConfig.model_validate(payload)
