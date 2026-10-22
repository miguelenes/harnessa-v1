from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest
from harnessa.adapters.specification_architect import (
    ArchitectDraft,
    SpecificationArchitect,
    SpecificationArchitectError,
)
from harnessa.config import settings
from harnessa.services.model_gateway import ModelGatewayError


def _draft_payload() -> dict[str, Any]:
    return {
        "proposal_md": "## Why\nA clear need.\n",
        "spec_md": "## ADDED Requirements\n\n### Requirement: Safe action\n\n#### Scenario: Safe\n- **WHEN** requested\n- **THEN** validated\n",
        "design_md": "## Context\nKeep existing boundaries.\n",
        "tasks_md": "## Tasks\n- [ ] Verify Safe action\n",
        "questions": [
            {
                "question": "Which actor may approve this workflow?",
                "classification": "blocking",
                "proposed_assumption": "A project reviewer.",
                "recommended_options": [{"label": "Reviewer", "description": "Reviewer membership."}],
                "affected_artifacts": ["proposal.md"],
            }
        ],
    }


@pytest.mark.anyio
async def test_architect_uses_bounded_harnessa_route_and_validates_structured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "openspec_model_alias", "fixture-budgeted")
    monkeypatch.setattr(settings, "openspec_model_max_output_tokens", 512)

    class Router:
        async def complete(self, _db: object, **kwargs: Any) -> dict[str, Any]:
            request = kwargs["request"]
            assert request.model_alias == "fixture-budgeted"
            assert request.max_output_tokens == 512
            assert request.requirements.minimum_context > 512
            assert kwargs["idempotency_key"].startswith("openspec-architect:")
            return {"response": {"choices": [{"message": {"content": json.dumps(_draft_payload())}}]}}

    draft = await SpecificationArchitect(Router()).generate(
        object(),  # type: ignore[arg-type]
        organization_id=uuid4(),
        project_id=uuid4(),
        task_id=uuid4(),
        interview_id=uuid4(),
        revision=4,
        topic="Add a bounded workflow",
        change_id="bounded-workflow",
        citations=[{"path": "openspec/specs/access.md", "untrusted_source_content": True}],
    )
    assert isinstance(draft, ArchitectDraft)
    assert draft.questions[0].classification == "blocking"


@pytest.mark.anyio
async def test_architect_fails_closed_when_model_route_is_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "openspec_model_alias", "")
    with pytest.raises(ModelGatewayError) as error:
        await SpecificationArchitect().generate(
            object(),  # type: ignore[arg-type]
            organization_id=uuid4(),
            project_id=uuid4(),
            task_id=uuid4(),
            interview_id=uuid4(),
            revision=1,
            topic="Bounded task",
            change_id="bounded-task",
            citations=[],
        )
    assert error.value.code == "openspec_model_not_configured"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "response",
    [
        {},
        {"response": None},
        {"response": {"choices": []}},
        {"response": {"choices": [{"message": {"content": "not-json"}}]}},
        {"response": {"choices": [{"message": {"content": json.dumps({"unexpected": True})}}]}},
    ],
)
async def test_architect_rejects_missing_or_invalid_model_responses(
    monkeypatch: pytest.MonkeyPatch, response: dict[str, Any]
) -> None:
    monkeypatch.setattr(settings, "openspec_model_alias", "fixture-budgeted")

    class Router:
        async def complete(self, *_: object, **__: object) -> dict[str, Any]:
            return response

    with pytest.raises(SpecificationArchitectError):
        await SpecificationArchitect(Router()).generate(
            object(),  # type: ignore[arg-type]
            organization_id=uuid4(),
            project_id=uuid4(),
            task_id=uuid4(),
            interview_id=uuid4(),
            revision=1,
            topic="Bounded task",
            change_id="bounded-task",
            citations=[],
        )


@pytest.mark.anyio
async def test_architect_rejects_response_over_output_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "openspec_model_alias", "fixture-budgeted")

    class Router:
        async def complete(self, *_: object, **__: object) -> dict[str, Any]:
            return {"response": {"choices": [{"message": {"content": "x" * 512_001}}]}}

    with pytest.raises(SpecificationArchitectError, match="exceeded its 512000-byte output bound"):
        await SpecificationArchitect(Router()).generate(
            object(),  # type: ignore[arg-type]
            organization_id=uuid4(),
            project_id=uuid4(),
            task_id=uuid4(),
            interview_id=uuid4(),
            revision=1,
            topic="Bounded task",
            change_id="bounded-task",
            citations=[],
        )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("message", "expected_error"),
    [
        ({}, "no text content"),
        ({"content": [{"type": "text", "text": "not accepted as a string"}]}, "was not text"),
    ],
)
async def test_architect_reports_bounded_content_shape_without_echoing_provider_data(
    monkeypatch: pytest.MonkeyPatch,
    message: dict[str, Any],
    expected_error: str,
) -> None:
    monkeypatch.setattr(settings, "openspec_model_alias", "fixture-budgeted")

    class Router:
        async def complete(self, *_: object, **__: object) -> dict[str, Any]:
            return {"response": {"choices": [{"message": message}]}}

    with pytest.raises(SpecificationArchitectError, match=expected_error) as error:
        await SpecificationArchitect(Router()).generate(
            object(),  # type: ignore[arg-type]
            organization_id=uuid4(),
            project_id=uuid4(),
            task_id=uuid4(),
            interview_id=uuid4(),
            revision=1,
            topic="Bounded task",
            change_id="bounded-task",
            citations=[],
        )
    assert "not accepted as a string" not in str(error.value)
