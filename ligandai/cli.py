# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Command-line interface for the LIGANDAI SDK.

Entry point: ``ligandai`` (registered in pyproject.toml).

Sub-commands
------------
keys mint    Mint a fresh wallet of rotating single-use JWTs.
keys status  Show the current wallet: path, count, target_hash, scope.
keys revoke  Delete the local wallet file (does NOT call the backend).
campaign run       Run a declarative campaign spec until its stop metrics are met.
campaign validate  Check a spec without spending anything.
campaign explain   Print the resolved spec and what would be queued first.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# ─── Helpers ─────────────────────────────────────────────────────────────────


def _print_err(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


# ── exit codes ───────────────────────────────────────────────────────────────
# ⛔ Every one of these used to be `1`, and three incomplete sub-commands
# exited `0`. A script or agent driving this CLI could not tell "your key is
# wrong" from "the network is down" from "you typed half a command and I did
# nothing" — and in the last case `$?` actively said SUCCESS. Distinct causes
# get distinct codes so a caller can branch without parsing prose.
# [bd-dre-rp11j.3]
EXIT_OK = 0
EXIT_FAILED = 1          # the operation ran and failed
EXIT_USAGE = 2           # bad/incomplete invocation — nothing was attempted
EXIT_AUTH = 3            # 401/403/tier/legal — identity or permission
EXIT_INCOMPLETE = 4      # ran, did real work, did not reach the goal
EXIT_UNREACHABLE = 5     # could not look: connection, CDN block, unreadable body
EXIT_CREDITS = 6         # 402 — out of credits / payment required
EXIT_CONFIG = 7          # the caller's configuration is unusable


def _fail(action: str, exc: BaseException) -> int:
    """Print an ACTIONABLE failure and return the exit code for its cause.

    ⛔ The CLI previously wrote `error: {action}: {exc}` and returned 1 for
    every cause alike. With the transport now raising typed, self-describing
    errors, the useful thing a CLI can add is (a) not hiding the type, and
    (b) turning the cause into an exit code a script can branch on. It must
    never swallow: `str(exc)` on these classes is multi-line and already says
    what was attempted, what went wrong, and what to do.
    """
    from ligandai.errors import (
        LigandAIAuthError,
        LigandAIConnectionError,
        LigandAICreditError,
        LigandAIEdgeBlocked,
        LigandAIForbidden,
        LigandAIInvalidConfig,
        LigandAILegalAcceptanceRequired,
        LigandAIPaidTierRequired,
        LigandAITierError,
        LigandAIUnreadableResponse,
    )

    print(f"error: {action} failed.", file=sys.stderr)
    print(f"  cause: {type(exc).__name__}", file=sys.stderr)
    for line in str(exc).splitlines() or ["<no message>"]:
        print(f"  {line}", file=sys.stderr)

    rid = getattr(exc, "request_id", None)
    if rid:
        print(f"  request id: {rid}", file=sys.stderr)

    if isinstance(exc, LigandAILegalAcceptanceRequired):
        print("  next step: run `ligandai legal accept`.", file=sys.stderr)
        return EXIT_AUTH
    if isinstance(exc, (LigandAIEdgeBlocked, LigandAIConnectionError, LigandAIUnreadableResponse)):
        return EXIT_UNREACHABLE
    if isinstance(exc, LigandAICreditError):
        return EXIT_CREDITS
    if isinstance(exc, (LigandAIPaidTierRequired, LigandAITierError)):
        return EXIT_AUTH
    if isinstance(exc, (LigandAIAuthError, LigandAIForbidden)):
        print(
            "  note: this is an identity/permission failure, not a network one. "
            "Check LIGANDAI_API_KEY.",
            file=sys.stderr,
        )
        return EXIT_AUTH
    if isinstance(exc, LigandAIInvalidConfig):
        return EXIT_CONFIG
    return EXIT_FAILED


class _VersionAction(argparse.Action):
    """Print the version unwrapped, plus a provenance note when the version
    could not be determined unambiguously.

    argparse's built-in ``action="version"`` runs the string through
    :class:`~argparse.HelpFormatter`, which re-wraps it to the terminal width
    and can split a version token across lines. The provenance note is the
    part that must stay readable, so it is written directly.
    """

    def __init__(self, option_strings, dest, help=None):
        super().__init__(option_strings, dest, nargs=0, help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        from ligandai._version import __version__, __version_provenance__

        print(f"ligandai {__version__}")
        if __version_provenance__:
            # Not a silent pick between two candidate versions: say which one
            # was reported and why the other was rejected.
            print(f"  ! version provenance: {__version_provenance__}", file=sys.stderr)
        parser.exit(0)


def _incomplete(parser: argparse.ArgumentParser, command: str) -> int:
    """An incomplete sub-command did NOTHING — say so and exit non-zero.

    ⛔ The original wrote `parser.parse_args([command, "--help"])` and then
    `return 1`. argparse's help action calls `sys.exit(0)` internally, so the
    `return 1` was DEAD CODE and `ligandai keys`, `ligandai campaign` and
    `ligandai legal` each printed help and exited 0 — a no-op reporting
    success to any script checking `$?`.
    """
    print(
        f"error: `ligandai {command}` is incomplete — no sub-command given, "
        f"so nothing was done.",
        file=sys.stderr,
    )
    print(f"  run `ligandai {command} --help` to see the sub-commands.", file=sys.stderr)
    return EXIT_USAGE


def _require_env_key() -> str | None:
    """Read the API key from env without printing it."""
    return (
        os.environ.get("LIGANDAI_API_KEY")
        or os.environ.get("LIGANDAL_API_KEY")
        or os.environ.get("LIGANDAI_TEST_API_KEY")
    )


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
        from ligandai.client import API_KEY_HELP
        _print_err(
            "LIGANDAI_API_KEY is not set, so there is no credential to mint with.\n"
            "  the SDK reads : LIGANDAI_API_KEY, LIGANDAL_API_KEY, LIGANDAI_TEST_API_KEY\n"
            f"  get a key     : {API_KEY_HELP}"
        )
        return EXIT_AUTH

    # One resolver for the whole CLI. This line, _make_client, and
    # cmd_legal_accept each used to resolve the base URL their OWN way, so
    # `ligandai legal accept` could check status against the public edge while
    # printing an acceptance URL for a local origin. [bd-dre-rp11j.3]
    from ligandai.client import _resolve_base_url
    base_url = _resolve_base_url(args.base_url)
    scope = args.scope
    target = args.target
    count = args.count

    print(f"Minting {count} JWT(s) [scope={scope}] …")

    try:
        client = LigandAI(api_key=api_key, base_url=base_url)
        wallet = client.mint_wallet(scope=scope, target_seq=target, count=count)
    except Exception as exc:
        return _fail("minting a key wallet", exc)

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
    # ⛔ [bd-dre-rp11j.1] `ligandai --version` exited 2 with an argparse usage
    # dump — byte-identical to what a genuine typo produces. It is the first
    # thing anyone types when filing a bug, and the ONLY version affordance a
    # CLI user has: `pip show ligandai` answers for the environment, not for
    # the binary actually on PATH, and those differ whenever more than one
    # venv is in play.
    parser.add_argument(
        "--version",
        action=_VersionAction,
        help="Print the installed SDK version and exit.",
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

    # ── campaign ──────────────────────────────────────────────────────────────
    campaign_parser = sub.add_parser(
        "campaign",
        help="Run a declarative campaign spec until its stop metrics are met.",
        description=(
            "A campaign spec is one YAML/JSON file naming the target, construct, "
            "cofactors, off-target paralogs, peptide classes, gates, and the STOP "
            "METRICS that define done. `campaign run` generates, folds, scores, "
            "checks the stop metrics, and queues whatever would close the ones that "
            "are unmet -- until they are all met, the credit cap is reached, or "
            "progress stalls."
        ),
    )
    campaign_sub = campaign_parser.add_subparsers(
        dest="campaign_command", title="campaign sub-commands")

    cr = campaign_sub.add_parser("run", help="Run the campaign.")
    cr.add_argument("spec", help="Path to the campaign spec (.yaml or .json).")
    cr.add_argument("--out", default=None,
                    help="Output directory (default: the spec's output_dir).")
    cr.add_argument("--dry-run", action="store_true",
                    help="Plan and cost every iteration; submit nothing, invent nothing.")
    cr.add_argument("--max-credits", type=int, default=None,
                    help="Lower the spec's credit cap for this run. Cannot raise it.")
    cr.add_argument("--timeout", type=float, default=7200.0,
                    help="Per-job wait timeout in seconds (default 7200).")
    cr.add_argument("--poll-interval", type=float, default=15.0,
                    help="Job poll interval in seconds (default 15).")

    cv = campaign_sub.add_parser("validate",
                                 help="Check a spec is runnable. Spends nothing.")
    cv.add_argument("spec", help="Path to the campaign spec.")
    cv.add_argument("--json", action="store_true", help="Also print the resolved spec.")

    ce = campaign_sub.add_parser(
        "explain", help="Show what the first iteration would queue, and its cost.")
    ce.add_argument("spec", help="Path to the campaign spec.")
    ce.add_argument("--out", default=None, help="Output directory.")

    # ── legal ─────────────────────────────────────────────────────────────────
    legal_parser = sub.add_parser(
        "legal",
        help="Review + accept the Terms of Service / EULA (docs/clickwrap_legal_acceptance.md).",
    )
    legal_sub = legal_parser.add_subparsers(dest="legal_command", title="legal sub-commands")

    # legal status
    legal_status_parser = legal_sub.add_parser(
        "status",
        help="Show current ToS/EULA acceptance status.",
    )
    legal_status_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit raw JSON instead of a formatted summary.",
    )

    # legal accept
    legal_accept_parser = legal_sub.add_parser(
        "accept",
        help="Accept the current ToS/EULA via the browser-redirect flow.",
    )
    legal_accept_parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open a browser automatically; just print the acceptance URL and poll.",
    )
    legal_accept_parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        metavar="SECS",
        help="Max seconds to wait for acceptance to be recorded (default: 300).",
    )

    return parser


# ─── credits commands ─────────────────────────────────────────────────────────


def _make_client(args: argparse.Namespace):
    """Construct a :class:`~ligandai.LigandAI` client honoring --base-url and
    env-based auth (``LIGANDAI_API_KEY`` / ``LIGANDAI_TEST_API_KEY``).

    Imported lazily so unit tests can patch this without dragging in the full
    transport stack.

    Note: this previously imported a nonexistent ``LigandAIClient`` name (no
    such export ever existed in ``ligandai/__init__.py``), so every CLI
    command that reached this helper — ``credits`` included — raised
    ``ImportError`` before making a single request. Fixed here as a
    prerequisite for `ligandai legal status`/`accept`, which call it too.
    """
    from ligandai import LigandAI

    kwargs: dict[str, Any] = {}
    base_url = getattr(args, "base_url", None)
    if base_url:
        kwargs["base_url"] = base_url
    return LigandAI(**kwargs)


def _format_progress_bar(pct: int, width: int = 40) -> str:
    """ASCII progress bar matching a compact terminal style.

    Filled cells show used spend; empty cells show remaining headroom.
    """
    pct = max(0, min(100, int(pct)))
    filled = round(width * pct / 100)
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
        "    2. Buy more  (ligandai credits top-up --amount 50)",
        f"    3. {'Disable' if auto else 'Enable'} auto-reload",
    ]
    return "\n".join(lines)


def cmd_credits_widget(args: argparse.Namespace) -> int:
    """Default `ligandai credits` — render the billing widget."""
    try:
        client = _make_client(args)
        widget = client.account.widget()
    except Exception as exc:
        return _fail("`ligandai credits` (fetching the balance)", exc)
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
        return _fail("`ligandai credits top-up`", exc)
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
        return _fail("`ligandai credits auto-reload`", exc)
    print(
        f"Auto-reload {'enabled' if cfg.enabled else 'disabled'} "
        f"(threshold={cfg.threshold_credits:,} cr, amount=${cfg.amount_usd})"
        if cfg.enabled
        else "Auto-reload disabled."
    )
    return 0


# ─── legal commands ────────────────────────────────────────────────────────


def cmd_legal_status(args: argparse.Namespace) -> int:
    """`ligandai legal status`."""
    try:
        client = _make_client(args)
        status = client.legal.status()
    except Exception as exc:
        return _fail("`ligandai legal status`", exc)

    if getattr(args, "json", False):
        print(json.dumps(status, indent=2))
        return 0

    tos_ok = bool(status.get("tosAccepted"))
    eula_ok = bool(status.get("eulaAccepted"))
    current = status.get("currentVersions") or {}
    accepted = status.get("userAcceptedVersions") or {}
    print(
        f"  ToS  : {'accepted' if tos_ok else 'NOT accepted':<13} "
        f"(current v{current.get('tos')}, yours v{accepted.get('tos') or '-'})"
    )
    print(
        f"  EULA : {'accepted' if eula_ok else 'NOT accepted':<13} "
        f"(current v{current.get('eula')}, yours v{accepted.get('eula') or '-'})"
    )
    if status.get("needsReAcceptance"):
        print("  A newer version is available. Run `ligandai legal accept`.")
    elif not (tos_ok and eula_ok):
        print("  Run `ligandai legal accept` to accept.")
    return 0


def cmd_legal_accept(args: argparse.Namespace) -> int:
    """`ligandai legal accept` — browser-redirect + poll-and-resume flow.

    Builds the same ``/legal/accept?channel=sdk_redirect`` URL the server's
    403 would carry (docs/clickwrap_legal_acceptance.md §3.6), then reuses
    :meth:`ligandai.resources.legal.Legal.resolve_interactively` to open it
    (unless ``--no-browser``) and poll ``GET /api/legal/status`` until the
    user accepts or ``--timeout`` elapses. Headless/CI-safe: if this process
    can't hand a human a browser click,
    :func:`ligandai._headless.is_noninteractive_environment` makes
    ``resolve_interactively`` fail fast with the URL rather than hang.
    """
    from ligandai._constants import DEFAULT_LEGAL_POLL_TIMEOUT_SECS
    from ligandai.errors import LigandAILegalAcceptanceRequired

    try:
        client = _make_client(args)
        status = client.legal.status()
    except Exception as exc:
        return _fail("`ligandai legal accept` (checking acceptance status)", exc)

    tos_ok = bool(status.get("tosAccepted"))
    eula_ok = bool(status.get("eulaAccepted"))
    if tos_ok and eula_ok and not status.get("needsReAcceptance"):
        print("Already accepted — nothing to do.")
        return 0

    # Same resolver the client used above — otherwise this prints an
    # acceptance URL for a different host than the status check just queried.
    from ligandai.client import _resolve_base_url
    base_url = _resolve_base_url(getattr(args, "base_url", None))
    acceptance_url = f"{base_url.rstrip('/')}/legal/accept?channel=sdk_redirect"
    err = LigandAILegalAcceptanceRequired(
        "You must accept the Terms of Service and End User License Agreement "
        f"to continue. Accept at: {acceptance_url}",
        acceptance_url=acceptance_url,
        needs_tos=not tos_ok,
        needs_eula=not eula_ok,
    )
    print(f"Open this URL to review and accept: {acceptance_url}")

    try:
        client.legal.resolve_interactively(
            err,
            auto_open_browser=not getattr(args, "no_browser", False),
            timeout=getattr(args, "timeout", None) or DEFAULT_LEGAL_POLL_TIMEOUT_SECS,
        )
    except LigandAILegalAcceptanceRequired:
        print(
            "Did not detect acceptance before timing out (or this is a "
            "non-interactive/headless environment). Open the URL above and "
            "run `ligandai legal accept` again once you've accepted.",
            file=sys.stderr,
        )
        return 1

    print("Accepted. Thank you.")
    return 0



# ─── campaign ────────────────────────────────────────────────────────────────


def _load_spec(path: str):
    from ligandai.campaign import CampaignSpec, SpecError

    try:
        return CampaignSpec.load(path)
    except SpecError as exc:
        _print_err(f"{path}: {exc}")
        return None
    except FileNotFoundError:
        _print_err(f"no such spec file: {path}")
        return None


def cmd_campaign_validate(args: argparse.Namespace) -> int:
    """ligandai campaign validate my_campaign.yaml"""
    spec = _load_spec(args.spec)
    if spec is None:
        return 2
    warnings = spec.validate()
    print(f"spec OK: {spec.name}")
    print(f"  fingerprint       {spec.fingerprint()[:16]}")
    print(f"  target            {spec.target.gene} / {spec.target.construct.name} "
          f"({len(spec.target.construct.sequence)} aa)")
    if spec.target.cofactors:
        print(f"  cofactors         {', '.join(c.name for c in spec.target.cofactors)}")
    if spec.off_targets:
        print("  off-targets       " + ", ".join(
            f"{o.gene} (margin >= {o.min_margin})" for o in spec.off_targets))
    print("  peptide classes   " + ", ".join(
        f"{p.name} {p.topology} {p.length_range[0]}-{p.length_range[1]}aa x{p.n_per_round}"
        for p in spec.peptide_classes))
    print("  gates             " + "; ".join(
        f"{g.metric} {g.op} {g.threshold:g}" for g in spec.gates))
    print(f"  certification     fold-failure rate, Wilson "
          f"{spec.reliability.confidence:.0%} upper bound <= "
          f"{spec.reliability.certification_bar}")
    print("  stop metrics:")
    for sm in spec.stop_metrics:
        print(f"    - {sm.id}: {sm.kind} target {sm.target:g} on {sm.of_population}")
    b = spec.budget
    print(f"  budget            {b.max_credits:,} credits, <= {b.max_iterations} "
          f"iterations, <= {b.max_wall_clock_hours}h; stop after "
          f"{b.no_progress.patience_iterations} iterations closing < "
          f"{b.no_progress.min_absolute_progress:g}")
    for w in warnings:
        print(f"  ! warning: {w}")
    if args.json:
        print(json.dumps(spec.as_dict(), indent=2, default=str))
    return 0


def cmd_campaign_explain(args: argparse.Namespace) -> int:
    """ligandai campaign explain my_campaign.yaml — plan without submitting."""
    from ligandai.campaign import CampaignEngine, DryRunPlatform

    spec = _load_spec(args.spec)
    if spec is None:
        return 2
    engine = CampaignEngine(spec, DryRunPlatform(),
                            output_dir=args.out or spec.output_dir)
    engine.run(dry_run=True)
    return 0


def cmd_campaign_run(args: argparse.Namespace) -> int:
    """ligandai campaign run my_campaign.yaml"""
    from ligandai.campaign import CampaignEngine, DryRunPlatform, SDKPlatform, StopReason

    spec = _load_spec(args.spec)
    if spec is None:
        return 2
    if args.max_credits is not None:
        if args.max_credits > spec.budget.max_credits:
            _print_err(
                f"--max-credits {args.max_credits:,} exceeds the spec's cap of "
                f"{spec.budget.max_credits:,}. The flag may only LOWER the cap."
            )
            return 2
        spec.budget.max_credits = int(args.max_credits)

    if args.dry_run:
        platform = DryRunPlatform()
    else:
        api_key = _require_env_key()
        if not api_key:
            from ligandai.client import API_KEY_HELP
            _print_err(
                "LIGANDAI_API_KEY is not set, so nothing can be submitted.\n"
                "  the SDK reads : LIGANDAI_API_KEY, LIGANDAL_API_KEY, "
                "LIGANDAI_TEST_API_KEY\n"
                f"  get a key     : {API_KEY_HELP}\n"
                "  or            : re-run with --dry-run to plan and cost the "
                "campaign without submitting anything."
            )
            return EXIT_AUTH
        try:
            client = _make_client(args)
        except Exception as exc:
            return _fail("creating an API client for the campaign run", exc)
        platform = SDKPlatform(client, timeout=args.timeout,
                               poll_interval=args.poll_interval,
                               progress=lambda m: print(f"    {m}", flush=True))

    engine = CampaignEngine(spec, platform, output_dir=args.out or spec.output_dir)
    try:
        result = engine.run(dry_run=args.dry_run)
    except KeyboardInterrupt:
        print("\ninterrupted — partial results are in "
              f"{engine.output_dir}", file=sys.stderr)
        return 130
    if result.stop_reason is StopReason.METRICS_MET:
        return 0
    # Every other exit is a real outcome, not a crash, but it is not success.
    return 0 if args.dry_run else 4


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
            return _incomplete(parser, "keys")
    elif args.command == "credits":
        sub = getattr(args, "credits_command", None)
        if sub in (None, ""):
            return cmd_credits_widget(args)
        elif sub in ("top-up", "topup", "buy"):
            return cmd_credits_topup(args)
        elif sub in ("auto-reload", "auto"):
            return cmd_credits_auto(args)
        else:
            return _incomplete(parser, "credits")
    elif args.command == "campaign":
        sub_cmd = getattr(args, "campaign_command", None)
        if sub_cmd == "run":
            return cmd_campaign_run(args)
        elif sub_cmd == "validate":
            return cmd_campaign_validate(args)
        elif sub_cmd == "explain":
            return cmd_campaign_explain(args)
        else:
            return _incomplete(parser, "campaign")
    elif args.command == "legal":
        sub = getattr(args, "legal_command", None)
        if sub == "status":
            return cmd_legal_status(args)
        elif sub == "accept":
            return cmd_legal_accept(args)
        else:
            return _incomplete(parser, "legal")
    else:
        # ⛔ NO SUB-COMMAND AT ALL. This used to print help and `return 0`.
        #
        # `ligandai $CMD` with an unset or empty CMD then printed usage and
        # reported SUCCESS, so a script that branches on `$?` was told the work
        # was done. It is also inconsistent with this same file's own
        # `_incomplete()`, which already exits EXIT_USAGE for `ligandai keys`,
        # `ligandai campaign` and `ligandai legal` — an invocation with no
        # sub-command is the same class of no-op as one with no sub-sub-command.
        # The Node SDK settled on the same contract for @ligandal/sdk under
        # bd-dre-rp11j.29, so the two CLIs now agree.
        #
        # `--help` / `-h` are untouched: argparse handles those before this
        # point and exits 0, which is what a caller asking for help wants.
        # [bd-dre-rp11j.28]
        print(
            "error: no command given, so nothing was done.",
            file=sys.stderr,
        )
        print("  run `ligandai --help` to see the commands.", file=sys.stderr)
        parser.print_help(sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
