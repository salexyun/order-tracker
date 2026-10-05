import os

# Keep test runs quiet: no console or network telemetry export.
os.environ.setdefault("OTEL_EXPORTERS", "none")
