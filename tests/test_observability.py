import asyncio

from fastapi.testclient import TestClient
from harnessa.config import settings
from harnessa.main import app
from harnessa.observability import OperationalMetrics, RequestMetrics, operational_metrics
from pydantic import SecretStr
from sqlalchemy.exc import SQLAlchemyError


def test_request_metrics_use_bounded_route_labels_and_cumulative_buckets() -> None:
    metrics = RequestMetrics()
    metrics.observe("GET", "/projects/{project_id}", 200, 0.02)
    metrics.observe("GET", "/projects/{project_id}", 200, 0.2)

    rendered = metrics.render()
    assert 'harnessa_http_requests_total{method="GET",route="/projects/{project_id}",status="200"} 2' in rendered
    assert 'harnessa_http_request_duration_seconds_bucket{method="GET",route="/projects/{project_id}",status="200",le="0.025"} 1' in rendered
    assert 'harnessa_http_request_duration_seconds_bucket{method="GET",route="/projects/{project_id}",status="200",le="0.25"} 2' in rendered
    assert 'harnessa_http_request_duration_seconds_bucket{method="GET",route="/projects/{project_id}",status="200",le="+Inf"} 2' in rendered


def test_request_metrics_collapse_unknown_methods_to_bounded_label() -> None:
    metrics = RequestMetrics()

    metrics.observe("HARNESSES-UNIQUE-EXTENSION", "/health", 200, 0.001)
    metrics.observe("ANOTHER-UNIQUE-EXTENSION", "/health", 200, 0.002)

    rendered = metrics.render()
    assert 'method="OTHER",route="/health",status="200"' in rendered
    assert "HARNESSES-UNIQUE-EXTENSION" not in rendered
    assert "ANOTHER-UNIQUE-EXTENSION" not in rendered
    assert rendered.count('method="OTHER"') == 15


def test_metrics_endpoint_requires_configured_bearer_token_and_request_ids_are_correlated(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "metrics_token", SecretStr("test-only-metrics-token"))

    async def render_operational_metrics() -> str:
        return "harnessa_operational_metrics_collection_success 1\n"

    monkeypatch.setattr(operational_metrics, "render", render_operational_metrics)
    trace_id = "0123456789abcdef0123456789abcdef"
    request_id = "ca9d6464-6c89-4c77-9709-04d2a7539d4b"
    with TestClient(app) as client:
        response = client.get(
            "/health/live",
            headers={"X-Request-ID": request_id, "X-Trace-ID": trace_id},
        )
        assert response.headers["X-Request-ID"] == request_id
        assert response.headers["X-Trace-ID"] == trace_id

        w3c_trace_id = "fedcba9876543210fedcba9876543210"
        traced_response = client.get(
            "/health/live",
            headers={"traceparent": f"00-{w3c_trace_id}-0123456789abcdef-01"},
        )
        assert traced_response.headers["X-Trace-ID"] == w3c_trace_id

        assert client.get("/internal/metrics").status_code == 404
        metrics_response = client.get(
            "/internal/metrics",
            headers={"Authorization": "Bearer test-only-metrics-token"},
        )
        assert metrics_response.status_code == 200
        assert 'route="/health/live"' in metrics_response.text
        assert "harnessa_operational_metrics_collection_success 1" in metrics_response.text
        assert "test-only-metrics-token" not in metrics_response.text


def test_operational_metrics_cache_keeps_database_queries_bounded(monkeypatch) -> None:
    metrics = OperationalMetrics()
    collection_count = 0

    async def collect() -> dict[str, float]:
        nonlocal collection_count
        collection_count += 1
        return {"queue_runs": 2.0, "artifact_bytes": 64.0}

    monkeypatch.setattr(metrics, "_collect", collect)

    expected = {
        "queue_runs": 2.0,
        "artifact_bytes": 64.0,
        "collection_success": 1.0,
    }
    assert asyncio.run(metrics.snapshot()) == expected
    assert asyncio.run(metrics.snapshot()) == expected
    assert collection_count == 1


def test_operational_metrics_report_collection_failure_without_leaking_error(
    monkeypatch, caplog
) -> None:
    metrics = OperationalMetrics()

    async def collect() -> dict[str, float]:
        raise SQLAlchemyError("postgres://user:secret@db.internal/harnessa")

    monkeypatch.setattr(metrics, "_collect", collect)

    assert asyncio.run(metrics.render()) == """# HELP harnessa_operational_metrics_collection_success Whether durable metrics were collected.
# TYPE harnessa_operational_metrics_collection_success gauge
harnessa_operational_metrics_collection_success 0
"""
    assert "secret" not in caplog.text
