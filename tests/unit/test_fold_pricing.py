# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""A fold costs 10 credits ($0.10) at the default 4 trajectories. It cost 100.

What is pinned here, and why each pin exists:

* the constants in ``ligandai.pricing`` — the ONE place the SDK states the price;
* every local estimator reads them (``_hardening`` pre-flight, ``CampaignSpec``
  budget arithmetic, the planner) — a second literal anywhere is how the old
  100-credit figure survived four documents and two estimators;
* budgets and spend ceilings computed from fold counts SCALE with the fold
  count, the trajectory count and the step multiplier;
* the prose (README, examples, skills) states the same numbers the constants do.

The platform is authoritative for what is actually charged. Nothing here claims
otherwise; these are the numbers an offline pre-flight and a budget validator use.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from ligandai import pricing
from ligandai._hardening import (
    estimate_fold_batch_credits,
    estimate_single_fold_credits,
)
from ligandai.campaign import CampaignSpec, FoldSettings, SpecError
from ligandai.campaign.planner import _fold_cost
from ligandai.pricing import (
    CREDITS_PER_FOLD_DEFAULT,
    CREDITS_PER_GENERATED_PEPTIDE,
    CREDITS_PER_SCORED_DESIGN,
    CREDITS_PER_TRAJECTORY,
    CREDITS_PER_USD,
    FOLD_MIN_CREDITS,
    TRAJECTORIES_DEFAULT,
    credits_to_usd,
    fold_batch_credits,
    fold_credits,
    fold_step_multiplier,
)

ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- #
# the constants
# --------------------------------------------------------------------------- #
def test_default_fold_is_ten_credits_and_ten_cents():
    assert CREDITS_PER_FOLD_DEFAULT == 10
    assert TRAJECTORIES_DEFAULT == 4
    assert fold_credits() == 10
    assert fold_credits(TRAJECTORIES_DEFAULT, 50) == 10
    assert credits_to_usd(fold_credits()) == 0.10


def test_one_credit_is_one_cent_and_matches_the_account_conversion():
    from ligandai.resources.account import _CREDITS_PER_USD

    assert CREDITS_PER_USD == 100 == _CREDITS_PER_USD
    assert credits_to_usd(1) == 0.01


def test_per_trajectory_rate_is_derived_proportionally_not_typed():
    assert CREDITS_PER_TRAJECTORY == CREDITS_PER_FOLD_DEFAULT / TRAJECTORIES_DEFAULT == 2.5
    assert FOLD_MIN_CREDITS == CREDITS_PER_FOLD_DEFAULT


def test_generation_and_scoring_prices_were_not_touched():
    assert CREDITS_PER_GENERATED_PEPTIDE == 100
    assert CREDITS_PER_SCORED_DESIGN == 25


def test_the_module_exports_what_it_documents():
    for name in pricing.__all__:
        assert hasattr(pricing, name), name


# --------------------------------------------------------------------------- #
# fold_credits: proportional per trajectory, per-fold floor, ceil step multiplier
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("trajectories,expected", [
    (1, 10),    # the floor: one trajectory is not cheaper than the default
    (2, 10),
    (3, 10),
    (4, 10),    # the default fold
    (5, 13),    # 12.5 -> rounds UP; a pre-flight must not under-state
    (8, 20),
    (10, 25),
    (16, 40),
])
def test_fold_cost_scales_proportionally_above_the_floor(trajectories, expected):
    assert fold_credits(trajectories, 50) == expected


@pytest.mark.parametrize("steps,mult", [
    (None, 1), (0, 1), (10, 1), (25, 1), (50, 1),   # never a discount
    (51, 2), (75, 2), (100, 2), (101, 3), (200, 4),
])
def test_step_multiplier_is_the_servers_ceil(steps, mult):
    assert fold_step_multiplier(steps) == mult
    assert fold_credits(4, steps) == 10 * mult


def test_the_minimum_is_per_fold_not_a_floor_on_the_batch():
    # three single-trajectory folds are 3 x 10, not max(10, 3 x 2.5)
    assert fold_batch_credits(3, 1, 50) == 30
    assert fold_batch_credits(0, 4, 50) == 0


@pytest.mark.parametrize("n", [1, 2, 10, 25, 300])
def test_a_batch_of_default_folds_is_n_times_ten(n):
    assert fold_batch_credits(n) == n * 10
    assert fold_batch_credits(n, 4, 50) == n * 10


# --------------------------------------------------------------------------- #
# the estimators read the one module
# --------------------------------------------------------------------------- #
def test_preflight_estimators_use_the_pricing_module():
    assert estimate_single_fold_credits(trajectories=4, sampling_steps=None) == 10
    assert estimate_fold_batch_credits(
        peptide_count=100, trajectories=4, sampling_steps=50) == 1000
    assert estimate_fold_batch_credits(
        peptide_count=100, trajectories=8, sampling_steps=100) == 100 * fold_credits(8, 100)


def test_no_module_outside_pricing_hardcodes_a_per_trajectory_literal():
    """The old figure survived because it was typed in several places. The
    estimators must not multiply by a bare 100 again."""
    src = (ROOT / "ligandai" / "_hardening.py").read_text()
    assert "* 100" not in src and "100 *" not in src


# --------------------------------------------------------------------------- #
# budgets computed from fold counts scale correctly
# --------------------------------------------------------------------------- #
def _spec(fold=None, budget=345, stop_target=25):
    rec = "ACDEFGHIKLMNPQRSTVWY" * 20
    d = {
        "campaign_spec_version": 1, "name": "t",
        "target": {"gene": "G", "construct": {"name": "C", "sequence": rec}},
        "peptide_classes": [{"name": "l", "length_range": [12, 24], "n_per_round": 50}],
        "gates": [{"metric": "peptide_ipsae_median", "op": ">=", "threshold": 0.67}],
        "stop_metrics": [{"id": "c", "kind": "certified_designs", "target": stop_target}],
        "ranking": {"score": "peptide_ipsae_median", "higher_is_better": True,
                    "population": "gate_passing"},
        "budget": {"max_credits": budget},
    }
    if fold is not None:
        d["fold"] = fold
    return CampaignSpec.from_dict(d, ".")


def test_a_default_spec_prices_a_fold_at_ten_credits():
    assert _spec().fold.credits_per_fold == 10
    assert FoldSettings().credits_per_fold == 10


def test_planner_fold_cost_is_designs_x_runs_x_ten():
    spec = _spec()
    assert _fold_cost(spec, n_designs=30, n_runs_each=22) == 30 * 22 * 10
    assert _fold_cost(spec, n_designs=1, n_runs_each=0) == 10     # at least one run


def test_one_certified_design_costs_100_plus_22_folds_plus_25():
    # 0.15 bar -> 22 clean runs. 100 generate + 22 x 10 + 25 score = 345.
    with pytest.raises(SpecError, match="cannot buy a single"):
        _spec(budget=344)
    assert _spec(budget=345).validate(), "one design's budget cannot buy 25: warns"
    assert _spec(budget=25 * 345).validate() == []
    assert _spec(budget=25 * 345 - 1).validate() != []


def test_a_spec_that_raises_trajectories_is_priced_at_that_setting():
    """8 trajectories = 20 credits a fold, so the same campaign needs a bigger
    budget — the budget scales with the fold settings it will actually run."""
    spec = _spec(fold={"diffusion_samples": 8}, budget=565)
    assert spec.fold.credits_per_fold == 20
    assert _fold_cost(spec, 1, 1) == 20
    with pytest.raises(SpecError, match="cannot buy a single"):
        _spec(fold={"diffusion_samples": 8}, budget=564)    # 100 + 22 x 20 + 25 = 565


def test_a_spec_that_raises_sampling_steps_is_priced_at_that_setting():
    assert _spec(fold={"sampling_steps": 100}, budget=565).fold.credits_per_fold == 20


def test_an_explicit_credits_per_fold_still_wins():
    assert _spec(fold={"credits_per_fold": 7}).fold.credits_per_fold == 7
    assert _spec(fold={"diffusion_samples": 8, "credits_per_fold": 7}).fold.credits_per_fold == 7


def test_budget_floor_message_states_the_new_arithmetic():
    with pytest.raises(SpecError) as e:
        _spec(budget=100)
    msg = str(e.value)
    assert "22 clean runs" in msg and "= 345 credits" in msg


# --------------------------------------------------------------------------- #
# the prose states the same numbers the constants do
# --------------------------------------------------------------------------- #
def _read(rel):
    return (ROOT / rel).read_text()


def test_readme_billing_section_states_the_constants():
    text = " ".join(_read("README.md").split())      # the paragraph is hard-wrapped
    assert f"**{CREDITS_PER_FOLD_DEFAULT} credits ($0.10) per fold at the default {TRAJECTORIES_DEFAULT} trajectories**" in text
    assert f"{CREDITS_PER_TRAJECTORY:g} credits per trajectory" in text
    assert f"**{FOLD_MIN_CREDITS}-credit minimum per fold**" in text


STALE = [
    r"25 credits per fold per trajectory",
    r"100-credit minimum",
    r"100 credits per fold(?! per trajectory)",
    r"\$1(\.00)? per fold",
    r"100 cr(?:edits)? ?/ ?fold",
]
PUBLIC_DOCS = [
    "README.md", "AGENTS.md", "docs/agents.md", "docs/workflows.md",
    "docs/api_reference.md", "docs/campaigns.md", "docs/quickstart.md",
    "examples/23_fold_batch.py", "examples/campaign/README.md",
    "examples/campaign/ca12_selective_binders.yaml",
    ".claude/skills/ligandai/campaign.md", ".claude/skills/ligandai/fold.md",
    ".claude/skills/ligandai/generate.md",
]


@pytest.mark.parametrize("rel", PUBLIC_DOCS)
def test_no_public_doc_still_states_the_old_fold_price(rel):
    text = _read(rel)
    for pat in STALE:
        hits = re.findall(pat, text)
        # README keeps exactly one historical sentence: "(A fold cost 100 credits / $1.00 before 0.16.0.)"
        assert not hits, f"{rel}: stale fold price {pat!r} -> {hits}"


def test_campaign_example_states_the_price_the_runner_defaults_to():
    import yaml

    d = yaml.safe_load(_read("examples/campaign/ca12_selective_binders.yaml"))
    assert d["fold"]["credits_per_fold"] == CREDITS_PER_FOLD_DEFAULT
    assert d["fold"]["credits_per_generated_peptide"] == CREDITS_PER_GENERATED_PEPTIDE
    assert d["fold"]["credits_per_scored_design"] == CREDITS_PER_SCORED_DESIGN


def test_campaign_docs_state_the_per_design_arithmetic_the_validator_uses():
    per_design = CREDITS_PER_GENERATED_PEPTIDE + 22 * CREDITS_PER_FOLD_DEFAULT + CREDITS_PER_SCORED_DESIGN
    for rel in ("examples/campaign/README.md", ".claude/skills/ligandai/campaign.md"):
        text = _read(rel)
        assert str(per_design) in text, rel
        assert f"{25 * per_design:,}" in text, rel


def test_fold_batch_example_derives_its_estimate_from_the_pricing_module():
    src = _read("examples/23_fold_batch.py")
    assert "from ligandai.pricing import fold_batch_credits" in src
    assert "* 100" not in src


def test_the_ensemble_resource_holds_no_rate_of_its_own():
    """Ensemble pricing is the server's, per engine. A rate typed into this module
    is a second copy of a price the platform changes."""
    src = _read("ligandai/resources/ensemble.py")
    assert "CREDITS_PER_FOLD_DEFAULT" not in src
    assert "perTrajectory" not in src.replace("``CREDIT_PRICING.cofold``", "")
