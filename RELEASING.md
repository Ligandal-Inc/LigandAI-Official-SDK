# Releasing `ligandai` to PyPI

> Traceability: `bd-dre-7x9bc`. Adopted 2026-07-30 while cutting 0.7.8.

**This repository is the ONLY tree that may publish the `ligandai` distribution.**
Two other trees in `/mnt/backup/LIGANDAI_ALPHA_V2` also declare `name = "ligandai"`
in their `pyproject.toml` and must never be uploaded from:

| tree | status |
|---|---|
| `/mnt/backup/ligandai-python-sdk` (here) | **canonical** ✅ |
| `LIGANDAI_ALPHA_V2/ligandai-api` | deprecated — see its `DEPRECATED.md` ⛔ |
| `LIGANDAI_ALPHA_V2/LIGANDAI_CLI` | different package (CLI/MCP) — see its `DO_NOT_PUBLISH.md` ⛔ |

## ⛔ Git history is NOT a reliable record of what is on PyPI

0.7.5 and 0.7.6 were published from a working-tree state that was never committed
anywhere — not to this repo's local branches, not to `origin/main`. The last
in-history release commit was 0.7.4, and `origin/main` sat at 0.6.4 on a **history
disjoint from local `main`** (no common ancestor — two separate roots).

Cutting 0.7.8 from any local branch would therefore have silently **deleted**
`ligandai/resources/legal.py` — the entire `client.legal` ToS/EULA surface, wired
into both sync and async clients and exported from `__init__` — plus 161 further
lines across 10 modules.

`twine check`, the test suite, and `pip install`-of-your-own-wheel **all pass** on
that regressing build. None of them compare against what users already have.

## Release procedure

1. **Fetch what is actually published** and reconcile before you branch:

   ```bash
   pip download ligandai==<pypi-latest> --no-deps --no-binary :all: -d /tmp/pub
   tar -xzf /tmp/pub/ligandai-<v>.tar.gz -C /tmp/pub
   diff -rq /tmp/pub/ligandai-<v>/ligandai ./ligandai
   ```

   Treat anything under "Only in `/tmp/pub/...`" as a **release blocker**.
   (`ligandai/agent_assets` is legitimately absent from built artifacts — it is
   excluded from the published wheel too.)

2. If the trees diverge, branch from the last commit that genuinely corresponds to
   a published release, commit the downloaded sdist's package tree as the true
   baseline, then `git merge` your feature branch onto it — **resolve every
   conflict by keeping both sides.** Never drop a symbol that exists upstream.

3. **Run the gate. Zero missing, or do not upload.**

   ```bash
   python3 scripts/check_published_superset.py            # vs PyPI latest
   python3 scripts/check_published_superset.py --version 0.7.6
   ```

   It AST-scans both trees and asserts every public module, class, function,
   method, module-level constant, and `__all__` entry in the published release
   still exists here. Exit 1 = stop.

4. Bump `ligandai/_version.py` **and** `pyproject.toml` (they must match), and add
   a `CHANGELOG.md` entry under the real version number. Never reuse a version
   number that already exists on PyPI for a different set of changes.

5. Build, check, upload:

   ```bash
   python3 -m build
   twine check dist/*
   twine upload dist/ligandai-<v>*
   ```

6. **Tag the commit and push the tag.** Absent tags are what let the 0.7.5/0.7.6
   drift go unnoticed for a month.

   ```bash
   git tag -a v<version> -m "ligandai <version> — <summary>"
   git push origin <branch> v<version>
   ```

7. Re-verify from a clean venv against the real index, then re-run the gate — it
   now compares your tree against what you just shipped:

   ```bash
   python3 -m venv /tmp/verify && /tmp/verify/bin/pip install -q ligandai==<v>
   /tmp/verify/bin/python -c "import ligandai; print(ligandai.__version__)"
   python3 scripts/check_published_superset.py
   ```

## Known state (2026-07-30)

- PyPI latest: **0.7.8** (`release/0.7.8`, tags `v0.7.6` reconstructed + `v0.7.8`).
- `origin/main` is at 0.6.4 on a disjoint history and **cannot be fast-forwarded**;
  reconciling it needs an owner decision (merge with `--allow-unrelated-histories`
  vs. replace). Until then, do not treat `main` as a release base.

Details: CortexWiki
`projects/ligandai_alpha_v2/wiki/server/decisions/pypi_ligandai_release_base__published_versions_absent_from_git_history.md`.
