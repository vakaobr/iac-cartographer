# Plain cron / systemd-timer

Run iac-cartographer as a scheduled batch job on a single VM. The
runnable wrapper lives at
[`examples/runtime/cron.sh`](https://github.com/vakaobr/iac-cartographer/blob/main/examples/runtime/cron.sh).

**When to pick this:** you already own a VM (or two) and don't want
to bring up a k8s cluster, ECS task, or GitHub Actions runner just
for this. The wrapper drives the published container image, so the
host only needs Docker — no Python install.

For a fancier deployment shape on the same host, see the
[docker-compose walkthrough](docker-compose.md) — same image, more
ergonomic for env files and bind mounts.

## Install

```bash
# Copy and make executable
sudo cp examples/runtime/cron.sh /usr/local/bin/iac-cartographer-run.sh
sudo chmod +x /usr/local/bin/iac-cartographer-run.sh

# Env file at /etc/iac-cartographer/env (mode 600):
sudo install -m 600 /dev/stdin /etc/iac-cartographer/env <<'EOF'
IAC_CARTOGRAPHER_SECRET_CONFLUENCE={"email":"bot@x","api_token":"ATATT-..."}
IAC_CARTOGRAPHER_SECRET_GITLAB={"token":"glpat-..."}
IAC_CARTOGRAPHER_SECRET_GITHUB={"token":"ghp_..."}
IAC_CARTOGRAPHER_SECRET_SLACK={"bot_token":"xoxb-..."}
IAC_CARTOGRAPHER_SECRET_ANTHROPIC={"api_key":"sk-ant-..."}
IAC_CARTOGRAPHER_PARAM_CONFLUENCE_PARENT_ID=123456789
EOF

# Config at /etc/iac-cartographer/config.yaml
sudo cp config.yaml /etc/iac-cartographer/config.yaml
```

## Schedule

=== "crontab"

    ```bash
    echo '0 6 * * 1 /usr/local/bin/iac-cartographer-run.sh >> /var/log/iac-cartographer.log 2>&1' \
      | sudo tee -a /etc/cron.d/iac-cartographer
    ```

=== "systemd-timer"

    ```ini
    # /etc/systemd/system/iac-cartographer.service
    [Unit]
    Description=iac-cartographer run
    After=network-online.target docker.service
    Wants=network-online.target

    [Service]
    Type=oneshot
    ExecStart=/usr/local/bin/iac-cartographer-run.sh

    # /etc/systemd/system/iac-cartographer.timer
    [Unit]
    Description=iac-cartographer weekly run

    [Timer]
    OnCalendar=Mon 06:00:00
    Persistent=true
    RandomizedDelaySec=10min

    [Install]
    WantedBy=timers.target
    ```

    Enable:

    ```bash
    systemctl daemon-reload
    systemctl enable --now iac-cartographer.timer
    ```

## What the wrapper does

```bash
#!/usr/bin/env bash
set -euo pipefail

IMAGE="${IAC_CARTOGRAPHER_IMAGE:-ghcr.io/vakaobr/iac-cartographer:latest}"
ENV_FILE="${IAC_CARTOGRAPHER_ENV_FILE:-/etc/iac-cartographer/env}"
CONFIG_FILE="${IAC_CARTOGRAPHER_CONFIG:-/etc/iac-cartographer/config.yaml}"

docker run --rm \
  --env-file "$ENV_FILE" \
  -v "$CONFIG_FILE:/etc/iac-cartographer/config.yaml:ro" \
  "$IMAGE" \
  --once \
  --config /etc/iac-cartographer/config.yaml \
  "$@"
```

All three env vars are overridable, so the same wrapper works for
multiple deployments on the same host (one cron line per env file).

## Pinning the image

In production, set `IAC_CARTOGRAPHER_IMAGE` to a specific semver tag
in either the env file or the cron line:

```cron
0 6 * * 1 IAC_CARTOGRAPHER_IMAGE=ghcr.io/vakaobr/iac-cartographer:v0.1.0 \
  /usr/local/bin/iac-cartographer-run.sh >> /var/log/iac-cartographer.log 2>&1
```

Verify the cosign signature before bumping:

```bash
cosign verify ghcr.io/vakaobr/iac-cartographer:v0.1.0 \
  --certificate-identity-regexp '^https://github\.com/vakaobr/iac-cartographer' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com'
```
