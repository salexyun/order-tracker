"""OpenTelemetry setup: metrics, traces, and logs.

OTEL_EXPORTERS selects where signals go, as a comma-separated list:
  console - print to stdout (visible in `docker compose logs app`)
  otlp    - send over OTLP/HTTP to OTEL_EXPORTER_OTLP_ENDPOINT
  none    - disable export (used by tests)
"""

import logging
import os
import time

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, ConsoleLogRecordExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.trace import Status, StatusCode
from starlette.routing import Match

SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "order-tracker")

tracer = trace.get_tracer("order-tracker")
meter = metrics.get_meter("order-tracker")
logger = logging.getLogger("order_tracker")

request_counter = meter.create_counter(
    "http.server.requests",
    unit="{request}",
    description="HTTP requests by route and status code",
)
request_duration = meter.create_histogram(
    "http.server.request.duration",
    unit="s",
    description="HTTP request duration by route and status code",
)

_configured = False


def configure():
    global _configured
    if _configured:
        return
    _configured = True

    exporters = {e.strip() for e in os.getenv("OTEL_EXPORTERS", "console").split(",")}
    if "none" in exporters:
        return

    resource = Resource.create({
        "service.name": SERVICE_NAME,
        "service.version": os.getenv("APP_VERSION", "dev"),
        "deployment.environment": os.getenv("APP_ENV", "local"),
    })
    interval_ms = int(os.getenv("OTEL_METRIC_EXPORT_INTERVAL", "10000"))

    span_exporters, metric_exporters, log_exporters = [], [], []
    if "console" in exporters:
        span_exporters.append(ConsoleSpanExporter())
        metric_exporters.append(ConsoleMetricExporter())
        log_exporters.append(ConsoleLogRecordExporter())
    if "otlp" in exporters:
        # Imported lazily so console-only runs do not need the endpoint configured.
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        span_exporters.append(OTLPSpanExporter())
        metric_exporters.append(OTLPMetricExporter())
        log_exporters.append(OTLPLogExporter())

    tracer_provider = TracerProvider(resource=resource)
    for exporter in span_exporters:
        tracer_provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(tracer_provider)

    metrics.set_meter_provider(MeterProvider(
        resource=resource,
        metric_readers=[
            PeriodicExportingMetricReader(exporter, export_interval_millis=interval_ms)
            for exporter in metric_exporters
        ],
    ))

    logger_provider = LoggerProvider(resource=resource)
    for exporter in log_exporters:
        logger_provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
    set_logger_provider(logger_provider)
    logger.addHandler(LoggingHandler(level=logging.INFO, logger_provider=logger_provider))
    logger.setLevel(logging.INFO)


def route_template(app, scope):
    """Return the matched route template (e.g. /api/orders/{order_id}), never the raw path."""
    route = scope.get("route")
    if route is not None:
        return route.path
    for candidate in app.router.routes:
        match, _ = candidate.matches(scope)
        if match == Match.FULL:
            return candidate.path
    return "unmatched"


def instrument(app):
    """Record a server span, request count, and duration for every HTTP request."""

    @app.middleware("http")
    async def telemetry_middleware(request, call_next):
        start = time.perf_counter()
        route = route_template(app, request.scope)
        status_code = 500
        with tracer.start_as_current_span(
            f"{request.method} {route}", kind=trace.SpanKind.SERVER,
        ) as span:
            span.set_attribute("http.request.method", request.method)
            span.set_attribute("http.route", route)
            try:
                response = await call_next(request)
                status_code = response.status_code
                return response
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                raise
            finally:
                span.set_attribute("http.response.status_code", status_code)
                if status_code >= 500:
                    span.set_status(Status(StatusCode.ERROR))
                attributes = {
                    "http.request.method": request.method,
                    "http.route": route,
                    "http.response.status_code": status_code,
                }
                request_counter.add(1, attributes)
                request_duration.record(time.perf_counter() - start, attributes)
