#!/usr/bin/env bash
# Copyright © 2025 Ligandal, Inc. All rights reserved.
# Release preflight: install the BUILT WHEEL into a throwaway venv and exercise
# it. Runs BETWEEN build and upload. [bd-dre-a8p6m]
#
# WHY THIS EXISTS
# ligandai 0.15.0 was uploaded to PyPI and could not be imported at all:
#   ImportError: cannot import name 'normalize_avoid_paralogs' from 'ligandai.contrastive'
# Every gate in RELEASING.md passed on that artifact:
#   twine check                  PASSED -- metadata only, never imports
#   1528-test unit suite         PASSED -- imports the SOURCE TREE, where a
#                                concurrent session's uncommitted contrastive.py
#                                was sitting next to the committed file that
#                                imported from it
#   check_published_superset.py  PASSED -- AST-scans files, never executes an import
# The only step that imports the built package ran AFTER upload (step 7), and by
# then the version is burned: PyPI does not allow re-uploading it.
#
# ⛔ THE POINT IS THE ISOLATION, NOT THE IMPORT. `python -c "import ligandai"` run
# from the repo imports the SOURCE TREE and will happily succeed on a wheel that
# cannot import, because the tree contains files the wheel does not. This script
# therefore installs the wheel into a fresh venv AND runs from a directory that is
# not the repo, so the source tree cannot be picked up.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WHEEL="${1:-}"
if [[ -z "$WHEEL" ]]; then
  WHEEL=$(ls -1t "$REPO"/dist/ligandai-*.whl 2>/dev/null | head -1)
fi
[[ -n "$WHEEL" && -f "$WHEEL" ]] || { echo "preflight: no wheel found (build first, or pass a path)" >&2; exit 2; }
echo "preflight: testing $(basename "$WHEEL")"

VENV=$(mktemp -d /tmp/ligandai-preflight-XXXXXX)
# Run from a neutral cwd. Inside $REPO, 'import ligandai' finds ./ligandai.
WORKDIR=$(mktemp -d /tmp/ligandai-preflight-cwd-XXXXXX)
cleanup() { rm -rf "$VENV" "$WORKDIR"; }
trap cleanup EXIT

python3 -m venv "$VENV" >/dev/null
"$VENV/bin/pip" install -q --upgrade pip >/dev/null 2>&1 || true
if ! "$VENV/bin/pip" install -q "$WHEEL"; then
  echo "preflight: FAIL — pip could not install the wheel" >&2; exit 1
fi

cat > "$WORKDIR/exercise.py" <<'PY'
import sys, pathlib
# Prove we are NOT importing the repo: the module must live under the venv.
import ligandai
origin = pathlib.Path(ligandai.__file__).resolve()
if "site-packages" not in origin.parts:
    print(f"preflight: FAIL - imported {origin}, which is not the installed wheel", file=sys.stderr)
    raise SystemExit(1)

print(f"  import ligandai        OK  ({ligandai.__version__}) from {origin.parent.name}")

# Touch every public name. This is what catches a module that imports a symbol
# its own package does not define -- the exact 0.15.0 failure.
missing = []
for name in getattr(ligandai, "__all__", []):
    try:
        getattr(ligandai, name)
    except Exception as exc:
        missing.append(f"{name}: {type(exc).__name__}: {exc}")
if missing:
    print(f"preflight: FAIL - {len(missing)} name(s) in __all__ are not resolvable:", file=sys.stderr)
    for m in missing[:10]:
        print(f"    {m}", file=sys.stderr)
    raise SystemExit(1)
print(f"  __all__ resolvable     OK  ({len(ligandai.__all__)} names)")

# Construct a client offline. A shape-valid fake key: never a real one, and no
# network call is made by construction.
c = ligandai.LigandAI(api_key="lgai_free_preflight_not_a_real_key")
print("  client construct       OK")

# Import every shipped submodule. __all__ only covers what the top level
# re-exports; a broken module nothing re-exports still ships broken.
import importlib, pkgutil
pkg = pathlib.Path(ligandai.__file__).parent
bad = []
for mod in pkgutil.walk_packages([str(pkg)], prefix="ligandai."):
    try:
        importlib.import_module(mod.name)
    except Exception as exc:
        bad.append(f"{mod.name}: {type(exc).__name__}: {exc}")
if bad:
    print(f"preflight: FAIL - {len(bad)} submodule(s) do not import:", file=sys.stderr)
    for b in bad[:10]:
        print(f"    {b}", file=sys.stderr)
    raise SystemExit(1)
print("  all submodules import  OK")
PY

( cd "$WORKDIR" && "$VENV/bin/python" exercise.py )
echo "preflight: OK — $(basename "$WHEEL") installs, imports and exercises cleanly"
