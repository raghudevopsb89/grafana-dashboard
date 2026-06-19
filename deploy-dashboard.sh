#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DASHBOARDS_DIR="${SCRIPT_DIR}/dashboards"
ENV_FILE="${SCRIPT_DIR}/.env"
GENERATOR="${SCRIPT_DIR}/generate-dashboards.py"

if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${ENV_FILE}"
  set +a
fi

GRAFANA_URL="${GRAFANA_URL:-http://grafana-dev.rdevopsb89.online}"
GRAFANA_USER="${GRAFANA_USER:-admin}"
GRAFANA_PASSWORD="$1"
FOLDER_UID="${FOLDER_UID:-roboshop}"
FOLDER_TITLE="${FOLDER_TITLE:-RoboShop}"

if [[ -z "${GRAFANA_PASSWORD}" ]]; then
  echo "GRAFANA_PASSWORD is required. Set it in the environment or in obs/.env" >&2
  exit 1
fi

AUTH=(-u "${GRAFANA_USER}:${GRAFANA_PASSWORD}")

echo "Checking Grafana connectivity at ${GRAFANA_URL} ..."
curl -sf "${AUTH[@]}" "${GRAFANA_URL}/api/health" >/dev/null

echo "Ensuring folder '${FOLDER_TITLE}' (uid=${FOLDER_UID}) exists ..."
FOLDER_HTTP="$(curl -s -o /tmp/grafana-folder.json -w '%{http_code}' "${AUTH[@]}" \
  "${GRAFANA_URL}/api/folders/${FOLDER_UID}")"
if [[ "${FOLDER_HTTP}" != "200" ]]; then
  curl -sf "${AUTH[@]}" -H "Content-Type: application/json" \
    -X POST "${GRAFANA_URL}/api/folders" \
    -d "{\"uid\":\"${FOLDER_UID}\",\"title\":\"${FOLDER_TITLE}\"}" >/tmp/grafana-folder.json
fi

deploy_one() {
  local dashboard_file="$1"
  local message="$2"
  local payload_file
  payload_file="$(mktemp)"
  python3 - "${dashboard_file}" "${FOLDER_UID}" "${message}" <<'PY' > "${payload_file}"
import json
import sys

dashboard_path, folder_uid, message = sys.argv[1:4]
with open(dashboard_path, encoding="utf-8") as handle:
    dashboard = json.load(handle)
json.dump(
    {
        "dashboard": dashboard,
        "folderUid": folder_uid,
        "overwrite": True,
        "message": message,
    },
    sys.stdout,
)
PY
  local response
  response="$(curl -sf "${AUTH[@]}" -H "Content-Type: application/json" \
    -X POST --data-binary @"${payload_file}" \
    "${GRAFANA_URL}/api/dashboards/db")"
  rm -f "${payload_file}"
  python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('uid',''), d.get('url',''))" <<< "${response}"
}

echo "Deploying dashboards ..."
while IFS= read -r dashboard_file; do
  name="$(basename "${dashboard_file}")"
  echo "  -> ${name}"
  read -r uid url <<< "$(deploy_one "${dashboard_file}" "Deploy ${name}")"
  echo "     UID : ${uid}"
  echo "     URL : ${GRAFANA_URL}${url}"
done < <(find "${DASHBOARDS_DIR}" -maxdepth 1 -name '*.json' | sort)

echo "All dashboards deployed to folder '${FOLDER_TITLE}'."
