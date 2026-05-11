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

    return parser


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
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
