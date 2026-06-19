# RoboShop Observability

Single focused Grafana dashboard — clean layout, essential metrics only.

## Layout

| Section | Visible by default | Panels |
|---------|-------------------|--------|
| **Overview** | Yes | 6 KPI stats (UP, RPS, latency, errors, nginx) |
| **Golden Signals** | Yes | 4 charts in 2×2 grid (traffic, latency, errors, CPU) |
| **Ingress** | Collapsed | Traefik + Nginx detail |
| **Service Health** | Collapsed | UP stat per service |
| **Saturation** | Collapsed | Memory, JVM, Node heap, DB pool |

## Deploy

```bash
cd obs
cp .env.example .env    # set GRAFANA_PASSWORD
./deploy-dashboard.sh
```

## URL

http://grafana-dev.rdevopsb89.online/d/roboshop-observability/roboshop-observability

## Regenerate only

```bash
python3 obs/generate-dashboards.py
```

## Frontend nginx metrics

Redeploy frontend after enabling stub_status + nginx-prometheus-exporter sidecar:

```bash
cd roboshop-helm-v1 && make upgrade component_name=frontend
```
