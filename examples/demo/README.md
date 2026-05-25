# iac-cartographer demo

Clone-and-run end-to-end exercise — no real credentials, no AWS account,
no Confluence access required. Produces real Markdown output from three
small public Terraform repositories so you can browse what
iac-cartographer actually emits before pointing it at your fleet.

## What it does

1. Reads [`repos.yaml`](repos.yaml) — three curated public Terraform repos:
   - [`terraform-aws-modules/terraform-aws-vpc`](https://github.com/terraform-aws-modules/terraform-aws-vpc)
   - [`cloudposse/terraform-null-label`](https://github.com/cloudposse/terraform-null-label)
   - [`terraform-aws-modules/terraform-aws-s3-bucket`](https://github.com/terraform-aws-modules/terraform-aws-s3-bucket)
2. Shallow-clones each over HTTPS (no auth needed for public repos).
3. Runs `terraform-docs` on every `.tf` directory.
4. Inserts a placeholder narrative for each repo (`--no-bedrock` skips
   the LLM call, so no Anthropic / Bedrock token is needed).
5. Writes rendered Markdown to `./demo-output/`.

## Run it

From the repo root:

```bash
./examples/demo/run.sh
```

That's it. The script:

- Sets stub values for the `IAC_CARTOGRAPHER_SECRET_*` env vars so the
  Pydantic credential models validate (the credentials are never sent
  anywhere — `--no-bedrock` skips the LLM, the markdown publisher
  writes locally, Confluence is not the active publisher).
- Runs `iac-cartographer --once --no-bedrock --config examples/demo/config.yaml`.
- Expected runtime: **30–90 seconds** depending on network speed.

Once the run completes:

```bash
open demo-output/index.md                            # macOS
xdg-open demo-output/index.md                        # Linux
cat demo-output/repos/terraform-aws-modules__*.md    # inspect a repo page
```

## What you'll see

- `demo-output/index.md` — overview index linking to each repo's page.
- `demo-output/repos/<slug>.md` — one per repo, with `terraform-docs`-derived
  providers, modules, inputs, outputs, resource counts, and a placeholder
  purpose summary.

The Slack post at the end of the run will fail with the dummy token —
that's expected and logged at WARN level; it doesn't fail the run.

You'll also see CloudWatch `put_metric_data` failures during the run
("Unable to locate credentials"). Same story — metric emission is
best-effort, the warnings are noisy but harmless. They go away on
deployments with real AWS credentials.

## Re-run idempotency

Run the script twice. The second run will report most pages as
`unchanged` (banner-SHA short-circuit) — no writes happen unless a repo's
HEAD actually changed since the last run.

## Try variations

Swap the publisher to standalone HTML:

```bash
sed -i.bak 's/kind: markdown/kind: html/' examples/demo/config.yaml
./examples/demo/run.sh
open demo-output-html/index.html
```

Or use the Anthropic LLM if you have an API key — drop `--no-bedrock`
from the run script, set `llm.backend: anthropic`, and add:

```bash
export IAC_CARTOGRAPHER_SECRET_ANTHROPIC='{"api_key":"sk-ant-..."}'
```

## What this is not

- This is a **read-only** exercise: the demo never mutates a Confluence
  page, posts to a real Slack workspace, or makes a Bedrock / Anthropic
  API call.
- The `last_commit_sha` and `last_commit_at` values in `repos.yaml` are
  static placeholders. A real deployment uses the VCS-host or
  Bitbucket discovery sources, which fetch the actual HEAD info.
- Output quality with `--no-bedrock` is reduced: the "Purpose"
  section shows a placeholder string. Production runs against your
  fleet will use a real LLM and produce one-paragraph repo summaries.
