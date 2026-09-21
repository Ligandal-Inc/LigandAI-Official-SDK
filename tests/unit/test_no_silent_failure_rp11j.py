# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Guards for the SILENT-FAILURE class. [bd-dre-rp11j.3]

Origin: a six-week silent no-op. A loader flattened two distinct import
faults — "'X' is not a package" (a real module-name collision) and "No module
named 'X'" (genuinely absent) — into one opaque "ENGINE_UNAVAILABLE" with
``proposed: 0``. Nobody noticed, because a zero produced by a broken lookup is
indistinguishable from a zero produced by an honest measurement.

Every test here was watched RED before its fix landed. They assert the three
properties that make that class impossible:

1. An accepted parameter is either WIRED UP or says so, naming itself.
2. "unavailable / empty / zero / None" is distinguishable from "I could not
   look", with distinct NAMED causes — never one catch-all.
3. No bare ``except`` returns a falsy value on a path a caller reads as a
   measurement.
"""

from __future__ import annotations

import ast
import contextlib
import inspect
import textwrap
import warnings

import pytest

from ligandai import LigandAI
from ligandai.errors import (
    LigandAIDiagnosticWarning,
    LigandAIEdgeBlocked,
    LigandAIError,
    LigandAIForbidden,
    LigandAIInvalidConfig,
    LigandAIMutationUnconfirmed,
    LigandAISurfaceNotInThisPackage,
    LigandAIUnreadableResponse,
    error_from_response,
)

BASE = "http://api.ligandai.test"
KEY = "lgai_pro_testkey0123456789ABCDEF"


def _client(**kw):
    kw.setdefault("check_entitlement", False)
    return LigandAI(api_key=KEY, base_url=BASE, max_retries=1, **kw)


# ── 1. THE ORIGINAL DEFECT: two import faults, one opaque message ───────────


def test_import_shadowing_surfaces_its_real_reason_not_a_generic_unavailable() -> None:
    """The guard that would have caught the original six-week no-op.

    A module-name COLLISION ("'x' is not a package") and a module genuinely
    ABSENT ("No module named 'x'") are different faults with different fixes:
    the first means something is shadowing the real module on sys.path, the
    second means it was never installed. Python distinguishes them; the loader
    that started this epic did not, and reported both as one
    "ENGINE_UNAVAILABLE" with a count of zero.

    This asserts the distinction is REACHABLE — i.e. that anything catching
    ImportError has the information needed to tell them apart, and so has no
    excuse for collapsing them.
    """
    # A real module-name collision: `ast` is a module, not a package, so
    # importing a submodule of it is the "is not a package" fault.
    with pytest.raises(ModuleNotFoundError) as collision:
        __import__("ast.not_a_submodule", fromlist=["x"])

    with pytest.raises(ModuleNotFoundError) as absent:
        __import__("ligandai_module_that_does_not_exist_rp11j")

    collision_msg = str(collision.value)
    absent_msg = str(absent.value)

    assert "is not a package" in collision_msg, collision_msg
    assert "No module named" in absent_msg, absent_msg
    # The whole point: the two faults do NOT produce the same string.
    assert collision_msg != absent_msg
    # ...and each names the module it was actually looking for.
    assert "ast" in collision_msg
    assert "ligandai_module_that_does_not_exist_rp11j" in absent_msg


def test_sdk_import_failure_is_not_swallowed_into_a_falsy_value() -> None:
    """Importing the package must FAIL LOUDLY, never degrade to a stub.

    A package that imports "successfully" while some namespace silently
    resolved to None is the same defect one layer up.
    """
    import ligandai

    for name in ("LigandAI", "AsyncLigandAI", "LigandAIError"):
        assert getattr(ligandai, name, None) is not None, (
            f"ligandai.{name} resolved to None — a namespace that imports "
            f"cleanly but is empty is indistinguishable from a working one"
        )


# ── 2. ACCEPTED-BUT-INERT PARAMETERS ────────────────────────────────────────


def _unread_params(func) -> list[str]:
    """Parameters declared by ``func`` and never LOADED in its body.

    An AST check, not a grep: ``target`` appearing inside the word
    "off-target" in a docstring is not a read, and that false positive is
    exactly why this defect survived review.
    """
    src = textwrap.dedent(inspect.getsource(func))
    fn = ast.parse(src).body[0]
    assert isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
    loaded = {
        n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    }
    declared = [
        a.arg
        for a in list(fn.args.args) + list(fn.args.kwonlyargs)
        if a.arg not in ("self", "cls")
    ]
    return [p for p in declared if p not in loaded]


@pytest.mark.parametrize(
    "dotted",
    [
        "resources.campaigns:Campaigns.contrastive_references",
        "resources.campaigns:Campaigns.fold_panel",
        "resources.ensemble:Ensemble.run",
        "resources.ensemble:AsyncEnsemble.run",
    ],
)
def test_no_declared_parameter_is_silently_unread(dotted: str) -> None:
    """An accepted parameter that is never read is the defect, full stop.

    Watched RED: ``Campaigns.contrastive_references`` declared ``target`` and
    never loaded it, so passing the target's own gene in ``paralogs`` built a
    contrastive panel containing the target — steering generation AWAY from
    the receptor it was designing against, silently.
    ``AsyncEnsemble.run`` declared ``timeout`` and never loaded it while its
    sync twin enforced it.
    """
    import importlib

    modpath, _, attr = dotted.partition(":")
    cls_name, meth = attr.split(".")
    mod = importlib.import_module(f"ligandai.{modpath}")
    func = getattr(getattr(mod, cls_name), meth)

    unread = _unread_params(func)
    assert not unread, (
        f"{dotted} accepts {unread} and never reads them. Either wire the "
        f"parameter up, or raise/warn NAMING IT — silently ignoring a "
        f"caller's argument is the defect."
    )


def test_inert_parameter_that_cannot_be_wired_must_name_itself() -> None:
    """``include_unfolded`` has no effect on the endpoint. That is allowed
    ONLY because it now says so, naming itself.

    Watched RED: the parameter was accepted, absent from ``params``, and
    documented as inert in a docstring — which is not a runtime signal. A
    caller passing it got a folded-only list that looks exactly like a
    complete one and would read the missing designs as "there are none".
    """
    c = _client()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with contextlib.suppress(LigandAIError):
            # No server here; the warning must fire before any request.
            c.peptides.list(gene="EGFR", include_unfolded=True)
    msgs = [str(w.message) for w in caught if issubclass(w.category, LigandAIDiagnosticWarning)]
    assert msgs, "include_unfolded=True was accepted with no warning at all"
    assert "include_unfolded" in msgs[0], "the warning must NAME the parameter"
    assert "NO EFFECT" in msgs[0].upper()
    c.close()


def test_dead_method_cannot_ship() -> None:
    """``list_by_program`` referenced an unbound name and raised NameError on
    every call, on BOTH twins. A public method that can never run is the
    loudest possible accepted-but-inert.
    """
    for cls_name in ("Peptides", "AsyncPeptides"):
        import ligandai.resources.peptides as mod

        func = getattr(mod, cls_name).list_by_program
        src = textwrap.dedent(inspect.getsource(func))
        fn = ast.parse(src).body[0]
        declared = {a.arg for a in list(fn.args.args) + list(fn.args.kwonlyargs)}
        declared |= {"self"}
        # Every Name loaded in the body must be resolvable: a parameter, a
        # local assignment, or a module/builtin global.
        assigned = {
            t.id for n in ast.walk(fn) for t in ast.walk(n)
            if isinstance(t, ast.Name) and isinstance(t.ctx, ast.Store)
        }
        import builtins
        globals_ = set(vars(mod)) | set(dir(builtins))
        loaded = {
            n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
        }
        unresolvable = loaded - declared - assigned - globals_ - set(dir(mod))
        assert not unresolvable, (
            f"{cls_name}.list_by_program loads unbound name(s) {unresolvable} — "
            f"it raises NameError before any HTTP call and can never have worked"
        )


# ── 3. EMPTY vs COULD-NOT-LOOK ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("ctype", "body", "expected_cause"),
    [
        ("text/html; charset=utf-8", "<html><body>login</body></html>", "non_json_content_type"),
        ("application/json", "{not json", "malformed_json"),
        ("application/json", "null", "null_document"),
        ("application/json", "", "empty_body"),
    ],
)
def test_success_status_with_unreadable_body_raises_a_named_cause(
    httpx_mock, ctype: str, body: str, expected_cause: str
) -> None:
    """A 200 whose body cannot be read must NEVER return a falsy value.

    Watched RED: all four of these returned ``None`` from ``request()``. Every
    resource method writes ``... or {}`` / ``or []`` over that, so an HTML
    login page from a proxy arrived at the caller as ``designs: []`` —
    a clean, plausible, WRONG measurement of zero.

    Each cause gets its OWN name. One catch-all is what this epic exists to
    eliminate.
    """
    httpx_mock.add_response(
        method="GET", url=f"{BASE}/api/probe", status_code=200,
        headers={"content-type": ctype}, content=body.encode(),
    )
    c = _client()
    with pytest.raises(LigandAIUnreadableResponse) as e:
        c.transport.request("GET", "/api/probe")
    assert e.value.cause == expected_cause
    # The error must say what was attempted and what to do about it.
    text = str(e.value)
    assert "GET" in text and "/api/probe" in text
    assert "what to do" in text
    assert "NOT an empty result" in text
    c.close()


def test_command_tolerates_an_empty_ack_but_a_measurement_does_not(httpx_mock) -> None:
    """A DELETE returning a bare 200 is a normal ack; a GET returning nothing
    is a measurement that failed. They must not behave the same."""
    httpx_mock.add_response(
        method="DELETE", url=f"{BASE}/api/thing/1", status_code=200,
        headers={"content-type": "application/json"}, content=b"",
    )
    c = _client()
    assert c.transport.request("DELETE", "/api/thing/1") is None  # ack, fine
    c.close()


def test_non_json_error_body_is_surfaced_not_discarded() -> None:
    """[bd-dre-2d7za.9] A legal 403 rewritten by a proxy used to degrade to
    the string "HTTP 403" with the acceptance URL thrown away."""
    err = error_from_response(
        403, None,
        raw_body="Forbidden: accept the terms at https://ligandai.com/legal/accept",
        content_type="text/plain",
    )
    assert "ligandai.com/legal/accept" in str(err), (
        "the only actionable thing in the body was discarded"
    )
    assert str(err) != "HTTP 403"


def test_cdn_block_is_not_reported_as_an_authorization_failure() -> None:
    """[bd-dre-3fduj] A Cloudflare Bot Fight Mode challenge is an HTTP 403
    with an HTML body. Reporting it as LigandAIForbidden("HTTP 403") sends
    the user to rotate an API key that was never read."""
    err = error_from_response(
        403, None,
        raw_body="<html><title>Just a moment...</title>"
                 "<h1>Sorry, you have been blocked</h1>"
                 "<p>Cloudflare Ray ID: 8f2a1b3c4d5e6f70</p></html>",
        content_type="text/html",
        headers={"Server": "cloudflare", "CF-RAY": "8f2a1b3c4d5e6f70-SJC"},
        method="GET", url="https://ligandai.com/api/credits",
    )
    assert isinstance(err, LigandAIEdgeBlocked)
    assert isinstance(err, LigandAIForbidden)  # back-compat for existing handlers
    assert err.edge == "cloudflare"
    assert err.ray_id == "8f2a1b3c4d5e6f70-SJC"
    assert "was NOT the problem" in str(err)


def test_a_json_403_from_our_own_server_is_still_an_ordinary_forbidden() -> None:
    """The edge check must not reclassify a real authorization refusal that
    merely PASSED THROUGH a CDN."""
    err = error_from_response(
        403, {"error": "pilot_restricted", "message": "not in the pilot"},
        raw_body='{"error":"pilot_restricted"}',
        content_type="application/json",
        headers={"Server": "cloudflare", "CF-RAY": "abc-SJC"},
    )
    assert isinstance(err, LigandAIForbidden)
    assert not isinstance(err, LigandAIEdgeBlocked)
    assert err.reason == "pilot_restricted"


# ── 4. NO BARE except RETURNING A FALSY MEASUREMENT ─────────────────────────


@pytest.mark.parametrize("status", [401, 403, 429, 500, 502, 503])
def test_mutation_does_not_report_an_unknown_outcome_as_a_refusal(
    httpx_mock, status: int
) -> None:
    """``False`` from a ``-> bool`` mutation must mean "the server declined".

    Watched RED: ten mutation methods ended in ``except Exception: return
    False``, so a dropped connection, an expired key, a CDN block and a 500
    all said "refused". ``job.cancel()`` answering False after a blip told a
    user their GPU job was still running when it may well have been cancelled.
    """
    httpx_mock.add_response(
        method="POST", url=f"{BASE}/api/jobs/job_1/cancel", status_code=status,
        json={"message": "boom"},
    )
    c = _client()
    with pytest.raises(LigandAIError) as e:
        c.jobs.cancel("job_1")
    # Whatever it is, it must NOT be a silent False, and it must say the
    # outcome is unknown rather than implying a refusal.
    if isinstance(e.value, LigandAIMutationUnconfirmed):
        assert "UNKNOWN" in str(e.value)
        assert e.value.operation == "jobs.cancel"
    c.close()


@pytest.mark.parametrize("status", [400, 404, 409, 410, 422])
def test_mutation_still_returns_false_when_the_server_genuinely_declined(
    httpx_mock, status: int
) -> None:
    """The bool contract is PRESERVED for a real 'no' — the server was
    reached, understood the request, and refused it."""
    httpx_mock.add_response(
        method="POST", url=f"{BASE}/api/jobs/job_1/cancel", status_code=status,
        json={"message": "already finished"},
    )
    c = _client()
    assert c.jobs.cancel("job_1") is False
    c.close()


def test_mutation_raises_rather_than_returning_false_on_a_dead_connection() -> None:
    """A connection that never landed is the purest 'I could not look'."""
    c = LigandAI(api_key=KEY, base_url="http://127.0.0.1:1", max_retries=1,
                 check_entitlement=False)
    with pytest.raises(LigandAIError):
        c.jobs.cancel("job_1")
    c.close()


# ── 5. SILENT FALLBACKS ─────────────────────────────────────────────────────


def test_base_url_env_var_is_actually_honoured(monkeypatch) -> None:
    """``LIGANDAI_BASE_URL`` was read by two CLI sub-commands and by NOTHING
    in the library — while the SDK's own DNS-diagnosis message instructed
    users to set it. Accepted in the environment, inert in the request."""
    monkeypatch.setenv("LIGANDAI_BASE_URL", "http://origin.example:5050")
    monkeypatch.setenv("LIGANDAI_API_KEY", KEY)
    c = LigandAI(check_entitlement=False)
    assert c.base_url == "http://origin.example:5050"
    c.close()


def test_explicit_base_url_still_beats_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("LIGANDAI_BASE_URL", "http://env.example")
    c = LigandAI(api_key=KEY, base_url="http://explicit.example", check_entitlement=False)
    assert c.base_url == "http://explicit.example"
    c.close()


def test_unusable_base_url_raises_instead_of_falling_back(monkeypatch) -> None:
    """Silently substituting the default for a base URL the caller set would
    send the request somewhere other than where they said."""
    monkeypatch.setenv("LIGANDAI_BASE_URL", "ligandai.com")  # no scheme
    monkeypatch.setenv("LIGANDAI_API_KEY", KEY)
    with pytest.raises(LigandAIInvalidConfig) as e:
        LigandAI(check_entitlement=False)
    assert "LIGANDAI_BASE_URL" in str(e.value)
    assert "will NOT fall back" in str(e.value)


# ── 6. A NAMESPACE THAT EXISTS BUT IS NOT THE ONE YOU WANT ──────────────────


@pytest.mark.parametrize("surface", ["start", "mature", "leaderboard", "status", "stop", "stream"])
def test_platform_campaign_surface_is_named_not_a_bare_attribute_error(surface: str) -> None:
    """[bd-dre-rp11j.9] The platform exposes a campaign/maturation API; this
    package ships no client for it, but DOES ship an unrelated ``campaigns``
    namespace. A bare AttributeError reads as a typo, leaving "this package
    cannot do campaigns" indistinguishable from "I called it wrong"."""
    c = _client()
    with pytest.raises(LigandAISurfaceNotInThisPackage) as e:
        getattr(c.campaigns, surface)
    assert e.value.surface == surface
    assert "/api/campaign" in str(e.value)
    assert "not a typo" in str(e.value)
    c.close()


def test_a_genuine_typo_is_still_an_ordinary_attribute_error() -> None:
    c = _client()
    with pytest.raises(AttributeError) as e:
        _ = c.campaigns.totally_made_up_name
    assert not isinstance(e.value, LigandAISurfaceNotInThisPackage)
    c.close()


# ── 7. THREE AUTH CAUSES, THREE MESSAGES ────────────────────────────────────


def test_malformed_api_key_warns_and_names_the_prefix(monkeypatch) -> None:
    """Verified live 2026-09-21: the server rejects a key on SHAPE before it
    checks validity, and answers ``200`` with the body ``null`` — identical to
    what it sends when NO key is supplied. So a 64-hex credential and no
    credential at all produced the same failure, and neither named the
    expected ``lgai_`` prefix or the env var to set.

    This WARNS rather than raising on purpose: tier detection deliberately
    fails open on an unrecognised prefix
    (``test_client.py::test_unknown_prefix_yields_none_tier``) and the server
    is authoritative, so refusing to construct would be a wall this SDK had
    deliberately not built — and would lock out a future key prefix.
    """
    for v in ("LIGANDAI_API_KEY", "LIGANDAL_API_KEY", "LIGANDAI_TEST_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        c = LigandAI(api_key="0" * 64, check_entitlement=False)
    msgs = [str(w.message) for w in caught
            if issubclass(w.category, LigandAIDiagnosticWarning)]
    assert msgs, "a key that cannot possibly authenticate was accepted in silence"
    text = msgs[0]
    assert "lgai_" in text, "the warning must name the expected prefix"
    assert "LIGANDAI_API_KEY" in text, "it must name the env var"
    assert "0" * 64 not in text, "⛔ the credential must never appear in the message"
    # The documented fail-open contract is preserved.
    assert c.tier is None
    c.close()


def test_absent_and_invalid_keys_get_different_guidance(monkeypatch) -> None:
    """The three causes must be distinguishable. They are distinguishable in
    the REQUEST even when the server's RESPONSE is identical, so the guidance
    is derived from the key that was actually sent."""
    from ligandai.client import auth_guidance, classify_api_key

    assert classify_api_key(None) == "absent"
    assert classify_api_key("   ") == "absent"
    assert classify_api_key("0" * 64) == "malformed"
    assert classify_api_key("lgai_pro_abc") == "well_formed"

    absent = auth_guidance(None)
    malformed = auth_guidance("0" * 64)
    invalid = auth_guidance("lgai_pro_abc")
    assert absent != malformed != invalid
    assert absent != invalid
    assert "LIGANDAI_API_KEY" in absent
    assert "shape" in malformed
    assert "revoked" in invalid
    # Every one of them says where to get a key.
    for g in (absent, malformed, invalid):
        assert "billing?tab=api-keys" in g


def test_401_carries_key_specific_guidance(httpx_mock) -> None:
    """A bare "Unauthorized" does not say WHICH auth problem this is."""
    httpx_mock.add_response(
        method="GET", url=f"{BASE}/api/probe", status_code=401,
        json={"message": "Unauthorized"},
    )
    c = _client()
    with pytest.raises(LigandAIError) as e:
        c.transport.request("GET", "/api/probe")
    assert "billing?tab=api-keys" in str(e.value), (
        "a 401 must say where to get a working key"
    )
    c.close()


def test_a_200_null_names_the_auth_problem_not_a_schema_complaint(httpx_mock) -> None:
    """``Account.me()`` used to turn a ``null`` body into
    ``User.model_validate({})`` and raise "Field required: id" — a complaint
    about OUR schema for what is really "the server did not identify you"."""
    httpx_mock.add_response(
        method="GET", url=f"{BASE}/api/auth/user", status_code=200,
        headers={"content-type": "application/json"}, content=b"null",
    )
    c = _client()
    with pytest.raises(LigandAIUnreadableResponse) as e:
        c.account.me()
    assert e.value.cause == "null_document"
    text = str(e.value)
    assert "Field required" not in text, "must not surface a pydantic schema error"
    assert "could not tell who you are" in text
    c.close()


# ── 8. THE CLI MUST FAIL NON-ZERO AND SAY WHY ───────────────────────────────


def _run_cli(*args: str, env: dict | None = None):
    import os
    import subprocess
    import sys

    e = dict(os.environ)
    e["LIGANDAI_SKIP_VERSION_CHECK"] = "1"
    for v in ("LIGANDAI_API_KEY", "LIGANDAL_API_KEY", "LIGANDAI_TEST_API_KEY",
              "LIGANDAI_BASE_URL"):
        e.pop(v, None)
    if env:
        e.update(env)
    return subprocess.run(
        [sys.executable, "-m", "ligandai.cli", *args],
        capture_output=True, text=True, timeout=120, env=e,
    )


@pytest.mark.parametrize("command", ["keys", "campaign", "legal"])
def test_incomplete_subcommand_exits_nonzero(command: str) -> None:
    """⛔ These each printed help and exited 0 — a no-op reporting SUCCESS.

    The original wrote ``parser.parse_args([cmd, "--help"])`` then ``return
    1``; argparse's help action calls ``sys.exit(0)`` internally, so the
    ``return 1`` was dead code.
    """
    proc = _run_cli(command)
    assert proc.returncode != 0, (
        f"`ligandai {command}` did nothing and exited 0 — a script checking $? "
        f"is told it succeeded. stdout={proc.stdout!r}"
    )
    assert "incomplete" in (proc.stdout + proc.stderr).lower()


def test_cli_distinguishes_unreachable_from_unauthorized() -> None:
    """Different causes, different exit codes — a script must be able to
    branch without parsing prose."""
    unreachable = _run_cli("--base-url", "http://127.0.0.1:1", "credits",
                           env={"LIGANDAI_API_KEY": KEY})
    assert unreachable.returncode == 5, (
        f"an unreachable origin should exit 5, got {unreachable.returncode}: "
        f"{unreachable.stderr!r}"
    )
    assert "LigandAIConnectionError" in unreachable.stderr

    nokey = _run_cli("campaign", "run", "examples/campaign/ca12_selective_binders.yaml")
    assert nokey.returncode == 3, (
        f"a missing key should exit 3, got {nokey.returncode}: {nokey.stderr!r}"
    )
    assert "LIGANDAI_API_KEY" in nokey.stderr
    assert unreachable.returncode != nokey.returncode


def test_cli_version_does_not_silently_report_another_installation() -> None:
    """``ligandai --version`` exited 2 with an argparse usage dump. Fixing it
    by trusting ``importlib.metadata`` alone would have printed the version of
    whatever ``ligandai`` distribution happened to be installed, which on a
    source checkout is a DIFFERENT and older package — a precise, confident,
    wrong answer. When the two disagree the CLI must say so."""
    import ligandai
    from ligandai._version import __version_provenance__

    proc = _run_cli("--version")
    assert proc.returncode == 0
    assert ligandai.__version__ in proc.stdout
    if __version_provenance__:
        assert "provenance" in proc.stderr, (
            "the version could not be determined unambiguously and the CLI "
            "printed it anyway with no warning"
        )
