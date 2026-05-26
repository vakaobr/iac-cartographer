# docker-compose

Run iac-cartographer as a one-shot job under docker-compose. Suits:

- **Local dev / one-off runs** against your real fleet without installing
  Python on the host.
- **On-prem VMs** that have Docker but no Kubernetes / Helm.
- **Air-gapped or non-cloud environments** — the only AWS code path
  (CloudWatch metric emission) fails best-effort, so the run completes
  regardless.

## Setup

```bash
cd examples/runtime/docker-compose
cp .env.example .env
cp config.example.yaml config.yaml
chmod 600 .env

# Edit both files — see comments in each for what each field does.
$EDITOR config.yaml .env
```

## Run

```bash
docker compose run --rm iac-cartographer
```

`compose run` (not `compose up`) is correct here: iac-cartographer is a
one-shot batch job, so `up` would exit immediately. Output lands under
`./output/`.

## Schedule

The compose file is the unit-of-deployment; pair it with whatever
scheduler runs on the host.

### cron

```cron
# /etc/cron.d/iac-cartographer
0 6 * * 1 cd /opt/iac-cartographer/docker-compose && \
  docker compose run --rm iac-cartographer >> /var/log/iac-cartographer.log 2>&1
```

### systemd-timer

```ini
# /etc/systemd/system/iac-cartographer.service
[Unit]
Description=iac-cartographer run
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/opt/iac-cartographer/docker-compose
ExecStart=/usr/bin/docker compose run --rm iac-cartographer

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

Then:

```bash
systemctl daemon-reload
systemctl enable --now iac-cartographer.timer
```

## What gets mounted

- **`./config.yaml`** → `/etc/iac-cartographer/config.yaml` *(read-only)*.
- **`./output/`** → `/var/run/iac-cartographer/output` *(read-write)* for
  the rendered Markdown. Remove this volume if `publisher.kind:
  confluence`.
- **`/tmp` is tmpfs** (1 GiB, in-container only) for the shallow git
  clones. Keeps clone state off the rootfs and out of the bind mount.

## What's in the `.env`

Each value is a JSON-encoded credential bundle (for `SECRET_*`) or a
plain string (for `PARAM_*`). The env-secrets backend maps logical
secret names to env vars by uppercasing and replacing `/` and `-` with
`_`:

| Logical name | Env var |
|---|---|
| `iac-cartographer/confluence` | `IAC_CARTOGRAPHER_SECRET_CONFLUENCE` |
| `iac-cartographer/gitlab` | `IAC_CARTOGRAPHER_SECRET_GITLAB` |
| `iac-cartographer/github` | `IAC_CARTOGRAPHER_SECRET_GITHUB` |
| `iac-cartographer/slack` | `IAC_CARTOGRAPHER_SECRET_SLACK` |
| `iac-cartographer/anthropic` | `IAC_CARTOGRAPHER_SECRET_ANTHROPIC` |
| `/iac-cartographer/confluence-parent-id` | `IAC_CARTOGRAPHER_PARAM_CONFLUENCE_PARENT_ID` |

See the project README's "Secrets backends" section for the full mapping.

## Bumping the image

Pin to a semver tag in production rather than `:latest`:

```yaml
services:
  iac-cartographer:
    image: ghcr.io/vakaobr/iac-cartographer:v0.1.0
```

Verify the image signature with cosign before pinning a new version:

```bash
cosign verify ghcr.io/vakaobr/iac-cartographer:v0.1.0 \
  --certificate-identity-regexp '^https://github\.com/vakaobr/iac-cartographer' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com'
```
