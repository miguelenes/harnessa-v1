# Distributed tracing

Harnessa uses OpenTelemetry's W3C Trace Context format. The API creates server
spans and returns the trace ID in `X-Trace-ID`. A valid incoming `traceparent`
continues an upstream trace; when only the legacy `X-Trace-ID` is supplied, the
API seeds a synthetic W3C parent with that trace ID for continuity. Task
transitions persist the current `traceparent`, scheduler
claims copy it onto the run, and the worker resumes that context for execution.
GitHub webhook receipts persist the ingress context so the outbox worker can
continue it after the HTTP request has ended. Model routing and GitHub effect
operations create bounded child spans.

The spans record service, operation, route template, task/run identifiers, model
alias/pool, or GitHub delivery/event metadata. They do not record prompt or
completion text, webhook payloads, credentials, user answers, repository paths,
or raw exception messages. An OTLP collector still receives operational IDs and
should be access-controlled and retained according to the deployment's data
policy.

## Configure an OTLP/HTTP collector

The API and worker send spans only when an OTLP endpoint is configured. Set the
collector base URL in `.env` (or the deployment's secret/config source) and
recreate API and worker:

```dotenv
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318
```

The default exporter posts to `/v1/traces`. For a traces-specific full endpoint,
set `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` and include its complete path, normally
ending in `/v1/traces`. The collector must be reachable from both API and
worker containers and accept OTLP over HTTP/protobuf. Keep it on a trusted
network; do not publish a collector port to the public internet. If authentication
is required, use the standard `OTEL_EXPORTER_OTLP_TRACES_HEADERS` environment
setting through the deployment secret manager. Do not put credentials in the
repository `.env.example`.

When no endpoint is set, Harnessa still produces trace IDs for request logs and
correlation, but spans are not retained or exported. A healthy LiteLLM proxy,
successful `/health/ready`, or a valid trace ID does not prove that an external
collector stored a trace. Confirm delivery by finding an API request and its
worker/model/GitHub child spans in the configured backend.

## Verify without a provider request

Send a harmless readiness request with a known W3C trace ID and inspect its
response header:

```sh
curl -sS -D - -o /dev/null \
  -H 'traceparent: 00-0123456789abcdef0123456789abcdef-0123456789abcdef-01' \
  http://localhost:8010/health/live
```

The response should include `X-Trace-ID: 0123456789abcdef0123456789abcdef`.
This checks context extraction only. To verify export, find the same trace in
the collector/backend after the normal batch flush; no LiteLLM completion or
other provider call is needed.
