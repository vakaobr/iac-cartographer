#!/usr/bin/env bash
# Plain-cron wrapper for running iac-cartographer on a host (or in any
# scheduler that just runs a shell command — systemd timer, Nomad
# `periodic` block, k3s-without-CronJob-CRD, etc.).
#
# Install:
#   1. Copy this file to /usr/local/bin/iac-cartographer-run.sh
#   2. Make it executable:  chmod +x /usr/local/bin/iac-cartographer-run.sh
#   3. Drop the matching env file at /etc/iac-cartographer/env (mode 600):
#
#        IAC_CARTOGRAPHER_SECRET_CONFLUENCE={"email":"...","api_token":"..."}
#        IAC_CARTOGRAPHER_SECRET_GITLAB={"token":"glpat-..."}
#        IAC_CARTOGRAPHER_SECRET_GITHUB={"token":"ghp_..."}
#        IAC_CARTOGRAPHER_SECRET_SLACK={"bot_token":"xoxb-..."}
#        IAC_CARTOGRAPHER_SECRET_ANTHROPIC={"api_key":"sk-ant-..."}
#        IAC_CARTOGRAPHER_PARAM_CONFLUENCE_PARENT_ID=123456789
#
#   4. Drop the config at /etc/iac-cartographer/config.yaml (see
#      examples/config.example.yaml).
#
#   5. Add to root's crontab (or use systemd-timer — see bottom of file):
#
#        0 6 * * 1 /usr/local/bin/iac-cartographer-run.sh >> /var/log/iac-cartographer.log 2>&1
#
# The script uses Docker so the host doesn't need Python / terraform-docs.
# If you'd rather install iac-cartographer locally with pip, replace the
# `docker run` invocation with a direct call to `iac-cartographer`.

set -euo pipefail

# Pinned image tag — bump intentionally after testing.
IMAGE="${IAC_CARTOGRAPHER_IMAGE:-ghcr.io/vakaobr/iac-cartographer:latest}"
ENV_FILE="${IAC_CARTOGRAPHER_ENV_FILE:-/etc/iac-cartographer/env}"
CONFIG_FILE="${IAC_CARTOGRAPHER_CONFIG:-/etc/iac-cartographer/config.yaml}"

[[ -r "$ENV_FILE"    ]] || { echo "missing or unreadable env file:    $ENV_FILE"    >&2; exit 2; }
[[ -r "$CONFIG_FILE" ]] || { echo "missing or unreadable config file: $CONFIG_FILE" >&2; exit 2; }

docker run --rm \
  --env-file "$ENV_FILE" \
  -v "$CONFIG_FILE:/etc/iac-cartographer/config.yaml:ro" \
  "$IMAGE" \
  --once \
  --config /etc/iac-cartographer/config.yaml \
  "$@"

# ─── systemd-timer alternative ─────────────────────────────────────────
#
# /etc/systemd/system/iac-cartographer.service
# ----------------------------------------------
# [Unit]
# Description=iac-cartographer run
# After=network-online.target docker.service
# Wants=network-online.target
#
# [Service]
# Type=oneshot
# ExecStart=/usr/local/bin/iac-cartographer-run.sh
#
# /etc/systemd/system/iac-cartographer.timer
# ----------------------------------------------
# [Unit]
# Description=iac-cartographer weekly run
#
# [Timer]
# OnCalendar=Mon 06:00:00
# Persistent=true
# RandomizedDelaySec=10min
#
# [Install]
# WantedBy=timers.target
#
# Enable with:
#   systemctl daemon-reload && systemctl enable --now iac-cartographer.timer
