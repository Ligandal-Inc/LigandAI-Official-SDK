# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Rotating single-use JWT wallet for the LIGANDAI SDK.

``~/.ligandai/keys.json`` (mode 0600) holds a small wallet of pre-minted
JWTs.  Each call to a scoped endpoint pops one JWT, sends it as the Bearer
token, and auto-refreshes from the server when the supply runs low.

Hash parity note
----------------
The canonical hash function here is the byte-for-byte Python mirror of Track
D's TypeScript ``canonicalTargetHash`` (require-scoped-key.ts line 176):

    const canonical = (seq || '').replace(/\\s+/g, '').toUpperCase();
    return crypto.createHash('sha256').update(canonical).digest('hex');

and its FastAPI mirror (scoped_key.py line 121):

    canonical = "".join((seq or "").split()).upper()
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

``KeyWallet.canonicalize_target`` and ``compute_target_hash`` implement the
same logic.  Any divergence is a hard bug — block and ask.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ligandai.errors import (
    KeyTargetMismatch,
    LigandAIScopeError,
    WalletEmpty,
)

if TYPE_CHECKING:
    # Avoid circular import; client references wallet, wallet references client
    # only in type annotations (used for the refresh() call).
    pass

logger = logging.getLogger("ligandai.wallet")

# ─── Constants ────────────────────────────────────────────────────────────────

WALLET_DIR = Path.home() / ".ligandai"
WALLET_PATH = WALLET_DIR / "keys.json"
WALLET_SCHEMA_VERSION = 1

# Trigger an async-refresh when the wallet drops below this many JWTs.
_REFRESH_THRESHOLD = 2


# ─── Schema ──────────────────────────────────────────────────────────────────
#
# {
#   "version": 1,
#   "issued_at": "2026-05-09T12:00:00Z",   (ISO-8601 string, informational)
#   "scope": "generate",
#   "target_hash": "sha256hex...",
#   "wallet": ["jwt1", "jwt2", ...],
#   "refresh_token": "jwt...",
#   "user_id": "uid_...",
#   "org_id": "org_..." | null
# }


class KeyWallet:
    """In-process view of the JWT wallet persisted at ``~/.ligandai/keys.json``.

    Typical usage (managed automatically by :class:`~ligandai.LigandAI`):

    .. code-block:: python

        wallet = KeyWallet.load()
        jwt = wallet.next_key()
        response = http_client.post(url, headers={"Authorization": f"Bearer {jwt}"})
        wallet.handle_response(response)
        wallet.save()

    The wallet is **not thread-safe**.  The SDK serializes wallet operations
    within a single client instance.
    """

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    # ─── Persistence ─────────────────────────────────────────────────────────

    @classmethod
    def load(cls, path: Path = WALLET_PATH) -> KeyWallet:
        """Load wallet from *path* (default ``~/.ligandai/keys.json``).

        Raises
        ------
        PermissionError
            If the wallet file is world-readable (mode & 0o004 != 0).
        FileNotFoundError
            If no wallet file exists yet.
        ValueError
            If the file cannot be parsed as a valid wallet.
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Wallet not found at {path}. Call client.mint_wallet() first.")

        # Security: refuse world-readable wallets on POSIX systems
        if os.name != "nt":
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode & stat.S_IROTH:
                raise PermissionError(
                    f"Wallet at {path} is world-readable (mode={oct(mode)}). "
                    "Fix with: chmod 600 ~/.ligandai/keys.json"
                )

        with path.open("r", encoding="utf-8") as fh:
            raw = fh.read()

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Wallet at {path} is not valid JSON: {exc}") from exc

        if not isinstance(data, dict):
            raise ValueError(f"Wallet at {path} has unexpected shape (not a JSON object)")

        if data.get("version") != WALLET_SCHEMA_VERSION:
            raise ValueError(
                f"Wallet schema version {data.get('version')!r} != expected {WALLET_SCHEMA_VERSION}"
            )

        if not isinstance(data.get("wallet"), list):
            raise ValueError("Wallet JSON missing 'wallet' list")

        return cls(data)

    def save(self, path: Path = WALLET_PATH) -> None:
        """Atomic write: tmpfile + os.rename, enforces mode 0600."""
        path = Path(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

        # Write to a temp file in the same directory so os.rename is atomic
        # (same filesystem).  We open with mode 0600 before writing any data.
        fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=".keys_tmp_", suffix=".json")
        try:
            os.chmod(tmp_path, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.rename(tmp_path, path)
        except Exception:
            # Best-effort cleanup; don't shadow the real exception.
            with suppress(OSError):
                os.unlink(tmp_path)
            raise

        # Double-check permissions after rename (some OS/FS ignore mkstemp mode).
        if os.name != "nt":
            os.chmod(path, 0o600)

    # ─── Key consumption ─────────────────────────────────────────────────────

    @property
    def remaining(self) -> int:
        """Number of JWTs currently in the wallet."""
        return len(self._data.get("wallet", []))

    @property
    def scope(self) -> str:
        """Scope this wallet was minted for (``"generate"``, ``"fold"``, ``"score"``)."""
        return self._data.get("scope", "")

    @property
    def target_hash(self) -> str:
        """SHA-256 hex of the canonical target sequence."""
        return self._data.get("target_hash", "")

    @property
    def refresh_token(self) -> str:
        """Refresh JWT for minting additional single-use keys."""
        return self._data.get("refresh_token", "")

    @property
    def user_id(self) -> str:
        return self._data.get("user_id", "")

    @property
    def org_id(self) -> str | None:
        return self._data.get("org_id")

    def next_key(self) -> str:
        """Pop and return the next single-use JWT.

        Raises
        ------
        WalletEmpty
            When no JWTs remain.
        """
        wallet_list: list = self._data.setdefault("wallet", [])
        if not wallet_list:
            raise WalletEmpty(
                "Wallet is empty. Call client.mint_wallet() to obtain a new wallet, "
                "or ensure the wallet was refreshed before all keys were consumed."
            )
        return wallet_list.pop(0)

    def handle_response(self, response: Any) -> None:
        """Consume the ``X-Next-Key`` header from a scoped-endpoint response.

        The server may pre-issue an additional JWT in ``X-Next-Key`` so the
        SDK can avoid a round-trip refresh. If present, append it to the
        wallet.

        Parameters
        ----------
        response
            An ``httpx.Response`` (or any object with a ``.headers`` mapping).
        """
        headers = getattr(response, "headers", {}) or {}
        # httpx headers are case-insensitive; also handle plain dicts.
        next_key = (
            headers.get("x-next-key")
            or headers.get("X-Next-Key")
        )
        if next_key and isinstance(next_key, str) and next_key.strip():
            self._data.setdefault("wallet", []).append(next_key.strip())
            logger.debug("X-Next-Key appended; wallet now has %d JWTs", self.remaining)

    # ─── Refresh ─────────────────────────────────────────────────────────────

    def refresh(self, client: Any, count: int = 5) -> None:
        """POST ``refresh_token`` to ``/api/auth/scoped-key/refresh``.

        Replaces the wallet list in-place and updates ``refresh_token`` when
        the server rotates it.  Caller must call :meth:`save` afterwards.

        Parameters
        ----------
        client
            A :class:`~ligandai.LigandAI` or :class:`~ligandai._http.HTTPTransport`
            instance with a ``.request()`` method.
        count
            Number of fresh JWTs to request (1-10).

        Raises
        ------
        WalletEmpty
            Refresh call failed and no JWTs remain.
            A transport-layer auth error if the refresh token expired or was
            revoked server-side.
        """
        if not self.refresh_token:
            raise WalletEmpty(
                "No refresh_token in wallet. Call client.mint_wallet() to start a new wallet."
            )

        transport = getattr(client, "_transport", client)
        payload = {
            "refresh_token": self.refresh_token,
            "count": count,
            "rotate_refresh": True,
        }

        try:
            resp = transport.request("POST", "/api/auth/scoped-key/refresh", json=payload)
        except Exception as exc:
            # Surface as WalletEmpty so callers get a consistent signal.
            raise WalletEmpty(
                f"Wallet refresh request failed: {exc}. "
                "Call client.mint_wallet() to start a new wallet."
            ) from exc

        # resp may be None on errors that error_from_response raised, but the
        # transport raises on 4xx/5xx so we only land here on success.
        if not resp or not isinstance(resp.get("wallet") if isinstance(resp, dict) else None, list):
            raise WalletEmpty("Refresh response missing 'wallet' list.")

        new_wallets: list[str] = resp["wallet"]
        self._data["wallet"] = new_wallets

        # Rotate refresh token if server provided a new one.
        new_rt = resp.get("refresh_token")
        if new_rt and isinstance(new_rt, str):
            self._data["refresh_token"] = new_rt

        logger.debug("Wallet refreshed; %d JWTs loaded", len(new_wallets))

    # ─── Hash helpers (hash parity with Track D) ─────────────────────────────

    @staticmethod
    def canonicalize_target(seq: str) -> str:
        """Strip whitespace, uppercase, remove non-amino-acid characters.

        This is the exact Python mirror of Track D's TypeScript:

            const canonical = (seq || '').replace(/\\s+/g, '').toUpperCase();

        and the FastAPI scoped_key.py mirror:

            canonical = "".join((seq or "").split()).upper()

        Only whitespace is stripped — non-amino-acid characters (digits,
        dashes, etc.) are **retained** to preserve exact server-side parity.
        The server hashes the sequence after only whitespace removal.
        """
        return "".join((seq or "").split()).upper()

    @classmethod
    def compute_target_hash(cls, seq: str) -> str:
        """SHA-256 hex digest of the canonicalized target sequence.

        Byte-for-byte matches ``canonicalTargetHash`` in
        ``server/middleware/require-scoped-key.ts``.
        """
        canonical = cls.canonicalize_target(seq)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # ─── Scope / target validation ───────────────────────────────────────────

    def assert_scope(self, required_scope: str) -> None:
        """Raise :class:`~ligandai.errors.LigandAIScopeError` if scopes differ."""
        if self.scope and self.scope != required_scope:
            raise LigandAIScopeError(
                f"Wallet scope is '{self.scope}' but endpoint requires '{required_scope}'. "
                "Mint a new wallet with the correct scope.",
                wallet_scope=self.scope,
                required_scope=required_scope,
            )

    def assert_target(self, target_seq: str) -> None:
        """Raise :class:`~ligandai.errors.KeyTargetMismatch` if the target hash differs."""
        if not self.target_hash:
            # No hash constraint — allow any target.
            return
        req_hash = self.compute_target_hash(target_seq)
        if req_hash != self.target_hash:
            raise KeyTargetMismatch(
                f"Wallet was minted for a different target sequence "
                f"(wallet_hash={self.target_hash[:12]}…, "
                f"request_hash={req_hash[:12]}…). "
                "Call client.mint_wallet() with the new target.",
                wallet_hash=self.target_hash,
                request_hash=req_hash,
            )

    # ─── Class constructors ───────────────────────────────────────────────────

    @classmethod
    def from_issue_response(
        cls,
        resp: dict[str, Any],
        *,
        path: Path = WALLET_PATH,
    ) -> KeyWallet:
        """Build a KeyWallet from a ``POST /api/auth/scoped-key/issue`` response dict."""
        import datetime

        data: dict[str, Any] = {
            "version": WALLET_SCHEMA_VERSION,
            "issued_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "scope": resp.get("scope", ""),
            "target_hash": resp.get("target_hash") or "",
            "wallet": resp.get("wallet", []),
            "refresh_token": resp.get("refresh_token") or "",
            "user_id": resp.get("user_id") or "",
            "org_id": resp.get("org_id"),
        }
        w = cls(data)
        w.save(path)
        return w

    # ─── Repr ────────────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        target = (self.target_hash[:12] + "…") if self.target_hash else "<any>"
        return (
            f"KeyWallet(scope={self.scope!r}, remaining={self.remaining}, "
            f"target_hash={target!r})"
        )


# ─── Low-level helpers (used by client.py) ────────────────────────────────────


def _load_or_none(path: Path = WALLET_PATH) -> KeyWallet | None:
    """Load wallet; return None on any load error (no wallet present is normal)."""
    try:
        return KeyWallet.load(path)
    except (FileNotFoundError, ValueError, PermissionError, json.JSONDecodeError):
        return None


__all__ = [
    "WALLET_DIR",
    "WALLET_PATH",
    "KeyWallet",
    "_load_or_none",
]
