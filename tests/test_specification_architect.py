from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest
from harnessa.adapters.specification_architect import (
    ArchitectDraft,
    SpecificationArchitect,
    SpecificationArchitectError,
    _draft_tool,
    _normalize_architect_markdown,
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


def test_architect_tool_schema_is_self_contained_and_matches_the_draft_fields() -> None:
    schema = _draft_tool()["function"]["parameters"]

    def has_reference(value: Any) -> bool:
        if isinstance(value, dict):
            return "$ref" in value or any(has_reference(item) for item in value.values())
        if isinstance(value, list):
            return any(has_reference(item) for item in value)
        return False

    assert schema["type"] == "object"
    assert set(schema["properties"]) == {
        "proposal_md",
        "spec_md",
        "design_md",
        "tasks_md",
        "questions",
    }
    assert not has_reference(schema)


def test_architect_restores_markdown_structure_from_flattened_tool_arguments() -> None:
    spec = _normalize_architect_markdown(
        "## ADDED Requirements### Requirement: greeting#### Scenario: named-"
        " **WHEN** a name is provided- **THEN** a greeting is returned",
        "greeting",
    )

    assert spec.startswith(
        "## ADDED Requirements\n\n### Requirement: greeting\n\n"
        "#### Scenario: named\n- **WHEN**"
    )
    assert "- **THEN** a greeting is returned\n" in spec

    prose_heading = _normalize_architect_markdown(
        "## WhyThe rationale references ## Impact, which is a literal heading label."
        "## ImpactActual impact follows.",
        "greeting",
    )
    assert "The rationale references ## Impact, which is a literal heading label." in prose_heading
    assert "## ImpactActual impact follows." in prose_heading

    spaced_template = _normalize_architect_markdown(
        "## WhyThe change reason. ## What Changesa scoped change. "
        "## Capabilities### greeting## Impactsmall footprint.",
        "greeting",
    )
    assert "## Why\n\nThe change reason." in spaced_template
    assert "## What Changes\n\na scoped change." in spaced_template
    assert "## Capabilities\n\n### greeting\n\n" in spaced_template
    assert "## Impact\n\nsmall footprint." in spaced_template

    prose_hyphen = _normalize_architect_markdown(
        "## WhyA scope check is required - Harnessa retains authority."
        "## ImpactRuntime stays bounded.",
        "greeting",
    )
    assert "required - Harnessa retains authority." in prose_hyphen
    assert "## ImpactRuntime stays bounded." in prose_hyphen

    numbered_tasks = _normalize_architect_markdown(
        "## 1. Implementation- [ ] Preserve the complete first task."
        "## 2. Verification- [ ] Preserve the complete second task.",
        "greeting",
    )
    assert "## 1. Implementation\n- [ ] Preserve the complete first task." in numbered_tasks
    assert "## 2. Verification\n- [ ] Preserve the complete second task." in numbered_tasks

    html_breaks = _normalize_architect_markdown(
        "## ADDED Requirements<br><br>### Requirement: greeting user<br>"
        "SHALL return a greeting.<br><br>#### Scenario: named<br>"
        "- **WHEN** a name is provided<br>- **THEN** a greeting is returned",
        "greeting",
    )
    assert "<br>" not in html_breaks
    assert (
        "### Requirement: greeting user\n\nThe system SHALL return a greeting."
        in html_breaks
    )
    assert "#### Scenario: named\n- **WHEN**" in html_breaks

    inline_normative = _normalize_architect_markdown(
        "## ADDED Requirements<br>### Requirement: Accept typed name **SHALL** "
        "accept a string name input.<br>#### Scenario: valid name<br>"
        "- **WHEN** a caller provides a name<br>- **THEN** the name is accepted",
        "greeting",
    )
    assert "### Requirement: Accept typed name\n\nThe system SHALL accept a string name input." in inline_normative
    assert "#### Scenario: valid name\n- **WHEN**" in inline_normative

    flattened_normative = _normalize_architect_markdown(
        "## ADDED Requirements### Requirement: GreetingPersonalizedThe helper SHALL "
        "return a friendly personalized greeting.#### Scenario: named greeting"
        "- **WHEN** a name is supplied- **THEN** a greeting is returned",
        "greeting",
    )
    assert (
        "### Requirement: GreetingPersonalized\n\nThe helper SHALL return a friendly "
        "personalized greeting."
    ) in flattened_normative
    assert "#### Scenario: named greeting\n- **WHEN**" in flattened_normative

    proposal = _normalize_architect_markdown(
        "## Why A reusable greeting helper is needed. ## What Changes Add a typed helper. "
        "## Capabilities ### greeting Provide a friendly greeting. ## Impact Low risk.\n",
        "greeting",
    )
    assert "## Why\n\nA reusable greeting helper is needed." in proposal
    assert "## What Changes\n\nAdd a typed helper." in proposal
    assert "## Capabilities\n\n### greeting" in proposal
    assert "## Impact\n\nLow risk." in proposal

    design = _normalize_architect_markdown(
        "## Context A small utility is needed. ## Goals and Non-Goals Provide a typed API. "
        "## Decisions Return a deterministic greeting. ## Risks / Trade-offs Low risk.\n",
        "greeting",
    )
    assert all(
        heading in design
        for heading in (
            "## Context\n\n",
            "## Goals and Non-Goals\n\n",
            "## Decisions\n\n",
            "## Risks / Trade-offs\n\n",
        )
    )

    separate_normative = _normalize_architect_markdown(
        "## ADDED Requirements \n\n### Requirement: Greeting Accepts Typed Name SHALL return "
        "a friendly personalized greeting.\n\n#### Scenario: named greeting\n"
        "- **WHEN** a name is supplied\n- **THEN** a greeting is returned\n",
        "greeting",
    )
    assert (
        "### Requirement: Greeting Accepts Typed Name\n\nThe system SHALL return "
        "a friendly personalized greeting."
    ) in separate_normative

    provider_tasks = _normalize_architect_markdown(
        "### Implementation: greeting helper - [ ] Implement the typed helper. "
        "### Implementation: unit tests - [ ] Test the personalized greeting. "
        "## Verification - Run the focused unit test. "
        "### Requirement mapping: Greeting Accepts Typed Name - Unit test verifies the scenario.\n",
        "greeting",
    )
    assert "## 1. Implementation: greeting helper\n\n- [ ] Implement" in provider_tasks
    assert "## 2. Implementation: unit tests\n\n- [ ] Test" in provider_tasks
    assert "## Verification\n\n- Run the focused unit test." in provider_tasks
    assert "### Requirement mapping: Greeting Accepts Typed Name\n\n- Unit test" in provider_tasks

    flattened_provider_tasks = _normalize_architect_markdown(
        "## Implementation ### 1. Implement GreetingHelper - [ ] Define function "
        "`greet(name: string): string` returning `Hello, <name>!`. "
        "### 2. Write unit tests - [ ] Add test case `greet(`Alice`)` expects "
        "`Hello, Alice!`. - [ ] Add test case `greet(`)` expects `Hello, !`. "
        "## Verification - [x] Run the unit tests and confirm all pass. "
        "### Requirement mapping: GreetingHelper shall produce a friendly "
        "personalized greeting\n",
        "greeting",
    )
    assert "## Implementation\n\n### 1. Implement GreetingHelper" in flattened_provider_tasks
    assert flattened_provider_tasks.count("- [ ]") == 3
    assert "## Verification\n\n- [x] Run the unit tests" in flattened_provider_tasks
    assert (
        "### Requirement mapping: GreetingHelper shall produce a friendly"
        in flattened_provider_tasks
    )

    numbered_provider_tasks = _normalize_architect_markdown(
        "1. **Implement `greet(name: string): string`**   - [ ] Write the function "
        "body returning `Hello, ${name}!`.2. **Add unit tests for `greet`**   - [ ] "
        "Test greeting with typical name.   - [ ] Test greeting with empty name."
        "## Verification- Complete all checkbox tasks.- Run tests; all must pass."
        "### Requirement mapping: greet_user- **Requirement: greet_user SHALL return "
        "a friendly personalized greeting**  - [ ] Verified by task 2 checkboxes.",
        "greeting",
    )
    assert "## 1. Implement `greet(name: string): string`" in numbered_provider_tasks
    assert "## 2. Add unit tests for `greet`" in numbered_provider_tasks
    assert numbered_provider_tasks.count("- [ ]") == 4
    assert "## Verification\n\n- Complete all checkbox tasks." in numbered_provider_tasks
    assert "### Requirement mapping: greet_user" in numbered_provider_tasks

    no_space_requirement = _normalize_architect_markdown(
        "## ADDED Requirements### Requirement: greet_userSHALL return a friendly "
        "personalized greeting.#### Scenario: Standard greeting- **WHEN** the function "
        "is called with a valid name- **THEN** it returns `Hello, {name}!`",
        "greeting",
    )
    assert "### Requirement: greet_user\n\nThe system SHALL return a friendly" in no_space_requirement

    content_integrity = (
        "## ADDED Requirements\n\n### Requirement: preserve examples\n\n"
        "The system SHALL preserve literal examples.\n\n"
        "Example `## Impact` and `&lt;tag&gt;`.\n\n"
        "```html\n## Impact\n&amp; stays encoded\n```\n"
    )
    assert _normalize_architect_markdown(content_integrity, "greeting") == content_integrity


def test_architect_recovers_flattened_spec_around_multiline_fenced_example() -> None:
    fenced = "```text\nhello\n```"
    source = (
        "## ADDED Requirements### Requirement: Show exampleThe helper SHALL return "
        "an example. #### Scenario: fenced example- **WHEN** a request is made- "
        f"**THEN** it returns:\n{fenced}"
    )

    normalized = _normalize_architect_markdown(source, "example")

    assert "### Requirement: Show example\n\nThe helper SHALL return an example." in normalized
    assert "#### Scenario: fenced example\n- **WHEN** a request is made" in normalized
    assert "- **THEN** it returns:\n" + fenced in normalized
    assert normalized.count(fenced) == 1


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
            assert request.requirements.tools is True
            assert request.tools[0]["function"]["name"] == "submit_architect_draft"
            tool_bytes = len(json.dumps(request.tools, separators=(",", ":")).encode())
            message_bytes = sum(len(message.content.encode()) for message in request.messages)
            assert request.estimated_input_tokens >= tool_bytes + message_bytes
            assert request.requirements.minimum_context == (
                request.estimated_input_tokens + request.max_output_tokens
            )
            assert kwargs["tool_choice"] == "required"
            assert kwargs["idempotency_key"].startswith("openspec-architect:")
            system_message = request.messages[0].content
            assert "## ADDED Requirements" in system_message
            assert "#### Scenario: <name>" in system_message
            assert "### Requirement mapping: <requirement>" in system_message
            assert "Markdown line breaks are mandatory" in system_message
            assert "Never join two headings or sections on one line" in system_message
            assert "the requirement heading alone is invalid" in system_message
            assert "Keep each file concise" in system_message
            return {
                "response": {
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "type": "function",
                                        "function": {
                                            "name": "submit_architect_draft",
                                            "arguments": json.dumps(
                                                {
                                                    **_draft_payload(),
                                                    "spec_md": (
                                                        "## ADDED Requirements&#x0A;&#x0A;"
                                                        "### Requirement: scoped-access&#x0A;&#x0A;"
                                                        "#### Scenario: authorized-read&#x0A;"
                                                        "- **WHEN** access is granted&#x0A;"
                                                        "- **THEN** content is returned"
                                                    ),
                                                }
                                            ),
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
            }

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
    assert draft.spec_md.startswith("## ADDED Requirements\n\n### Requirement:")
    assert "#### Scenario: authorized-read\n" in draft.spec_md


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


@pytest.mark.anyio
async def test_architect_diagnoses_empty_text_without_echoing_provider_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "openspec_model_alias", "fixture-budgeted")

    class Router:
        async def complete(self, *_: object, **__: Any) -> dict[str, Any]:
            return {
                "response": {
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {
                                "content": None,
                                "reasoning_content": "private provider text",
                                "refusal": "private refusal text",
                            },
                        }
                    ]
                }
            }

    with pytest.raises(SpecificationArchitectError) as error:
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

    message = str(error.value)
    assert message == (
        "The Architect response contained no text content "
        "(finish_reason=length; assistant_fields=content,reasoning_content,refusal)"
    )
    assert "private provider text" not in message
    assert "private refusal text" not in message


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("content", "expected_diagnostic"),
    [
        ("private malformed response", "json_error=expected_value; position=0; content_chars=26"),
        (
            json.dumps({"proposal_md": "private draft text", "private_key": "private field"}),
            "validation_errors=5; fields=design_md,questions,spec_md,tasks_md",
        ),
    ],
)
async def test_architect_reports_safe_json_and_schema_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    content: str,
    expected_diagnostic: str,
) -> None:
    monkeypatch.setattr(settings, "openspec_model_alias", "fixture-budgeted")

    class Router:
        async def complete(self, *_: object, **__: Any) -> dict[str, Any]:
            return {"response": {"choices": [{"message": {"content": content}}]}}

    with pytest.raises(SpecificationArchitectError) as error:
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

    message = str(error.value)
    assert expected_diagnostic in message
    assert "private malformed response" not in message
    assert "private draft text" not in message
    assert "private field" not in message
