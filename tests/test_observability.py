from fastapi.testclient import TestClient
from harnessa.config import settings
from harnessa.main import app
from harnessa.observability import RequestMetrics
from pydantic import SecretStr


def test_request_metrics_use_bounded_route_labels_and_cumulative_buckets() -> None:
    metrics = RequestMetrics()
    metrics.observe("GET", "/projects/{project_id}", 200, 0.02)
    metrics.observe("GET", "/projects/{project_id}", 200, 0.2)

    rendered = metrics.render()
    assert 'harnessa_http_requests_total{method="GET",route="/projects/{project_id}",status="200"} 2' in rendered
    assert 'harnessa_http_request_duration_seconds_bucket{method="GET",route="/projects/{project_id}",status="200",le="0.025"} 1' in rendered
    assert 'harnessa_http_request_duration_seconds_bucket{method="GET",route="/projects/{project_id}",status="200",le="0.25"} 2' in rendered
    assert 'harnessa_http_request_duration_seconds_bucket{method="GET",route="/projects/{project_id}",status="200",le="+Inf"} 2' in rendered


def test_metrics_endpoint_requires_configured_bearer_token_and_request_ids_are_correlated(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "metrics_token", SecretStr("test-only-metrics-token"))
    trace_id = "0123456789abcdef0123456789abcdef"
    request_id = "ca9d6464-6c89-4c77-9709-04d2a7539d4b"
    with TestClient(app) as client:
        response = client.get(
            "/health/live",
            headers={"X-Request-ID": request_id, "X-Trace-ID": trace_id},
        )
        assert response.headers["X-Request-ID"] == request_id
        assert response.headers["X-Trace-ID"] == trace_id

        assert client.get("/internal/metrics").status_code == 404
        metrics_response = client.get(
            "/internal/metrics",
            headers={"Authorization": "Bearer test-only-metrics-token"},
        )
        assert metrics_response.status_code == 200
        assert 'route="/health/live"' in metrics_response.text
        assert "test-only-metrics-token" not in metrics_response.text
