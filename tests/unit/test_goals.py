# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Persistent goal-run SDK surface."""

from __future__ import annotations

import json

import pytest
from pytest_httpx import HTTPXMock, IteratorStream

from ligandai import (
    GoalAcceptanceCriterion,
    GoalEvaluation,
    GoalProjectState,
    GoalRun,
    GoalRunEvent,
    GoalRunStart,
    LigandAI,
)

BASE = "http://api.ligandai.test"


@pytest.fixture
def client() -> LigandAI:
    return LigandAI(api_key="lgai_pro_test", base_url=BASE, max_retries=1)


def _body(httpx_mock: HTTPXMock) -> dict:
    req = httpx_mock.get_request()
    assert req is not None
    return json.loads(req.content)


def test_start_requires_automatic_mode_ack(client: LigandAI) -> None:
    with pytest.raises(ValueError, match="automatic_mode=True"):
        client.goals.start("optimize IL31 peptide selectivity")


def test_start_posts_goal_budget_and_context(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/autoresearch/start",
        method="POST",
        json={"runId": "arun_123"},
    )

    started = client.goals.start(
        "optimize IL31 peptide selectivity",
        automatic_mode=True,
        budget_cap_credits=5_000,
        program_db_id=12,
        project_db_id=34,
        program_id="ptf_program_uuid",
        project_id="ptf_project_uuid",
        conversation_id="conv_abc",
        max_iterations=5,
    )

    assert isinstance(started, GoalRunStart)
    assert started.run_id == "arun_123"
    assert _body(httpx_mock) == {
        "goal": "optimize IL31 peptide selectivity",
        "automaticMode": True,
        "automaticModeAcknowledged": True,
        "budgetCapCredits": 5000,
        "programDbId": 12,
        "projectDbId": 34,
        "programId": "ptf_program_uuid",
        "projectId": "ptf_project_uuid",
        "conversationId": "conv_abc",
        "maxIterations": 5,
    }


def test_list_and_get_parse_goal_runs(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    run_payload = {
        "runId": "arun_123",
        "userId": "u_1",
        "conversationId": "conv_abc",
        "goal": "design binders",
        "status": "running",
        "currentStepIdx": 1,
        "plan": [{"step": 1, "intent": "Generate candidates", "tool": "generate_peptides"}],
        "acceptanceCriteria": [
            {
                "id": "ipSAE",
                "label": "At least one folded peptide has iPSAE >= 0.7",
                "metric": "ipSAE",
                "operator": ">=",
                "target": 0.7,
                "required": True,
            }
        ],
        "evaluationHistory": [
            {
                "evaluationIdx": 0,
                "evaluatedAt": "2026-05-05T00:01:00Z",
                "status": "partial",
                "rationale": "Generation ran but folds are not complete.",
                "satisfiedCriteria": [],
                "unsatisfiedCriteria": ["ipSAE"],
                "nextAction": "continue_plan",
            }
        ],
        "satisfactionStatus": "partial",
        "iterationCount": 1,
        "maxIterations": 3,
        "stepHistory": [{"stepIdx": 0, "tool": "load_structure", "startedAt": "2026-05-05T00:00:00Z"}],
        "tokensUsed": 123,
        "creditsConsumed": 400,
        "budgetCapCredits": 5000,
        "automaticModeAcknowledged": True,
        "automaticModeAcknowledgedAt": "2026-05-05T00:00:30Z",
        "goalState": {
            "objective": "design binders",
            "status": "running",
            "satisfactionStatus": "partial",
            "checklist": [
                {
                    "id": "step-1",
                    "type": "step",
                    "label": "Generate candidates",
                    "status": "completed",
                    "dependsOn": [],
                },
                {
                    "id": "criterion-ipSAE",
                    "type": "criterion",
                    "label": "At least one folded peptide has iPSAE >= 0.7",
                    "status": "partial",
                    "metric": "ipSAE",
                    "operator": ">=",
                    "target": 0.7,
                    "dependsOn": ["step-1"],
                    "evidence": "Folds are not complete.",
                    "blockers": ["Missing fold evidence"],
                    "nextActions": ["Fold candidates."],
                },
            ],
            "dependencies": [
                {"from": "step-1", "to": "criterion-ipSAE", "reason": "Evidence source for acceptance criterion"}
            ],
            "evidence": {"latestEvaluation": {"ipSAE": "Folds are not complete."}},
            "blockers": [],
            "nextActions": ["Continue with step 2: Fold candidates."],
            "progress": {
                "totalItems": 2,
                "completedItems": 1,
                "totalCriteria": 1,
                "satisfiedCriteria": 0,
                "planSteps": 1,
                "currentStepIdx": 1,
                "percent": 50,
            },
            "budget": {"capCredits": 5000, "consumedCredits": 400, "remainingCredits": 4600},
        },
    }
    httpx_mock.add_response(url=f"{BASE}/api/autoresearch/runs", json={"runs": [run_payload]})
    httpx_mock.add_response(
        url=f"{BASE}/api/autoresearch/runs?programId=ptf_program_uuid&conversationId=conv_abc",
        json={"runs": [run_payload]},
    )
    httpx_mock.add_response(url=f"{BASE}/api/autoresearch/runs/arun_123", json={"run": run_payload})

    runs = client.goals.list()
    assert len(runs) == 1
    assert isinstance(runs[0], GoalRun)
    assert runs[0].run_id == "arun_123"
    assert runs[0].plan and runs[0].plan[0].tool == "generate_peptides"
    assert isinstance(runs[0].acceptance_criteria[0], GoalAcceptanceCriterion)
    assert runs[0].acceptance_criteria[0].metric == "ipSAE"
    assert isinstance(runs[0].evaluation_history[0], GoalEvaluation)
    assert isinstance(runs[0].goal_state, GoalProjectState)
    assert runs[0].goal_state is not None
    assert runs[0].goal_state.progress.percent == 50
    assert runs[0].goal_state.checklist[1].depends_on == ["step-1"]
    assert runs[0].goal_state.dependencies[0].from_item == "step-1"
    assert runs[0].satisfaction_status == "partial"
    assert runs[0].iteration_count == 1
    assert runs[0].max_iterations == 3
    assert runs[0].step_history[0].step_idx == 0

    scoped_runs = client.goals.list(program_id="ptf_program_uuid", conversation_id="conv_abc")
    assert scoped_runs[0].run_id == "arun_123"

    run = client.goals.get("arun_123")
    assert run.conversation_id == "conv_abc"
    assert run.credits_consumed == 400
    assert run.automatic_mode_acknowledged is True
    assert run.automatic_mode_acknowledged_at is not None


def test_goal_graph_endpoint(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    graph_payload = {
        "objective": "design binders",
        "status": "running",
        "satisfactionStatus": "partial",
        "checklist": [{"id": "step-1", "type": "step", "label": "Generate", "status": "completed"}],
        "dependencies": [],
        "evidence": {},
        "blockers": [],
        "nextActions": ["Fold candidates."],
        "progress": {
            "totalItems": 1,
            "completedItems": 1,
            "totalCriteria": 0,
            "satisfiedCriteria": 0,
            "planSteps": 1,
            "currentStepIdx": 1,
            "percent": 100,
        },
        "budget": {"capCredits": 5000, "consumedCredits": 400, "remainingCredits": 4600},
    }
    httpx_mock.add_response(
        url=f"{BASE}/api/autoresearch/runs/arun_123/graph",
        json={"runId": "arun_123", "goalState": graph_payload},
    )

    graph = client.goals.graph("arun_123")

    assert isinstance(graph, GoalProjectState)
    assert graph.progress.percent == 100
    assert graph.next_actions == ["Fold candidates."]


def test_goal_stream_parses_sse_events(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/autoresearch/runs/arun_123/stream",
        stream=IteratorStream(
            [
                b'data: {"type":"hello","runId":"arun_123","run":{"runId":"arun_123","goal":"design binders","status":"running"}}\n\n',
                b'data: {"type":"goal_evaluated","runId":"arun_123","evaluation":{"status":"partial","rationale":"Need folds","satisfiedCriteria":[],"unsatisfiedCriteria":["ipSAE"]}}\n\n',
            ]
        ),
    )

    events = list(client.goals.stream("arun_123"))

    assert len(events) == 2
    assert all(isinstance(event, GoalRunEvent) for event in events)
    assert events[0].type == "hello"
    assert events[0].run is not None
    assert events[0].run.run_id == "arun_123"
    assert events[1].evaluation is not None
    assert events[1].evaluation.unsatisfied_criteria == ["ipSAE"]


def test_goal_run_controls(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    for action in ("pause", "resume", "stop"):
        httpx_mock.add_response(
            url=f"{BASE}/api/autoresearch/runs/arun_123/{action}",
            method="POST",
            json={"ok": True},
        )

    assert client.goals.pause("arun_123") is True
    assert client.goals.resume("arun_123") is True
    assert client.goals.stop("arun_123") is True
