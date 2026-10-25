from __future__ import annotations

import json

import httpx
import pytest
from harnessa.config import settings
from harnessa.contracts import ModelProtocol
from harnessa.services.model_gateway import (
    LiteLLMModelRouter,
    ModelGatewayError,
    _error_code,
    _extract_usage,
    _retry_after,
    _stream_contains_tool_call,
    _stream_usage,
)
from pydantic import SecretStr


def test_litellm_request_headers_use_the_scoped_gateway_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "model_gateway_api_key", SecretStr("sk-test-virtual-key"))

    headers = LiteLLMModelRouter._request_headers("request-123", ModelProtocol.ANTHROPIC_MESSAGES)

    assert headers == {
        "X-Request-ID": "request-123",
        "Authorization": "Bearer sk-test-virtual-key",
        "anthropic-version": "2023-06-01",
    }


@pytest.mark.anyio
async def test_network_gateway_client_fails_closed_without_a_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "model_gateway_url", "http://litellm:4000")
    monkeypatch.setattr(settings, "model_gateway_api_key", None)

    with pytest.raises(ModelGatewayError, match="credential is not configured") as raised:
        await LiteLLMModelRouter()._get_client()

    assert raised.value.code == "model_gateway_credentials_missing"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (429, "provider_rate_limited"),
        (503, "provider_unavailable"),
        (504, "provider_timeout"),
        (501, "provider_server_error"),
        (401, "provider_authentication_failed"),
        (422, "provider_request_rejected"),
        (None, "provider_transport_error"),
    ],
)
def test_provider_error_statuses_map_to_safe_codes(status: int | None, expected: str) -> None:
    assert _error_code(status) == expected


def test_provider_timeout_exception_has_timeout_code() -> None:
    assert _error_code(None, httpx.ReadTimeout("timed out")) == "provider_timeout"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        (" 2.5 ", 2.5),
        ("-4", 0.0),
        ("invalid", None),
        ("Wed, 21 Oct 2015 07:28:00 GMT", 0.0),
    ],
)
def test_retry_after_accepts_bounded_numeric_and_date_values(value: str | None, expected: float | None) -> None:
    result = _retry_after(value)
    if expected == 0.0 and value and value.startswith("Wed"):
        assert result is not None and result == 0.0
    else:
        assert result == expected


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        ({"prompt_tokens": 3, "completion_tokens": 5}, (3, 5, True)),
        ({"input_tokens": "4", "output_tokens": "6"}, (4, 6, True)),
        ({"prompt_tokens": -1, "completion_tokens": 0}, (None, None, False)),
        ({"prompt_tokens": "bad", "completion_tokens": 0}, (None, None, False)),
        ({"prompt_tokens": 1}, (None, None, False)),
        (None, (None, None, False)),
    ],
)
def test_completion_usage_parser_rejects_incomplete_or_invalid_usage(usage, expected) -> None:
    assert _extract_usage({"usage": usage}) == expected


def test_stream_usage_handles_responses_and_anthropic_protocol_events() -> None:
    responses = json.dumps({"response": {"usage": {"input_tokens": 7, "output_tokens": 2}}})
    assert _stream_usage(responses, ModelProtocol.RESPONSES) == (7, 2)
    start = json.dumps({"type": "message_start", "usage": {}, "message": {"usage": {"input_tokens": 5}}})
    delta = json.dumps({"type": "message_delta", "usage": {"output_tokens": 3}})
    assert _stream_usage(start, ModelProtocol.ANTHROPIC_MESSAGES) == (5, None)
    assert _stream_usage(delta, ModelProtocol.ANTHROPIC_MESSAGES) == (None, 3)
    assert _stream_usage("[DONE]", ModelProtocol.CHAT_COMPLETIONS) is None
    assert _stream_usage("not-json", ModelProtocol.CHAT_COMPLETIONS) is None


def test_stream_tool_call_detection_is_protocol_specific() -> None:
    assert _stream_contains_tool_call(
        json.dumps({"choices": [{"delta": {"tool_calls": [{"index": 0}]}}]}),
        ModelProtocol.CHAT_COMPLETIONS,
    )
    assert _stream_contains_tool_call(
        json.dumps({"item": {"type": "function_call"}}), ModelProtocol.RESPONSES
    )
    assert _stream_contains_tool_call(
        json.dumps({"content_block": {"type": "tool_use"}}),
        ModelProtocol.ANTHROPIC_MESSAGES,
    )
    assert not _stream_contains_tool_call("not-json", ModelProtocol.CHAT_COMPLETIONS)
