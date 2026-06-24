#!/usr/bin/env python3
"""Generate the RoboShop one-stop incident-response Grafana dashboard.

Designed for "something is happening, what's wrong, where, why" — top of
fold answers it in 5 seconds; collapsed sections drill into each layer.

Layout (top to bottom):
  1. Health banner — alerts, services up, errors, latency, restarts, OOMs,
     nodes, DB-VM disk
  2. Active alerts table — what's already firing
  3. Golden signals (per-service) — traffic, latency, errors, CPU charts
  4. Triage table — one row per service: RPS, err/s, p95, restarts, ready
  5. Drilldowns (collapsed):
     - Ingress (Traefik)
     - Frontend (Nginx)
     - APIs detail
     - Datastores (HikariCP + Azure DB VMs)
     - JVM (orders, shipping)
     - Node.js (user, cart)
     - Infrastructure (worker nodes, pods)
     - Kubernetes (restarts, OOMs, deployment readiness)
     - DNS (CoreDNS)

Per-service label catalog (verified against live Prometheus):

  job                metric                                path-label  status-label  code-style
  ---------------    -----------------------------------   ----------  ------------  ----------
  user, cart         http_requests_total                   route       status_code   full (200)
  catalogue          http_requests_total                   path        status        full (200)
  payment            http_requests_total                   handler     status        class (2xx/5xx)
  ratings            flask_http_request_total              -           status        full
  orders, shipping   http_server_requests_seconds_count    uri         status        full
  frontend (nginx)   nginx_http_requests_total             -           -             -

Spring Boot (orders, shipping) ships `_count`/`_sum`/`_max` only — NO
histogram bucket — so p95 is impossible; we use rate(_sum)/rate(_count)
mean instead.

Spring also exposes `process_cpu_usage` (0..1 gauge) instead of the
Prom-client `process_cpu_seconds_total` counter — the CPU saturation
chart combines both.

Nginx is the stub_status exporter — only RPS and connection states; no
status-code breakdown, no latency histogram.

Datastores (mongodb, mysql, valkey, rabbitmq) are external Azure VMs with
node-exporter only; no DB-native exporters are scraped. We surface them
via host CPU/memory/disk and the app-side HikariCP pool (shipping→MySQL).
"""

from __future__ import annotations

import json
from pathlib import Path

# ───────────────────────────────────────────────────────────────────────
# Constants
# ───────────────────────────────────────────────────────────────────────

DS = {"type": "prometheus", "uid": "prometheus"}
NS = "roboshop"
RI = "$__rate_interval"

JOBS = "roboshop-user|roboshop-cart|roboshop-catalogue|roboshop-payment|roboshop-ratings|roboshop-orders|roboshop-shipping"
ALL_JOBS = f"{JOBS}|roboshop-frontend"
PROMCLIENT_JOBS = "roboshop-user|roboshop-cart|roboshop-catalogue|roboshop-payment"
SPRING_JOBS = "roboshop-orders|roboshop-shipping"
TRAEFIK_JOB = "traefik-metrics"
TRAEFIK_FRONTEND = 'service=~"roboshop.*frontend.*"'
NGINX_JOB = f'job="roboshop-frontend", namespace="{NS}"'

# Pod-name regex extracts the service from "roboshop-<svc>-<rs>-<hash>".
# Anchors `[a-f0-9]+-[a-z0-9]+` exclude orphan single-replica "*-db-xxxxx"
# pods (which would otherwise pollute restart counts under e.g. "shipping").
POD_RE = "roboshop-(user|cart|catalogue|payment|ratings|orders|shipping|frontend)-[a-f0-9]+-[a-z0-9]+"

# Layout — 24-col grid, tuned to fit 1920×900-ish viewport.
# Always-visible content (banner + alerts + golden signals + triage) lands
# around 24 grid rows ≈ 720px, leaving headroom for browser chrome.
W_FULL = 24
W_HALF = 12
W_THIRD = 8
W_QUARTER = 6
W_STAT = 4

H_STAT = 3       # tightened from 4 so banner (2 rows) fits above the fold
H_CHART = 7
H_TABLE = 9

# ───────────────────────────────────────────────────────────────────────
# Per-service query catalog — each backend uses different labels
# ───────────────────────────────────────────────────────────────────────

TRAFFIC_PER_JOB = f"""sum by (job) (
  rate(http_requests_total{{job="roboshop-user", route!="/metrics", namespace="{NS}"}}[{RI}])
  or rate(http_requests_total{{job="roboshop-cart", route!="/metrics", namespace="{NS}"}}[{RI}])
  or rate(http_requests_total{{job="roboshop-catalogue", path!="/metrics", namespace="{NS}"}}[{RI}])
  or rate(http_requests_total{{job="roboshop-payment", handler!="/metrics", namespace="{NS}"}}[{RI}])
  or rate(http_server_requests_seconds_count{{job=~"{SPRING_JOBS}", uri!~"/actuator.*", namespace="{NS}"}}[{RI}])
  or rate(flask_http_request_total{{job="roboshop-ratings", namespace="{NS}"}}[{RI}])
)"""

ERRORS_PER_JOB = f"""sum by (job) (
  rate(http_requests_total{{job=~"roboshop-user|roboshop-cart", status_code=~"5..", namespace="{NS}"}}[{RI}])
  or rate(http_requests_total{{job="roboshop-catalogue", status=~"5..", namespace="{NS}"}}[{RI}])
  or rate(http_requests_total{{job="roboshop-payment", status="5xx", namespace="{NS}"}}[{RI}])
  or rate(http_server_requests_seconds_count{{job=~"{SPRING_JOBS}", status=~"5..", namespace="{NS}"}}[{RI}])
  or rate(flask_http_request_total{{job="roboshop-ratings", status=~"5..", namespace="{NS}"}}[{RI}])
)"""

LATENCY_TARGETS = [
    ("{{job}} p95", f'histogram_quantile(0.95, sum by (le, job) (rate(http_request_duration_seconds_bucket{{job=~"{PROMCLIENT_JOBS}", route!="/metrics", namespace="{NS}"}}[{RI}])))'),
    ("{{job}} p95", f'histogram_quantile(0.95, sum by (le, job) (rate(flask_http_request_duration_seconds_bucket{{job="roboshop-ratings", namespace="{NS}"}}[{RI}])))'),
    ("{{job}} mean", f'sum by (job) (rate(http_server_requests_seconds_sum{{job=~"{SPRING_JOBS}", uri!~"/actuator.*", namespace="{NS}"}}[{RI}])) / sum by (job) (rate(http_server_requests_seconds_count{{job=~"{SPRING_JOBS}", uri!~"/actuator.*", namespace="{NS}"}}[{RI}]))'),
]

CPU_PER_JOB = f"""sum by (job) (
  rate(process_cpu_seconds_total{{job=~"roboshop-user|roboshop-cart|roboshop-catalogue|roboshop-payment|roboshop-ratings|roboshop-frontend", namespace="{NS}"}}[{RI}])
  or process_cpu_usage{{job=~"{SPRING_JOBS}", namespace="{NS}"}}
)"""

OUT = Path(__file__).resolve().parent / "dashboards"

# ───────────────────────────────────────────────────────────────────────
# Panel defaults
# ───────────────────────────────────────────────────────────────────────

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

UNIT_DECIMALS = {
    "reqps": 2,
    "s": 3,
    "percentunit": 2,
    # Counts (alerts, restarts, errors-in-window, connections). `increase()`
    # extrapolates and returns floats — without this, "46.4 errors" appears
    # where the real value is ~46. Bytes/seconds/CPU are intentionally
    # absent so Grafana picks its own precision.
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


# ───────────────────────────────────────────────────────────────────────
# Builder
# ───────────────────────────────────────────────────────────────────────

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

    # ---- stat ----
    def stat(self, title: str, expr: str, *, x: int, y: int, w: int = W_STAT, h: int = H_STAT,
             unit: str = "short", steps=None, text_mode: str = "value",
             graph_mode: str = "none", color_mode: str = "value") -> dict:
        steps = steps or [{"color": "green", "value": None}]
        panel = {
            "id": self._id,
            "type": "stat",
            "title": title,
            "gridPos": {"h": h, "w": w, "x": x, "y": y},
            "datasource": DS,
            "targets": [{"datasource": DS, "expr": expr, "refId": "A", "instant": True}],
            "fieldConfig": {
                "defaults": field_defaults(unit, thresholds=steps),
                "overrides": [],
            },
            "options": {
                "reduceOptions": {"calcs": ["lastNotNull"], "fields": ""},
                "orientation": "auto",
                "textMode": text_mode,
                "colorMode": color_mode,
                "graphMode": graph_mode,
            },
        }
        self._id += 1
        return panel

    # ---- timeseries chart ----
    def chart(self, title: str, exprs, *, y: int, w: int = W_HALF, h: int = H_CHART,
              x: int = 0, unit: str = "short", legend_format: str | None = None,
              stacking: bool = False, draw_style: str = "line") -> dict:
        """draw_style: "line" (default) or "bars" (for count-per-bucket charts)."""
        if isinstance(exprs, str):
            exprs = [(legend_format or "{{job}}", exprs)]
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
        defaults = field_defaults(unit)
        custom = dict(defaults["custom"])
        if draw_style == "bars":
            custom["drawStyle"] = "bars"
            custom["fillOpacity"] = 70
            custom["lineWidth"] = 0
        if stacking:
            custom["stacking"] = {"mode": "normal"}
        defaults = {**defaults, "custom": custom}
        panel = {
            "id": self._id,
            "type": "timeseries",
            "title": title,
            "gridPos": {"h": h, "w": w, "x": x, "y": y},
            "datasource": DS,
            "targets": targets,
            "fieldConfig": {"defaults": defaults, "overrides": []},
            "options": TS_OPTIONS,
        }
        self._id += 1
        return panel

    # ---- table panel: multi-instant-query joined by a key label ----
    def table(self, title: str, columns: list[tuple[str, str]], *, y: int, x: int = 0,
              w: int = W_FULL, h: int = H_TABLE, join_label: str = "job",
              value_unit: str = "short") -> dict:
        """columns = [(column_title, prom_expr), ...]
        Each expr should return one series per join_label value.
        First column's join_label becomes the row identifier.
        """
        targets = []
        for i, (col_title, expr) in enumerate(columns):
            refid = chr(65 + i)
            targets.append({
                "datasource": DS,
                "expr": expr,
                "refId": refid,
                "instant": True,
                "range": False,
                "format": "table",
                "editorMode": "code",
            })

        transformations = [
            {"id": "merge", "options": {}},
            {
                "id": "organize",
                "options": {
                    "excludeByName": {"Time": True, "__name__": True},
                    "indexByName": {},
                    "renameByName": {
                        **{f"Value #{chr(65+i)}": col_title for i, (col_title, _) in enumerate(columns)},
                    },
                },
            },
        ]

        panel = {
            "id": self._id,
            "type": "table",
            "title": title,
            "gridPos": {"h": h, "w": w, "x": x, "y": y},
            "datasource": DS,
            "targets": targets,
            "transformations": transformations,
            "fieldConfig": {
                "defaults": {
                    "custom": {"align": "auto", "displayMode": "auto", "inspect": False},
                    "unit": value_unit,
                },
                "overrides": [],
            },
            "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}},
        }
        self._id += 1
        return panel

    # ---- text/markdown ----
    def text(self, content: str, h: int = 2) -> None:
        self.panels.append({
            "id": self._id,
            "type": "text",
            "gridPos": {"h": h, "w": W_FULL, "x": 0, "y": self._next_y(h)},
            "options": {"mode": "markdown", "content": content},
        })
        self._id += 1

    # ---- row ----
    def section(self, title: str, *, collapsed: bool = False) -> list[dict]:
        self.panels.append({
            "id": self._id,
            "type": "row",
            "title": title,
            "gridPos": {"h": 1, "w": W_FULL, "x": 0, "y": self._next_y(1)},
            "collapsed": collapsed,
            "panels": [],
        })
        self._id += 1
        return self.panels[-1]["panels"]

    # ---- helpers that auto-place ----
    def add_stat(self, nested: list[dict] | None, title: str, expr: str, *, x: int,
                 y: int | None = None, w: int = W_STAT, h: int = H_STAT, **kwargs) -> None:
        if nested is None:
            y = self._y if y is None else y
            self._y = max(self._y, y + h)
        else:
            y = self._nested_y(nested, h) if y is None else y
        target = nested if nested is not None else self.panels
        target.append(self.stat(title, expr, x=x, y=y, w=w, h=h, **kwargs))

    def add_chart(self, nested: list[dict] | None, title: str, exprs, *, x: int = 0,
                  y: int | None = None, w: int = W_HALF, h: int = H_CHART, **kwargs) -> None:
        if nested is None:
            y = self._next_y(h) if y is None else y
            self._y = max(self._y, y + h)
        else:
            y = self._nested_y(nested, h) if y is None else y
        target = nested if nested is not None else self.panels
        target.append(self.chart(title, exprs, y=y, x=x, w=w, h=h, **kwargs))

    def add_table(self, nested: list[dict] | None, title: str, columns, *, x: int = 0,
                  y: int | None = None, w: int = W_FULL, h: int = H_TABLE, **kwargs) -> None:
        if nested is None:
            y = self._next_y(h) if y is None else y
            self._y = max(self._y, y + h)
        else:
            y = self._nested_y(nested, h) if y is None else y
        target = nested if nested is not None else self.panels
        target.append(self.table(title, columns, y=y, x=x, w=w, h=h, **kwargs))

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


# ───────────────────────────────────────────────────────────────────────
# Dashboard composition
# ───────────────────────────────────────────────────────────────────────

# Threshold steps used throughout
ERR_STEPS = [{"color": "green", "value": None},
             {"color": "yellow", "value": 0.05},
             {"color": "red",    "value": 0.5}]
INV_STEPS = [{"color": "green", "value": None},
             {"color": "yellow", "value": 1},
             {"color": "red",    "value": 5}]   # count of bad things (alerts, restarts)
UP_STEPS = [{"color": "red",   "value": None},
            {"color": "green", "value": 1}]
DISK_STEPS = [{"color": "green",  "value": None},
              {"color": "yellow", "value": 0.80},
              {"color": "red",    "value": 0.90}]
CPU_LOAD_STEPS = [{"color": "green",  "value": None},
                  {"color": "yellow", "value": 0.70},
                  {"color": "red",    "value": 0.90}]


def observability_dashboard() -> dict:
    b = Builder("roboshop-observability",
                "RoboShop — Incident Dashboard",
                ["roboshop", "observability", "sre", "incident"])

    b.text(
        "**One-stop incident view** · top-of-fold is the health summary · "
        "expand collapsed rows below to drill into Traefik, Nginx, APIs, datastores, "
        "JVM/Node, infrastructure, k8s, DNS."
    )

    # Cumulative 1h ingress 5xx — Traefik catches both backend 5xx AND upstream
    # connection refused (which the app-side metrics miss completely). Use this
    # as the user-facing error truth.
    ingress_5xx_1h = (
        f'sum(increase(traefik_service_requests_total{{job="{TRAEFIK_JOB}", '
        f'{TRAEFIK_FRONTEND}, code=~"5.."}}[1h])) or vector(0)'
    )

    # Gap between Ready pods and Ready endpoints — non-zero means kube-proxy
    # / EndpointSlice issue (Service ClusterIP routes to fewer pods than exist).
    # 91 errors in ELK were caused by exactly this kind of misalignment.
    pods_ready_per_svc = (
        f'sum by (svc) (label_replace(kube_deployment_status_replicas_ready{{namespace="{NS}", '
        f'deployment=~"roboshop-(user|cart|catalogue|payment|ratings|orders|shipping|frontend)"}}, '
        f'"svc", "$1", "deployment", "roboshop-(.*)"))'
    )
    ep_ready_per_svc = (
        f'sum by (svc) (label_replace(count by (endpointslice) ('
        f'kube_endpointslice_endpoints{{namespace="{NS}", ready="true", '
        f'endpointslice=~"roboshop-(user|cart|catalogue|payment|ratings|orders|shipping|frontend)-[a-z0-9]+"}}), '
        f'"svc", "$1", "endpointslice", "roboshop-([^-]+)-[a-z0-9]+"))'
    )
    ep_gap_total = f'sum(({pods_ready_per_svc}) - ({ep_ready_per_svc}) > 0) or vector(0)'

    # ── 1. Health banner — 8 stats arranged 4-wide × 2 rows ────────────
    # Row 1: incident-now signals (red = act). Row 2: state signals.
    b.section("Health Banner")
    row1_y = b._y
    banner_row1 = [
        ("Active alerts",
         'count(ALERTS{alertstate="firing", alertname!="Watchdog"})',
         "short", INV_STEPS),
        ("Ingress 5xx 1h",
         ingress_5xx_1h,
         "short", [{"color": "green", "value": None},
                   {"color": "yellow", "value": 1},
                   {"color": "red", "value": 10}]),
        ("API p95 max",
         f'max(histogram_quantile(0.95, sum by (le, job) (rate(http_request_duration_seconds_bucket{{job=~"{PROMCLIENT_JOBS}", route!="/metrics", namespace="{NS}"}}[{RI}]))))',
         "s", [{"color": "green",  "value": None},
               {"color": "yellow", "value": 0.5},
               {"color": "red",    "value": 1}]),
        ("Svc endpoint gap",
         ep_gap_total,
         "short", INV_STEPS),
    ]
    for i, (title, expr, unit, steps) in enumerate(banner_row1):
        b.add_stat(None, title, expr, x=i * W_QUARTER, y=row1_y, w=W_QUARTER, h=H_STAT,
                   unit=unit, steps=steps)

    row2_y = b._y
    banner_row2 = [
        ("Endpoints UP",
         f'count(up{{job=~"{ALL_JOBS}"}} == 1)',
         "short", [{"color": "red", "value": None},
                   {"color": "yellow", "value": 10},
                   {"color": "green", "value": 14}]),
        ("Pod restarts 15m",
         f'sum(increase(kube_pod_container_status_restarts_total{{namespace="{NS}", pod=~"{POD_RE}"}}[15m])) or vector(0)',
         "short", INV_STEPS),
        ("OOMKill 1h",
         f'sum(kube_pod_container_status_last_terminated_reason{{namespace="{NS}", reason="OOMKilled"}} == 1) or vector(0)',
         "short", INV_STEPS),
        ("DB VM disk worst",
         'max(1 - (node_filesystem_avail_bytes{job="azure-vms", mountpoint="/"} / node_filesystem_size_bytes{job="azure-vms", mountpoint="/"}))',
         "percentunit", DISK_STEPS),
    ]
    for i, (title, expr, unit, steps) in enumerate(banner_row2):
        b.add_stat(None, title, expr, x=i * W_QUARTER, y=row2_y, w=W_QUARTER, h=H_STAT,
                   unit=unit, steps=steps)

    # ── 2. Active alerts table ─────────────────────────────────────────
    b.section("Active Alerts (firing now)")
    b.add_table(None, "Firing alerts", [
        ("Alert",       'ALERTS{alertstate="firing", alertname!="Watchdog"}'),
    ], h=5, join_label="alertname")

    # ── 3. Golden signals — per-service, real 2×2 grid ─────────────────
    # add_chart auto-stacks unless we pin explicit y for side-by-side pairs.
    # Errors panel uses COUNT-per-interval bars (not rate) so "91 errors in
    # the last hour" is visible at a glance instead of "0.025 req/s".
    b.section("Golden Signals — per service")
    gs_h = 6
    gs_row1_y = b._y
    b.add_chart(None, "Traffic · req/s by service", TRAFFIC_PER_JOB,
                x=0, y=gs_row1_y, w=W_HALF, h=gs_h, unit="reqps")
    b.add_chart(None, "Latency · p95 (orders/shipping = mean)", LATENCY_TARGETS,
                x=W_HALF, y=gs_row1_y, w=W_HALF, h=gs_h, unit="s")
    gs_row2_y = b._y
    errors_count_per_job = f"""sum by (job) (
  increase(http_requests_total{{job=~"roboshop-user|roboshop-cart", status_code=~"5..", namespace="{NS}"}}[{RI}])
  or increase(http_requests_total{{job="roboshop-catalogue", status=~"5..", namespace="{NS}"}}[{RI}])
  or increase(http_requests_total{{job="roboshop-payment", status="5xx", namespace="{NS}"}}[{RI}])
  or increase(http_server_requests_seconds_count{{job=~"{SPRING_JOBS}", status=~"5..", namespace="{NS}"}}[{RI}])
  or increase(flask_http_request_total{{job="roboshop-ratings", status=~"5..", namespace="{NS}"}}[{RI}])
)"""
    b.add_chart(None, "Errors · 5xx count by service (per interval)",
                errors_count_per_job,
                x=0, y=gs_row2_y, w=W_HALF, h=gs_h, unit="short",
                draw_style="bars", stacking=True)
    b.add_chart(None, "Saturation · CPU (cores | utilization)", CPU_PER_JOB,
                x=W_HALF, y=gs_row2_y, w=W_HALF, h=gs_h, unit="percentunit")

    # ── 4. Per-service triage table — collapsed by default so the
    # always-visible area stays under one screen.
    triage = b.section("▼ Triage — per service", collapsed=True)
    b.add_table(triage, "Per-service status (live)", [
        ("RPS",
         f'sum by (job) (label_replace({TRAFFIC_PER_JOB},"job","$1","job","(.*)"))'),
        ("5xx/s",
         f'sum by (job) (label_replace({ERRORS_PER_JOB},"job","$1","job","(.*)"))'),
        # Cumulative app-side 5xx in last 1h — per-service total errors
        ("5xx 1h",
         f'sum by (job) ('
         f'  increase(http_requests_total{{job=~"roboshop-user|roboshop-cart", status_code=~"5..", namespace="{NS}"}}[1h])'
         f'  or increase(http_requests_total{{job="roboshop-catalogue", status=~"5..", namespace="{NS}"}}[1h])'
         f'  or increase(http_requests_total{{job="roboshop-payment", status="5xx", namespace="{NS}"}}[1h])'
         f'  or increase(http_server_requests_seconds_count{{job=~"{SPRING_JOBS}", status=~"5..", namespace="{NS}"}}[1h])'
         f'  or increase(flask_http_request_total{{job="roboshop-ratings", status=~"5..", namespace="{NS}"}}[1h])'
         f')'),
        ("Latency (s)",
         f'(histogram_quantile(0.95, sum by (le, job) (rate(http_request_duration_seconds_bucket{{job=~"{PROMCLIENT_JOBS}", route!="/metrics", namespace="{NS}"}}[{RI}])))'
         f' or histogram_quantile(0.95, sum by (le, job) (rate(flask_http_request_duration_seconds_bucket{{job="roboshop-ratings", namespace="{NS}"}}[{RI}])))'
         f' or (sum by (job) (rate(http_server_requests_seconds_sum{{job=~"{SPRING_JOBS}", uri!~"/actuator.*", namespace="{NS}"}}[{RI}])) / sum by (job) (rate(http_server_requests_seconds_count{{job=~"{SPRING_JOBS}", uri!~"/actuator.*", namespace="{NS}"}}[{RI}]))))'),
        ("CPU",  CPU_PER_JOB),
        # Aggregate over the synthetic `job` label so the merge transformation
        # joins this column with the other RPS/err/latency/cpu columns by job.
        ("Restarts 1h",
         f'sum by (job) (label_replace(sum by (pod) (increase(kube_pod_container_status_restarts_total{{namespace="{NS}", pod=~"{POD_RE}"}}[1h])), "job", "roboshop-$1", "pod", "{POD_RE}"))'),
    ], x=0, y=0, w=W_FULL, h=8)

    # ── 5. Drilldown: Ingress (Traefik) ────────────────────────────────
    # 1 stat row (4 stats × w=6) + 3 chart rows (2×w=12 each) = compact.
    ingress = b.section("▼ Ingress (Traefik)", collapsed=True)
    b.add_stat(ingress, "Traefik RPS",
               f'sum(rate(traefik_service_requests_total{{job="{TRAEFIK_JOB}", {TRAEFIK_FRONTEND}}}[{RI}]))',
               x=0, y=0, w=W_QUARTER, unit="reqps")
    b.add_stat(ingress, "Traefik p95",
               f'histogram_quantile(0.95, sum(rate(traefik_service_request_duration_seconds_bucket{{job="{TRAEFIK_JOB}", {TRAEFIK_FRONTEND}}}[{RI}])) by (le))',
               x=W_QUARTER, y=0, w=W_QUARTER, unit="s")
    # 5xx 1h (cumulative) replaces 5xx/s rate — louder and easier to act on.
    b.add_stat(ingress, "Ingress 5xx 1h",
               ingress_5xx_1h,
               x=W_QUARTER * 2, y=0, w=W_QUARTER, unit="short",
               steps=[{"color": "green", "value": None},
                      {"color": "yellow", "value": 1},
                      {"color": "red",    "value": 10}])
    b.add_stat(ingress, "Open connections",
               f'sum(traefik_open_connections{{job="{TRAEFIK_JOB}"}})',
               x=W_QUARTER * 3, y=0, w=W_QUARTER)
    # Row 2: traffic + latency, side by side.
    b.add_chart(ingress, "Traefik · requests by status",
                f'sum by (code) (rate(traefik_service_requests_total{{job="{TRAEFIK_JOB}", {TRAEFIK_FRONTEND}}}[{RI}]))',
                x=0, y=3, w=W_HALF, h=H_CHART, unit="reqps")
    b.add_chart(
        ingress, "Traefik · latency percentiles",
        [
            ("p50", f'histogram_quantile(0.50, sum(rate(traefik_service_request_duration_seconds_bucket{{job="{TRAEFIK_JOB}", {TRAEFIK_FRONTEND}}}[{RI}])) by (le))'),
            ("p95", f'histogram_quantile(0.95, sum(rate(traefik_service_request_duration_seconds_bucket{{job="{TRAEFIK_JOB}", {TRAEFIK_FRONTEND}}}[{RI}])) by (le))'),
            ("p99", f'histogram_quantile(0.99, sum(rate(traefik_service_request_duration_seconds_bucket{{job="{TRAEFIK_JOB}", {TRAEFIK_FRONTEND}}}[{RI}])) by (le))'),
        ],
        x=W_HALF, y=3, w=W_HALF, unit="s",
    )
    # Row 3: 5xx COUNT (bars, per interval) + 5xx by code as rate.
    # Count chart answers "how many errors happened" in the selected range,
    # not just the per-second rate.
    b.add_chart(ingress, "Traefik · 5xx COUNT by code (per interval, bars)",
                [("{{code}}",
                  f'sum by (code) (increase(traefik_service_requests_total{{job="{TRAEFIK_JOB}", {TRAEFIK_FRONTEND}, code=~"5.."}}[{RI}]))')],
                x=0, y=10, w=W_HALF, h=H_CHART, unit="short",
                draw_style="bars", stacking=True)
    b.add_chart(ingress, "Traefik · 5xx by code (req/s rate)",
                [("{{code}}",
                  f'sum by (code) (rate(traefik_service_requests_total{{job="{TRAEFIK_JOB}", {TRAEFIK_FRONTEND}, code=~"5.."}}[{RI}]))')],
                x=W_HALF, y=10, w=W_HALF, h=H_CHART, unit="reqps")
    # Row 4: router error breakdown + entrypoint throughput.
    b.add_chart(ingress, "Traefik · router 4xx/5xx",
                [("{{router}} {{code}}",
                  f'sum by (router, code) (rate(traefik_router_requests_total{{job="{TRAEFIK_JOB}", code=~"4..|5.."}}[{RI}]))')],
                x=0, y=17, w=W_HALF, h=H_CHART, unit="reqps")
    b.add_chart(ingress, "Traefik · entrypoint throughput",
                f'sum by (entrypoint) (rate(traefik_entrypoint_requests_bytes_total{{job="{TRAEFIK_JOB}"}}[{RI}]))',
                x=W_HALF, y=17, w=W_HALF, h=H_CHART, unit="Bps")

    # ── 6. Drilldown: Frontend Nginx ───────────────────────────────────
    nginx = b.section("▼ Frontend (Nginx, stub_status — no p95/5xx available)", collapsed=True)
    b.add_stat(nginx, "Nginx UP",
               f'min(nginx_up{{{NGINX_JOB}}})',
               x=0, y=0, w=W_QUARTER, steps=UP_STEPS)
    b.add_stat(nginx, "Nginx RPS",
               f'sum(rate(nginx_http_requests_total{{{NGINX_JOB}}}[{RI}]))',
               x=W_QUARTER, y=0, w=W_QUARTER, unit="reqps")
    b.add_stat(nginx, "Nginx Active conn",
               f'sum(nginx_connections_active{{{NGINX_JOB}}})',
               x=W_QUARTER * 2, y=0, w=W_QUARTER)
    b.add_stat(nginx, "Nginx Accepts/s",
               f'sum(rate(nginx_connections_accepted{{{NGINX_JOB}}}[{RI}]))',
               x=W_QUARTER * 3, y=0, w=W_QUARTER, unit="reqps")
    b.add_chart(nginx, "Nginx · connection states",
                [
                    ("reading", f'sum(nginx_connections_reading{{{NGINX_JOB}}})'),
                    ("writing", f'sum(nginx_connections_writing{{{NGINX_JOB}}})'),
                    ("waiting", f'sum(nginx_connections_waiting{{{NGINX_JOB}}})'),
                    ("active",  f'sum(nginx_connections_active{{{NGINX_JOB}}})'),
                ],
                x=0, y=3, w=W_HALF)
    b.add_chart(nginx, "Nginx · TCP accept rate (accepted vs handled)",
                [
                    ("accepted", f'sum(rate(nginx_connections_accepted{{{NGINX_JOB}}}[{RI}]))'),
                    ("handled",  f'sum(rate(nginx_connections_handled{{{NGINX_JOB}}}[{RI}]))'),
                ],
                x=W_HALF, y=3, w=W_HALF, unit="reqps")

    # ── 7. APIs detail — per service ───────────────────────────────────
    apis = b.section("▼ APIs (per-service detail)", collapsed=True)
    # Top-routes table — collapse all sources, surface slowest
    b.add_chart(apis, "Top routes by p95 latency (prom-client services)",
                [
                    ("{{job}} {{route}}",
                     f'topk(8, histogram_quantile(0.95, sum by (le, job, route) (rate(http_request_duration_seconds_bucket{{job=~"{PROMCLIENT_JOBS}", route!="/metrics", namespace="{NS}"}}[{RI}]))))'),
                ],
                x=0, y=0, w=W_HALF, unit="s")
    b.add_chart(apis, "Top routes by error rate (5xx)",
                [
                    ("{{job}} {{route}}",
                     f'topk(8, sum by (job, route) (rate(http_requests_total{{job=~"roboshop-user|roboshop-cart", status_code=~"5..", namespace="{NS}"}}[{RI}])))'),
                    ("{{job}} {{path}}",
                     f'topk(8, sum by (job, path) (rate(http_requests_total{{job="roboshop-catalogue", status=~"5..", namespace="{NS}"}}[{RI}])))'),
                    ("{{job}} {{handler}}",
                     f'topk(8, sum by (job, handler) (rate(http_requests_total{{job="roboshop-payment", status="5xx", namespace="{NS}"}}[{RI}])))'),
                    ("{{job}} {{uri}}",
                     f'topk(8, sum by (job, uri) (rate(http_server_requests_seconds_count{{job=~"{SPRING_JOBS}", status=~"5..", namespace="{NS}"}}[{RI}])))'),
                ],
                x=W_HALF, y=0, w=W_HALF, unit="reqps")
    b.add_chart(apis, "Orders/Shipping · top routes by mean latency",
                [
                    ("{{job}} {{uri}}",
                     f'topk(8, sum by (job, uri) (rate(http_server_requests_seconds_sum{{job=~"{SPRING_JOBS}", uri!~"/actuator.*", namespace="{NS}"}}[{RI}])) / sum by (job, uri) (rate(http_server_requests_seconds_count{{job=~"{SPRING_JOBS}", uri!~"/actuator.*", namespace="{NS}"}}[{RI}])))'),
                ],
                x=0, y=7, w=W_HALF, unit="s")
    b.add_chart(apis, "Process memory (RSS) per service",
                f'sum by (job) (process_resident_memory_bytes{{job=~"{ALL_JOBS}", namespace="{NS}"}})',
                x=W_HALF, y=7, w=W_HALF, unit="bytes")

    # ── 8. Datastores ──────────────────────────────────────────────────
    db = b.section("▼ Datastores (HikariCP + Azure DB VMs)", collapsed=True)
    # HikariCP for shipping → MySQL
    b.add_stat(db, "Hikari active",
               'sum(hikaricp_connections_active{job="roboshop-shipping"})',
               x=0, y=0, w=W_QUARTER)
    b.add_stat(db, "Hikari pending",
               'sum(hikaricp_connections_pending{job="roboshop-shipping"})',
               x=W_QUARTER, y=0, w=W_QUARTER, steps=INV_STEPS)
    b.add_stat(db, "Hikari timeouts 5m",
               f'sum(increase(hikaricp_connections_timeout_total{{job="roboshop-shipping"}}[5m]))',
               x=W_QUARTER * 2, y=0, w=W_QUARTER, steps=INV_STEPS)
    # No `_bucket` exposed for hikaricp_connections_acquire — use mean from sum/count.
    b.add_stat(db, "Hikari acquire mean (s)",
               f'sum(rate(hikaricp_connections_acquire_seconds_sum{{job="roboshop-shipping"}}[{RI}])) / sum(rate(hikaricp_connections_acquire_seconds_count{{job="roboshop-shipping"}}[{RI}]))',
               x=W_QUARTER * 3, y=0, w=W_QUARTER, unit="s",
               steps=[{"color": "green", "value": None},
                      {"color": "yellow", "value": 0.05},
                      {"color": "red", "value": 0.5}])
    b.add_chart(db, "HikariCP · pool state",
                [
                    ("active",  'sum(hikaricp_connections_active{job="roboshop-shipping"})'),
                    ("idle",    'sum(hikaricp_connections_idle{job="roboshop-shipping"})'),
                    ("pending", 'sum(hikaricp_connections_pending{job="roboshop-shipping"})'),
                    ("max",     'max(hikaricp_connections_max{job="roboshop-shipping"})'),
                ],
                x=0, y=3, w=W_HALF)
    b.add_chart(db, "Azure DB VMs · CPU utilization",
                [("{{instance_name}}",
                  f'1 - avg by (instance, instance_name) (rate(node_cpu_seconds_total{{job="azure-vms", mode="idle"}}[{RI}]))')],
                x=W_HALF, y=3, w=W_HALF, unit="percentunit")
    b.add_chart(db, "Azure DB VMs · memory available",
                [("{{instance_name}}",
                  'node_memory_MemAvailable_bytes{job="azure-vms"}')],
                x=0, y=10, w=W_HALF, unit="bytes")
    b.add_chart(db, "Azure DB VMs · root-fs used %",
                [("{{instance_name}}",
                  '1 - (node_filesystem_avail_bytes{job="azure-vms", mountpoint="/"} / node_filesystem_size_bytes{job="azure-vms", mountpoint="/"})')],
                x=W_HALF, y=10, w=W_HALF, unit="percentunit")
    b.add_chart(db, "Azure DB VMs · network rx/tx (bytes/s)",
                [
                    ("{{instance_name}} rx", f'sum by (instance_name) (rate(node_network_receive_bytes_total{{job="azure-vms", device!~"lo|veth.*|docker.*"}}[{RI}]))'),
                    ("{{instance_name}} tx", f'sum by (instance_name) (rate(node_network_transmit_bytes_total{{job="azure-vms", device!~"lo|veth.*|docker.*"}}[{RI}]))'),
                ],
                x=0, y=17, w=W_FULL, unit="Bps")

    # ── 9. JVM (orders, shipping) ──────────────────────────────────────
    jvm = b.section("▼ JVM (orders, shipping)", collapsed=True)
    b.add_chart(jvm, "JVM heap used",
                [("{{job}} {{area}}",
                  f'sum by (job, area) (jvm_memory_used_bytes{{job=~"{SPRING_JOBS}", area="heap"}})')],
                x=0, y=0, w=W_HALF, unit="bytes")
    b.add_chart(jvm, "JVM heap max",
                [("{{job}}",
                  f'sum by (job) (jvm_memory_max_bytes{{job=~"{SPRING_JOBS}", area="heap"}})')],
                x=W_HALF, y=0, w=W_HALF, unit="bytes")
    b.add_chart(jvm, "GC pause time/sec (rate of jvm_gc_pause_seconds_sum)",
                [("{{job}}",
                  f'sum by (job) (rate(jvm_gc_pause_seconds_sum{{job=~"{SPRING_JOBS}"}}[{RI}]))')],
                x=0, y=7, w=W_HALF, unit="s")
    b.add_chart(jvm, "JVM threads",
                [
                    ("{{job}} live",   f'sum by (job) (jvm_threads_live_threads{{job=~"{SPRING_JOBS}"}})'),
                    ("{{job}} daemon", f'sum by (job) (jvm_threads_daemon_threads{{job=~"{SPRING_JOBS}"}})'),
                ],
                x=W_HALF, y=7, w=W_HALF)

    # ── 10. Node.js (user, cart) ───────────────────────────────────────
    node = b.section("▼ Node.js (user, cart)", collapsed=True)
    b.add_chart(node, "Node.js heap used",
                f'nodejs_heap_size_used_bytes{{job=~"roboshop-user|roboshop-cart"}}',
                x=0, y=0, w=W_HALF, unit="bytes")
    b.add_chart(node, "Event loop lag (p99 seconds)",
                f'nodejs_eventloop_lag_p99_seconds{{job=~"roboshop-user|roboshop-cart"}}',
                x=W_HALF, y=0, w=W_HALF, unit="s")
    b.add_chart(node, "Active handles / requests",
                [
                    ("{{job}} handles",  'nodejs_active_handles_total{job=~"roboshop-user|roboshop-cart"}'),
                    ("{{job}} requests", 'nodejs_active_requests_total{job=~"roboshop-user|roboshop-cart"}'),
                ],
                x=0, y=7, w=W_HALF)
    b.add_chart(node, "GC duration (rate sum / sec)",
                [("{{job}} {{kind}}",
                  f'sum by (job, kind) (rate(nodejs_gc_duration_seconds_sum{{job=~"roboshop-user|roboshop-cart"}}[{RI}]))')],
                x=W_HALF, y=7, w=W_HALF, unit="s")

    # ── 11. Infrastructure (worker nodes + pods) ───────────────────────
    infra = b.section("▼ Infrastructure (worker nodes + pods)", collapsed=True)
    b.add_chart(infra, "Worker node · CPU utilization",
                [("{{instance}}",
                  f'1 - avg by (instance) (rate(node_cpu_seconds_total{{job="node-exporter", mode="idle"}}[{RI}]))')],
                x=0, y=0, w=W_HALF, unit="percentunit")
    b.add_chart(infra, "Worker node · memory used",
                [("{{instance}}",
                  '1 - (node_memory_MemAvailable_bytes{job="node-exporter"} / node_memory_MemTotal_bytes{job="node-exporter"})')],
                x=W_HALF, y=0, w=W_HALF, unit="percentunit")
    b.add_chart(infra, "Worker node · load1 / cores",
                [("{{instance}}",
                  f'node_load1{{job="node-exporter"}} / on(instance) count by (instance) (node_cpu_seconds_total{{job="node-exporter", mode="idle"}})')],
                x=0, y=7, w=W_HALF)
    b.add_chart(infra, "Worker node · disk used % (root)",
                [("{{instance}}",
                  '1 - (node_filesystem_avail_bytes{job="node-exporter", mountpoint="/"} / node_filesystem_size_bytes{job="node-exporter", mountpoint="/"})')],
                x=W_HALF, y=7, w=W_HALF, unit="percentunit")
    b.add_chart(infra, "Pod CPU usage (cAdvisor)",
                [("{{pod}}",
                  f'sum by (pod) (rate(container_cpu_usage_seconds_total{{namespace="{NS}", pod=~"{POD_RE}", container!=""}}[{RI}]))')],
                x=0, y=14, w=W_HALF)
    b.add_chart(infra, "Pod memory working-set (cAdvisor)",
                [("{{pod}}",
                  f'sum by (pod) (container_memory_working_set_bytes{{namespace="{NS}", pod=~"{POD_RE}", container!=""}})')],
                x=W_HALF, y=14, w=W_HALF, unit="bytes")

    # ── 12. Kubernetes object state ────────────────────────────────────
    k8s = b.section("▼ Kubernetes (restarts, OOMs, deployment state)", collapsed=True)
    b.add_chart(k8s, "Pod restart count (1h increase)",
                [("{{pod}}",
                  f'sum by (pod) (increase(kube_pod_container_status_restarts_total{{namespace="{NS}", pod=~"{POD_RE}"}}[1h])) > 0')],
                x=0, y=0, w=W_HALF)
    b.add_chart(k8s, "Last terminated reason (1 = present)",
                [("{{pod}} {{reason}}",
                  f'kube_pod_container_status_last_terminated_reason{{namespace="{NS}", pod=~"{POD_RE}"}} == 1')],
                x=W_HALF, y=0, w=W_HALF)
    b.add_chart(k8s, "Deployment ready vs desired",
                [
                    ("{{deployment}} ready",   f'sum by (deployment) (kube_deployment_status_replicas_ready{{namespace="{NS}"}})'),
                    ("{{deployment}} desired", f'sum by (deployment) (kube_deployment_spec_replicas{{namespace="{NS}"}})'),
                ],
                x=0, y=7, w=W_HALF)
    b.add_chart(k8s, "Pod phase != Running (count)",
                [("{{phase}}",
                  f'sum by (phase) (kube_pod_status_phase{{namespace="{NS}", phase!="Running"}} == 1)')],
                x=W_HALF, y=7, w=W_HALF)
    # Per-service endpoint health — pods Ready vs Service endpoints Ready.
    # A non-zero gap is the smoking gun for "kube-proxy / EndpointSlice broken":
    # nginx connects via Service ClusterIP, kube-proxy routes to fewer pods than
    # are actually Ready → intermittent "Connection refused".
    b.add_table(k8s, "Service endpoints (Ready pods vs Service endpoints)", [
        ("Pods Ready",      pods_ready_per_svc),
        ("Endpoints Ready", ep_ready_per_svc),
        ("Gap",             f'({pods_ready_per_svc}) - ({ep_ready_per_svc})'),
    ], x=0, y=14, w=W_FULL, h=8, join_label="svc")

    # ── 13. DNS (CoreDNS) ──────────────────────────────────────────────
    dns = b.section("▼ DNS (CoreDNS)", collapsed=True)
    b.add_stat(dns, "CoreDNS QPS",
               f'sum(rate(coredns_dns_requests_total[{RI}]))',
               x=0, y=0, w=W_QUARTER, unit="reqps")
    b.add_stat(dns, "CoreDNS p99 (s)",
               f'histogram_quantile(0.99, sum(rate(coredns_dns_request_duration_seconds_bucket[{RI}])) by (le))',
               x=W_QUARTER, y=0, w=W_QUARTER, unit="s",
               steps=[{"color": "green", "value": None}, {"color": "yellow", "value": 0.05}, {"color": "red", "value": 0.5}])
    b.add_stat(dns, "Error responses/s",
               f'sum(rate(coredns_dns_responses_total{{rcode!~"NOERROR|NXDOMAIN"}}[{RI}])) or vector(0)',
               x=W_QUARTER * 2, y=0, w=W_QUARTER, unit="reqps", steps=ERR_STEPS)
    b.add_stat(dns, "Panics/s",
               f'sum(rate(coredns_panics_total[{RI}])) or vector(0)',
               x=W_QUARTER * 3, y=0, w=W_QUARTER, unit="reqps", steps=ERR_STEPS)
    b.add_chart(dns, "CoreDNS · QPS by rcode",
                [("{{rcode}}",
                  f'sum by (rcode) (rate(coredns_dns_responses_total[{RI}]))')],
                x=0, y=3, w=W_HALF, unit="reqps")
    b.add_chart(dns, "CoreDNS · latency percentiles",
                [
                    ("p50", f'histogram_quantile(0.50, sum(rate(coredns_dns_request_duration_seconds_bucket[{RI}])) by (le))'),
                    ("p95", f'histogram_quantile(0.95, sum(rate(coredns_dns_request_duration_seconds_bucket[{RI}])) by (le))'),
                    ("p99", f'histogram_quantile(0.99, sum(rate(coredns_dns_request_duration_seconds_bucket[{RI}])) by (le))'),
                ],
                x=W_HALF, y=3, w=W_HALF, unit="s")

    return b.build()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "roboshop-observability.json"
    body = observability_dashboard()
    path.write_text(json.dumps(body, indent=2), encoding="utf-8")
    print(f"Wrote {path}  ({len(body['panels'])} top-level panels)")


if __name__ == "__main__":
    main()
