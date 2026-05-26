# Quick start

## Zero-credentials demo

The fastest way to see what `iac-cartographer` produces. Clones three small
public Terraform repos and writes the rendered Markdown inventory locally,
no AWS account or Confluence space needed.

```bash
git clone https://github.com/vakaobr/iac-cartographer.git
cd iac-cartographer
pip install -e .
./examples/demo/run.sh
# Open demo-output/index.md
```

Expected runtime: 30–90 seconds. See
[`examples/demo/README.md`](https://github.com/vakaobr/iac-cartographer/blob/main/examples/demo/README.md)
for variations (swap to HTML / JSON publisher, plug in a real LLM, …).

## Install

=== "PyPI"

    ```bash
    pip install iac-cartographer
    ```

=== "From a checkout"

    ```bash
    git clone https://github.com/vakaobr/iac-cartographer.git
    cd iac-cartographer
    pip install -e .
    ```

=== "Docker"

    ```bash
    docker pull ghcr.io/vakaobr/iac-cartographer:latest
    ```

    See [Running on a schedule](operations/runtime.md) for the matching
    Kubernetes CronJob (raw + Helm), GitHub Actions workflow, and plain cron
    snippet that drive the container image.

Requirements:

- Python 3.12+
- [`terraform-docs`](https://terraform-docs.io) on your PATH
- Credentials for at least one [LLM provider](backends/llm.md) — Bedrock,
  Anthropic API, Vertex AI, Azure OpenAI, OpenAI, or a local Ollama
- A [publisher target](backends/publishers.md) — Confluence space, Notion
  parent page, GitHub repo with the wiki enabled, or a writable
  directory for Markdown / HTML / JSON

## Scaffold a config

`iac-cartographer --init` writes a starter `config.yaml` (and an optional
`.env` template for the `env` secrets backend) tailored to your chosen
backend combination:

```bash
iac-cartographer --init \
  --secrets-backend env \      # or aws | vault
  --publisher markdown \       # or confluence | html | json
  --llm anthropic \            # or bedrock
  --config-path ./iac-cartographer.config.yaml \
  --env-path    ./iac-cartographer.env
```

Both files have every required field marked with `REPLACE_ME-...`
placeholders. Edit them, source the `.env`, then dry-run:

```bash
set -a; . ./iac-cartographer.env; set +a
iac-cartographer --once --dry-run --config ./iac-cartographer.config.yaml
```

When the dry-run looks good, drop `--dry-run` to publish for real.

## Read the output

The dry-run skips the publisher itself but shows you the planned
behaviour. For non-`confluence` publishers, removing `--dry-run` writes
files locally — no irreversible mutations. For `confluence`, removing
`--dry-run` writes pages to the configured space.

See the [publishers reference](backends/publishers.md) for what the
output looks like in each format.

## Next steps

- Wire up [discovery sources](backends/discovery.md) so the run scans
  your actual repos.
- Pick a [secrets backend](backends/secrets.md) — env vars for CI,
  Vault for on-prem, AWS Secrets Manager + SSM for the legacy path.
- Schedule it: [Running on a schedule](operations/runtime.md).
