# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Tests for the credits widget + CLI subcommand.

Covers:
  - _credits_to_usd conversion (100 cr = $0.01)
  - CreditsWidget round-trips through pydantic validation
  - render_credits_widget renders a compact billing layout
  - cmd_credits_widget normalizes /api/credits/balance payload
  - CLI argument parsing for credits / credits top-up / credits auto-reload
  - top-up < $25 rejected with exit code 2
  - auto-reload --enable without threshold/amount rejected with exit code 2

Pure unit tests — no live HTTP; all transport interactions mocked.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from unittest import mock

import pytest

from ligandai.types import CreditsWidget
from ligandai.resources.account import _credits_to_usd
from ligandai import cli


# ── conversion math ──────────────────────────────────────────────────────────


class TestCreditsToUsd:
    def test_zero_credits_zero_usd(self):
        assert _credits_to_usd(0) == 0.0

    def test_100_credits_one_cent(self):
        assert _credits_to_usd(100) == 0.01

    def test_10_000_credits_one_dollar(self):
        assert _credits_to_usd(10_000) == 1.00

    def test_107_61_dollars_in_credits(self):
        assert _credits_to_usd(1_076_100) == 107.61

    def test_none_returns_zero(self):
        assert _credits_to_usd(None) == 0.0

    def test_rounding_two_decimals(self):
        assert _credits_to_usd(99) == 0.01


# ── CreditsWidget round-trip ──────────────────────────────────────────────────


class TestCreditsWidgetModel:
    def test_round_trips_from_server_payload(self):
        widget = CreditsWidget.model_validate({
            "available": 5_001,
            "total": 2_000_000,
            "usedThisMonth": 1_076_100,
            "balance_usd": 0.50,
            "monthly_limit_usd": 200.0,
            "spent_this_month_usd": 107.61,
            "pct_used": 54,
            "resetDate": "2026-06-01T00:00:00Z",
            "autoReplenish": False,
            "threshold": None,
            "replenishAmount": None,
            "tier": "pro",
        })
        assert widget.balance_credits == 5_001
        assert widget.monthly_limit_credits == 2_000_000
        assert widget.balance_usd == 0.50
        assert widget.monthly_limit_usd == 200.0
        assert widget.spent_this_month_usd == 107.61
        assert widget.pct_used == 54
        assert widget.auto_reload_enabled is False


# ── progress bar primitives ───────────────────────────────────────────────────


class TestProgressBar:
    def test_zero_pct_empty_filled(self):
        bar = cli._format_progress_bar(0, width=10)
        assert bar == "░" * 10

    def test_full_pct_all_filled(self):
        bar = cli._format_progress_bar(100, width=10)
        assert bar == "█" * 10

    def test_pct_clamps_above_100(self):
        bar = cli._format_progress_bar(150, width=10)
        assert "░" not in bar

    def test_pct_clamps_below_zero(self):
        bar = cli._format_progress_bar(-5, width=10)
        assert "█" not in bar


# ── widget rendering matches a screenshot shape ────────────────────────


class TestRenderCreditsWidget:
    def test_renders_spent_used_balance_lines(self):
        widget = {
            "spent_this_month_usd": 107.61,
            "monthly_limit_usd": 200.0,
            "pct_used": 54,
            "balance_usd": 50.01,
            "auto_reload_enabled": False,
            "reset_date": datetime(2026, 6, 1),
        }
        out = cli.render_credits_widget(widget)
        assert "$107.61 spent" in out
        assert "54% used" in out
        assert "$200 monthly limit" in out
        assert "$50.01 balance" in out
        assert "auto-reload off" in out
        assert "Resets" in out
        assert "Buy more" in out

    def test_auto_reload_on_states(self):
        widget = {
            "spent_this_month_usd": 0.0,
            "monthly_limit_usd": 100.0,
            "pct_used": 0,
            "balance_usd": 100.0,
            "auto_reload_enabled": True,
            "reset_date": None,
        }
        out = cli.render_credits_widget(widget)
        assert "auto-reload on" in out
        assert "Disable auto-reload" in out


# ── widget command normalizes server response ────────────────────────────────


def _stub_client_with_widget(payload: dict):
    from ligandai.types import CreditsWidget as W

    bal = int(payload.get("available") or 0)
    total = int(payload.get("total") or 0)
    used = int(payload.get("used_this_month") or max(0, total - bal))
    pct = int(round(100 * used / total)) if total > 0 else 0
    widget = W.model_validate({
        "available": bal,
        "total": total,
        "usedThisMonth": used,
        "balance_usd": round(bal / 10_000, 2),
        "monthly_limit_usd": round(total / 10_000, 2),
        "spent_this_month_usd": round(used / 10_000, 2),
        "pct_used": pct,
        "resetDate": payload.get("reset_date"),
        "autoReplenish": bool(payload.get("auto_replenish") or False),
        "threshold": payload.get("threshold"),
        "replenishAmount": payload.get("replenish_amount"),
        "tier": payload.get("tier"),
    })
    fake_client = mock.MagicMock()
    fake_client.account.widget.return_value = widget
    return fake_client


class TestCmdCreditsWidget:
    def test_widget_command_renders_for_human(self, capsys, monkeypatch):
        fake_client = _stub_client_with_widget({
            "available": 500_100,
            "total": 2_000_000,
            "used_this_month": 1_076_100,
            "reset_date": "2026-06-01T00:00:00Z",
            "auto_replenish": False,
        })
        monkeypatch.setattr(cli, "_make_client", lambda _args: fake_client)
        args = argparse.Namespace(base_url=None, json=False)
        rc = cli.cmd_credits_widget(args)
        captured = capsys.readouterr()
        assert rc == 0
        assert "$107.61 spent" in captured.out
        assert "$200 monthly limit" in captured.out
        assert "$50.01 balance" in captured.out

    def test_widget_command_json_emits_raw(self, capsys, monkeypatch):
        fake_client = _stub_client_with_widget({
            "available": 5_001,
            "total": 100_000,
            "used_this_month": 94_999,
            "auto_replenish": False,
        })
        monkeypatch.setattr(cli, "_make_client", lambda _args: fake_client)
        args = argparse.Namespace(base_url=None, json=True)
        rc = cli.cmd_credits_widget(args)
        captured = capsys.readouterr()
        assert rc == 0
        out = captured.out
        assert "available" in out or "balance_credits" in out


# ── CLI top-up validation ────────────────────────────────────────────────────


class TestCmdCreditsTopup:
    def test_below_min_amount_returns_2(self, capsys):
        args = argparse.Namespace(base_url=None, amount=10, save_card=False)
        rc = cli.cmd_credits_topup(args)
        err = capsys.readouterr().err
        assert rc == 2
        assert "$25" in err or "min" in err.lower()

    def test_above_max_amount_returns_2(self):
        args = argparse.Namespace(base_url=None, amount=30_000, save_card=False)
        assert cli.cmd_credits_topup(args) == 2

    def test_in_range_calls_top_up(self, capsys, monkeypatch):
        fake_result = mock.MagicMock()
        fake_result.checkout_url = "https://checkout.stripe.com/c/pay/cs_test_abc"
        fake_client = mock.MagicMock()
        fake_client.account.top_up.return_value = fake_result
        monkeypatch.setattr(cli, "_make_client", lambda _args: fake_client)

        args = argparse.Namespace(base_url=None, amount=200, save_card=False)
        rc = cli.cmd_credits_topup(args)
        assert rc == 0
        fake_client.account.top_up.assert_called_once_with(
            amount_usd=200, save_card=False,
        )
        assert "checkout.stripe.com" in capsys.readouterr().out


# ── CLI auto-reload validation ───────────────────────────────────────────────


class TestCmdCreditsAuto:
    def test_enable_without_threshold_amount_returns_2(self):
        args = argparse.Namespace(
            base_url=None, enable=True, disable=False,
            threshold=None, amount=None,
        )
        assert cli.cmd_credits_auto(args) == 2

    def test_enable_with_too_low_threshold_returns_2(self):
        args = argparse.Namespace(
            base_url=None, enable=True, disable=False,
            threshold=50, amount=100,
        )
        assert cli.cmd_credits_auto(args) == 2

    def test_enable_succeeds(self, monkeypatch, capsys):
        fake_cfg = mock.MagicMock(
            enabled=True, threshold_credits=5000, amount_usd=100,
        )
        fake_client = mock.MagicMock()
        fake_client.account.configure_auto_topup.return_value = fake_cfg
        monkeypatch.setattr(cli, "_make_client", lambda _args: fake_client)

        args = argparse.Namespace(
            base_url=None, enable=True, disable=False,
            threshold=5000, amount=100,
        )
        rc = cli.cmd_credits_auto(args)
        assert rc == 0
        fake_client.account.configure_auto_topup.assert_called_once_with(
            enabled=True, threshold_credits=5000, amount_usd=100,
        )
        out = capsys.readouterr().out.lower()
        assert "enabled" in out

    def test_disable_succeeds(self, monkeypatch, capsys):
        fake_cfg = mock.MagicMock(
            enabled=False, threshold_credits=None, amount_usd=None,
        )
        fake_client = mock.MagicMock()
        fake_client.account.configure_auto_topup.return_value = fake_cfg
        monkeypatch.setattr(cli, "_make_client", lambda _args: fake_client)

        args = argparse.Namespace(
            base_url=None, enable=False, disable=True,
            threshold=None, amount=None,
        )
        rc = cli.cmd_credits_auto(args)
        assert rc == 0
        assert "disabled" in capsys.readouterr().out.lower()


# ── argparse wiring ──────────────────────────────────────────────────────────


class TestArgparseWiring:
    def test_credits_bare_command_parses(self):
        parser = cli._build_parser()
        ns = parser.parse_args(["credits"])
        assert ns.command == "credits"
        assert getattr(ns, "credits_command", None) in (None, "")

    def test_credits_topup_amount_required(self):
        parser = cli._build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["credits", "top-up"])

    def test_credits_topup_with_amount(self):
        parser = cli._build_parser()
        ns = parser.parse_args(["credits", "top-up", "--amount", "200"])
        assert ns.amount == 200

    def test_credits_topup_aliases(self):
        parser = cli._build_parser()
        ns_a = parser.parse_args(["credits", "topup", "--amount", "50"])
        ns_b = parser.parse_args(["credits", "buy", "--amount", "50"])
        assert ns_a.amount == 50
        assert ns_b.amount == 50

    def test_credits_auto_reload_requires_enable_or_disable(self):
        parser = cli._build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["credits", "auto-reload"])

    def test_credits_auto_disable_no_threshold(self):
        parser = cli._build_parser()
        ns = parser.parse_args(["credits", "auto-reload", "--disable"])
        assert ns.disable is True
        assert ns.enable is False
