# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""``client.programs`` against the bodies production blue really sends.

``programs.get`` / ``access`` / ``get_session`` / ``share`` / ``invite`` validated
the whole ``{"success": true, "<key>": {...}}`` envelope into a model whose fields
are all optional -- a program with a name and a gene list came back with every
field ``None`` -- and ``Session.id`` was required under a name the server never
sends. Bodies are trimmed copies of 2026-10-02 reads from ``http://127.0.0.1:5050``
(keys verbatim, rows cut); the write-route bodies (share / invite / accept) are
taken from ``server/program-share-routes.ts``, since exercising them live would
mutate someone's access. [bd-dre-3imtv.4]
"""

from __future__ import annotations

from typing import Any

import pytest
from pytest_httpx import HTTPXMock

from ligandai import AsyncLigandAI, LigandAI
from ligandai.errors import LigandAIUnreadableResponse, LigandAIValidationError
from ligandai.types import Program, Session

BASE = "http://api.ligandai.test"
NOW = "2026-10-02T18:14:29.555Z"

PROGRAMS_LIST = {
    "success": True,
    "totalPrograms": 268,
    "programs": [
        {
            "programId": "program_1790966093849_4dn8d2",
            "name": "EGFR Challenge Lab",
            "status": "active",
            "geneCount": 1,
            "createdAt": NOW,
            "genes": [{"gene": "EGFR", "status": "queued"}],
        }
    ],
}
PROGRAM_BY_ID = {
    "success": True,
    "program": {
        "id": 1011,
        "programId": "program_1779380960039_6f098c18",
        "userId": "dev-local-dre",
        "name": "Ovarian Longevity & Regeneration",
        "status": "active",
        "conversationId": "517bacdc-e719-4c40-96bd-2e2ebb790e0c",
    },
}
ACCESS = {
    "success": True,
    "access": {
        "programId": 1254,
        "effective": "owner",
        "provenance": "owner",
        "can": {"read": True, "comment": True, "mutate": True, "reshare": True,
                "delete": True, "spendCredits": True, "transfer": True},
    },
}
SESSIONS_LIST = {
    "success": True,
    "count": 1,
    "sessions": [
        {
            "session_id": "session_parallel_1790964364610_41222069",
            "user_id": "dev-local-dre",
            "gene": "EGFR",
            "created_at": "2026-10-02T18:06:04.616Z",
            "status": "generation_complete",
            "total_peptides": 5,
        }
    ],
}
SESSION_ONE = {
    "success": True,
    "session": {
        "session_id": "session_parallel_1790964869524_fbc1a237",
        "gene": "EGFR",
        "created_at": "2026-10-02T18:14:29.536Z",
        "status": "generation_complete",
        "peptides": {"EGFR": [{"sequence": "PDDKKYVTGHIYSRAVLTA"}]},
    },
}
BY_GENE = {
    "success": True,
    "gene": "EGFR",
    "totalSessions": 232,
    "totalPeptides": 882368,
    "sessions": [
        {"sessionId": "session_parallel_1790964869524_fbc1a237", "gene": "EGFR",
         "peptideCount": 4, "createdAt": NOW, "status": "running"},
        {"sessionId": "session_parallel_1790964364610_41222069", "gene": "EGFR",
         "peptideCount": 5, "createdAt": "2026-10-02T18:06:04.616Z", "status": "generation_complete"},
    ],
}


def _mk(key: str = "lgai_pro_testkey0123456789ABCDEF") -> LigandAI:
    return LigandAI(api_key=key, base_url=BASE, max_retries=1)


def test_programs_list_exposes_a_usable_reference_for_handle_only_programs(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(url=f"{BASE}/api/ptf/programs", json=PROGRAMS_LIST)
    (p,) = _mk().programs.list()
    assert p.id is None  # the server sent no numeric id for this program
    assert p.program_id == "program_1790966093849_4dn8d2"
    assert p.ref == "program_1790966093849_4dn8d2"
    assert Program(id=7, programId="program_x").ref == 7  # numeric id wins


def test_programs_get_unwraps_the_program_envelope(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=f"{BASE}/api/ptf/programs/1011", json=PROGRAM_BY_ID)
    d = _mk().programs.get(1011)
    assert d.id == 1011
    assert d.name == "Ovarian Longevity & Regeneration"  # was None: whole body validated
    assert d.status == "active"
    assert d.program_id == "program_1779380960039_6f098c18"


def test_programs_get_accepts_the_handle_that_list_returns(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/ptf/programs/program_1790966093849_4dn8d2",
        json={"success": True, "program": PROGRAMS_LIST["programs"][0]},
    )
    assert _mk().programs.get("program_1790966093849_4dn8d2").name == "EGFR Challenge Lab"


def test_programs_get_refuses_a_body_it_cannot_read_instead_of_an_empty_record(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(url=f"{BASE}/api/ptf/programs/9", json={"success": True})
    with pytest.raises(LigandAIUnreadableResponse):
        _mk().programs.get(9)


def test_programs_access_unwraps_and_takes_a_handle(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/ptf/programs/program_1790966093849_4dn8d2/access", json=ACCESS
    )
    a = _mk().programs.access("program_1790966093849_4dn8d2")
    assert a.program_id == 1254
    assert a.effective == "owner"
    assert a.can.reshare is True


@pytest.mark.parametrize("bad", ["abc", "", "program", "../etc", 0, -3, None])
def test_program_id_guard_still_rejects_garbage(bad: Any) -> None:
    with pytest.raises(LigandAIValidationError):
        _mk().programs.access(bad)


def test_list_sessions_reads_snake_case_rows(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=f"{BASE}/api/ptf/sessions?limit=20", json=SESSIONS_LIST)
    (s,) = _mk().programs.list_sessions()
    assert s.id == "session_parallel_1790964364610_41222069"
    assert s.gene == "EGFR"
    assert s.created_at is not None and s.created_at.year == 2026


def test_get_session_unwraps_the_session_envelope(httpx_mock: HTTPXMock) -> None:
    sid = "session_parallel_1790964869524_fbc1a237"
    httpx_mock.add_response(url=f"{BASE}/api/ptf/sessions/{sid}", json=SESSION_ONE)
    d = _mk().programs.get_session(sid)
    assert d.id == sid
    assert d.status == "generation_complete"
    assert d.model_extra and "peptides" in d.model_extra


def test_find_session_by_gene_returns_the_newest_session_row(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=f"{BASE}/api/ptf/sessions/by-gene/EGFR", json=BY_GENE)
    s = _mk().programs.find_session_by_gene("EGFR")
    assert s is not None
    assert s.id == "session_parallel_1790964869524_fbc1a237"  # first = newest


def test_find_session_by_gene_is_none_only_when_the_server_lists_none(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/ptf/sessions/by-gene/ZZZ",
        json={"success": True, "gene": "ZZZ", "totalSessions": 0, "sessions": []},
    )
    assert _mk().programs.find_session_by_gene("ZZZ") is None


def test_find_session_by_gene_refuses_an_unrecognised_body(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=f"{BASE}/api/ptf/sessions/by-gene/EGFR", json={"success": True})
    with pytest.raises(LigandAIUnreadableResponse):
        _mk().programs.find_session_by_gene("EGFR")


def test_session_model_accepts_every_id_spelling_the_server_uses() -> None:
    assert Session.model_validate({"id": "a"}).id == "a"
    assert Session.model_validate({"session_id": "b"}).id == "b"
    assert Session.model_validate({"sessionId": "c", "createdAt": NOW}).created_at is not None
    with pytest.raises(Exception):
        Session.model_validate({"gene": "EGFR"})  # still needs SOME id


def test_programs_shares_refuses_a_wrong_envelope_key_instead_of_zero_rows(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(url=f"{BASE}/api/ptf/programs/5/shares", json={"success": True, "grants": []})
    with pytest.raises(LigandAIUnreadableResponse):
        _mk().programs.shares(5)


def test_share_update_share_and_invite_read_the_server_envelopes(httpx_mock: HTTPXMock) -> None:
    share = {"id": 31, "programId": 5, "sharedWithUserEmail": "a@b.org", "permission": "view"}
    httpx_mock.add_response(
        url=f"{BASE}/api/ptf/programs/5/shares", method="POST", status_code=201,
        json={"success": True, "share": share},
    )
    httpx_mock.add_response(
        url=f"{BASE}/api/ptf/programs/5/shares/31", method="PATCH",
        json={"success": True, "share": {**share, "permission": "edit"}},
    )
    httpx_mock.add_response(
        url=f"{BASE}/api/ptf/programs/5/invites", method="POST", status_code=201,
        json={
            "success": True,
            "invite": {"id": 9, "programId": 5, "email": "c@d.org", "permission": "view"},
            "inviteUrl": "/invites/accept?token=abc",
        },
    )
    c = _mk()
    assert c.programs.share(5, user_email="a@b.org").id == 31
    assert c.programs.update_share(5, 31, "edit").permission == "edit"
    inv = c.programs.invite(5, "c@d.org")
    assert inv.id == 9
    assert inv.invite_url == "/invites/accept?token=abc"  # sits BESIDE the invite object


def test_accept_invite_flattens_the_share_and_keeps_access_blocked(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/ptf/share-invites/accept", method="POST",
        json={"success": True, "share": {"id": 31, "programId": 5, "permission": "view"},
              "accessBlocked": True, "reason": "pending_approval"},
    )
    r = _mk().programs.accept_invite("tok")
    assert (r.program_id, r.share_id, r.permission) == (5, 31, "view")
    assert r.model_extra and r.model_extra.get("accessBlocked") is True


async def test_async_programs_use_the_same_envelopes(
    httpx_mock: HTTPXMock, async_client: AsyncLigandAI
) -> None:
    httpx_mock.add_response(url=f"{BASE}/api/ptf/programs/1011", json=PROGRAM_BY_ID)
    httpx_mock.add_response(url=f"{BASE}/api/ptf/sessions/by-gene/EGFR", json=BY_GENE)
    httpx_mock.add_response(
        url=f"{BASE}/api/ptf/programs/program_1790966093849_4dn8d2/access", json=ACCESS
    )
    assert (await async_client.programs.get(1011)).name == "Ovarian Longevity & Regeneration"
    s = await async_client.programs.find_session_by_gene("EGFR")
    assert s is not None and s.id.startswith("session_parallel_")
    a = await async_client.programs.access("program_1790966093849_4dn8d2")
    assert a.effective == "owner"
