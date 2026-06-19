#!/usr/bin/env python3
"""Generate a clean, focused RoboShop Grafana dashboard."""

from __future__ import annotations

import json
from pathlib import Path

DS = {"type": "prometheus", "uid": "prometheus"}
NS = "roboshop"
JOBS = "roboshop-user|roboshop-cart|roboshop-catalogue|roboshop-payment|roboshop-ratings|roboshop-orders|roboshop-shipping"
TRAEFIK_JOB = "traefik-metrics"
TRAEFIK_FRONTEND = 'service=~"roboshop.*frontend.*"'
NGINX_JOB = f'job=~"roboshop-frontend", namespace="{NS}"'

# Layout constants — balanced panel sizes (24-col grid)
STAT_W, STAT_H = 4, 4
CHART_H = 7
HALF = 12

TRAFFIC = f"""sum by (job) (
  rate(http_requests_total{{job=~"roboshop-user|roboshop-cart|roboshop-catalogue|roboshop-payment", route!="/metrics", handler!="/metrics", namespace="{NS}"}}[5m])
  or rate(http_server_requests_seconds_count{{job=~"roboshop-orders|roboshop-shipping", uri!~"/actuator.*", namespace="{NS}"}}[5m])
  or rate(flask_http_request_total{{job="roboshop-ratings", namespace="{NS}"}}[5m])
)"""

ERRORS = f"""sum by (job) (
  rate(http_requests_total{{job=~"roboshop-user|roboshop-cart|roboshop-catalogue", route!="/metrics", status_code!~"2..", namespace="{NS}"}}[5m])
  or rate(http_requests_total{{job="roboshop-payment", handler!="/metrics", status!~"2..", namespace="{NS}"}}[5m])
  or rate(http_server_requests_seconds_count{{job=~"roboshop-orders|roboshop-shipping", uri!~"/actuator.*", status=~"5..", namespace="{NS}"}}[5m])
  or rate(flask_http_request_total{{job="roboshop-ratings", status=~"5..", namespace="{NS}"}}[5m])
)"""

LATENCY_P95 = [
    ("{{job}}", f'histogram_quantile(0.95, sum by (le, job) (rate(http_request_duration_seconds_bucket{{job=~"roboshop-user|roboshop-cart|roboshop-catalogue|roboshop-payment", route!="/metrics", namespace="{NS}"}}[5m])))'),
    ("{{job}}", f'histogram_quantile(0.95, sum by (le, job) (rate(http_server_requests_seconds_bucket{{job=~"roboshop-orders|roboshop-shipping", uri!~"/actuator.*", namespace="{NS}"}}[5m])))'),
    ("{{job}}", f'histogram_quantile(0.95, sum by (le, job) (rate(flask_http_request_duration_seconds_bucket{{job="roboshop-ratings", namespace="{NS}"}}[5m])))'),
]

OUT = Path(__file__).resolve().parent / "dashboards"

TS_DEFAULTS = {
    "color": {"mode": "palette-classic"},
    "custom": {
        "drawStyle": "line",
        "lineWidth": 1,
        "fillOpacity": 8,
        "gradientMode": "opacity",
        "showPoints": "never",
        "spanNulls": False,
        "stacking": {"mode": "none"},
    },
}

TS_OPTIONS = {
    "legend": {"displayMode": "list", "placement": "bottom", "calcs": ["lastNotNull", "max"]},
    "tooltip": {"mode": "multi", "sort": "desc"},
}

# Grafana decimals by unit — req/s shown as whole numbers
UNIT_DECIMALS = {
    "reqps": 0,
    "s": 2,
    "short": 0,
    "none": 0,
}


def field_defaults(unit: str, *, thresholds=None) -> dict:
    defaults = {**TS_DEFAULTS, "unit": unit}
    if unit in UNIT_DECIMALS:
        defaults["decimals"] = UNIT_DECIMALS[unit]
    if thresholds is not None:
        defaults["color"] = {"mode": "thresholds"}
        defaults["thresholds"] = {"mode": "absolute", "steps": thresholds}
    return defaults


class Builder:
    def __init__(self, uid: str, title: str, tags: list[str]):
        self.uid = uid
        self.title = title
        self.tags = tags
        self.panels: list[dict] = []
        self._id = 1
        self._y = 0

    def _next_y(self, h: int) -> int:
        y, self._y = self._y, self._y + h
        return y

    def _nested_y(self, nested: list[dict], h: int) -> int:
        return 0 if not nested else max(p["gridPos"]["y"] + p["gridPos"]["h"] for p in nested)

    def stat(self, title: str, expr: str, *, x: int, y: int, w: int = STAT_W, h: int = STAT_H, unit: str = "short", steps=None, text_mode: str = "value") -> dict:
        steps = steps or [{"color": "green", "value": None}]
        panel = {
            "id": self._id,
            "type": "stat",
            "title": title,
            "gridPos": {"h": h, "w": w, "x": x, "y": y},
            "datasource": DS,
            "targets": [{"datasource": DS, "expr": expr, "refId": "A"}],
            "fieldConfig": {
                "defaults": field_defaults(unit, thresholds=steps),
                "overrides": [],
            },
            "options": {
                "reduceOptions": {"calcs": ["lastNotNull"]},
                "orientation": "auto",
                "textMode": text_mode,
                "colorMode": "value",
                "graphMode": "none",
            },
        }
        self._id += 1
        return panel

    def chart(self, title: str, exprs: list[tuple[str, str]] | str, *, y: int, w: int = HALF, h: int = CHART_H, x: int = 0, unit: str = "short") -> dict:
        if isinstance(exprs, str):
            exprs = [("{{job}}", exprs)]
        targets = [
            {
                "datasource": DS,
                "expr": expr,
                "refId": chr(65 + i),
                "legendFormat": leg,
                "range": True,
                "editorMode": "code",
            }
            for i, (leg, expr) in enumerate(exprs)
        ]
        panel = {
            "id": self._id,
            "type": "timeseries",
            "title": title,
            "gridPos": {"h": h, "w": w, "x": x, "y": y},
            "datasource": DS,
            "targets": targets,
            "fieldConfig": {"defaults": field_defaults(unit), "overrides": []},
            "options": TS_OPTIONS,
        }
        self._id += 1
        return panel

    def text(self, content: str, h: int = 2) -> None:
        self.panels.append(
            {
                "id": self._id,
                "type": "text",
                "gridPos": {"h": h, "w": 24, "x": 0, "y": self._next_y(h)},
                "options": {"mode": "markdown", "content": content},
            }
        )
        self._id += 1

    def section(self, title: str, *, collapsed: bool = False) -> list[dict]:
        self.panels.append(
            {
                "id": self._id,
                "type": "row",
                "title": title,
                "gridPos": {"h": 1, "w": 24, "x": 0, "y": self._next_y(1)},
                "collapsed": collapsed,
                "panels": [],
            }
        )
        self._id += 1
        return self.panels[-1]["panels"]

    def add_stat(self, nested: list[dict] | None, title: str, expr: str, *, x: int, y: int | None = None, w: int = STAT_W, h: int = STAT_H, **kwargs) -> None:
        if nested is None:
            y = self._y if y is None else y
            self._y = max(self._y, y + h)
        else:
            y = self._nested_y(nested, h) if y is None else y
        target = nested if nested is not None else self.panels
        target.append(self.stat(title, expr, x=x, y=y, w=w, h=h, **kwargs))

    def add_chart(self, nested: list[dict] | None, title: str, exprs, *, x: int = 0, y: int | None = None, w: int = HALF, h: int = CHART_H, **kwargs) -> None:
        if nested is None:
            y = self._next_y(h) if y is None else y
            self._y = max(self._y, y + h)
        else:
            y = self._nested_y(nested, h) if y is None else y
        target = nested if nested is not None else self.panels
        target.append(self.chart(title, exprs, y=y, x=x, w=w, h=h, **kwargs))

    def build(self) -> dict:
        return {
            "uid": self.uid,
            "title": self.title,
            "tags": self.tags,
            "timezone": "browser",
            "schemaVersion": 39,
            "version": 1,
            "refresh": "30s",
            "time": {"from": "now-1h", "to": "now"},
            "editable": True,
            "graphTooltip": 1,
            "links": [],
            "annotations": {"list": []},
            "templating": {"list": []},
            "panels": self.panels,
        }


def observability_dashboard() -> dict:
    b = Builder("roboshop-observability", "RoboShop - Observability", ["roboshop", "observability", "sre"])

    b.text("Traefik → Nginx → APIs · Four golden signals at a glance · expand rows for detail")

    # ── Page 1: KPI strip (always visible) ──────────────────────────
    b.section("Overview")
    row_y = b._y
    kpis = [
        ("Services UP", f'count(up{{job=~"{JOBS}", namespace="{NS}"}} == 1)', "short", [{"color": "red", "value": None}, {"color": "yellow", "value": 5}, {"color": "green", "value": 7}]),
        ("Ingress RPS", f'sum(rate(traefik_service_requests_total{{job="{TRAEFIK_JOB}", {TRAEFIK_FRONTEND}}}[5m]))', "reqps", None),
        ("API RPS", f"sum({TRAFFIC.strip()})", "reqps", None),
        ("Ingress p95", f'histogram_quantile(0.95, sum(rate(traefik_service_request_duration_seconds_bucket{{job="{TRAEFIK_JOB}", {TRAEFIK_FRONTEND}}}[5m])) by (le))', "s", None),
        ("API Errors/s", f"sum({ERRORS.strip()})", "reqps", [{"color": "green", "value": None}, {"color": "red", "value": 1}]),
        ("Nginx Conn.", f'nginx_connections_active{{{NGINX_JOB}}}', "short", None),
    ]
    for i, (title, expr, unit, steps) in enumerate(kpis):
        kwargs = {"unit": unit}
        if steps:
            kwargs["steps"] = steps
        b.add_stat(None, title, expr, x=i * STAT_W, y=row_y, w=STAT_W, **kwargs)

    # ── Page 2: Four golden signals — 2×2 grid ───────────────────────
    b.section("Golden Signals — Microservices")
    b.add_chart(None, "Traffic · req/s by service", TRAFFIC, x=0, w=HALF, unit="reqps")
    b.add_chart(None, "Latency · p95 by service", LATENCY_P95, x=HALF, w=HALF, unit="s")
    b.add_chart(None, "Errors · req/s by service", ERRORS, x=0, w=HALF, unit="reqps")
    b.add_chart(None, "Saturation · CPU cores", f'rate(process_cpu_seconds_total{{job=~"{JOBS}", namespace="{NS}"}}[5m])', x=HALF, w=HALF)

    # ── Collapsed: Ingress layer ─────────────────────────────────────
    ingress = b.section("Ingress — Traefik & Nginx", collapsed=True)
    b.add_stat(ingress, "Traefik RPS", f'sum(rate(traefik_service_requests_total{{job="{TRAEFIK_JOB}", {TRAEFIK_FRONTEND}}}[5m]))', x=0, y=0, w=6, unit="reqps")
    b.add_stat(ingress, "Traefik p95", f'histogram_quantile(0.95, sum(rate(traefik_service_request_duration_seconds_bucket{{job="{TRAEFIK_JOB}", {TRAEFIK_FRONTEND}}}[5m])) by (le))', x=6, y=0, w=6, unit="s")
    b.add_stat(ingress, "Traefik 5xx/s", f'sum(rate(traefik_service_requests_total{{job="{TRAEFIK_JOB}", {TRAEFIK_FRONTEND}, code=~"5.."}}[5m]))', x=12, y=0, w=6, unit="reqps", steps=[{"color": "green", "value": None}, {"color": "red", "value": 0.1}])
    b.add_stat(ingress, "Nginx RPS", f'sum(rate(nginx_http_requests_total{{{NGINX_JOB}}}[5m]))', x=18, y=0, w=6, unit="reqps")
    b.add_chart(ingress, "Traefik · requests by status", f'sum by (code) (rate(traefik_service_requests_total{{job="{TRAEFIK_JOB}", {TRAEFIK_FRONTEND}}}[5m]))', x=0, y=4, w=HALF, h=6, unit="reqps")
    b.add_chart(
        ingress,
        "Traefik · latency percentiles",
        [
            ("p50", f'histogram_quantile(0.50, sum(rate(traefik_service_request_duration_seconds_bucket{{job="{TRAEFIK_JOB}", {TRAEFIK_FRONTEND}}}[5m])) by (le))'),
            ("p95", f'histogram_quantile(0.95, sum(rate(traefik_service_request_duration_seconds_bucket{{job="{TRAEFIK_JOB}", {TRAEFIK_FRONTEND}}}[5m])) by (le))'),
        ],
        x=HALF,
        y=4,
        w=HALF,
        h=6,
        unit="s",
    )
    b.add_chart(ingress, "Nginx · connections", f'nginx_connections_active{{{NGINX_JOB}}}', x=0, y=10, w=HALF, h=6)
    b.add_chart(
        ingress,
        "Nginx · connection states",
        [
            ("reading", f'nginx_connections_reading{{{NGINX_JOB}}}'),
            ("writing", f'nginx_connections_writing{{{NGINX_JOB}}}'),
            ("waiting", f'nginx_connections_waiting{{{NGINX_JOB}}}'),
        ],
        x=HALF,
        y=10,
        w=HALF,
        h=6,
    )

    # ── Collapsed: Per-service health ────────────────────────────────
    health = b.section("Service Health", collapsed=True)
    services = ["user", "cart", "catalogue", "payment", "ratings", "orders", "shipping", "frontend"]
    up_steps = [{"color": "red", "value": None}, {"color": "green", "value": 1}]
    for i, svc in enumerate(services):
        b.add_stat(
            health,
            svc,
            f'up{{job=~"roboshop-{svc}", namespace="{NS}"}}',
            x=(i % 4) * 6,
            y=(i // 4) * 3,
            w=6,
            h=3,
            unit="none",
            steps=up_steps,
            text_mode="value_and_name",
        )

    # ── Collapsed: Saturation detail ───────────────────────────────
    sat = b.section("Saturation — Detail", collapsed=True)
    b.add_chart(sat, "Memory RSS by service", f'process_resident_memory_bytes{{job=~"{JOBS}", namespace="{NS}"}}', x=0, y=0, w=HALF, h=6, unit="bytes")
    b.add_chart(sat, "JVM heap · orders & shipping", f'jvm_memory_used_bytes{{job=~"roboshop-orders|roboshop-shipping", area="heap", namespace="{NS}"}}', x=HALF, y=0, w=HALF, h=6, unit="bytes")
    b.add_chart(sat, "Node.js heap · user & cart", f'nodejs_heap_size_used_bytes{{job=~"roboshop-user|roboshop-cart", namespace="{NS}"}}', x=0, y=6, w=HALF, h=6, unit="bytes")
    b.add_chart(sat, "DB pool · shipping", f'hikaricp_connections_active{{job="roboshop-shipping", namespace="{NS}"}}', x=HALF, y=6, w=HALF, h=6)

    return b.build()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "roboshop-observability.json"
    body = observability_dashboard()
    path.write_text(json.dumps(body, indent=2), encoding="utf-8")
    print(f"Wrote {path} ({len(body['panels'])} sections, clean layout)")


if __name__ == "__main__":
    main()
