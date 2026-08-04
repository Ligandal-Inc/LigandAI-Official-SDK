# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Structured, machine-readable diagnostics — the SDK half of the platform
diagnostics contract.  [bd-LIGANDAI_ALPHA_V2-x4vt8.3.2]

WHY THIS EXISTS
    A customer excluded surface residues on their target. The exclusion was
    silently a no-op (a residue-numbering bug, since fixed) and 25/25 designed
    peptides bound the excluded region. **Nothing anywhere in the product told
    them** — not the SDK, not the API response, not the website. They found out
    by eyeballing structures in a viewer. The numbering bug was the shallow
    defect; the SILENCE was the deep one.

    This module makes server-side diagnostic signal reach a Python SDK user, as
    data they can branch on — not just as a ``warnings.warn`` they may have
    filtered away. Every job now exposes :attr:`~ligandai.jobs.Job.diagnostics`,
    and every client-side targeting validation returns a
    :class:`~ligandai._targeting.TargetingValidationResult` whose
    ``.diagnostics`` carries the same envelopes.

THE WIRE ENVELOPE (snake_case, identical across FastAPI / Express / SSE / REST)::

    {
      "schema_version": 1,
      "code": "avoid_residues_none_resolved",
      "severity": "info" | "warning" | "error",
      "stage": "request" | "generate" | "fold" | "results" | "client",
      "title": "None of your excluded residues were found",
      "message": "11 of 12 residues you asked to avoid ...",
      "remediation": "Residue numbers must use PDB author numbering ...",
      "details": {...},
      "doc_anchor": "sdk/python/allosteric-targeting#event-avoid",
      "occurred_at": "2026-08-04T18:22:01Z",
      "subject_id": "design_17"      // optional
    }

DESIGN RULES (these are the contract, not preferences)
    1. ``code`` is the join key across FastAPI, Express, both SDKs, and the
       website. Adding a code is additive. Renaming or removing one is a
       breaking wire change.
    2. **UNKNOWN CODES MUST PASS THROUGH UNCHANGED.** This layer never filters
       to a known-code allowlist — a diagnostic the SDK has never heard of is
       exactly the diagnostic most likely to matter, and dropping it is the
       precise failure mode this module exists to prevent. The same applies to
       unknown ``severity`` values and unknown envelope keys (preserved in
       :attr:`Diagnostic.raw`).
    3. The envelope's own ``severity`` is authoritative; the registry supplies
       only the default, so a caller may escalate without inventing a code.
    4. Nothing in here may raise into a request or stream path. A diagnostics
       layer that can fail a job is worse than no diagnostics layer. Every
       public helper is defensive.

REGISTRY PROVENANCE
    :data:`DIAGNOSTIC_CODES` is a **copy** of the platform registry
    (``shared/warnings.json`` in the server repo, also read by
    ``fastapi_backend/core/warnings.py`` and ``shared/warnings.ts``). It is
    copied rather than imported so the published SDK has no build-time or
    runtime dependency on the server repo. Codes, severities, stages, titles
    and remediations are byte-identical to that file. Entries below the
    ``CLIENT-ONLY`` marker are emitted by this SDK before any HTTP call and are
    not (yet) in the shared registry — they are proposed additions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

__all__ = [
    "DIAGNOSTIC_CODES",
    "DIAGNOSTIC_EVENT",
    "DIAGNOSTIC_SCHEMA_VERSION",
    "SEVERITIES",
    "STAGES",
    "Diagnostic",
    "diagnostic_from_payload",
    "diagnostic_key",
    "diagnostics_from_container",
    "is_diagnostic_envelope",
    "known_codes",
    "make_diagnostic",
    "merge_diagnostics",
    "severity_rank",
    "spec_for",
    "worst_severity",
]

#: Canonical SSE event name for one diagnostic envelope. One event = one
#: :class:`Diagnostic`.
DIAGNOSTIC_EVENT = "diagnostic"

DIAGNOSTIC_SCHEMA_VERSION = 1

SEVERITIES = ("info", "warning", "error")
STAGES = ("request", "generate", "fold", "results", "client")

_SEVERITY_ORDER: dict[str, int] = {"info": 0, "warning": 1, "error": 2}

#: Copy of ``shared/warnings.json``'s ``codes`` map. See REGISTRY PROVENANCE
#: above — keep byte-identical with that file for every shared code.
DIAGNOSTIC_CODES: dict[str, dict[str, str]] = {
    "avoid_residues_unresolved": {
        "severity": "warning",
        "stage": "generate",
        "title": "Some excluded residues were not found",
        "remediation": (
            "Residue numbers must match the receptor structure's PDB author numbering, "
            "including the chain ID. Check the numbering of the structure you targeted "
            "and re-run with corrected residue numbers."
        ),
        "doc_anchor": "sdk/python/allosteric-targeting#event-avoid",
    },
    "avoid_residues_none_resolved": {
        "severity": "error",
        "stage": "generate",
        "title": "None of your excluded residues were found",
        "remediation": (
            "Your exclusion had NO EFFECT on this run. Every residue number you asked to "
            "avoid is absent from the receptor's featurized residue set - almost always a "
            "numbering mismatch (PDB author numbering vs. sequence index, or the wrong "
            "chain). Re-run with residue numbers taken from the structure itself."
        ),
        "doc_anchor": "sdk/python/allosteric-targeting#event-avoid",
    },
    "target_residues_unresolved": {
        "severity": "warning",
        "stage": "generate",
        "title": "Some target residues were not found",
        "remediation": (
            "Residue numbers must match the receptor structure's PDB author numbering, "
            "including the chain ID. The run proceeded using only the residues that resolved."
        ),
        "doc_anchor": "sdk/python/allosteric-targeting#event-target",
    },
    "target_residues_none_resolved": {
        "severity": "error",
        "stage": "generate",
        "title": "None of your target residues were found",
        "remediation": (
            "Targeting had NO EFFECT: generation fell back to the unfiltered pocket, so "
            "designs were not steered to the site you asked for. Re-run with residue "
            "numbers taken from the structure's own author numbering."
        ),
        "doc_anchor": "sdk/python/allosteric-targeting#event-target",
    },
    "degenerate_pocket_after_avoid": {
        "severity": "error",
        "stage": "generate",
        "title": "Exclusion left almost no pocket to steer with",
        "remediation": (
            "After removing the excluded residues and their packing shell, too few pocket "
            "residues remain to steer generation reliably. Designs may drift back onto the "
            "excluded surface. Widen the target face, narrow the avoid set, or reduce "
            "avoid_shell_angstrom."
        ),
        "doc_anchor": "sdk/python/allosteric-targeting#event-warning",
    },
    "avoid_conformance_violation": {
        "severity": "warning",
        "stage": "fold",
        "title": "Designs contacted residues you excluded",
        "remediation": (
            "Pocket targeting steers which sequences are generated; it does not physically "
            "constrain where the folder places the finished peptide. To bind the fold to the "
            "requested site, re-fold with fold_pocket_residues set. Rank or reject the "
            "violating designs before synthesis."
        ),
        "doc_anchor": "sdk/python/allosteric-targeting#prior-not-constraint",
    },
    "avoid_conformance_clean": {
        "severity": "info",
        "stage": "fold",
        "title": "Designs avoided the excluded residues",
        "remediation": "",
        "doc_anchor": "sdk/python/allosteric-targeting#prior-not-constraint",
    },
    "avoid_conformance_unavailable": {
        "severity": "info",
        "stage": "fold",
        "title": "Exclusion could not be verified on the structure",
        "remediation": (
            "The folded complex was not available for a contact check, so the exclusion was "
            "not verified. Inspect the structure, or re-run the conformance check on the "
            "finished complex."
        ),
        "doc_anchor": "sdk/python/allosteric-targeting#prior-not-constraint",
    },
    "param_ignored": {
        "severity": "warning",
        "stage": "request",
        "title": "A parameter you set was ignored",
        "remediation": (
            "The named parameter is accepted by the request schema but does not change "
            "pipeline behavior. Remove it from your request, or use the documented parameter "
            "that does apply."
        ),
        "doc_anchor": "sdk/python/api-reference",
    },
    "param_dropped_in_transport": {
        "severity": "warning",
        "stage": "request",
        "title": "A parameter you set did not reach the pipeline",
        "remediation": (
            "The named parameter was lost between API layers, so the server default was used "
            "instead. This is a platform bug - the value you sent was not applied."
        ),
        "doc_anchor": "sdk/python/api-reference",
    },
    "sdk_outdated": {
        "severity": "warning",
        "stage": "client",
        "title": "Your LigandAI SDK is out of date",
        "remediation": (
            "Upgrade to pick up current parameter handling, diagnostics, and bug fixes. Older "
            "clients can silently drop parameters that newer server versions expect."
        ),
        "doc_anchor": "",
    },
    "sdk_unsupported": {
        "severity": "error",
        "stage": "client",
        "title": "Your LigandAI SDK is below the minimum supported version",
        "remediation": (
            "Upgrade now. Versions this old are known to mis-send or drop request parameters, "
            "which can silently change what your job actually ran."
        ),
        "doc_anchor": "",
    },
    "sdk_version_unknown": {
        "severity": "info",
        "stage": "client",
        "title": "SDK version could not be determined",
        "remediation": (
            "Requests from an unrecognized client cannot be checked for version "
            "compatibility. Use an official LigandAI SDK, or set a User-Agent of the form "
            "ligandai-python/<version> or ligandai-node/<version>."
        ),
        "doc_anchor": "",
    },
    # ─── CLIENT-ONLY ────────────────────────────────────────────────────────
    # Emitted by this SDK's pre-flight validation, BEFORE any HTTP call. Not in
    # shared/warnings.json yet — proposed additions (reported upstream on
    # bd-LIGANDAI_ALPHA_V2-x4vt8.3.2). A server that later adopts these codes
    # must keep these severities/stages.
    "targeting_region_swapped": {
        "severity": "info",
        "stage": "client",
        "title": "A targeting region had start > end",
        "remediation": (
            "The pair was swapped automatically to match server behaviour. Pass start <= end "
            "to silence this."
        ),
        "doc_anchor": "sdk/python/allosteric-targeting#residue-numbering",
    },
    "targeting_chain_ambiguous": {
        "severity": "warning",
        "stage": "client",
        "title": "A targeting region did not name a chain",
        "remediation": (
            "This request names more than one chain, so a region without a chain matches any "
            "chain carrying that residue number. Pass chain= on every region to disambiguate."
        ),
        "doc_anchor": "sdk/python/allosteric-targeting#residue-numbering",
    },
    "avoid_shell_disabled": {
        "severity": "info",
        "stage": "client",
        "title": "The avoid packing shell is disabled",
        "remediation": (
            "avoid_shell_angstrom=0 excludes only the named residues, not their packing "
            "neighbours. Leave it unset to use the server default."
        ),
        "doc_anchor": "sdk/python/allosteric-targeting#event-avoid",
    },
    "degenerate_pocket_risk": {
        "severity": "warning",
        "stage": "client",
        "title": "Exclusion may leave almost no pocket to steer with",
        "remediation": (
            "The avoid span is at least as large as the target span, which usually leaves a "
            "degenerate (near-empty) design pocket. Check for overlapping or transposed "
            "ranges, widen the target face, or narrow the avoid set."
        ),
        "doc_anchor": "sdk/python/allosteric-targeting#event-warning",
    },
    "targeting_contradiction": {
        "severity": "warning",
        "stage": "client",
        "title": "A residue is both required and excluded",
        "remediation": (
            "The same residue appears in the must-contact set and the must-avoid set, so no "
            "design can satisfy both criteria. Remove it from one of the two lists."
        ),
        "doc_anchor": "sdk/python/allosteric-targeting#event-warning",
    },
}


def known_codes() -> list[str]:
    """Every code this SDK build knows about.

    Unknown codes are still legal on the wire and are never dropped — this list
    is for introspection and docs, **not** for filtering.
    """
    return sorted(DIAGNOSTIC_CODES.keys())


def spec_for(code: str) -> dict[str, str]:
    """Registry entry for ``code``, or an empty dict for an unregistered code."""
    return dict(DIAGNOSTIC_CODES.get(code) or {})


def severity_rank(severity: str | None) -> int:
    """Order a severity for threshold comparisons.

    An unrecognized severity ranks as ``warning`` rather than ``info`` — a
    future severity this build has never seen must not be silently demoted
    below the warn threshold.
    """
    if not severity:
        return _SEVERITY_ORDER["warning"]
    return _SEVERITY_ORDER.get(str(severity).strip().lower(), _SEVERITY_ORDER["warning"])


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class Diagnostic:
    """One user-facing diagnostic. Serializes to the canonical wire envelope.

    Mirrors ``fastapi_backend/core/warnings.py``'s ``Diagnostic`` field-for-field,
    plus :attr:`raw` — the untouched payload this object was parsed from, so an
    envelope key a newer server added is still reachable from an older SDK.

    Attributes
    ----------
    code : str
        Stable machine identifier; the join key across every layer.
    message : str
        Human-readable, interpolated, specific to this occurrence.
    severity : str
        ``"info"`` | ``"warning"`` | ``"error"``. A value outside that set is
        preserved verbatim and treated as at-least-``warning``.
    stage : str
        ``"request"`` | ``"generate"`` | ``"fold"`` | ``"results"`` | ``"client"``.
    title : str
        Short (<60 char) headline from the registry.
    remediation : str
        What the user should actually do about it. May be empty.
    details : dict
        Code-specific machine payload; stable keys per code, additive-only.
    doc_anchor : str
        Docs anchor, no leading slash. May be empty.
    occurred_at : str
        ISO-8601 UTC timestamp.
    subject_id : str | None
        Optional identifier for the thing the diagnostic is about (a design id,
        a gene, a job id).
    raw : dict
        The original wire payload when this was parsed from one; ``{}`` when
        constructed locally.
    """

    code: str
    message: str
    severity: str = "warning"
    stage: str = "results"
    title: str = ""
    remediation: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    doc_anchor: str = ""
    occurred_at: str = ""
    subject_id: str | None = None
    schema_version: int = DIAGNOSTIC_SCHEMA_VERSION
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        """Canonical wire envelope. Unknown keys from :attr:`raw` are re-emitted
        so a round-trip through this SDK is lossless."""
        out: dict[str, Any] = {
            "schema_version": self.schema_version,
            "code": self.code,
            "severity": self.severity,
            "stage": self.stage,
            "title": self.title,
            "message": self.message,
            "remediation": self.remediation,
            "details": self.details,
            "doc_anchor": self.doc_anchor,
            "occurred_at": self.occurred_at,
        }
        if self.subject_id:
            out["subject_id"] = self.subject_id
        for key, value in self.raw.items():
            if key not in out and key not in _ENVELOPE_ALIASES:
                out[key] = value
        return out

    @property
    def is_known_code(self) -> bool:
        """Whether this build's registry recognizes :attr:`code`.

        Informational only — an unknown code is still surfaced in full.
        """
        return self.code in DIAGNOSTIC_CODES

    def at_least(self, severity: str) -> bool:
        """Whether this diagnostic is at or above ``severity``."""
        return severity_rank(self.severity) >= severity_rank(severity)

    def format(self) -> str:
        """One human-readable block: title, message, remediation, details."""
        head = self.title or self.code
        parts = [f"[{self.severity}] {head} ({self.code})"]
        if self.message:
            parts.append(self.message)
        if self.remediation:
            parts.append(self.remediation)
        if self.details:
            try:
                parts.append(f"details: {json.dumps(self.details, sort_keys=True, default=str)}")
            except Exception:  # pragma: no cover - defensive
                parts.append(f"details: {self.details}")
        return "\n".join(parts)

    def __str__(self) -> str:
        return self.format()


# camelCase spellings tolerated on input. The contract is snake_case; these
# exist because a diagnostic that arrives in the wrong case must still surface.
_ENVELOPE_ALIASES: dict[str, str] = {
    "docAnchor": "doc_anchor",
    "occurredAt": "occurred_at",
    "subjectId": "subject_id",
    "schemaVersion": "schema_version",
}

_ENVELOPE_KEYS = frozenset(
    {
        "schema_version",
        "code",
        "severity",
        "stage",
        "title",
        "message",
        "remediation",
        "details",
        "doc_anchor",
        "occurred_at",
        "subject_id",
        *_ENVELOPE_ALIASES.keys(),
    }
)


def make_diagnostic(
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    *,
    severity: str | None = None,
    stage: str | None = None,
    title: str | None = None,
    remediation: str | None = None,
    doc_anchor: str | None = None,
    subject_id: str | None = None,
    occurred_at: str | None = None,
) -> Diagnostic:
    """Build a canonical diagnostic, enriched from the registry when known.

    Mirrors ``fastapi_backend.core.warnings.make_diagnostic``. This is the
    **emitter** path (client-side diagnostics this SDK originates), so an
    invalid severity/stage is coerced to a safe default. Diagnostics *received*
    from the wire go through :func:`diagnostic_from_payload`, which preserves
    unrecognized values instead of coercing them.
    """
    spec = DIAGNOSTIC_CODES.get(code) or {}
    sev = severity or spec.get("severity") or "warning"
    if sev not in SEVERITIES:
        sev = "warning"
    stg = stage or spec.get("stage") or "results"
    if stg not in STAGES:
        stg = "results"
    return Diagnostic(
        code=code,
        message=message,
        severity=sev,
        stage=stg,
        title=title if title is not None else (spec.get("title") or code),
        remediation=remediation if remediation is not None else (spec.get("remediation") or ""),
        details=dict(details or {}),
        doc_anchor=doc_anchor if doc_anchor is not None else (spec.get("doc_anchor") or ""),
        occurred_at=occurred_at or _utcnow_iso(),
        subject_id=subject_id,
    )


def is_diagnostic_envelope(payload: Any) -> bool:
    """Heuristic: does ``payload`` look like a canonical diagnostic envelope?

    Used to recognize a diagnostic that arrives without the canonical
    ``event: diagnostic`` SSE name (e.g. embedded in another event's body).
    Deliberately conservative: a ``code`` plus at least one other envelope-only
    key.
    """
    if not isinstance(payload, dict):
        return False
    code = payload.get("code")
    if not isinstance(code, str) or not code.strip():
        return False
    markers = ("schema_version", "schemaVersion", "severity", "remediation", "doc_anchor")
    return any(k in payload for k in markers)


def diagnostic_from_payload(
    payload: Any,
    *,
    default_code: str = "unspecified",
    default_stage: str = "results",
) -> Diagnostic | None:
    """Parse a wire envelope into a :class:`Diagnostic`. **Never filters.**

    This is the **receiver** path. Unknown codes, unknown severities and unknown
    envelope keys are preserved verbatim (the latter in :attr:`Diagnostic.raw`)
    — an SDK that only understood a fixed code list would reintroduce exactly
    the silence this contract exists to remove.

    Missing registry-supplied fields (``title``, ``remediation``, ``severity``,
    ``stage``, ``doc_anchor``) are backfilled from the local registry copy when
    the code is known, so an older server that emits a bare
    ``{"code": ..., "message": ...}`` still yields a fully-formed diagnostic.

    Returns ``None`` only when ``payload`` is not a mapping.
    """
    if not isinstance(payload, dict):
        return None

    def _pick(key: str) -> Any:
        if key in payload:
            return payload[key]
        for camel, snake in _ENVELOPE_ALIASES.items():
            if snake == key and camel in payload:
                return payload[camel]
        return None

    raw_code = _pick("code")
    code = raw_code.strip() if isinstance(raw_code, str) and raw_code.strip() else default_code
    spec = DIAGNOSTIC_CODES.get(code) or {}

    severity = _pick("severity")
    severity_str = (
        (str(severity).strip() if isinstance(severity, str) and severity.strip() else None)
        or spec.get("severity")
        or "warning"
    )

    stage = _pick("stage")
    stage_str = (
        (str(stage).strip() if isinstance(stage, str) and stage.strip() else None)
        or spec.get("stage")
        or default_stage
    )

    message = _pick("message")
    if not isinstance(message, str) or not message.strip():
        message = spec.get("title") or code

    title = _pick("title")
    if not isinstance(title, str) or not title.strip():
        title = spec.get("title") or code

    remediation = _pick("remediation")
    if not isinstance(remediation, str):
        remediation = spec.get("remediation") or ""

    doc_anchor = _pick("doc_anchor")
    if not isinstance(doc_anchor, str):
        doc_anchor = spec.get("doc_anchor") or ""

    details = _pick("details")
    if not isinstance(details, dict):
        # Keep a non-dict `details` rather than discarding it.
        details = {} if details is None else {"value": details}

    occurred_at = _pick("occurred_at")
    if not isinstance(occurred_at, str) or not occurred_at.strip():
        occurred_at = _utcnow_iso()

    subject_id = _pick("subject_id")
    if not isinstance(subject_id, str) or not subject_id.strip():
        subject_id = None

    schema_version = _pick("schema_version")
    try:
        schema_version_int = (
            int(schema_version) if schema_version is not None else DIAGNOSTIC_SCHEMA_VERSION
        )
    except (TypeError, ValueError):
        schema_version_int = DIAGNOSTIC_SCHEMA_VERSION

    extras = {k: v for k, v in payload.items() if k not in _ENVELOPE_KEYS}

    return Diagnostic(
        code=code,
        message=message,
        severity=severity_str,
        stage=stage_str,
        title=title,
        remediation=remediation,
        details=details,
        doc_anchor=doc_anchor,
        occurred_at=occurred_at,
        subject_id=subject_id,
        schema_version=schema_version_int,
        raw=extras,
    )


def diagnostics_from_container(payload: Any, *, _depth: int = 0) -> list[Diagnostic]:
    """Pull a ``diagnostics: [...]`` array out of a REST job/results payload.

    Looks at the top level and inside the handful of result containers the
    platform's status endpoints use (``result``, ``results``, ``data``,
    ``output``, ``job``), because the same logical field appears at different
    depths depending on which endpoint answered. Returns ``[]`` when there is
    nothing to find — never ``None``, never raises.
    """
    if not isinstance(payload, dict) or _depth > 3:
        return []
    out: list[Diagnostic] = []
    raw = payload.get("diagnostics")
    if isinstance(raw, list):
        for entry in raw:
            diag = diagnostic_from_payload(entry)
            if diag is not None:
                out.append(diag)
    for key in ("result", "results", "data", "output", "job"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            out.extend(diagnostics_from_container(nested, _depth=_depth + 1))
    return out


def diagnostic_key(d: Diagnostic) -> str:
    """Stable identity for de-duplication across a stream + REST poll."""
    try:
        payload = json.dumps(d.details, sort_keys=True, default=str)
    except Exception:  # pragma: no cover - defensive
        payload = str(d.details)
    return f"{d.code}|{d.subject_id or ''}|{d.message}|{payload}"


def merge_diagnostics(
    existing: list[Diagnostic], incoming: list[Diagnostic]
) -> tuple[list[Diagnostic], list[Diagnostic]]:
    """Append ``incoming`` to ``existing``, dropping exact repeats.

    Returns ``(merged, newly_added)``. ``newly_added`` is what a caller should
    surface on the warning channel — a job that polls its status endpoint every
    few seconds must not re-warn the same diagnostic on every tick.
    """
    merged = list(existing)
    seen = {diagnostic_key(d) for d in merged}
    added: list[Diagnostic] = []
    for d in incoming:
        key = diagnostic_key(d)
        if key in seen:
            continue
        seen.add(key)
        merged.append(d)
        added.append(d)
    return merged, added


def worst_severity(diags: list[Diagnostic]) -> str | None:
    """Highest severity present, or ``None`` for an empty list."""
    worst: str | None = None
    for d in diags:
        if worst is None or severity_rank(d.severity) > severity_rank(worst):
            worst = d.severity
    return worst
