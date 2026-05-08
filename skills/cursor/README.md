# LigandAI Skill — Cursor IDE

Drop `.cursorrules` into the root of any Cursor workspace where you'll be
calling the LigandAI Python SDK. Cursor reads `.cursorrules` automatically
on every chat / Cmd-K / Cmd-L request, so the rules are always in context.

## Install

```bash
cp .cursorrules <YOUR_PROJECT_ROOT>/.cursorrules
pip install ligandai>=0.5.0
export LIGANDAI_API_KEY=lgai_pro_...
```

## What the rules teach Cursor

- The auth surface (`LIGANDAI_API_KEY`, key prefixes per tier).
- The four canonical workflows (gene, PDB+chain, custom CIF, pocket-targeted).
- Tier-aware fold-GPU defaults.
- Error-handling patterns (401/402/403 → user-actionable messaging).
- Cost preview discipline before large jobs.
- Pointers to `examples/` and `AGENTS.md` for deeper coverage.

## Capability coverage

The rules reference all 17 public namespaces on `client.*`:
`account`, `bivalent`, `charts`, `discovery`, `diseases`, `folds`, `goals`,
`jobs`, `memory`, `msa`, `peptides`, `programs`, `proteins`, `receptors`,
`reports`, `structures`, `synthesis`. For any namespace the user hits, the
example file under `examples/` covers it (see SKILL.md mapping in
`../claude-code/ligandai/SKILL.md`).

## Notes

- `.cursorrules` lives at the workspace root, NOT under `.cursor/`. Cursor
  walks up the directory tree looking for it.
- Cursor merges multiple `.cursorrules` files when present (parent dir +
  workspace dir) — the parent rules are loaded first.
