import pytest
from harnessa.contracts import ModelMessage, ModelProtocol, ModelRouteRequest, RouteRequirements
from harnessa.services.model_gateway import _protocol_path, _retry_after, _supports


def test_provider_protocol_payloads_use_their_server_owned_shapes():
    chat = ModelRouteRequest(
        requirements=RouteRequirements(protocol=ModelProtocol.CHAT_COMPLETIONS, json_mode=True),
        max_output_tokens=20,
        estimated_input_tokens=10,
        messages=[ModelMessage(role="user", content="hello")],
        metadata={"secret": "must-not-forward"},
    )
    payload = chat.to_provider_payload("gateway-model")
    assert payload["model"] == "gateway-model"
    assert payload["messages"] == [{"role": "user", "content": "hello"}]
    assert "metadata" not in payload
    assert _protocol_path(ModelProtocol.CHAT_COMPLETIONS) == "/v1/chat/completions"
    json_payload = chat.to_provider_payload(
        "gateway-model",
        response_format={"type": "json_object"},
    )
    assert json_payload["response_format"] == {"type": "json_object"}
    with pytest.raises(ValueError, match="non-streaming chat completions"):
        responses_request = ModelRouteRequest(
            requirements=RouteRequirements(protocol=ModelProtocol.RESPONSES),
            max_output_tokens=20,
            estimated_input_tokens=10,
            input="hello",
        )
        responses_request.to_provider_payload(
            "gateway-model", response_format={"type": "json_object"}
        )

    undeclared_json_mode = ModelRouteRequest(
        requirements=RouteRequirements(protocol=ModelProtocol.CHAT_COMPLETIONS),
        max_output_tokens=20,
        estimated_input_tokens=10,
        messages=[ModelMessage(role="user", content="hello")],
    )
    with pytest.raises(ValueError, match="non-streaming chat completions"):
        undeclared_json_mode.to_provider_payload(
            "gateway-model", response_format={"type": "json_object"}
        )
    with pytest.raises(ValueError, match="only for non-streaming requests"):
        ModelRouteRequest(
            requirements=RouteRequirements(
                protocol=ModelProtocol.CHAT_COMPLETIONS,
                streaming=True,
                json_mode=True,
            ),
            max_output_tokens=20,
            estimated_input_tokens=10,
            stream=True,
            messages=[ModelMessage(role="user", content="hello")],
        )

    tool_request = ModelRouteRequest(
        requirements=RouteRequirements(protocol=ModelProtocol.CHAT_COMPLETIONS, tools=True),
        max_output_tokens=20,
        estimated_input_tokens=10,
        messages=[ModelMessage(role="user", content="return a typed result")],
        tools=[{"type": "function", "function": {"name": "submit", "parameters": {}}}],
    )
    tool_payload = tool_request.to_provider_payload("gateway-model", tool_choice="required")
    assert tool_payload["tool_choice"] == "required"
    assert tool_payload["tools"] == tool_request.tools

    responses = ModelRouteRequest(
        requirements=RouteRequirements(protocol=ModelProtocol.RESPONSES),
        max_output_tokens=20,
        estimated_input_tokens=10,
        input="hello",
        system="Be concise",
    )
    assert responses.to_provider_payload("gateway-model")["instructions"] == "Be concise"
    assert _protocol_path(ModelProtocol.RESPONSES) == "/v1/responses"

    anthropic = ModelRouteRequest(
        requirements=RouteRequirements(protocol=ModelProtocol.ANTHROPIC_MESSAGES),
        max_output_tokens=20,
        estimated_input_tokens=10,
        messages=[ModelMessage(role="user", content="hello")],
    )
    assert anthropic.to_provider_payload("gateway-model")["max_tokens"] == 20
    assert _protocol_path(ModelProtocol.ANTHROPIC_MESSAGES) == "/v1/messages"


def test_route_compatibility_requires_every_requested_capability():
    capabilities = {
        "protocols": ["chat_completions", "responses"],
        "streaming": True,
        "tools": False,
        "usage_reporting": True,
        "cancellation": False,
    }
    assert _supports(
        capabilities,
        RouteRequirements(protocol=ModelProtocol.CHAT_COMPLETIONS, streaming=True),
    )
    assert not _supports(
        capabilities,
        RouteRequirements(protocol=ModelProtocol.CHAT_COMPLETIONS, tools=True),
    )
    assert not _supports(
        capabilities,
        RouteRequirements(protocol=ModelProtocol.ANTHROPIC_MESSAGES),
    )
    assert not _supports(
        capabilities,
        RouteRequirements(protocol=ModelProtocol.CHAT_COMPLETIONS, json_mode=True),
    )
    capabilities["json_mode"] = True
    assert _supports(
        capabilities,
        RouteRequirements(protocol=ModelProtocol.CHAT_COMPLETIONS, json_mode=True),
    )


def test_retry_after_accepts_seconds_and_http_dates_without_unbounded_values():
    assert _retry_after("0") == 0
    assert _retry_after("4.5") == 4.5
    assert _retry_after("not-a-date") is None
