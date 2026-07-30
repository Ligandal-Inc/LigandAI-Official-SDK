#!/usr/bin/env python3
# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Release gate: assert this tree is a public-symbol SUPERSET of what is on PyPI.

Green tests, `twine check`, and installing your own wheel all pass on a build that
silently deletes shipped code — none of them compare against what users already
have. This does.

    python3 scripts/check_published_superset.py                 # vs PyPI latest
    python3 scripts/check_published_superset.py --version 0.7.6
    python3 scripts/check_published_superset.py --published-dir /tmp/x/ligandai

Exits non-zero if any public module, class, function, method, module-level
constant, or ``__all__`` entry present in the published package is missing here.

Background: bd-dre-7x9bc. On 2026-07-30 a release cut from a local branch would
have deleted ``ligandai/resources/legal.py`` (the whole ``client.legal`` surface)
because PyPI 0.7.5/0.7.6 were published from a tree state never committed to git.
"""
from __future__ import annotations

import argparse
import ast
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import urllib.request

PKG = "ligandai"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def scan(root: str) -> dict[str, set[tuple[str, str]]]:
    """Map each .py file (relative path) -> set of (kind, public name)."""
    out: dict[str, set[tuple[str, str]]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, root)
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            syms: set[tuple[str, str]] = set()
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if not node.name.startswith("_"):
                        syms.add(("func", node.name))
                elif isinstance(node, ast.ClassDef):
                    if node.name.startswith("_"):
                        continue
                    syms.add(("class", node.name))
                    for sub in node.body:
                        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            if not sub.name.startswith("_"):
                                syms.add(("method", f"{node.name}.{sub.name}"))
                        elif isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name):
                            if not sub.target.id.startswith("_"):
                                syms.add(("attr", f"{node.name}.{sub.target.id}"))
                elif isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and not target.id.startswith("_"):
                            syms.add(("const", target.id))
                        elif isinstance(target, ast.Name) and target.id == "__all__":
                            syms.add(("const", "__all__"))
            # __all__ entries are the real public contract — capture each name.
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
                ):
                    if isinstance(node.value, (ast.List, ast.Tuple)):
                        for elt in node.value.elts:
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                syms.add(("__all__", elt.value))
            out[rel] = syms
    return out


def fetch_published(version: str | None) -> tuple[str, str]:
    """Download the sdist from PyPI; return (extracted package dir, version)."""
    meta_url = f"https://pypi.org/pypi/{PKG}/json"
    meta = json.load(urllib.request.urlopen(meta_url, timeout=30))
    version = version or meta["info"]["version"]
    files = meta["releases"].get(version)
    if not files:
        sys.exit(f"FAIL: version {version} not found on PyPI")
    sdist = next((f for f in files if f["filename"].endswith(".tar.gz")), None)
    if sdist is None:
        sys.exit(f"FAIL: no sdist for {PKG}=={version}")
    blob = urllib.request.urlopen(sdist["url"], timeout=120).read()
    tmp = tempfile.mkdtemp(prefix="ligandai_published_")
    with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
        tar.extractall(tmp)
    pkg_dir = os.path.join(tmp, f"{PKG}-{version}", PKG)
    if not os.path.isdir(pkg_dir):
        sys.exit(f"FAIL: {PKG}/ not found inside sdist {PKG}-{version}")
    return pkg_dir, version


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--version", help="published version to compare against (default: PyPI latest)")
    ap.add_argument("--published-dir", help="use an already-extracted published ligandai/ dir")
    ap.add_argument("--local-dir", default=os.path.join(REPO_ROOT, PKG))
    args = ap.parse_args()

    cleanup = None
    if args.published_dir:
        pub_dir, version = args.published_dir, "(local dir)"
    else:
        pub_dir, version = fetch_published(args.version)
        cleanup = os.path.dirname(os.path.dirname(pub_dir))

    try:
        old = scan(pub_dir)
        new = scan(args.local_dir)

        old_syms = sum(len(v) for v in old.values())
        new_syms = sum(len(v) for v in new.values())
        print(f"published {version}: {len(old)} modules, {old_syms} public symbols")
        print(f"this tree:            {len(new)} modules, {new_syms} public symbols")

        missing_files = sorted(set(old) - set(new))
        missing_syms: list[str] = []
        for rel, syms in old.items():
            gone = syms - new.get(rel, set())
            for kind, name in sorted(gone):
                missing_syms.append(f"{rel}: {kind} {name}")

        if missing_files:
            print(f"\nMISSING FILES ({len(missing_files)}):")
            for f in missing_files:
                print(f"   {f}")
        if missing_syms:
            print(f"\nMISSING SYMBOLS ({len(missing_syms)}):")
            for s in missing_syms:
                print(f"   {s}")

        if missing_files or missing_syms:
            print(
                "\nFAIL — this tree would REMOVE code that is already published.\n"
                "Do not upload. Reconcile first: branch from the published state,\n"
                "commit the downloaded sdist as the baseline, then merge your work\n"
                "onto it keeping BOTH sides. See RELEASING.md (bd-dre-7x9bc).\n"
                "Deliberate removals must be acknowledged explicitly, not silently."
            )
            return 1

        print("\nPASS — public surface is a superset of the published release.")
        return 0
    finally:
        if cleanup and os.path.isdir(cleanup):
            shutil.rmtree(cleanup, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
