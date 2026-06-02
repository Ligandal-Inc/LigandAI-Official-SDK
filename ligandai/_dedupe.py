# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Local sqlite-backed dedupe + credit-ledger for the LigandAI SDK.

Created 2026-05-17 in response to the a production duplicate-submission
incident where the SDK had no client-side dedupe, allowing 130 duplicate fold
submissions to crash through GPU slots before the server rate-limited.

Two databases under ``~/.ligandai/``:

* ``submitted.db`` — :class:`SubmittedSet`. Prevents identical
  ``client.fold(...)`` / ``client.fold_batch(...)`` calls from re-submitting
  within :data:`~ligandai._constants.DEFAULT_DEDUPE_WINDOW_SECS` (24h). Also
  tracks in-flight submissions so the SDK can enforce client-side concurrency
  caps without a round-trip.
* ``credit_ledger.db`` — :class:`CreditLedger`. Append-only log of credit
  consumption for local audit / reconciliation against server-side billing.

Files are mode ``0600``; parent dir is mode ``0700``. Stdlib only
(``sqlite3`` + ``hashlib`` + ``json`` + ``threading``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

from ligandai._constants import (
    CREDIT_LEDGER_DB_NAME,
    DEFAULT_DEDUPE_WINDOW_SECS,
    LOCAL_STATE_DIR_NAME,
    SUBMITTED_DB_NAME,
    SUBMITTED_ORPHAN_SECS,
)

_logger = logging.getLogger("ligandai.dedupe")

__all__ = [
    "SubmittedSet",
    "CreditLedger",
    "compute_submission_hash",
    "compute_api_key_hash",
    "default_state_dir",
]


def default_state_dir() -> Path:
    """Resolve ``~/.ligandai/`` and ensure it exists with mode ``0700``."""
    home = Path.home()
    d = home / LOCAL_STATE_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        # Permission set may already be correct or fs may not support chmod;
        # never fail the SDK because of permission tightening.
        pass
    return d


def _set_file_mode_0600(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _canonical_peptide(peptide_seq: str | Sequence[str]) -> str:
    """Canonicalize peptide input so order/whitespace/case don't affect the hash.

    For a single peptide string, returns the stripped uppercase sequence.
    For a list/tuple of peptide strings, returns the joined sorted-unique
    sequences separated by ``|`` — so ``["ACDE", "PQRS"]`` and
    ``["PQRS", "ACDE"]`` and ``["ACDE", "PQRS", "ACDE"]`` all hash identically.
    """
    if isinstance(peptide_seq, str):
        return peptide_seq.strip().upper()
    cleaned = [str(p).strip().upper() for p in peptide_seq if str(p).strip()]
    # Sorted + unique so reordering / duplicates collapse.
    return "|".join(sorted(set(cleaned)))


def compute_submission_hash(
    *,
    peptide_seq: str | Sequence[str],
    receptor_seq: str,
    gpu: str,
    params: dict[str, Any] | None = None,
) -> str:
    """SHA-256 of the canonical submission tuple.

    Returns a 64-char hex digest. The hash is stable under:

    * Peptide list reordering / case / leading-trailing whitespace.
    * Receptor sequence case / whitespace.
    * Dict key ordering in ``params`` (we sort keys before JSON-dumping).

    The hash CHANGES when:

    * Any peptide is added or removed.
    * The receptor sequence differs by even one residue.
    * Any param value differs (``diffusion_samples``, ``sampling_steps``,
      etc.).
    * The GPU type differs (but in practice the SDK only forwards
      ``b200_plus``, so this is informational).
    """
    canon_pep = _canonical_peptide(peptide_seq)
    canon_rec = (receptor_seq or "").strip().upper()
    canon_gpu = (gpu or "").strip().lower()
    canon_params = json.dumps(
        params or {}, sort_keys=True, separators=(",", ":"), default=str,
    )
    blob = "\x1f".join((canon_pep, canon_rec, canon_gpu, canon_params)).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def compute_api_key_hash(api_key: str | None) -> str:
    """Truncated SHA-256 of the API key (first 16 hex chars).

    Empty string for ``None``. Never invertible; safe to store and log.
    """
    if not api_key:
        return ""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


# ─── SubmittedSet ───────────────────────────────────────────────────────────


class SubmittedSet:
    """Sqlite-backed dedupe + concurrency-tracker for SDK submissions.

    Stored at ``~/.ligandai/submitted.db`` (mode 0600).
    """

    _SCHEMA = """
        CREATE TABLE IF NOT EXISTS submissions (
            submission_hash   TEXT NOT NULL,
            api_key_hash      TEXT NOT NULL,
            job_id            TEXT,
            status            TEXT NOT NULL,
            gpu               TEXT NOT NULL,
            kind              TEXT NOT NULL,
            submitted_at      REAL NOT NULL,
            completed_at      REAL,
            estimated_credits INTEGER,
            actual_credits    INTEGER,
            meta              TEXT,
            PRIMARY KEY (submission_hash, api_key_hash)
        );
    """
    _INDEX = """
        CREATE INDEX IF NOT EXISTS ix_subs_keystatus_time
        ON submissions(api_key_hash, status, submitted_at);
    """

    def __init__(self, db_path: Path | None = None) -> None:
        if db_path is None:
            db_path = default_state_dir() / SUBMITTED_DB_NAME
        else:
            # Ensure parent dir exists for caller-supplied paths.
            db_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(db_path.parent, 0o700)
            except OSError:
                pass
        self._path = Path(db_path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self._path),
            check_same_thread=False,
            isolation_level=None,  # autocommit; we manage transactions explicitly
        )
        self._conn.row_factory = sqlite3.Row
        # Pragmas: WAL is friendly to multiple processes; busy_timeout avoids
        # transient SQLITE_BUSY when the recovery worker and the SDK race.
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=2000")
        except sqlite3.Error:
            pass
        with self._lock:
            self._conn.execute(self._SCHEMA)
            self._conn.execute(self._INDEX)
        _set_file_mode_0600(self._path)
        # Opportunistic stale-row purge — never blocks the caller; failures
        # are swallowed (we don't want a broken purge to break submissions).
        try:
            self.purge_stale()
        except Exception:  # noqa: BLE001 — best-effort housekeeping only
            pass

    # ------ static hashing helpers (also exposed as module functions) ----

    @staticmethod
    def compute_hash(
        *,
        peptide_seq: str | Sequence[str],
        receptor_seq: str,
        gpu: str,
        params: dict[str, Any] | None = None,
    ) -> str:
        return compute_submission_hash(
            peptide_seq=peptide_seq,
            receptor_seq=receptor_seq,
            gpu=gpu,
            params=params,
        )

    @staticmethod
    def hash_api_key(api_key: str | None) -> str:
        return compute_api_key_hash(api_key)

    # ------ public API ---------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    def lookup(
        self,
        submission_hash: str,
        api_key_hash: str,
        *,
        window_secs: int = DEFAULT_DEDUPE_WINDOW_SECS,
    ) -> dict[str, Any] | None:
        """Return the row as a dict if a recent submission exists, else None.

        Treats ``'failed'`` rows as eligible-for-resubmit (returns ``None``).
        Treats ``'submitted'`` rows older than
        :data:`~ligandai._constants.SUBMITTED_ORPHAN_SECS` with no job_id as
        orphaned (returns ``None``) — prevents permanent lockout when the
        network drops mid-POST.
        """
        now = time.time()
        cutoff = now - max(1, window_secs)
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT submission_hash, api_key_hash, job_id, status, gpu,
                       kind, submitted_at, completed_at, estimated_credits,
                       actual_credits, meta
                  FROM submissions
                 WHERE submission_hash = ? AND api_key_hash = ?
                """,
                (submission_hash, api_key_hash),
            )
            row = cur.fetchone()
        if row is None:
            return None
        # Failed rows: eligible for retry.
        if row["status"] == "failed":
            return None
        # Orphaned submitted (no job_id) past the orphan window: ignore.
        if (
            row["status"] == "submitted"
            and not row["job_id"]
            and now - float(row["submitted_at"]) > SUBMITTED_ORPHAN_SECS
        ):
            _logger.info(
                "[SDK dedupe] ignoring orphaned 'submitted' row hash=%s… age=%.0fs",
                submission_hash[:12], now - float(row["submitted_at"]),
            )
            return None
        # Outside the dedupe window: treat as no hit.
        if float(row["submitted_at"]) < cutoff:
            return None
        # Return the row as a plain dict.
        d = dict(row)
        if d.get("meta"):
            try:
                d["meta"] = json.loads(d["meta"])
            except (TypeError, ValueError):
                pass
        return d

    def record_submission(
        self,
        submission_hash: str,
        api_key_hash: str,
        *,
        gpu: str,
        kind: str,
        estimated_credits: int | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        """Insert a new 'submitted' row, or replace a stale/failed one.

        If a 'submitted' or 'completed' row already exists within the window,
        this is a no-op (caller should have used :meth:`lookup` first).
        Failed rows are replaced.
        """
        now = time.time()
        meta_json = json.dumps(meta, sort_keys=True, default=str) if meta else None
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT status, submitted_at, job_id
                  FROM submissions
                 WHERE submission_hash = ? AND api_key_hash = ?
                """,
                (submission_hash, api_key_hash),
            )
            existing = cur.fetchone()
            should_replace = False
            if existing is None:
                should_replace = True
            elif existing["status"] == "failed":
                should_replace = True
            elif (
                existing["status"] == "submitted"
                and not existing["job_id"]
                and now - float(existing["submitted_at"]) > SUBMITTED_ORPHAN_SECS
            ):
                should_replace = True
            # Live 'submitted' / 'completed' rows → no-op (idempotent).
            if not should_replace:
                return
            self._conn.execute(
                """
                INSERT OR REPLACE INTO submissions (
                    submission_hash, api_key_hash, job_id, status, gpu, kind,
                    submitted_at, completed_at,
                    estimated_credits, actual_credits, meta
                ) VALUES (?, ?, NULL, 'submitted', ?, ?, ?, NULL, ?, NULL, ?)
                """,
                (
                    submission_hash, api_key_hash, gpu, kind, now,
                    int(estimated_credits) if estimated_credits is not None else None,
                    meta_json,
                ),
            )

    def update_job_id(
        self,
        submission_hash: str,
        api_key_hash: str,
        job_id: str,
    ) -> None:
        """Attach a server-assigned job id to a previously recorded submission."""
        if not job_id:
            return
        with self._lock:
            self._conn.execute(
                """
                UPDATE submissions
                   SET job_id = ?
                 WHERE submission_hash = ? AND api_key_hash = ?
                """,
                (str(job_id), submission_hash, api_key_hash),
            )

    def mark_completed(
        self,
        submission_hash: str,
        api_key_hash: str,
        *,
        actual_credits: int | None = None,
    ) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                UPDATE submissions
                   SET status = 'completed',
                       completed_at = ?,
                       actual_credits = COALESCE(?, actual_credits)
                 WHERE submission_hash = ? AND api_key_hash = ?
                """,
                (
                    now,
                    int(actual_credits) if actual_credits is not None else None,
                    submission_hash,
                    api_key_hash,
                ),
            )

    def mark_failed(
        self,
        submission_hash: str,
        api_key_hash: str,
        *,
        reason: str | None = None,
    ) -> None:
        """Mark a submission as failed (eligible for retry on next call)."""
        now = time.time()
        # Encode the failure reason inside meta so we don't need a schema change.
        meta_blob: str | None = None
        if reason:
            meta_blob = json.dumps({"failure_reason": str(reason)[:512]})
        with self._lock:
            if meta_blob is not None:
                self._conn.execute(
                    """
                    UPDATE submissions
                       SET status = 'failed',
                           completed_at = ?,
                           meta = COALESCE(meta, ?) -- only set if currently NULL
                     WHERE submission_hash = ? AND api_key_hash = ?
                    """,
                    (now, meta_blob, submission_hash, api_key_hash),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE submissions
                       SET status = 'failed', completed_at = ?
                     WHERE submission_hash = ? AND api_key_hash = ?
                    """,
                    (now, submission_hash, api_key_hash),
                )

    def count_in_flight(
        self,
        api_key_hash: str,
        *,
        window_secs: int = DEFAULT_DEDUPE_WINDOW_SECS,
    ) -> int:
        """Count rows with status='submitted' inside the dedupe window.

        Orphaned rows (no job_id, older than ``SUBMITTED_ORPHAN_SECS``) are
        excluded — they're treated as dead and don't count against the
        client-side concurrency cap.
        """
        now = time.time()
        cutoff = now - max(1, window_secs)
        orphan_cutoff = now - SUBMITTED_ORPHAN_SECS
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT COUNT(*) AS n
                  FROM submissions
                 WHERE api_key_hash = ?
                   AND status = 'submitted'
                   AND submitted_at >= ?
                   AND NOT (job_id IS NULL AND submitted_at < ?)
                """,
                (api_key_hash, cutoff, orphan_cutoff),
            )
            row = cur.fetchone()
        return int(row["n"]) if row else 0

    def list_in_flight(
        self,
        api_key_hash: str,
        *,
        window_secs: int = DEFAULT_DEDUPE_WINDOW_SECS,
    ) -> list[dict[str, Any]]:
        """Return in-flight submission rows for inspection / debugging."""
        now = time.time()
        cutoff = now - max(1, window_secs)
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT * FROM submissions
                 WHERE api_key_hash = ?
                   AND status = 'submitted'
                   AND submitted_at >= ?
                """,
                (api_key_hash, cutoff),
            )
            rows = cur.fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            if d.get("meta"):
                try:
                    d["meta"] = json.loads(d["meta"])
                except (TypeError, ValueError):
                    pass
            out.append(d)
        return out

    def purge_stale(self, *, older_than_secs: int = 30 * 24 * 3600) -> int:
        """Delete rows older than ``older_than_secs`` (default 30 days).

        Returns the number of rows deleted. Best-effort: caller should treat
        any exception as non-fatal.
        """
        cutoff = time.time() - max(1, older_than_secs)
        with self._lock:
            cur = self._conn.execute(
                """
                DELETE FROM submissions
                 WHERE submitted_at < ?
                """,
                (cutoff,),
            )
            return int(cur.rowcount or 0)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass


# ─── CreditLedger ───────────────────────────────────────────────────────────


class CreditLedger:
    """Sqlite-backed append-only ledger of credit consumption events.

    Stored at ``~/.ligandai/credit_ledger.db`` (mode 0600). Independent of
    server-side billing — useful for offline audit / reconciliation.
    """

    _SCHEMA = """
        CREATE TABLE IF NOT EXISTS credit_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            api_key_hash    TEXT NOT NULL,
            job_id          TEXT,
            kind            TEXT NOT NULL,
            ts              REAL NOT NULL,
            estimated       INTEGER,
            actual          INTEGER,
            balance_before  INTEGER,
            balance_after   INTEGER,
            note            TEXT
        );
    """
    _INDEX = """
        CREATE INDEX IF NOT EXISTS ix_events_key_ts
        ON credit_events(api_key_hash, ts);
    """

    def __init__(self, db_path: Path | None = None) -> None:
        if db_path is None:
            db_path = default_state_dir() / CREDIT_LEDGER_DB_NAME
        else:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(db_path.parent, 0o700)
            except OSError:
                pass
        self._path = Path(db_path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self._path), check_same_thread=False, isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=2000")
        except sqlite3.Error:
            pass
        with self._lock:
            self._conn.execute(self._SCHEMA)
            self._conn.execute(self._INDEX)
        _set_file_mode_0600(self._path)

    @property
    def path(self) -> Path:
        return self._path

    def record(
        self,
        *,
        api_key_hash: str,
        kind: str,
        job_id: str | None = None,
        estimated: int | None = None,
        actual: int | None = None,
        balance_before: int | None = None,
        balance_after: int | None = None,
        note: str | None = None,
    ) -> int:
        """Append one credit event. Returns the new row id."""
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO credit_events (
                    api_key_hash, job_id, kind, ts,
                    estimated, actual,
                    balance_before, balance_after, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    api_key_hash,
                    str(job_id) if job_id else None,
                    kind,
                    time.time(),
                    int(estimated) if estimated is not None else None,
                    int(actual) if actual is not None else None,
                    int(balance_before) if balance_before is not None else None,
                    int(balance_after) if balance_after is not None else None,
                    str(note) if note else None,
                ),
            )
            return int(cur.lastrowid or 0)

    def total_consumed(
        self,
        api_key_hash: str,
        *,
        since_ts: float | None = None,
    ) -> int:
        """Sum of actual credits consumed (falls back to estimated when actual is NULL)."""
        with self._lock:
            if since_ts is None:
                cur = self._conn.execute(
                    """
                    SELECT COALESCE(SUM(COALESCE(actual, estimated, 0)), 0) AS total
                      FROM credit_events
                     WHERE api_key_hash = ?
                    """,
                    (api_key_hash,),
                )
            else:
                cur = self._conn.execute(
                    """
                    SELECT COALESCE(SUM(COALESCE(actual, estimated, 0)), 0) AS total
                      FROM credit_events
                     WHERE api_key_hash = ? AND ts >= ?
                    """,
                    (api_key_hash, float(since_ts)),
                )
            row = cur.fetchone()
        return int(row["total"]) if row else 0

    def recent_events(
        self,
        api_key_hash: str,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return the most recent events for the given api_key_hash (newest first)."""
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT * FROM credit_events
                 WHERE api_key_hash = ?
                 ORDER BY ts DESC
                 LIMIT ?
                """,
                (api_key_hash, max(1, int(limit))),
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
