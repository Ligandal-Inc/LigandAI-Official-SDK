# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Command-line interface for the LIGANDAI SDK.

Entry point: ``ligandai`` (registered in pyproject.toml).

Sub-commands
------------
keys mint    Mint a fresh wallet of rotating single-use JWTs.
keys status  Show the current wallet: path, count, target_hash, scope.
keys revoke  Delete the local wallet file (does NOT call the backend).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# ─── Helpers ─────────────────────────────────────────────────────────────────


def _print_err(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


def _require_env_key() -> str | None:
    """Read the API key from env without printing it."""
    return os.environ.get("LIGANDAI_API_KEY") or os.environ.get("LIGANDAI_TEST_API_KEY")


# ─── keys mint ───────────────────────────────────────────────────────────────


def cmd_keys_mint(args: argparse.Namespace) -> int:
    """ligandai keys mint --scope generate --target MKTAYIAKQR... --count 5"""
    try:
        from ligandai.client import LigandAI
        from ligandai.key_wallet import WALLET_PATH
    except ImportError as exc:
        _print_err(f"SDK not importable: {exc}")
        return 1

    api_key = _require_env_key()
    if not api_key:
        _print_err(
            "LIGANDAI_API_KEY environment variable is not set. "
            "Export your lgai_*_ key before running this command."
        )
        return 1

    base_url = args.base_url or os.environ.get("LIGANDAI_BASE_URL", "https://ligandai.com")
    scope = args.scope
    target = args.target
    count = args.count

    print(f"Minting {count} JWT(s) [scope={scope}] …")

    try:
        client = LigandAI(api_key=api_key, base_url=base_url)
        wallet = client.mint_wallet(scope=scope, target_seq=target, count=count)
    except Exception as exc:
        _print_err(f"Failed to mint wallet: {exc}")
        return 1

    print(f"Wallet saved to {WALLET_PATH}")
    print(f"  scope       : {wallet.scope}")
    print(f"  count       : {wallet.remaining}")
    print(f"  target_hash : {wallet.target_hash[:16]}…" if wallet.target_hash else "  target_hash : <none>")
    return 0


# ─── keys status ─────────────────────────────────────────────────────────────


def cmd_keys_status(args: argparse.Namespace) -> int:
    """ligandai keys status"""
    from ligandai.key_wallet import WALLET_PATH, _load_or_none

    wallet_path = Path(args.path) if getattr(args, "path", None) else WALLET_PATH

    wallet = _load_or_none(wallet_path)
    if wallet is None:
        print(f"No wallet found at {wallet_path}")
        print("Run: ligandai keys mint --scope generate --target <seq>")
        return 0

    target_display = (wallet.target_hash[:16] + "…") if wallet.target_hash else "<any>"
    rt_display = "present" if wallet.refresh_token else "absent"

    print(f"Wallet path   : {wallet_path}")
    print(f"Scope         : {wallet.scope or '<unknown>'}")
    print(f"Keys remaining: {wallet.remaining}")
    print(f"Target hash   : {target_display}")
    print(f"Refresh token : {rt_display}")
    print(f"User ID       : {wallet.user_id or '<unknown>'}")
    print(f"Org ID        : {wallet.org_id or '<none>'}")
    return 0


# ─── keys revoke ─────────────────────────────────────────────────────────────


def cmd_keys_revoke(args: argparse.Namespace) -> int:
    """ligandai keys revoke — deletes local wallet only (no backend call)."""
    from ligandai.key_wallet import WALLET_PATH

    wallet_path = Path(args.path) if getattr(args, "path", None) else WALLET_PATH

    if not wallet_path.exists():
        print(f"No wallet at {wallet_path}. Nothing to revoke.")
        return 0

    try:
        wallet_path.unlink()
        print(f"Wallet deleted: {wallet_path}")
        print("Note: this only removes the local file. "
              "Server-side JWTs expire after 60 s. "
              "Use the admin panel to revoke the refresh token.")
    except OSError as exc:
        _print_err(f"Failed to delete wallet: {exc}")
        return 1
    return 0


# ─── Argument parser ──────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ligandai",
        description="LIGANDAI SDK command-line interface.",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        metavar="URL",
        help="Override API base URL (default: https://ligandai.com).",
    )

    sub = parser.add_subparsers(dest="command", title="commands")

    # ── keys ──────────────────────────────────────────────────────────────────
    keys_parser = sub.add_parser("keys", help="Manage rotating-JWT key wallets.")
    keys_sub = keys_parser.add_subparsers(dest="keys_command", title="keys sub-commands")

    # keys mint
    mint_parser = keys_sub.add_parser(
        "mint",
        help="Mint a fresh wallet of single-use JWTs.",
    )
    mint_parser.add_argument(
        "--scope",
        required=True,
        choices=["generate", "fold", "score"],
        help="Scope for the wallet.",
    )
    mint_parser.add_argument(
        "--target",
        required=True,
        metavar="SEQ",
        help="Full amino-acid sequence of the target protein.",
    )
    mint_parser.add_argument(
        "--count",
        type=int,
        default=5,
        choices=range(1, 11),
        metavar="N",
        help="Number of JWTs to mint (1-10, default 5).",
    )

    # keys status
    status_parser = keys_sub.add_parser(
        "status",
        help="Show current wallet path, count, target_hash, and scope.",
    )
    status_parser.add_argument(
        "--path",
        default=None,
        metavar="FILE",
        help="Override wallet file path.",
    )

    # keys revoke
    revoke_parser = keys_sub.add_parser(
        "revoke",
        help="Delete local wallet file (does NOT call backend).",
    )
    revoke_parser.add_argument(
        "--path",
        default=None,
        metavar="FILE",
        help="Override wallet file path.",
    )

    # ── credits ───────────────────────────────────────────────────────────────
    credits_parser = sub.add_parser(
        "credits",
        help="Show balance + spend widget; top up; toggle auto-reload.",
    )
    credits_sub = credits_parser.add_subparsers(dest="credits_command", title="credits sub-commands")

    # credits (no sub) → widget. Bare `ligandai credits` is the default.
    credits_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit raw JSON instead of the styled widget.",
    )

    # credits top-up
    topup_parser = credits_sub.add_parser(
        "top-up",
        aliases=["topup", "buy"],
        help="Purchase credits ($25 minimum). Returns a Stripe checkout URL.",
    )
    topup_parser.add_argument(
        "--amount", "-a",
        type=int,
        required=True,
        metavar="USD",
        help="Dollar amount to top up (integer, min 25, max 20000).",
    )
    topup_parser.add_argument(
        "--save-card",
        action="store_true",
        help="Save the payment method for future use (enables off-session auto top-up).",
    )

    # credits auto-reload
    auto_parser = credits_sub.add_parser(
        "auto-reload",
        aliases=["auto"],
        help="Configure auto-reload (paid tiers only).",
    )
    auto_group = auto_parser.add_mutually_exclusive_group(required=True)
    auto_group.add_argument(
        "--enable",
        action="store_true",
        help="Enable auto-reload with --threshold and --amount.",
    )
    auto_group.add_argument(
        "--disable",
        action="store_true",
        help="Disable auto-reload.",
    )
    auto_parser.add_argument(
        "--threshold",
        type=int,
        metavar="CREDITS",
        help="Balance threshold (in credits) that triggers a reload. 100-100000.",
    )
    auto_parser.add_argument(
        "--amount",
        type=int,
        choices=[25, 100, 200, 500, 1000, 2000],
        metavar="USD",
        help="Dollar amount per reload event.",
    )

    return parser


# ─── credits commands ─────────────────────────────────────────────────────────


def _make_client(args: argparse.Namespace):
    """Construct a LigandAIClient honoring --base-url and env-based auth.

    Imported lazily so unit tests can patch this without dragging in the full
    transport stack.
    """
    from ligandai import LigandAIClient

    kwargs: dict[str, Any] = {}
    base_url = getattr(args, "base_url", None)
    if base_url:
        kwargs["base_url"] = base_url
    return LigandAIClient(**kwargs)


def _format_progress_bar(pct: int, width: int = 40) -> str:
    """ASCII progress bar matching a compact terminal style.

    Filled cells show used spend; empty cells show remaining headroom.
    """
    pct = max(0, min(100, int(pct)))
    filled = int(round(width * pct / 100))
    return "█" * filled + "░" * (width - filled)


def render_credits_widget(widget: Any, *, no_color: bool = False) -> str:
    """Render a CreditsWidget like a compact screenshot.

    Pure formatter — takes the widget object (or a dict-like with the same
    fields) and returns the multi-line string. Separated out so the unit
    tests can render against a fixture without an HTTP round-trip.
    """
    # Duck-typing: accept either pydantic model or dict.
    get = lambda k, default=None: getattr(widget, k, None) if hasattr(widget, k) else widget.get(k, default)  # noqa: E731
    spent = get("spent_this_month_usd", 0.0) or 0.0
    limit = get("monthly_limit_usd", 0.0) or 0.0
    pct = get("pct_used", 0) or 0
    balance_usd = get("balance_usd", 0.0) or 0.0
    auto = bool(get("auto_reload_enabled", False))
    reset = get("reset_date", None)
    # reset_date may be datetime, str, or None. Format defensively.
    if reset and hasattr(reset, "strftime"):
        reset_str = reset.strftime("%b %-d")
    elif isinstance(reset, str):
        reset_str = reset[:10]
    else:
        reset_str = "next month"

    bar = _format_progress_bar(pct)
    lines = [
        f"  ${spent:>6.2f} spent  {bar}  {pct}% used",
        f"  Resets {reset_str} · ${limit:.0f} monthly limit",
        "",
        f"  ▸ 1. ${balance_usd:.2f} balance · auto-reload {'on' if auto else 'off'}",
        f"    2. Buy more  (ligandai credits top-up --amount 50)",
        f"    3. {'Disable' if auto else 'Enable'} auto-reload",
    ]
    return "\n".join(lines)


def cmd_credits_widget(args: argparse.Namespace) -> int:
    """Default `ligandai credits` — render the billing widget."""
    try:
        client = _make_client(args)
        widget = client.account.widget()
    except Exception as exc:
        print(f"ligandai credits: failed to fetch balance: {exc}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        # Pydantic dumps via .model_dump_json on v2
        try:
            print(widget.model_dump_json(by_alias=True))
        except Exception:
            print(repr(widget))
        return 0
    print(render_credits_widget(widget))
    return 0


def cmd_credits_topup(args: argparse.Namespace) -> int:
    """`ligandai credits top-up --amount 50`."""
    if args.amount < 25 or args.amount > 20_000:
        print("--amount must be between $25 and $20000.", file=sys.stderr)
        return 2
    try:
        client = _make_client(args)
        result = client.account.top_up(
            amount_usd=args.amount,
            save_card=bool(getattr(args, "save_card", False)),
        )
    except Exception as exc:
        print(f"ligandai credits top-up: failed: {exc}", file=sys.stderr)
        return 1
    if getattr(result, "checkout_url", None):
        print(f"Complete the purchase: {result.checkout_url}")
        return 0
    if getattr(result, "success", False):
        credits = getattr(result, "credits_added", None)
        new_bal = getattr(result, "new_balance", None)
        suffix = f" → balance now {new_bal:,} credits" if new_bal else ""
        print(f"Top-up succeeded: +{credits:,} credits{suffix}")
        return 0
    print("Top-up did not complete; check Stripe setup.", file=sys.stderr)
    return 1


def cmd_credits_auto(args: argparse.Namespace) -> int:
    """`ligandai credits auto-reload --enable --threshold 5000 --amount 100`."""
    if args.enable:
        if not args.threshold or not args.amount:
            print("--enable requires both --threshold and --amount.", file=sys.stderr)
            return 2
        if args.threshold < 100 or args.threshold > 100_000:
            print("--threshold must be 100-100000 credits.", file=sys.stderr)
            return 2
    try:
        client = _make_client(args)
        cfg = client.account.configure_auto_topup(
            enabled=bool(args.enable),
            threshold_credits=args.threshold or 10_000,
            amount_usd=args.amount or 200,
        )
    except Exception as exc:
        print(f"ligandai credits auto-reload: failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"Auto-reload {'enabled' if cfg.enabled else 'disabled'} "
        f"(threshold={cfg.threshold_credits:,} cr, amount=${cfg.amount_usd})"
        if cfg.enabled
        else f"Auto-reload disabled."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "keys":
        if args.keys_command == "mint":
            return cmd_keys_mint(args)
        elif args.keys_command == "status":
            return cmd_keys_status(args)
        elif args.keys_command == "revoke":
            return cmd_keys_revoke(args)
        else:
            parser.parse_args(["keys", "--help"])
            return 1
    elif args.command == "credits":
        sub = getattr(args, "credits_command", None)
        if sub in (None, ""):
            return cmd_credits_widget(args)
        elif sub in ("top-up", "topup", "buy"):
            return cmd_credits_topup(args)
        elif sub in ("auto-reload", "auto"):
            return cmd_credits_auto(args)
        else:
            parser.parse_args(["credits", "--help"])
            return 1
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
