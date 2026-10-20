# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""The server ``is_elite`` bar is 0.66, and the SDK must not say 0.80.

WHY THIS FILE EXISTS [bd-dre-f29kd]. The platform stamps
``ptf_fold_results.is_elite`` in exactly one place — ``isEliteByBinderIpsae()``
called from ``storage.insertPtfFoldResult`` — against
``ELITE_PEPTIDE_IPSAE_THRESHOLD`` in ``shared/peptide-ipsae.ts``, applied to the
BINDER (peptide-receptor interface) iPSAE. That constant is 0.66. 0.80 was
considered for it and REJECTED, because it would have demoted genuine binders in
[0.66, 0.80).

This SDK nevertheless documented the bar as 0.80 in ``docs/api_reference.md``,
in ``peptides.search()``'s docstring, in the ``Peptide.is_elite`` field
description and in the shipped Claude Agent skill — four published statements of
a number the gate has never enforced, read by customers reconciling their own
counts. Nothing held the prose to the constant, which is the whole failure: a
number in a docstring has no mechanism keeping it true.

``IS_ELITE_PEPTIDE_IPSAE_THRESHOLD`` in ``ligandai._constants`` is now the one
place this SDK states it (the platform stays authoritative; a Python package
cannot import a TypeScript constant, so it mirrors it and the platform's
``scripts/check-docs-drift.mjs`` check #10 holds the mirror to the source).
These tests hold the PROSE to the mirror.

⛔ Two different rules share the word "elite" and these tests must not conflate
them: the legacy single-metric server FLAG (``is_elite``, 0.66) and the
acceptance ladder's TIER (``peptide_ipsae >= 0.85``,
``shared/acceptance-gate.json``). Statements about the tier are correct at 0.85
and are deliberately out of scope here.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from ligandai._constants import IS_ELITE_PEPTIDE_IPSAE_THRESHOLD
from ligandai.types import Peptide

ROOT = Path(__file__).resolve().parents[2]

BAR = IS_ELITE_PEPTIDE_IPSAE_THRESHOLD


def _read(rel: str) -> str:
    return (ROOT / rel).read_text()


def test_the_mirror_is_the_platform_value():
    """A wrong mirror would make every other test here agree on a wrong number."""
    assert BAR == 0.66, (
        "ligandai._constants.IS_ELITE_PEPTIDE_IPSAE_THRESHOLD must equal "
        "ELITE_PEPTIDE_IPSAE_THRESHOLD in the platform's shared/peptide-ipsae.ts. "
        "If the platform really changed the bar, change it here AND re-check every "
        "doc this test covers — do not relax the assertion."
    )


# Every statement of the is_elite bar in prose the customer reads. Each pattern
# is ANCHORED to the phrase that introduces THIS bar, never to a bare
# "iPSAE >= N": an unanchored pattern here matched the neighbouring super_elite
# leg (iPSAE >= 0.67, iPTM >= 0.80) and failed on a correct document, which is
# the same class of bug as the drift it is meant to catch. Each site keeps the
# number immediately after the inequality so a drift is machine-detectable.
#
# Patterns run against WHITESPACE-NORMALIZED text, because every one of these
# statements is wrapped prose and the inequality routinely lands on a different
# line from the phrase that anchors it.
BAR_STATEMENTS = [
    ("docs/api_reference.md",
     r"server-stamped `is_elite` flag: BINDER \(peptide-receptor interface\) iPSAE ≥ ([0-9]*\.?[0-9]+)"),
    ("ligandai/resources/peptides.py",
     r"``is_elite`` flag — BINDER \(peptide- receptor interface\) peptide_ipsae >= ([0-9]*\.?[0-9]+)"),
    ("ligandai/types.py",
     r"Legacy single-metric server flag -- binder \(peptide\) peptide_ipsae >= ([0-9]*\.?[0-9]+)"),
    ("skills/claude-agent-sdk/ligandai-skill.ts",
     r"`is_elite` flag is its own rule: binder iPSAE ≥ ([0-9]*\.?[0-9]+)"),
]


@pytest.mark.parametrize("rel,pattern", BAR_STATEMENTS)
def test_doc_states_the_real_is_elite_bar(rel: str, pattern: str):
    text = " ".join(_read(rel).split())
    hits = re.findall(pattern, text)
    assert hits, (
        f"{rel}: no is_elite bar statement matched {pattern!r}. The statement was "
        "reworded so this guard no longer sees it — a silently-uncovered site is "
        "exactly how 0.80 survived in four documents. Restore the shape or update "
        "the pattern."
    )
    for h in hits:
        assert float(h) == BAR, f"{rel}: states the is_elite bar as {h}, gate is {BAR}"


# Prose that is specifically wrong, not merely unpinned. 0.80 as THE is_elite bar
# is the exact claim that shipped; keep it un-shippable even if the statements
# above are restructured. The super_elite legs legitimately use 0.80 for iPTM, so
# this only fires when 0.80 is attached to is_elite itself.
STALE_IS_ELITE = [
    r"is_elite[^\n]{0,80}(?:iPSAE|ipSAE|peptide_ipsae)\s*(?:>=|≥|>)\s*0\.80?\b",
    r"(?:iPSAE|ipSAE|peptide_ipsae)\s*(?:>=|≥|>)\s*0\.80?\b[^\n]{0,60}is_elite",
    r"Server-flagged elite \(iPSAE ≥ 0\.80",
    r"server-flagged elite \(default iPSAE ≥ 0\.80",
    r"elite ≥ 0\.80\b",
]
PUBLIC_SURFACES = [
    "README.md",
    "AGENTS.md",
    "docs/api_reference.md",
    "docs/agents.md",
    "docs/workflows.md",
    "docs/campaigns.md",
    "docs/quickstart.md",
    "ligandai/types.py",
    "ligandai/resources/peptides.py",
    "ligandai/resources/evaluator.py",
    "ligandai/resources/validated_binder.py",
    "skills/claude-agent-sdk/ligandai-skill.ts",
    "skills/openai-tool-calling/tools.json",
    ".claude/skills/ligandai/SKILL.md",
    ".claude/skills/ligandai/fold.md",
    ".claude/skills/ligandai/campaign.md",
    ".claude/skills/ligandai/generate.md",
]


@pytest.mark.parametrize("rel", PUBLIC_SURFACES)
def test_no_public_surface_still_calls_the_is_elite_bar_0_80(rel: str):
    path = ROOT / rel
    if not path.exists():  # a surface may be renamed; the pinned list is not the point
        pytest.skip(f"{rel} not present")
    text = path.read_text()
    for pat in STALE_IS_ELITE:
        hits = re.findall(pat, text, flags=re.IGNORECASE)
        assert not hits, (
            f"{rel}: states the is_elite bar as 0.80 -> {hits}. The gate is "
            f"{BAR} (ELITE_PEPTIDE_IPSAE_THRESHOLD, shared/peptide-ipsae.ts); the "
            "0.85 'elite' TIER is a different rule and must be named as the tier."
        )


def test_peptide_is_elite_field_description_points_at_the_source_of_truth():
    """A corrected number still rots unless the reader is told where it comes from."""
    desc = Peptide.model_fields["is_elite"].description or ""
    assert "peptide_ipsae >= 0.66" in desc, desc
    assert "shared/peptide-ipsae.ts" in desc, desc
    assert "IS_ELITE_PEPTIDE_IPSAE_THRESHOLD" in desc, desc
    # And it must keep warning that the 0.85 tier is a different rule, since the
    # two were conflated in the SDK for several releases.
    assert "0.85" in desc, desc
