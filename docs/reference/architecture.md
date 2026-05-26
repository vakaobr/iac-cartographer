# Architecture

`iac-cartographer` is structured as a linear pipeline orchestrated by
`cli.py`. Each phase is a separate module with a narrow interface;
pluggable backends sit behind ABCs at five seams: discovery, LLM,
publisher, secrets, and notifications.

## Pipeline phases

1. **Config + secrets load** (`cli._load_config`, `cli._load_secrets`)
   — read `config.yaml` from a file or SSM; build a
   `SecretsProvider`; fetch the credential bundles needed for the
   active backends.
2. **Preflight** (`cli._run_once_async`) — Confluence parent-page
   reachability check (skipped for non-Confluence publishers + dry-runs).
3. **Discovery** (`discovery.discover_from_sources`) — every
   configured `DiscoverySource` runs concurrently; results are deduped
   by `full_name` (first-seen wins); `deny_repos` globs filter the merged
   set. At least one source must be configured.
4. **Per-repo pipeline** (`cli._process_repo`, run under
   `asyncio.Semaphore(3)`):
   1. `fetcher.clone` — shallow `git clone --depth=1` to a temp dir.
   2. `extractor.run_terraform_docs` — invoke terraform-docs on every
      `.tf` directory; aggregate the JSON outputs.
   3. `extractor` HCL fallback — parse `required_providers` blocks
      directly since terraform-docs strips the `source` field from JSON.
   4. `narrator.summarize` — call the configured LLM backend; validate
      the response against the strict Pydantic schema; reject and retry
      once on validation failure; insert a placeholder narrative on
      persistent failure or AI-H1 trigger-phrase detection.
   5. `fetcher.cleanup` — remove the temp clone.
5. **Publish** (`cli._build_publisher` → `Publisher.publish_*`) —
   one publisher subclass handles everything (Confluence, Notion,
   GitHub Wiki, Markdown, HTML, JSON). Children are published first
   so the overview can link to them. Banner-SHA short-circuit per
   page.
6. **Run summary** — emit per-repo + aggregate metrics to logs +
   CloudWatch + the `NotificationDispatcher` (warn / info / error
   based on outcome; fans out to every configured channel
   concurrently with per-channel error isolation).

## Load-bearing patterns

### Pydantic v2 strict mode

Every model in `models.py` extends `_Strict` (`extra="forbid"`). Schema
drift in upstream responses (terraform-docs, Confluence v2, GitLab,
Bedrock) produces a loud validation error, not a silent partial parse.
The pipeline's per-repo failure isolation means one schema-drift
casualty doesn't sink the run.

### Banner-SHA idempotency

Every publishable artifact carries an embedded SHA computed from its
content. On the next run, the publisher reads the prior SHA back out of
the existing artifact and compares against the freshly-computed value
— equal means skip the write entirely.

Same contract across all six publishers:

| Publisher | SHA location |
|---|---|
| Confluence | HTML comment inside the page's ADF body |
| Notion | 🔖 callout block at the top of every page |
| GitHub Wiki | First-line HTML comment in each `.md` file (matches the local-markdown publisher) |
| Markdown | First-line HTML comment |
| HTML | `<meta name="iac-cartographer-sha" content="...">` |
| JSON | Top-level `iac_cartographer.sha` field |

Reads are bounded (HTML reader scans only the first 1 KB; JSON reader
uses `json.loads`; Notion reader fetches only the first block) so
the per-page "is this unchanged" check stays fast.

### Per-repo failure isolation

A bad single repo must never sink the whole pipeline. Per-repo
exceptions are captured into the `RunOutcome.failed: dict[full_name, str]`
and surfaced in the final Slack summary; the run still publishes
every repo that succeeded.

Exit codes in `cli.main`:

| Code | Meaning |
|---|---|
| 0 | Every discovered repo published or correctly skipped-unchanged. |
| 1 | Partial success — some repos failed; the rest were published. |
| 2 | Known error caught at top level (`ConfigError`, `MissingSecretError`, …). |
| 3 | Unhandled exception. |

### Pluggable backends behind ABCs

Five subsystems, five ABCs, five factory functions — the rest of the
pipeline doesn't know which implementation is active.

| Subsystem | ABC | Factory |
|---|---|---|
| Discovery | `DiscoverySource` | `cli._build_sources` |
| LLM | `LLMBackend` | `cli._build_llm_backend` |
| Publisher | `Publisher` | `cli._build_publisher` |
| Secrets | `SecretsProvider` | `secrets.build_provider` |
| Notifications | `NotificationChannel` | `notifications.build_dispatcher` |

Adding a new implementation means subclassing the ABC, adding a
literal to the discriminator in `models.py`, and adding a branch to the
factory. Every other module stays untouched.

### Defense against prompt injection

Repo content is fundamentally untrusted. Defense lives in three layers:

1. **Schema validation** of the LLM response (Pydantic strict + field
   validators reject URLs in narrative free-text fields).
2. **Trigger-phrase scan** over the model output
   (`narrator.detect_suspicious_phrases`) — matches replace the
   narrative with a placeholder and flag the repo for AI-H1 review in
   the Slack summary.
3. **Read-only blast radius** — the LLM output never invokes a tool;
   the worst case is "garbled narrative on one page", recovered on the
   next run.

The trigger-phrase watchlist is curated against real production data,
not generic IaC vocabulary — false positives would bury the genuine
signal.

## Module map

```
iac_cartographer/
├── cli.py                    # orchestrator + argparse + --init dispatcher
├── models.py                 # every Pydantic model + Strict base
├── aws.py                    # boto3 wrappers (Secrets Manager, SSM, Bedrock, CloudWatch)
├── confluence.py             # Confluence v2 client (ADF body, banner-SHA)
├── fetcher.py                # shallow git clone with per-host auth dispatch
├── extractor.py              # terraform-docs + HCL `required_providers` parser
├── llm.py                    # LLMBackend ABC + 6 implementations (Bedrock, Anthropic, Vertex, Azure OpenAI, OpenAI, Ollama)
├── narrator.py               # prompt assembly, schema validation, retry, AI-H1 scan
├── renderer.py               # shared rendering helpers (banner, provider inference)
├── init_scaffold.py          # `iac-cartographer --init`
│
├── discovery/                # DiscoverySource ABC + 5 implementations
│   ├── base.py
│   ├── gitlab.py
│   ├── github.py
│   ├── bitbucket.py
│   ├── gitea.py              # covers Forgejo too (API-compatible)
│   ├── file.py
│   └── orchestrator.py       # discover_from_sources()
│
├── publishers/               # Publisher ABC + 6 implementations
│   ├── base.py
│   ├── confluence.py
│   ├── notion.py + notion_renderer.py
│   ├── github_wiki.py        # git-based; reuses the markdown renderer
│   ├── markdown.py + markdown_renderer.py
│   ├── html.py + html_renderer.py
│   └── json_publisher.py + json_renderer.py
│
├── secrets/                  # SecretsProvider ABC + 3 implementations
│   ├── base.py
│   ├── aws.py
│   ├── env.py
│   └── vault.py
│
└── notifications/            # NotificationChannel ABC + 10 channels + dispatcher
    ├── base.py
    ├── dispatcher.py         # multi-channel fanout + per-level filter + error isolation
    ├── slack.py              # bot-token `chat.postMessage`
    ├── slack_webhook.py      # Slack incoming / RocketChat / Mattermost
    ├── teams.py              # Adaptive Card v1.4
    ├── email.py              # SMTP via aiosmtplib (optional [email] extra)
    ├── sns.py                # AWS SNS publish (identity-based)
    ├── pagerduty.py          # Events API v2
    ├── opsgenie.py           # Alerts API; US + EU regions
    ├── discord.py            # webhook
    ├── webhook.py            # generic JSON POST
    └── stdout.py             # JSON Lines on stdout / stderr
```
