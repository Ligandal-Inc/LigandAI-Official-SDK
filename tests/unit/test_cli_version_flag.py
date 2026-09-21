# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""The installed CLI must be able to state its own version. [bd-dre-rp11j.1]

Observed live 2026-09-20, against BOTH a clean-venv `pip install ligandai`
(0.12.2 from PyPI) and a clean install of the locally-built 0.13.0 wheel:

    $ ligandai --version
    usage: ligandai [-h] [--base-url URL] {keys,credits,campaign,legal} ...
    ligandai: error: unrecognized arguments: --version
    $ echo $?
    2

`--version` is the first thing anyone types when a bug report needs a version,
and it is the only version affordance a CLI user has — `pip show ligandai`
answers for the environment, not for the binary that is actually on PATH, and
those differ whenever more than one venv is in play. Exit 2 with an argparse
usage dump is the "I could not look" shape this bead exists to remove: it is
byte-identical to what a genuine typo produces.

OWNERSHIP: the fix is three lines inside `ligandai/cli.py`, which is owned by
worker W3 (dre-rp11j.3) and was under active edit while this file was written.
This guard is deliberately NOT the fix. It is marked `xfail(strict=True)`, which
means:

  * today it reports XFAIL, so it does not redden anyone's gate; and
  * the moment W3 lands `--version`, it reports XPASS — and a STRICT xfail that
    passes is a FAILURE. Whoever fixes the CLI is forced to come here and delete
    the marker. The guard cannot be silently outlived.

The patch W3 needs, in `_build_parser()` (around cli.py:231):

    from ligandai._version import __version__
    parser.add_argument(
        "--version", action="version",
        version=f"ligandai {__version__}",
    )
"""

from __future__ import annotations

import subprocess
import sys



def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    """Invoke the CLI the way a customer does — as a process, through main()."""
    return subprocess.run(
        [sys.executable, "-m", "ligandai.cli", *args],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_cli_reports_its_own_version() -> None:
    """W3 landed `--version` in ligandai/cli.py (dre-rp11j.3); the strict-xfail
    marker W1 left here has been removed in the same change, as instructed."""
    import ligandai

    proc = _run_cli("--version")
    assert proc.returncode == 0, (
        f"`ligandai --version` exited {proc.returncode}.\n"
        f"stdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
    )
    assert ligandai.__version__ in (proc.stdout + proc.stderr), (
        f"`ligandai --version` did not print {ligandai.__version__}.\n"
        f"stdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
    )


def test_cli_help_exits_zero_and_names_its_commands() -> None:
    """`--help` is the one path that already works; keep it that way."""
    proc = _run_cli("--help")
    assert proc.returncode == 0, f"`ligandai --help` exited {proc.returncode}"
    assert "usage: " in proc.stdout


def test_cli_rejects_an_unknown_subcommand_distinguishably() -> None:
    """An invalid command must fail non-zero AND name the valid choices.

    A CLI that accepts an unknown verb, or that fails without saying what it
    would have accepted, is the interactive form of an accepted-but-inert arg.
    """
    proc = _run_cli("definitely-not-a-command")
    assert proc.returncode != 0, (
        "the CLI accepted an unknown subcommand — an unrecognised verb must never "
        f"exit 0. stdout: {proc.stdout!r}"
    )
    combined = proc.stdout + proc.stderr
    assert "invalid choice" in combined, f"error did not name the problem: {combined!r}"
    assert "definitely-not-a-command" in combined, (
        f"error did not echo back what the user typed: {combined!r}"
    )


def test_bare_invocation_does_not_report_success() -> None:
    """`ligandai` with no command did nothing and exited 0. [bd-dre-rp11j.28]

    MEASURED 2026-09-21 through the INSTALLED console script (`<venv>/bin/ligandai`,
    not `python -m`, because an entry point never runs `if __name__ ==
    "__main__"` and the Node twin's inertness hid behind exactly that
    distinction):

        argv=[--version]    rc=0  stdout=15B   stderr=0B
        argv=[--help]       rc=0  stdout=796B  stderr=0B
        argv=[]             rc=0  stdout=796B  stderr=0B   <-- this one
        argv=[frobnicate]   rc=2  stdout=0B    stderr=217B

    `ligandai $CMD` with an unset CMD printed usage and told the calling script
    the work succeeded. It also contradicted this same file's `_incomplete()`,
    which already exits 2 for `ligandai keys` / `campaign` / `legal`. The Node
    SDK settled on the same contract under bd-dre-rp11j.29.
    """
    proc = _run_cli()
    assert proc.returncode == 2, (
        f"`ligandai` with no arguments exited {proc.returncode}. A bare invocation "
        f"does nothing, so it must not report success — a shell doing "
        f"`ligandai $CMD` with an unset CMD would read 0 as 'done'.\n"
        f"stdout: {proc.stdout[:400]!r}\nstderr: {proc.stderr[:400]!r}"
    )
    assert proc.stderr, "a bare invocation must say something on stderr"
    assert "no command given" in proc.stderr
    assert "nothing was done" in proc.stderr
    assert "--help" in proc.stderr, "the error must name how to discover the commands"


def test_help_still_exits_zero_after_the_bare_invocation_change() -> None:
    """The fix above must not catch `--help` / `-h`, which are requests that
    argparse satisfies before dispatch."""
    for flag in ("--help", "-h"):
        proc = _run_cli(flag)
        assert proc.returncode == 0, f"`ligandai {flag}` exited {proc.returncode}"
        assert "usage: " in proc.stdout
        assert proc.stdout, f"`ligandai {flag}` produced no stdout"
