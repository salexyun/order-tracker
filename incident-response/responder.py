"""Incident responder: receives Grafana alerts and starts a headless coding agent.

POST /alerts  (Grafana webhook payload)
  1. Save the alert payload to incidents/<incident-id>/alert.json
  2. Collect an evidence packet with read-only queries (Prometheus, Loki, Tempo, git, compose)
  3. Start Claude Code in headless mode with responder-task.md and the incident directory
  4. Save the agent's answer to incidents/<incident-id>/agent-response.md

Run from the repository root:  python3 incident-response/responder.py
Standard library only, so it runs without installing anything.
"""

import json
import os
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
INCIDENTS = HERE / "incidents"
TASK_FILE = HERE / "responder-task.md"

# Loopback only: this endpoint starts an agent that can edit code.
HOST = os.getenv("RESPONDER_HOST", "127.0.0.1")
PORT = int(os.getenv("RESPONDER_PORT", "8001"))
PROMETHEUS = os.getenv("PROMETHEUS_URL", "http://127.0.0.1:9090")
LOKI = os.getenv("LOKI_URL", "http://127.0.0.1:3100")
TEMPO = os.getenv("TEMPO_URL", "http://127.0.0.1:3200")
SERVICE = os.getenv("SERVICE_NAME", "order-tracker")
EVIDENCE_WINDOW_S = 15 * 60
AGENT_TIMEOUT_S = int(os.getenv("RESPONDER_AGENT_TIMEOUT", "1800"))
AGENT_MODEL = os.getenv("RESPONDER_MODEL", "")

# What the agent may do without asking. Anything else is denied in headless mode.
# It can read and edit this repo, run tests, rebuild the app container, and query
# the local app and telemetry. It cannot push, delete volumes, or touch other services.
ALLOWED_TOOLS = [
    "Read", "Grep", "Glob", "Edit", "Write",
    "Bash(curl:*)",
    "Bash(uv run:*)",
    "Bash(docker compose up --build -d --wait app)",
    "Bash(docker compose ps:*)",
    "Bash(docker compose logs:*)",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)",
    "Bash(git add:*)", "Bash(git commit:*)",
]
DISALLOWED_TOOLS = [
    "Bash(git push:*)", "Bash(docker compose down:*)", "Bash(rm:*)",
    "WebFetch", "WebSearch",
]

_active = set()          # alert keys with an agent currently running
_lock = threading.Lock()


def log(msg):
    print(f"[responder {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------- read-only evidence collection ----------

def http_get_json(base, path, params=None, timeout=10):
    url = f"{base}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.load(resp)
    except Exception as exc:  # evidence is best-effort; record what failed
        return {"error": f"{type(exc).__name__}: {exc}", "url": url}


def run(cmd):
    try:
        out = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=30)
        return (out.stdout + out.stderr).strip()
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


def prom_query(expr):
    data = http_get_json(PROMETHEUS, "/api/v1/query", {"query": expr})
    if "error" in data and "data" not in data:
        return data
    return [
        {"labels": r["metric"], "value": r["value"][1]}
        for r in data.get("data", {}).get("result", [])
    ]


def collect_metrics():
    sel = f'service_name="{SERVICE}"'
    return {
        "requests_by_route_status_15m": prom_query(
            f"sum by (http_route, http_request_method, http_response_status_code) "
            f"(increase(http_server_requests_total{{{sel}}}[15m]))"),
        "server_errors_total_by_route": prom_query(
            f'sum by (http_route, http_request_method) '
            f'(http_server_requests_total{{{sel}, http_response_status_code=~"5.."}})'),
    }


def collect_logs(end_ns):
    query = f'{{service_name="{SERVICE}"}} | severity_text=~"ERROR|CRITICAL|FATAL"'
    data = http_get_json(LOKI, "/loki/api/v1/query_range", {
        "query": query, "limit": 20, "direction": "backward",
        "start": end_ns - EVIDENCE_WINDOW_S * 10**9, "end": end_ns,
    })
    if "error" in data and "data" not in data:
        return {"query": query, **data}
    entries = []
    for stream in data.get("data", {}).get("result", []):
        meta = stream["stream"]
        for ts, line in stream["values"]:
            entries.append({
                "time": datetime.fromtimestamp(int(ts) / 1e9, timezone.utc).isoformat(),
                "message": line,
                "severity": meta.get("severity_text"),
                "trace_id": meta.get("trace_id"),
                "attributes": {k: v for k, v in meta.items()
                               if k.startswith(("order_", "exception_", "code_"))},
            })
    entries.sort(key=lambda e: e["time"], reverse=True)
    return {"query": query, "entries": entries}


def summarize_trace(trace):
    spans = []
    for batch in trace.get("batches", trace.get("resourceSpans", [])):
        for scope in batch.get("scopeSpans", batch.get("instrumentationLibrarySpans", [])):
            for span in scope.get("spans", []):
                attrs = {a["key"]: next(iter(a["value"].values()), None)
                         for a in span.get("attributes", [])}
                events = [{
                    "name": e.get("name"),
                    "attributes": {a["key"]: next(iter(a["value"].values()), None)
                                   for a in e.get("attributes", [])},
                } for e in span.get("events", [])]
                spans.append({
                    "name": span.get("name"),
                    "status": span.get("status", {}),
                    "attributes": attrs,
                    "events": events,
                })
    return spans


def collect_traces(end_s):
    query = f'{{resource.service.name="{SERVICE}" && status=error}}'
    found = http_get_json(TEMPO, "/api/search", {
        "q": query, "limit": 5, "start": end_s - EVIDENCE_WINDOW_S, "end": end_s,
    })
    if "error" in found and "traces" not in found:
        return {"query": query, **found}
    traces = []
    for t in found.get("traces", [])[:3]:
        full = http_get_json(TEMPO, f"/api/traces/{t['traceID']}")
        traces.append({
            "trace_id": t["traceID"],
            "root": t.get("rootTraceName"),
            "spans": summarize_trace(full) if "error" not in full else full,
        })
    return {"query": query, "traces": traces}


def collect_evidence(incident_dir, alert):
    now = time.time()
    evidence = {
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "window": f"last {EVIDENCE_WINDOW_S // 60}m",
        "endpoint": alert_endpoint(alert),
        "metrics": collect_metrics(),
        "logs": collect_logs(int(now * 1e9)),
        "traces": collect_traces(int(now)),
        "deployment": {
            "git_head": run(["git", "log", "-1", "--format=%H %s"]),
            "recent_commits": run(["git", "log", "-5", "--format=%h %ad %s", "--date=iso"]),
            "working_tree": run(["git", "status", "--short"]),
            "containers": run(["docker", "compose", "ps", "--format",
                               "{{.Service}} {{.Image}} {{.Status}}"]),
        },
    }
    (incident_dir / "evidence.json").write_text(json.dumps(evidence, indent=2))
    return evidence


# ---------- alert handling ----------

def alert_endpoint(alert):
    labels, ann = alert.get("labels", {}), alert.get("annotations", {})
    if ann.get("endpoint"):
        return ann["endpoint"]
    route = labels.get("http_route")
    return f"{labels.get('http_request_method', '')} {route}".strip() if route else None


def alert_key(alert):
    labels = alert.get("labels", {})
    return alert.get("fingerprint") or json.dumps(labels, sort_keys=True)


def new_incident_dir(alert):
    name = alert.get("labels", {}).get("alertname", "alert")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()[:40]
    incident_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{slug}"
    path = INCIDENTS / incident_id
    path.mkdir(parents=True, exist_ok=False)
    return incident_id, path


def agent_env():
    # Drop variables from any parent Claude Code session so the agent starts clean.
    return {k: v for k, v in os.environ.items()
            if not (k.startswith("CLAUDE_CODE_") or k in {"CLAUDECODE", "CLAUDE_PID"})}


def run_agent(incident_id, incident_dir, key):
    try:
        rel = incident_dir.relative_to(REPO)
        prompt = (
            f"{TASK_FILE.read_text()}\n\n"
            f"Incident ID: {incident_id}\n"
            f"Incident directory: {rel}/ (alert.json, evidence.json)\n"
        )
        cmd = [
            "claude", "-p", prompt,
            "--output-format", "json",
            "--permission-mode", "acceptEdits",
            "--strict-mcp-config",  # no MCP servers: the agent only gets the tools listed below
            "--allowedTools", *ALLOWED_TOOLS,
            "--disallowedTools", *DISALLOWED_TOOLS,
        ]
        if AGENT_MODEL:
            cmd += ["--model", AGENT_MODEL]
        (incident_dir / "agent-command.json").write_text(json.dumps(
            {"cmd": cmd[:2] + ["<responder-task.md + incident context>"] + cmd[3:]}, indent=2))

        log(f"{incident_id}: starting agent")
        started = time.time()
        proc = subprocess.run(cmd, cwd=REPO, env=agent_env(), capture_output=True,
                              text=True, timeout=AGENT_TIMEOUT_S, stdin=subprocess.DEVNULL)
        (incident_dir / "agent-output.json").write_text(proc.stdout or "")
        if proc.stderr:
            (incident_dir / "agent-stderr.log").write_text(proc.stderr)
        try:
            result = json.loads(proc.stdout).get("result", "")
        except json.JSONDecodeError:
            result = proc.stdout
        (incident_dir / "agent-response.md").write_text(result + "\n")
        last = result.strip().splitlines()[-1] if result.strip() else "(empty)"
        log(f"{incident_id}: agent exited {proc.returncode} in {time.time() - started:.0f}s; "
            f"last line: {last}")
    except subprocess.TimeoutExpired:
        (incident_dir / "agent-response.md").write_text("Agent timed out; escalate to a human.\n")
        log(f"{incident_id}: agent timed out")
    except Exception as exc:
        (incident_dir / "agent-response.md").write_text(f"Responder error: {exc}\n")
        log(f"{incident_id}: responder error {exc}")
    finally:
        with _lock:
            _active.discard(key)


def handle_alert(alert, payload):
    key = alert_key(alert)
    with _lock:
        if key in _active:
            log(f"skip duplicate alert {key}: agent already running")
            return None
        _active.add(key)
    try:
        incident_id, incident_dir = new_incident_dir(alert)
        (incident_dir / "alert.json").write_text(json.dumps(
            {"alert": alert, "group": {k: v for k, v in payload.items() if k != "alerts"}},
            indent=2))
        collect_evidence(incident_dir, alert)
    except Exception:
        with _lock:
            _active.discard(key)
        raise
    log(f"{incident_id}: evidence saved to {incident_dir.relative_to(REPO)}")
    threading.Thread(target=run_agent, args=(incident_id, incident_dir, key), daemon=True).start()
    return incident_id


class Handler(BaseHTTPRequestHandler):
    def _reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/healthz":
            return self._reply(200, {"status": "ok", "active_agents": len(_active)})
        self._reply(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/alerts":
            return self._reply(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            alerts = payload.get("alerts", [])
            if not isinstance(alerts, list):
                raise ValueError("alerts must be a list")
        except (ValueError, json.JSONDecodeError) as exc:
            return self._reply(400, {"error": str(exc)})

        started, ignored = [], []
        for alert in alerts:
            name = alert.get("labels", {}).get("alertname", "?")
            if alert.get("status") != "firing":
                ignored.append({"alertname": name, "status": alert.get("status")})
                log(f"recorded {alert.get('status')} alert {name}; no agent started")
                continue
            incident_id = handle_alert(alert, payload)
            (started if incident_id else ignored).append(
                incident_id or {"alertname": name, "reason": "agent already running"})
        self._reply(202, {"incidents": started, "ignored": ignored})

    def log_message(self, fmt, *args):
        log(f"{self.address_string()} {fmt % args}")


def main():
    INCIDENTS.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    log(f"listening on {HOST}:{PORT}, repo {REPO}")
    server.serve_forever()


if __name__ == "__main__":
    main()
