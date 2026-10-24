from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from harnessa.tracing import (
    _create_tracer_provider,
    extract_trace_context,
    trace_context_from_parent,
)
from opentelemetry import trace


class _OtlpReceiver(BaseHTTPRequestHandler):
    requests_received = 0

    def do_POST(self) -> None:
        type(self).requests_received += 1
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


def test_w3c_trace_context_extracts_remote_parent_identifier() -> None:
    trace_id = "fedcba9876543210fedcba9876543210"
    parent_id = "0123456789abcdef"
    parent = f"00-{trace_id}-{parent_id}-01"

    context = extract_trace_context({"TraceParent": parent})
    span_context = trace.get_current_span(context).get_span_context()

    assert span_context.is_valid
    assert f"{span_context.trace_id:032x}" == trace_id
    assert f"{span_context.span_id:016x}" == parent_id
    assert span_context.is_remote


def test_invalid_durable_trace_context_starts_a_fresh_trace() -> None:
    context = trace_context_from_parent("not-a-traceparent")

    assert not trace.get_current_span(context).get_span_context().is_valid


def test_otlp_exporter_posts_spans_to_trace_specific_endpoint() -> None:
    _OtlpReceiver.requests_received = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OtlpReceiver)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}/v1/traces"
    provider = _create_tracer_provider("harnessa-test", traces_endpoint=endpoint)
    try:
        tracer = trace.get_tracer("harnessa.trace-test", tracer_provider=provider)
        with tracer.start_as_current_span("trace.export.test") as span:
            span.set_attribute("harnessa.test", True)
        assert provider.force_flush(timeout_millis=5_000)
        assert _OtlpReceiver.requests_received == 1
    finally:
        provider.shutdown()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
