#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/derry-cheng/Computing-Power-Scheduling.git}"
APP_DIR="${APP_DIR:-/opt/Computing-Power-Scheduling}"
SERVICE_NAME="l1-scheduler-ui"
ENV_DIR="/etc/l1-scheduler-ui"
ENV_FILE="${ENV_DIR}/environment"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this script as root or with sudo." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y git python3 openssl

if ! id -u l1scheduler >/dev/null 2>&1; then
  useradd --system --home-dir "${APP_DIR}" --shell /usr/sbin/nologin l1scheduler
fi

if [[ -d "${APP_DIR}/.git" ]]; then
  git -C "${APP_DIR}" pull --ff-only origin main
else
  install -d -m 0755 "$(dirname "${APP_DIR}")"
  git clone "${REPO_URL}" "${APP_DIR}"
fi

install -d -m 0750 -o root -g l1scheduler "${ENV_DIR}"
if [[ ! -s "${ENV_FILE}" ]]; then
  printf 'SCHEDULER_UI_TOKEN=%s\n' "$(openssl rand -hex 32)" > "${ENV_FILE}"
fi
chown root:l1scheduler "${ENV_FILE}"
chmod 0640 "${ENV_FILE}"
chown -R l1scheduler:l1scheduler "${APP_DIR}"
chmod 0755 "${APP_DIR}/webapp/server.py"

install -m 0644 "${APP_DIR}/deploy/${SERVICE_NAME}.service" "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}"
systemctl --no-pager --full status "${SERVICE_NAME}"
curl --fail --silent http://127.0.0.1:8080/api/health
echo
