# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""THREE rules share the word "elite". This file keeps the SDK's prose on all three.

  elite TIER      0.80  what "elite" MEANS as a label or a count. Andre,
                        2026-10-20: "0.67 is great+. 0.8 is elite", restating
                        bd-x8vop of 2026-07-01.
  elite CRITERION 0.85  ``shared/acceptance-gate.json`` criteria.elite, rank 2 —
                        a stricter acceptance rung, used as a verdict filter.
  is_elite FLAG   0.66  the GREAT+ bar, and what the server column is actually
                        stamped at. A LEGACY LABEL for great+, not the tier.

WHY THIS FILE EXISTS [bd-dre-f29kd]. The platform stamps
``ptf_fold_results.is_elite`` in exactly one place —
``isGreatPlusByBinderIpsae()`` called from ``storage.insertPtfFoldResult`` —
against ``GREAT_PLUS_PEPTIDE_IPSAE_THRESHOLD`` in ``shared/peptide-ipsae.ts``,
applied to the BINDER (peptide-receptor interface) iPSAE. That constant is 0.66,
so this SDK documenting the FLAG as 0.80 was wrong and is still forbidden.

⛔ WHY IT WAS REWRITTEN [bd-dre-vtao3]. Round 1 of this file concluded that 0.80
was wrong EVERYWHERE, on the authority of the platform constant's NAME — which
was ``ELITE_PEPTIDE_IPSAE_THRESHOLD`` while holding the great+ value. On that
reading it forbade ``elite >= 0.80`` as a public statement, and a pass duly
pushed the shipped Claude Agent skill's tier table from "elite >= 0.80" to 0.66
— a number the TIER has never used. A guard pinned to a misnamed constant does
not prevent drift, it enforces it. So the tier bar is now pinned POSITIVELY
(TIER_STATEMENTS), the flag bar separately (BAR_STATEMENTS), and the two are
asserted distinct.

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

⛔ These tests must not conflate the three rules in the table above. A statement
is only a FLAG statement when it names ``is_elite``/``isElite``; a statement
introduced by "the elite tier" is a TIER statement even when it mentions the
flag in order to distinguish the two.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from ligandai._constants import (
    ELITE_TIER_PEPTIDE_IPSAE_THRESHOLD,
    IS_ELITE_PEPTIDE_IPSAE_THRESHOLD,
)
from ligandai.types import Peptide

ROOT = Path(__file__).resolve().parents[2]

BAR = IS_ELITE_PEPTIDE_IPSAE_THRESHOLD
TIER_BAR = ELITE_TIER_PEPTIDE_IPSAE_THRESHOLD


def _read(rel: str) -> str:
    return (ROOT / rel).read_text()


def test_the_mirror_is_the_platform_value():
    """A wrong mirror would make every other test here agree on a wrong number."""
    assert BAR == 0.66, (
        "ligandai._constants.IS_ELITE_PEPTIDE_IPSAE_THRESHOLD must equal "
        "GREAT_PLUS_PEPTIDE_IPSAE_THRESHOLD in the platform's shared/peptide-ipsae.ts. "
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
     r"`is_elite` flag is a label for the great\+ bar: binder iPSAE ≥ ([0-9]*\.?[0-9]+)"),
]

# The elite TIER bar (0.80), which is a DIFFERENT rule from the flag above.
# bd-dre-vtao3: a pass under bd-dre-f29kd read the platform's then-misnamed
# ELITE_PEPTIDE_IPSAE_THRESHOLD as policy and pushed these statements from 0.80
# down to 0.66. Andre, 2026-10-20: "0.67 is great+. 0.8 is elite" — restating
# bd-x8vop. These patterns hold the restored statements in place.
TIER_STATEMENTS = [
    ("ligandai/resources/peptides.py",
     r"the elite tier is ``peptide_ipsae >= ([0-9]*\.?[0-9]+)``"),
    ("skills/claude-agent-sdk/ligandai-skill.ts",
     r"elite ≥ ([0-9]*\.?[0-9]+) \| great\+"),
    ("ligandai/_constants.py",
     r"ELITE_TIER_PEPTIDE_IPSAE_THRESHOLD: float = ([0-9]*\.?[0-9]+)"),
]


@pytest.mark.parametrize("rel,pattern", TIER_STATEMENTS)
def test_doc_states_the_real_elite_tier_bar(rel: str, pattern: str):
    """The tier bar is 0.80 and these five sites were flipped to 0.66 once."""
    text = " ".join(_read(rel).split())
    hits = re.findall(pattern, text)
    assert hits, (
        f"{rel}: no elite TIER statement matched {pattern!r}. Zero coverage is "
        "indistinguishable from a pass, and this is one of the sites that was "
        "silently flipped to 0.66 under bd-dre-f29kd. Restore the shape or "
        "update the pattern."
    )
    for h in hits:
        assert float(h) == TIER_BAR, (
            f"{rel}: states the elite tier as {h}; the tier is {TIER_BAR}. "
            "0.66 is the GREAT+ bar (and what the is_elite flag is stamped at); "
            "0.85 is the acceptance `elite` CRITERION. Three rules, three numbers."
        )


def test_the_three_bars_are_distinct():
    """One number under two names is the defect bd-dre-vtao3 fixed."""
    assert TIER_BAR != BAR, (
        f"the elite tier bar and the is_elite flag bar have RE-CONVERGED at {BAR}. "
        "The flag is a legacy label for great+ (0.66); the tier is 0.80. "
        "Collapsing them re-creates the misnomer that corrupted five documents."
    )
    assert TIER_BAR > BAR, f"elite ({TIER_BAR}) must be stricter than great+ ({BAR})"
    assert TIER_BAR != 0.85, "0.85 is the acceptance criterion, not the tier label bar"


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
#
# ⛔ `elite ≥ 0.80` WAS ON THIS LIST AND HAS BEEN REMOVED [bd-dre-vtao3]. It is
# the CORRECT tier statement — Andre, 2026-10-20: "0.67 is great+. 0.8 is elite".
# Forbidding it is what let a pass strip the agent skill's tier table down to
# 0.66. The tier bar is pinned positively by TIER_STATEMENTS above; what is
# forbidden here is attaching 0.80 to the FLAG, which does gate at 0.66.
STALE_IS_ELITE = [
    r"is_elite[^\n]{0,80}(?:iPSAE|ipSAE|peptide_ipsae)\s*(?:>=|≥|>)\s*0\.80?\b",
    r"(?:iPSAE|ipSAE|peptide_ipsae)\s*(?:>=|≥|>)\s*0\.80?\b[^\n]{0,60}is_elite",
    r"Server-flagged elite \(iPSAE ≥ 0\.80",
    r"server-flagged elite \(default iPSAE ≥ 0\.80",
]

# The inverse defect: a TIER statement pushed down onto the great+ bar. This is
# the regression that actually shipped, in the published agent skill.
STALE_ELITE_TIER = [
    r"elite ≥ 0\.66\b",
    r"elite tier[^\n]{0,60}(?:iPSAE|ipSAE|peptide_ipsae)\s*(?:>=|≥|>)\s*0\.66\b",
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
            f"{BAR} (GREAT_PLUS_PEPTIDE_IPSAE_THRESHOLD, shared/peptide-ipsae.ts); "
            f"the {TIER_BAR} elite TIER and the 0.85 acceptance criterion are "
            "different rules and must each be named as such."
        )


@pytest.mark.parametrize("rel", PUBLIC_SURFACES)
def test_no_public_surface_states_the_elite_tier_as_0_66(rel: str):
    """The inverse of the test above: the regression that actually shipped."""
    path = ROOT / rel
    if not path.exists():
        pytest.skip(f"{rel} not present")
    text = path.read_text()
    for pat in STALE_ELITE_TIER:
        hits = re.findall(pat, text, flags=re.IGNORECASE)
        assert not hits, (
            f"{rel}: states the elite TIER as 0.66 -> {hits}. The tier is "
            f"{TIER_BAR}. 0.66 is the GREAT+ bar and what the legacy is_elite "
            "flag is stamped at — it is not the elite tier [bd-dre-vtao3]."
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
