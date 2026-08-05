# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Programs, workstreams, projects, and sessions."""

from __future__ import annotations

from typing import List

from ligandai.errors import LigandAILegalAcceptanceRequired, LigandAIValidationError
from ligandai.resources._base import AsyncResource, Resource
from ligandai.types import (
    Program,
    ProgramAccess,
    ProgramDetail,
    ProgramInvite,
    ProgramInviteAcceptResult,
    ProgramInviteCreated,
    ProgramPermission,
    ProgramShare,
    Session,
    SessionDetail,
    SharedProgram,
    Workstream,
)


def _validate_program_id(program_id: int | str) -> int:
    """Client-side guard: reject a non-positive/non-integer program id before
    any HTTP call, rather than letting a malformed id reach the server as a
    URL path segment (silent 404) or a JSON body key.
    """
    try:
        value = int(program_id)
    except (TypeError, ValueError) as exc:
        raise LigandAIValidationError(
            f"program_id must be a positive integer (got {program_id!r})"
        ) from exc
    if value <= 0:
        raise LigandAIValidationError(f"program_id must be > 0 (got {value})")
    return value


def _validate_positive_int(value: int | str, field: str) -> int:
    """Same guard as :func:`_validate_program_id`, parameterized by field name
    (``share_id`` / ``invite_id``)."""
    try:
        out = int(value)
    except (TypeError, ValueError) as exc:
        raise LigandAIValidationError(
            f"{field} must be a positive integer (got {value!r})"
        ) from exc
    if out <= 0:
        raise LigandAIValidationError(f"{field} must be > 0 (got {out})")
    return out


class Programs(Resource):
    """``/api/ptf/programs/*``, ``/api/ptf/workstreams``, and ``/api/ptf/sessions/*``."""

    def list(self) -> List[Program]:
        payload = self._transport.request("GET", "/api/ptf/programs") or []
        items = payload if isinstance(payload, list) else payload.get("programs", [])
        return [Program.model_validate(p) for p in items]

    def create(
        self,
        name: str,
        description: str | None = None,
        color: str = "#3b82f6",
    ) -> Program:
        body: dict[str, object] = {"name": name, "color": color}
        if description is not None:
            body["description"] = description
        return Program.model_validate(
            self._transport.request("POST", "/api/ptf/programs", json=body) or {}
        )

    def get(self, program_id: int) -> ProgramDetail:
        return ProgramDetail.model_validate(
            self._transport.request("GET", f"/api/ptf/programs/{program_id}") or {}
        )

    def update(self, program_id: int, **fields: object) -> Program:
        return Program.model_validate(
            self._transport.request("PATCH", f"/api/ptf/programs/{program_id}", json=fields) or {}
        )

    def archive(self, program_id: int) -> bool:
        try:
            self._transport.request("DELETE", f"/api/ptf/programs/{program_id}")
            return True
        except LigandAILegalAcceptanceRequired:
            # ⛔ A legal 403 must not read as "server refused".
            raise
        except Exception:
            return False

    def workstreams(self, program_id: int | None = None) -> List[Workstream]:
        params = {"program_id": program_id} if program_id is not None else None
        payload = self._transport.request("GET", "/api/ptf/workstreams", params=params) or []
        items = payload if isinstance(payload, list) else payload.get("workstreams", [])
        return [Workstream.model_validate(w) for w in items]

    def create_workstream(
        self,
        program_id: int,
        name: str,
        description: str | None = None,
        color: str = "#3b82f6",
        genes: List[str] | None = None,
    ) -> Workstream:
        body: dict[str, object] = {
            "programId": program_id,
            "name": name,
            "color": color,
        }
        if description is not None:
            body["description"] = description
        if genes is not None:
            body["genes"] = genes
        return Workstream.model_validate(
            self._transport.request("POST", "/api/ptf/workstreams", json=body) or {}
        )

    def list_sessions(self, gene: str | None = None, limit: int = 20) -> List[Session]:
        params: dict[str, object] = {"limit": limit}
        if gene is not None:
            params["gene"] = gene
        payload = self._transport.request("GET", "/api/ptf/sessions", params=params) or []
        items = payload if isinstance(payload, list) else payload.get("sessions", [])
        return [Session.model_validate(s) for s in items]

    def get_session(self, session_id: str) -> SessionDetail:
        return SessionDetail.model_validate(
            self._transport.request("GET", f"/api/ptf/sessions/{session_id}") or {}
        )

    def find_session_by_gene(self, gene: str) -> Session | None:
        payload = self._transport.request("GET", f"/api/ptf/sessions/by-gene/{gene}")
        if not payload:
            return None
        return Session.model_validate(payload)

    # ------------------------------------------------------------------
    # Program sharing — collaborator access to a program you don't own.
    # `list()` / `get()` above, and the structures/folds/peptides resources,
    # already work unchanged against a shared program once the server
    # authorizes it — there's nothing to opt into client-side.
    # ------------------------------------------------------------------

    def shared_with_me(self) -> List[SharedProgram]:
        """``GET /api/ptf/programs/shared-with-me`` — programs shared with you.

        Every program you can see via a grant you don't own — direct share,
        org share, or org membership — annotated with your ``permission`` and
        who/what granted it (see :class:`~ligandai.types.SharedProgram`).
        Programs you own outright are not included; use :meth:`list` for those.
        """
        payload = self._transport.request("GET", "/api/ptf/programs/shared-with-me") or []
        items = payload if isinstance(payload, list) else payload.get("programs", [])
        return [SharedProgram.model_validate(p) for p in items]

    def access(self, program_id: int) -> ProgramAccess:
        """``GET /api/ptf/programs/{id}/access`` — your own effective access.

        Works for any grantee — owner, org member, or share recipient — so
        it's the one call to make before trying an operation on a program you
        don't own. Raises :class:`~ligandai.errors.LigandAINotFoundError` if
        you have no grant at all on this program.
        """
        program_id = _validate_program_id(program_id)
        return ProgramAccess.model_validate(
            self._transport.request("GET", f"/api/ptf/programs/{program_id}/access") or {}
        )

    def shares(self, program_id: int) -> List[ProgramShare]:
        """``GET /api/ptf/programs/{id}/shares`` — who has access (owner only).

        Raises :class:`~ligandai.errors.LigandAIForbidden` (403) if you are
        not this program's owner.
        """
        program_id = _validate_program_id(program_id)
        payload = self._transport.request("GET", f"/api/ptf/programs/{program_id}/shares") or []
        items = payload if isinstance(payload, list) else payload.get("shares", [])
        return [ProgramShare.model_validate(s) for s in items]

    def share(
        self,
        program_id: int,
        *,
        user_email: str | None = None,
        org_id: int | None = None,
        permission: ProgramPermission = "view",
    ) -> ProgramShare:
        """``POST /api/ptf/programs/{id}/shares`` — grant access (owner only).

        Pass exactly one of ``user_email`` (share with one person) or
        ``org_id`` (share with an entire organization) — passing both or
        neither raises :class:`~ligandai.errors.LigandAIValidationError`
        locally, before any HTTP call. Raises
        :class:`~ligandai.errors.LigandAIForbidden` (403) if you are not this
        program's owner.

        Args:
            program_id: Program to share.
            user_email: Share with one collaborator by email.
            org_id: Share with an entire organization.
            permission: One of ``"view"``, ``"comment"``, ``"edit"``.
        """
        program_id = _validate_program_id(program_id)
        if (user_email is None) == (org_id is None):
            raise LigandAIValidationError(
                "share() requires exactly one of user_email= or org_id= "
                f"(got user_email={user_email!r}, org_id={org_id!r})"
            )
        body: dict[str, object] = {"permission": permission}
        if user_email is not None:
            body["userEmail"] = user_email
        if org_id is not None:
            body["orgId"] = org_id
        return ProgramShare.model_validate(
            self._transport.request(
                "POST", f"/api/ptf/programs/{program_id}/shares", json=body
            )
            or {}
        )

    def update_share(
        self, program_id: int, share_id: int, permission: ProgramPermission
    ) -> ProgramShare:
        """``PATCH /api/ptf/programs/{id}/shares/{share_id}`` — change a
        grant's permission level (owner only).
        """
        program_id = _validate_program_id(program_id)
        share_id = _validate_positive_int(share_id, "share_id")
        return ProgramShare.model_validate(
            self._transport.request(
                "PATCH",
                f"/api/ptf/programs/{program_id}/shares/{share_id}",
                json={"permission": permission},
            )
            or {}
        )

    def revoke_share(self, program_id: int, share_id: int) -> bool:
        """``DELETE /api/ptf/programs/{id}/shares/{share_id}`` — revoke a
        grant (owner only).

        Unlike :meth:`archive`, errors here are never swallowed to ``False``
        — a 403 means "you are not this program's owner" and must reach the
        caller distinctly from a genuine success. Returns ``True`` on
        success; raises the usual typed :class:`~ligandai.errors.LigandAIError`
        subclass on failure.
        """
        program_id = _validate_program_id(program_id)
        share_id = _validate_positive_int(share_id, "share_id")
        self._transport.request(
            "DELETE", f"/api/ptf/programs/{program_id}/shares/{share_id}"
        )
        return True

    def invite(
        self,
        program_id: int,
        email: str,
        *,
        permission: ProgramPermission = "view",
        expires_in_days: int | None = None,
    ) -> ProgramInviteCreated:
        """``POST /api/ptf/programs/{id}/invites`` — invite an external
        collaborator by email (owner only).

        The response's ``invite_url`` is shown **exactly once** — relay it to
        the invitee immediately (email, chat, however you reach them). It
        cannot be retrieved again; :meth:`invites` never includes it or the
        raw token. The invitee accepts with :meth:`accept_invite`.

        Args:
            program_id: Program to invite the collaborator to.
            email: Invitee's email address. On accept, the authenticated
                caller's verified email must match this exactly.
            permission: One of ``"view"``, ``"comment"``, ``"edit"``.
            expires_in_days: Invite lifetime. Server default is 14 days,
                capped at 90.
        """
        program_id = _validate_program_id(program_id)
        email = email.strip()
        if not email or "@" not in email:
            raise LigandAIValidationError(f"invite() requires a valid email (got {email!r})")
        body: dict[str, object] = {"email": email, "permission": permission}
        if expires_in_days is not None:
            body["expiresInDays"] = expires_in_days
        return ProgramInviteCreated.model_validate(
            self._transport.request(
                "POST", f"/api/ptf/programs/{program_id}/invites", json=body
            )
            or {}
        )

    def invites(self, program_id: int) -> List[ProgramInvite]:
        """``GET /api/ptf/programs/{id}/invites`` — list pending invites
        (owner only). Never includes the raw token — see :meth:`invite`.
        """
        program_id = _validate_program_id(program_id)
        payload = self._transport.request(
            "GET", f"/api/ptf/programs/{program_id}/invites"
        ) or []
        items = payload if isinstance(payload, list) else payload.get("invites", [])
        return [ProgramInvite.model_validate(i) for i in items]

    def revoke_invite(self, program_id: int, invite_id: int) -> bool:
        """``DELETE /api/ptf/programs/{id}/invites/{invite_id}`` — revoke a
        pending invite (owner only). The token stops working immediately.

        Same error-surfacing contract as :meth:`revoke_share` — never
        swallowed to ``False``.
        """
        program_id = _validate_program_id(program_id)
        invite_id = _validate_positive_int(invite_id, "invite_id")
        self._transport.request(
            "DELETE", f"/api/ptf/programs/{program_id}/invites/{invite_id}"
        )
        return True

    def accept_invite(self, token: str) -> ProgramInviteAcceptResult:
        """``POST /api/ptf/share-invites/accept`` — accept an email invite.

        Call this once, authenticated as the invited collaborator (the
        account whose verified email matches the invite). ``token`` is the
        opaque string embedded in the ``invite_url`` from :meth:`invite`. A
        failed accept (expired, already used, revoked, or wrong account)
        raises a single generic error by design — the server never reveals
        which of those it was.
        """
        token = token.strip()
        if not token:
            raise LigandAIValidationError("accept_invite() requires a non-empty token")
        return ProgramInviteAcceptResult.model_validate(
            self._transport.request(
                "POST", "/api/ptf/share-invites/accept", json={"token": token}
            )
            or {}
        )


class AsyncPrograms(AsyncResource):
    async def list(self) -> List[Program]:
        payload = await self._transport.request("GET", "/api/ptf/programs") or []
        items = payload if isinstance(payload, list) else payload.get("programs", [])
        return [Program.model_validate(p) for p in items]

    async def create(
        self,
        name: str,
        description: str | None = None,
        color: str = "#3b82f6",
    ) -> Program:
        body: dict[str, object] = {"name": name, "color": color}
        if description is not None:
            body["description"] = description
        return Program.model_validate(
            await self._transport.request("POST", "/api/ptf/programs", json=body) or {}
        )

    async def get(self, program_id: int) -> ProgramDetail:
        return ProgramDetail.model_validate(
            await self._transport.request("GET", f"/api/ptf/programs/{program_id}") or {}
        )

    async def update(self, program_id: int, **fields: object) -> Program:
        return Program.model_validate(
            await self._transport.request("PATCH", f"/api/ptf/programs/{program_id}", json=fields) or {}
        )

    async def archive(self, program_id: int) -> bool:
        try:
            await self._transport.request("DELETE", f"/api/ptf/programs/{program_id}")
            return True
        except LigandAILegalAcceptanceRequired:
            # ⛔ A legal 403 must not read as "server refused".
            raise
        except Exception:
            return False

    async def workstreams(self, program_id: int | None = None) -> List[Workstream]:
        params = {"program_id": program_id} if program_id is not None else None
        payload = await self._transport.request("GET", "/api/ptf/workstreams", params=params) or []
        items = payload if isinstance(payload, list) else payload.get("workstreams", [])
        return [Workstream.model_validate(w) for w in items]

    async def create_workstream(
        self,
        program_id: int,
        name: str,
        description: str | None = None,
        color: str = "#3b82f6",
        genes: List[str] | None = None,
    ) -> Workstream:
        body: dict[str, object] = {
            "programId": program_id,
            "name": name,
            "color": color,
        }
        if description is not None:
            body["description"] = description
        if genes is not None:
            body["genes"] = genes
        return Workstream.model_validate(
            await self._transport.request("POST", "/api/ptf/workstreams", json=body) or {}
        )

    async def list_sessions(self, gene: str | None = None, limit: int = 20) -> List[Session]:
        params: dict[str, object] = {"limit": limit}
        if gene is not None:
            params["gene"] = gene
        payload = await self._transport.request("GET", "/api/ptf/sessions", params=params) or []
        items = payload if isinstance(payload, list) else payload.get("sessions", [])
        return [Session.model_validate(s) for s in items]

    async def get_session(self, session_id: str) -> SessionDetail:
        return SessionDetail.model_validate(
            await self._transport.request("GET", f"/api/ptf/sessions/{session_id}") or {}
        )

    async def find_session_by_gene(self, gene: str) -> Session | None:
        payload = await self._transport.request("GET", f"/api/ptf/sessions/by-gene/{gene}")
        if not payload:
            return None
        return Session.model_validate(payload)

    # ------------------------------------------------------------------
    # Program sharing — async twins. See the sync class for docstrings.
    # ------------------------------------------------------------------

    async def shared_with_me(self) -> List[SharedProgram]:
        """Async variant of :meth:`Programs.shared_with_me`."""
        payload = await self._transport.request("GET", "/api/ptf/programs/shared-with-me") or []
        items = payload if isinstance(payload, list) else payload.get("programs", [])
        return [SharedProgram.model_validate(p) for p in items]

    async def access(self, program_id: int) -> ProgramAccess:
        """Async variant of :meth:`Programs.access`."""
        program_id = _validate_program_id(program_id)
        return ProgramAccess.model_validate(
            await self._transport.request("GET", f"/api/ptf/programs/{program_id}/access") or {}
        )

    async def shares(self, program_id: int) -> List[ProgramShare]:
        """Async variant of :meth:`Programs.shares`."""
        program_id = _validate_program_id(program_id)
        payload = await self._transport.request(
            "GET", f"/api/ptf/programs/{program_id}/shares"
        ) or []
        items = payload if isinstance(payload, list) else payload.get("shares", [])
        return [ProgramShare.model_validate(s) for s in items]

    async def share(
        self,
        program_id: int,
        *,
        user_email: str | None = None,
        org_id: int | None = None,
        permission: ProgramPermission = "view",
    ) -> ProgramShare:
        """Async variant of :meth:`Programs.share`."""
        program_id = _validate_program_id(program_id)
        if (user_email is None) == (org_id is None):
            raise LigandAIValidationError(
                "share() requires exactly one of user_email= or org_id= "
                f"(got user_email={user_email!r}, org_id={org_id!r})"
            )
        body: dict[str, object] = {"permission": permission}
        if user_email is not None:
            body["userEmail"] = user_email
        if org_id is not None:
            body["orgId"] = org_id
        return ProgramShare.model_validate(
            await self._transport.request(
                "POST", f"/api/ptf/programs/{program_id}/shares", json=body
            )
            or {}
        )

    async def update_share(
        self, program_id: int, share_id: int, permission: ProgramPermission
    ) -> ProgramShare:
        """Async variant of :meth:`Programs.update_share`."""
        program_id = _validate_program_id(program_id)
        share_id = _validate_positive_int(share_id, "share_id")
        return ProgramShare.model_validate(
            await self._transport.request(
                "PATCH",
                f"/api/ptf/programs/{program_id}/shares/{share_id}",
                json={"permission": permission},
            )
            or {}
        )

    async def revoke_share(self, program_id: int, share_id: int) -> bool:
        """Async variant of :meth:`Programs.revoke_share`. Same
        never-swallowed-to-False error contract."""
        program_id = _validate_program_id(program_id)
        share_id = _validate_positive_int(share_id, "share_id")
        await self._transport.request(
            "DELETE", f"/api/ptf/programs/{program_id}/shares/{share_id}"
        )
        return True

    async def invite(
        self,
        program_id: int,
        email: str,
        *,
        permission: ProgramPermission = "view",
        expires_in_days: int | None = None,
    ) -> ProgramInviteCreated:
        """Async variant of :meth:`Programs.invite`."""
        program_id = _validate_program_id(program_id)
        email = email.strip()
        if not email or "@" not in email:
            raise LigandAIValidationError(f"invite() requires a valid email (got {email!r})")
        body: dict[str, object] = {"email": email, "permission": permission}
        if expires_in_days is not None:
            body["expiresInDays"] = expires_in_days
        return ProgramInviteCreated.model_validate(
            await self._transport.request(
                "POST", f"/api/ptf/programs/{program_id}/invites", json=body
            )
            or {}
        )

    async def invites(self, program_id: int) -> List[ProgramInvite]:
        """Async variant of :meth:`Programs.invites`."""
        program_id = _validate_program_id(program_id)
        payload = await self._transport.request(
            "GET", f"/api/ptf/programs/{program_id}/invites"
        ) or []
        items = payload if isinstance(payload, list) else payload.get("invites", [])
        return [ProgramInvite.model_validate(i) for i in items]

    async def revoke_invite(self, program_id: int, invite_id: int) -> bool:
        """Async variant of :meth:`Programs.revoke_invite`. Same
        never-swallowed-to-False error contract."""
        program_id = _validate_program_id(program_id)
        invite_id = _validate_positive_int(invite_id, "invite_id")
        await self._transport.request(
            "DELETE", f"/api/ptf/programs/{program_id}/invites/{invite_id}"
        )
        return True

    async def accept_invite(self, token: str) -> ProgramInviteAcceptResult:
        """Async variant of :meth:`Programs.accept_invite`."""
        token = token.strip()
        if not token:
            raise LigandAIValidationError("accept_invite() requires a non-empty token")
        return ProgramInviteAcceptResult.model_validate(
            await self._transport.request(
                "POST", "/api/ptf/share-invites/accept", json={"token": token}
            )
            or {}
        )
