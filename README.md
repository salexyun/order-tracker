# Order Tracker

A small order tracking app for the AI Dev Tools Zoomcamp observability homework. It includes a web page, API, tests, and a Docker Compose setup. You add telemetry, alerts, and an incident responder in Homework 4.

The main user flow is creating an order and checking its status. Three sample orders are created on first startup.

## Run it

You need Docker with Compose. To run the tests, you also need Python 3.11+ and `uv`.

```bash
docker compose up --build -d --wait
```

Open <http://127.0.0.1:8000>. The API is at `/api/orders`, and the health check is at `/healthz`. Data is stored in a Docker volume and survives container recreation.

If port 8000 is occupied, set `ORDER_TRACKER_PORT`, for example:

```bash
ORDER_TRACKER_PORT=18080 docker compose up --build -d --wait
```

Run tests with `uv run --frozen pytest -q`. Stop the app with `docker compose down`. Add `-v` only if you also want to delete the order data.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | Web page |
| GET | `/healthz` | Database health check |
| GET | `/api/orders` | List orders |
| POST | `/api/orders` | Create an order |
| GET | `/api/orders/{id}` | Check an order |
| PATCH | `/api/orders/{id}` | Change an order status |

The app uses SQLite to keep setup small. Run one app container at a time. The course exercise is about detecting and handling an incident, not scaling the database.

## Observability and incident response

`docker compose up --build -d --wait` also starts the telemetry stack from `observability/compose.yaml`:

| Service | URL | Purpose |
| --- | --- | --- |
| Grafana | <http://localhost:3000> | Dashboard "Order Tracker: requests and errors", alert rules |
| Prometheus | <http://localhost:9090> | Metrics (`http_server_requests_total` by `http_route`, `http_response_status_code`) |
| Loki | <http://localhost:3100> | Logs, linked to traces by `trace_id` |
| Tempo | <http://localhost:3200> | Traces |

The app sends OpenTelemetry metrics, logs, and traces over OTLP to the Collector (`observability/collector.yaml`). Set `OTEL_EXPORTERS=console` to print them to `docker compose logs app` instead.

The Grafana alert "Order Tracker 5xx responses" fires when any route returned a 5xx in the last 5 minutes and sends a webhook to the incident responder:

```bash
python3 incident-response/responder.py   # listens on 127.0.0.1:8001, POST /alerts
```

For each firing alert, the responder saves the alert and a read-only evidence packet to `incident-response/incidents/<id>/`. It then runs Claude Code headless with `incident-response/responder-task.md` and a fixed tool allowlist. The agent can edit and test this repo, rebuild the app container, and commit locally. It cannot push. Its answer and `report.md` are saved next to the evidence.
