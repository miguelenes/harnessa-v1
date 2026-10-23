from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from harnessa.adapters.harness_evaluator import HTTPHarnessEvaluator
from harnessa.config import settings
from harnessa.worker import _configured_harness_evaluator
from pydantic import SecretStr


def test_evaluator_requires_tls_except_for_loopback() -> None:
    with pytest.raises(ValueError, match="HTTPS or loopback HTTP"):
        HTTPHarnessEvaluator("http://grader.example/evaluate", "token")

    with pytest.raises(ValueError, match="HTTPS or loopback HTTP"):
        HTTPHarnessEvaluator("https://user:password@grader.example/evaluate", "token")

    with pytest.raises(ValueError, match="HTTPS or loopback HTTP"):
        HTTPHarnessEvaluator("https://grader.example/evaluate?token=secret", "token")

    with pytest.raises(ValueError, match="bearer token"):
        HTTPHarnessEvaluator("http://127.0.0.1:18020/evaluate", " ")


@pytest.mark.anyio
async def test_evaluator_sends_only_manifest_and_returns_json_receipt() -> None:
    manifest = {"schema_version": 1, "digest": "a" * 64, "artifacts": []}
    receipt = {
        "status": "passed",
        "manifest_digest": manifest["digest"],
        "judge_revision": "private-judge-v1",
    }
    seen: dict[str, object] = {}

    async def handle(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization")
        seen["manifest"] = json.loads(request.content)
        return httpx.Response(200, json=receipt)

    evaluator = HTTPHarnessEvaluator(
        "http://127.0.0.1:18020/evaluate",
        "private-token",
        _transport=httpx.MockTransport(handle),
    )

    result = await evaluator.evaluate(manifest)

    assert seen == {"authorization": "Bearer private-token", "manifest": manifest}
    assert result == receipt


@pytest.mark.anyio
async def test_evaluator_rejects_redirects_without_exposing_body() -> None:
    async def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://unexpected.example"}, text="secret")

    evaluator = HTTPHarnessEvaluator(
        "http://127.0.0.1:18020/evaluate",
        "private-token",
        _transport=httpx.MockTransport(handle),
    )

    with pytest.raises(RuntimeError, match="HTTP 302") as exc:
        await evaluator.evaluate({"schema_version": 1})

    assert "secret" not in str(exc.value)


def test_holdout_manifest_schema_is_valid_json_and_excludes_hidden_cases() -> None:
    path = Path(__file__).parents[1] / "docs/schemas/harness-holdout-manifest-v1.schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))

    assert schema["properties"]["artifacts"]["type"] == "array"
    assert "digest" in schema["required"]
    assert "cases" not in schema["properties"]
    assert "answers" not in schema["properties"]


def test_unconfigured_worker_evaluator_starts_without_empty_environment_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "harness_holdout_evaluator_url", "")
    monkeypatch.setattr(settings, "harness_holdout_evaluator_token", SecretStr(""))

    assert _configured_harness_evaluator() is None


def test_worker_rejects_partial_evaluator_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "harness_holdout_evaluator_url", "")
    monkeypatch.setattr(settings, "harness_holdout_evaluator_token", SecretStr("configured"))

    with pytest.raises(ValueError, match="must be set together"):
        _configured_harness_evaluator()
