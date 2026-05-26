# docker-compose

End-to-end walkthrough for running iac-cartographer as a one-shot
container under docker-compose.

**When to pick this:** local dev runs against your real fleet, on-prem
VMs with Docker but no k8s, or air-gapped boxes where neither cloud nor
cluster is available.

The runnable code lives at
[`examples/runtime/docker-compose/`](https://github.com/vakaobr/iac-cartographer/tree/main/examples/runtime/docker-compose).

## Setup

```bash
git clone https://github.com/vakaobr/iac-cartographer.git
cd iac-cartographer/examples/runtime/docker-compose

cp .env.example .env
cp config.example.yaml config.yaml
chmod 600 .env

# Edit both files — see the inline comments for what each field does.
$EDITOR config.yaml .env
```

## Run

```bash
docker compose run --rm iac-cartographer
```

!!! note
    `compose run` (not `compose up`) is correct. iac-cartographer is a
    one-shot batch job, so `up` would exit immediately. The `--rm`
    flag cleans up the per-invocation container after the run.

Output lands under `./output/` thanks to the bind mount in
`docker-compose.yml`. For the Confluence publisher, no local output is
written — the publish goes directly to the configured space.

## Schedule

The compose file is the unit-of-deployment; pair it with whatever
scheduler runs on the host.

=== "cron"

    ```cron
    # /etc/cron.d/iac-cartographer
    0 6 * * 1 cd /opt/iac-cartographer/docker-compose && \
      docker compose run --rm iac-cartographer >> /var/log/iac-cartographer.log 2>&1
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

    Enable:

    ```bash
    systemctl daemon-reload
    systemctl enable --now iac-cartographer.timer
    ```

=== "Nomad periodic"

    ```hcl
    job "iac-cartographer" {
      type = "batch"
      periodic {
        cron = "0 6 * * 1"
        prohibit_overlap = true
      }
      group "main" {
        task "run" {
          driver = "docker"
          config {
            image = "ghcr.io/vakaobr/iac-cartographer:v0.1.0"
            args  = ["--once", "--config", "/etc/iac-cartographer/config.yaml"]
            mount {
              type     = "bind"
              source   = "/opt/iac-cartographer/config.yaml"
              target   = "/etc/iac-cartographer/config.yaml"
              readonly = true
            }
          }
          env {
            IAC_CARTOGRAPHER_SECRET_CONFLUENCE = "{\"email\":\"...\",\"api_token\":\"...\"}"
            # ...
          }
        }
      }
    }
    ```

## What gets mounted

| Host path | Container path | Purpose |
|---|---|---|
| `./config.yaml` | `/etc/iac-cartographer/config.yaml` *(ro)* | The iac-cartographer config. Loaded at startup. |
| `./output/` | `/var/run/iac-cartographer/output` | Markdown / HTML / JSON publisher output. Drop this volume if `publisher.kind: confluence`. |
| *(tmpfs)* | `/tmp` *(1 GiB)* | Shallow git clones. Sized for ~50 typical IaC repos. |

Container runs non-root (`user: "1000:1000"`). The mounted output dir
needs to be writable by UID 1000 on the host:

```bash
mkdir -p output
sudo chown 1000:1000 output  # if running rootless, this may already be correct
```

## What's in the `.env`

Each value is JSON-encoded for `SECRET_*`, plain string for `PARAM_*`.
The env-secrets backend translates logical secret names to env vars by
uppercasing and replacing `/` and `-` with `_`:

| Logical name | Env var |
|---|---|
| `iac-cartographer/confluence` | `IAC_CARTOGRAPHER_SECRET_CONFLUENCE` |
| `iac-cartographer/gitlab` | `IAC_CARTOGRAPHER_SECRET_GITLAB` |
| `iac-cartographer/github` | `IAC_CARTOGRAPHER_SECRET_GITHUB` |
| `iac-cartographer/slack` | `IAC_CARTOGRAPHER_SECRET_SLACK` |
| `iac-cartographer/anthropic` | `IAC_CARTOGRAPHER_SECRET_ANTHROPIC` |
| `/iac-cartographer/confluence-parent-id` | `IAC_CARTOGRAPHER_PARAM_CONFLUENCE_PARENT_ID` |

See the [secrets backends](../backends/secrets.md) page for the full
mapping rules.

## Production-grade

A few tweaks worth making before pointing this at a real fleet:

**Pin the image.** `:latest` is a convenience tag. In production:

```yaml
services:
  iac-cartographer:
    image: ghcr.io/vakaobr/iac-cartographer:v0.1.0
```

Verify the cosign signature before pinning a new version:

```bash
cosign verify ghcr.io/vakaobr/iac-cartographer:v0.1.0 \
  --certificate-identity-regexp '^https://github\.com/vakaobr/iac-cartographer' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com'
```

**Externalise credentials.** Storing real tokens in `.env` is fine for
small deployments; for anything larger, use an external secret store
and overlay env vars via your scheduler's secret-injection mechanism
(systemd `EnvironmentFile=` with a chmod-0600 file, Nomad
`vault {}` block, Docker Swarm secrets, etc.).

**Log shipping.** The CLI emits JSON-formatted log lines to stdout —
pipe to your log shipper of choice. With cron + the redirect shown
above, the log file at `/var/log/iac-cartographer.log` is JSON Lines.

## Troubleshooting

**`permission denied` writing to `./output/`** — UID 1000 inside the
container needs write access. Set the host directory ownership to match
(`sudo chown 1000:1000 output`).

**`unauthorized` from GitHub during clone** — see the
[fetcher token handling](../backends/discovery.md#github). The fetcher
splices the GitHub token into the clone URL; an invalid token fails
even on public repos. For dry-runs against public repos only, see the
[demo](https://github.com/vakaobr/iac-cartographer/tree/main/examples/demo).

**Slack post returns `invalid_auth`** — best-effort failure, logged at
WARN level. Doesn't fail the run. Fix the bot token or remove the
Slack channel from the config to silence.
