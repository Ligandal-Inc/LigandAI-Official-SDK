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

   > ⛔ Corrected 2026-09-21 [bd-dre-rp11j.28]. This step used to end with
   > "`ligandai/agent_assets` is legitimately absent from built artifacts — it is
   > excluded from the published wheel too." That is no longer true and the
   > second half was never the reason. `ligandai/agent_assets/` does not exist
   > in this source tree at all: it is CREATED AT BUILD TIME by
   > `[tool.hatch.build.targets.wheel.force-include]`, which copies `skills/`
   > and `.claude/skills/` into it. Since 41ecba3 it **is** in the wheel (that
   > is the fix for the wheel writing `skills/` into the root of
   > site-packages), and it is still not in the sdist, which carries those
   > directories at top level instead. So `agent_assets` appearing on only one
   > side of this diff is expected; verify it against
   > `tests/unit/test_packaging_contract.py`, which asserts the wheel's payload
   > matches `git ls-files`, rather than against this sentence.

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

5. Build and check:

   ```bash
   python3 -m build
   twine check dist/*
   ```

6. **⛔ PREFLIGHT THE BUILT WHEEL — BEFORE UPLOAD, NOT AFTER.**

   ```bash
   ./scripts/preflight_wheel_import.sh          # newest dist/*.whl, or pass a path
   ```

   **Do not upload if this fails.** `ligandai 0.15.0` was published unimportable
   (`ImportError: cannot import name 'normalize_avoid_paralogs' from
   'ligandai.contrastive'`) and every gate above it passed on that artifact:
   `twine check` reads metadata and never imports; the 1528-test suite imports the
   SOURCE TREE, where a concurrent session's uncommitted `contrastive.py` was
   sitting next to the committed file that imported from it; and
   `check_published_superset.py` AST-scans files without executing an import. The
   only step that imported the built package was step 8, and it ran **after**
   upload — by which time the version number is burned, because PyPI does not
   allow re-uploading it.

   The preflight installs the wheel into a throwaway venv, runs from a directory
   that is not the repo (inside the repo, `import ligandai` finds `./ligandai` and
   succeeds on a wheel that cannot import), asserts the imported module lives in
   `site-packages`, resolves every name in `__all__`, constructs a client offline,
   and imports every shipped submodule — `__all__` only covers what the top level
   re-exports, and a broken module nothing re-exports still ships broken.

   Validated against the real artifacts: it FAILS on the wheel actually published
   as 0.15.0 (downloaded from PyPI) with that exact ImportError, and PASSES on
   0.14.0. Note the local `dist/ligandai-0.15.0-py3-none-any.whl` is **not** the
   file that was uploaded — 719,397 bytes locally vs 729,144 on PyPI, same version
   number, which is the drift this document warns about at the top.

7. Upload:

   ```bash
   twine upload dist/ligandai-<v>*
   ```

8. **Tag the commit and push the tag.** Absent tags are what let the 0.7.5/0.7.6
   drift go unnoticed for a month.

   ```bash
   git tag -a v<version> -m "ligandai <version> — <summary>"
   git push origin <branch> v<version>
   ```

9. Re-verify from a clean venv against the real index, then re-run the gate — it
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
