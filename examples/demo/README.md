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
4. Inserts a placeholder narrative for each repo (`--no-llm` skips
   the LLM call, so no Anthropic / Bedrock token is needed).
5. Writes rendered Markdown to `./demo-output/`.

## Run it

From the repo root:

```bash
./examples/demo/run.sh
```

That's the default (Markdown publisher, placeholder narratives). See
[Try variations](#try-variations) below for HTML / JSON publishers and a
real local LLM via Ollama — all still credential-free.

That's it. The script:

- Sets stub values for the `IAC_CARTOGRAPHER_SECRET_*` env vars so the
  Pydantic credential models validate (the credentials are never sent
  anywhere — `--no-llm` skips the LLM, the markdown publisher
  writes locally, Confluence is not the active publisher).
- Runs `iac-cartographer --once --no-llm --config examples/demo/config.yaml`.
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

The runner takes two flags so you can exercise more of the matrix
without touching any config by hand — and still with **zero cloud
credentials**. Each variant has its own sibling config in this
directory (`config-html.yaml`, `config-json.yaml`, `config-ollama.yaml`)
and writes to its own output directory, so they don't clobber each
other.

### HTML publisher

Self-contained HTML (embedded CSS, no JS, no external fonts) — works
opened straight from disk, mailed as an attachment, or dropped on S3 /
GitHub Pages with no build step:

```bash
./examples/demo/run.sh --publisher html
open demo-output-html/index.html        # macOS
xdg-open demo-output-html/index.html    # Linux
```

### JSON publisher

Machine-readable output — an `index.json` feed plus one file per repo
carrying the full inventory. Handy as a feed for Backstage catalogs,
internal CMDBs, dashboards, or custom drift tooling:

```bash
./examples/demo/run.sh --publisher json
cat demo-output-json/index.json
cat demo-output-json/repos/*.json
```

### Real local LLM via Ollama

The base demo passes `--no-llm`, so the "Purpose" section is a
placeholder. If you have [Ollama](https://ollama.com) installed, this
variant generates **real** one-paragraph narratives from a model running
on your own machine — no API key, no outbound traffic:

```bash
ollama pull llama3.1:8b               # one-time; or any model you prefer
./examples/demo/run.sh --llm ollama
open demo-output-ollama/index.md
```

It degrades gracefully: if no Ollama server answers at
`http://localhost:11434` (override with `OLLAMA_HOST`), the runner
prints setup instructions and **skips** the run — it never hangs on a
timeout or crashes. Point it at a different model by editing
`model_id` in [`config-ollama.yaml`](config-ollama.yaml), or at a remote
host via `ollama_base_url`. Narrative quality scales with model size;
the structural inventory (providers, modules, resources) is identical
regardless of the LLM.

### Hosted Anthropic LLM (needs an API key)

Not zero-credential, but if you have an Anthropic API key: set
`llm.backend: anthropic` in a config, run without `--no-llm`, and
provide the key:

```bash
export IAC_CARTOGRAPHER_SECRET_ANTHROPIC='{"api_key":"sk-ant-..."}'
```

Any unrecognised flags (e.g. `--verbose`) are forwarded to the
`iac-cartographer` CLI as-is, so `./examples/demo/run.sh --publisher html --verbose`
works too.

## What this is not

- This is a **read-only** exercise: the demo never mutates a Confluence
  page, posts to a real Slack workspace, or makes a Bedrock / Anthropic
  API call.
- The `last_commit_sha` and `last_commit_at` values in `repos.yaml` are
  static placeholders. A real deployment uses the VCS-host or
  Bitbucket discovery sources, which fetch the actual HEAD info.
- Output quality with `--no-llm` is reduced: the "Purpose"
  section shows a placeholder string. Production runs against your
  fleet will use a real LLM and produce one-paragraph repo summaries.
