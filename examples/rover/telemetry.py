"""Optional: export the rover's spans to an OpenTelemetry backend.

zenode records spans (``publish``, ``process``, ``serve``) but never builds a
``TracerProvider``, picks an exporter or reads ``OTEL_*``. The application
decides where spans go, and this function is the whole of that setup. Unless
the ``otel`` extra is installed and a provider is registered, the span code
does nothing.

Set ``OTLP_ENDPOINT`` to switch it on::

    pip install 'zenode[otel]' opentelemetry-sdk opentelemetry-exporter-otlp-proto-http
    docker run --rm -p 3000:3000 -p 4318:4318 grafana/otel-lgtm
    OTLP_ENDPOINT=http://localhost:4318 uv run python -m examples.rover drive

Then open http://localhost:3000, pick the Tempo datasource, and search by
service name. Run ``rangers`` and ``safety`` the same way to see one trace
cross all three.
"""

from __future__ import annotations

import os


def setup_telemetry(service_name: str) -> bool:
    """Register a provider if ``$OTLP_ENDPOINT`` is set; say whether it did."""
    endpoint = os.environ.get("OTLP_ENDPOINT")
    if not endpoint:
        return False

    # Imported here so the rover runs without any of these installed.
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    # service.name is what a backend groups spans by. One process per node
    # means one service per node.
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces"))
    )
    trace.set_tracer_provider(provider)
    return True
