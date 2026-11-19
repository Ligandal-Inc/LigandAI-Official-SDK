"""Pricing boundary regressions against the server rules, with no live jobs."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from ligandai.errors import LigandAIError, LigandAIUnreadableResponse
from ligandai.pricing import generation_credits, ligandiq_score_credits
from ligandai.resources.peptides import AsyncPeptides, Peptides, estimate_multi_cost
from ligandai.resources.synthesis import AsyncSynthesis, Synthesis


@pytest.mark.parametrize("count", [1, 4, 99, 100, 1000, 5000, 50000])
@pytest.mark.parametrize("immuno,stability,rate", [
    (False, False, 10), (True, False, 15), (False, True, 15), (True, True, 20),
])
def test_ptf_guidance_cap_flat_rate_and_no_setup_fee(count, immuno, stability, rate):
    flags = {"immunogenicity": immuno, "serum_stability": stability,
             "quality_guided": True, "ligandiq_fold_ranking": True}
    assert generation_credits(count, **flags) == count * rate
    quote = estimate_multi_cost(10, count, **flags)
    assert quote["credits_one_request"] == quote["credits_n_separate_runs"] == 10 * count * rate
    assert quote["credits_saved"] == quote["bulk_discount_percent"] == 0


@pytest.mark.parametrize("targets,count", [(0, 1), (1, 0), (-1, -1), (-1, 1)])
def test_multi_estimator_rejects_nonpositive_dimensions(targets, count):
    with pytest.raises(LigandAIError):
        estimate_multi_cost(targets, count)


@pytest.mark.parametrize("count,cost", [(0, 0), (1, 20), (3, 20), (4, 20), (5, 25), (100, 500)])
def test_ligandiq_floor_applies_per_job(count, cost):
    assert ligandiq_score_credits(count) == cost
    assert sum(ligandiq_score_credits(1) for _ in range(4)) == 80


BAD_QUOTES = [None, {}, [], {"status": "coming_soon"}, {"credits": 0},
              {"cost_usd": 0}, {"credits": None, "cost_usd": None},
              {"credits": 1, "costUsd": float("nan")},
              {"credits": float("inf"), "costUsd": 1},
              {"credits": -1, "costUsd": -0.01},
              {"credits": 1.5, "costUsd": 0.015},
              {"credits": False, "costUsd": 0},
              {"credits": 1, "costUsd": 0.01, "cost_usd": 0}]


def _call(resource, **kwargs):
    if isinstance(resource, (Synthesis, AsyncSynthesis)):
        return resource.estimate_cost("BRD4", **kwargs)
    return resource.estimate_cost(num_peptides=300, **kwargs)


@pytest.mark.parametrize("resource_type", [Synthesis, Peptides])
@pytest.mark.parametrize("payload", BAD_QUOTES)
def test_missing_quote_is_not_free(resource_type, payload):
    resource = resource_type(SimpleNamespace(request=Mock(return_value=payload)))
    with pytest.raises(LigandAIUnreadableResponse, match="unknown, not zero"):
        _call(resource)


@pytest.mark.parametrize("resource_type", [AsyncSynthesis, AsyncPeptides])
@pytest.mark.parametrize("payload", BAD_QUOTES)
async def test_async_missing_quote_is_not_free(resource_type, payload):
    resource = resource_type(SimpleNamespace(request=AsyncMock(return_value=payload)))
    with pytest.raises(LigandAIUnreadableResponse, match="unknown, not zero"):
        await _call(resource)


@pytest.mark.parametrize("resource_type", [Synthesis, Peptides, AsyncSynthesis, AsyncPeptides])
@pytest.mark.parametrize("usd_key", ["cost_usd", "costUsd"])
@pytest.mark.parametrize("credits", [0, 20, 10000])
async def test_explicit_quote_and_guidance_survive(resource_type, usd_key, credits):
    payload = {"credits": credits, usd_key: credits / 100,
               "breakdown": {"rates": {"per_peptide": 20}}, "future_field": "kept"}
    is_async = resource_type in (AsyncSynthesis, AsyncPeptides)
    request = (AsyncMock if is_async else Mock)(return_value=payload)
    result = _call(resource_type(SimpleNamespace(request=request)),
                   immunogenicity=True, serum_stability=True,
                   quality_guided=True, ligandiq_fold_ranking=True, sampling_steps=100)
    quote = await result if is_async else result
    assert quote.credits == credits and quote.cost_usd == credits / 100
    assert quote.breakdown == payload["breakdown"]
    assert quote.model_extra["future_field"] == "kept"
    params = request.call_args.kwargs["params"]
    assert params["sampling_steps"] == 100
    assert all(params[key] is True for key in
               ("immuno", "stability", "quality_guided", "ligandiq_fold_ranking"))


def test_campaign_guidance_matches_adapter_override_precedence():
    from ligandai.campaign import CampaignSpec
    from ligandai.campaign.spec import FoldSettings, GenerationSettings

    spec = SimpleNamespace(fold=FoldSettings(),
                           generation=GenerationSettings(heads={"immuno": 0.3, "proteolytic": True}))
    pc = SimpleNamespace(generator_overrides={})
    assert CampaignSpec.generation_rate(spec, pc) == 20
    spec.generation.extra["serum_stability"] = False
    assert CampaignSpec.generation_rate(spec, pc) == 15
    pc.generator_overrides["immunogenicity"] = False
    assert CampaignSpec.generation_rate(spec, pc) == 10
    spec.fold.credits_per_generated_peptide = 17
    assert CampaignSpec.generation_rate(spec, pc) == 17
