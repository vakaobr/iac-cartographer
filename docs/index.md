---
title: iac-cartographer
description: Fleet-level documentation for your Terraform / IaC estate.
---

<picture>
  <source srcset="banner.webp" type="image/webp">
  <img src="banner.png" alt="iac-cartographer banner" style="width: 100%; max-width: 100%;">
</picture>

# iac-cartographer

> Fleet-level documentation for your Terraform / IaC estate.

`iac-cartographer` discovers every Terraform repository across your
configured sources (GitLab groups, GitHub organisations, Bitbucket
workspaces, self-hosted Gitea / Forgejo orgs, or a hand-curated file),
extracts structural facts with [`terraform-docs`](https://terraform-docs.io)
(plus an HCL parser fallback for fields `terraform-docs` strips),
asks a pluggable LLM backend (Bedrock / Anthropic / Vertex AI /
Azure OpenAI / OpenAI / Ollama) to write a short purpose summary for
each repo, and publishes the result to Confluence, Notion, GitHub
Wiki, local Markdown, standalone HTML, or machine-readable JSON.

Pages republish only when the underlying content changes (banner-SHA
short-circuit), so it's safe to run as often as you like.

## Why

- **Self-onboarding for engineers.** A new hire opens one page and sees the
  entire IaC estate — what each repo does, which providers, which modules,
  last commit and author.
- **Always current.** Re-runs are idempotent and refresh on a schedule of
  your choosing. The page never lies for long.
- **Fix-it signals are visible.** Repos missing a `required_providers`
  block render with a `(not declared)` marker; repos with unpinned versions
  get `(unpinned)`. The page surfaces problems instead of hiding them.
- **Cheap.** Single-shot LLM spend per run is typically well under €1 for a
  small fleet (30-ish repos against Bedrock + Sonnet 4.5 with prompt
  caching). Run for free against a local Ollama model — the structural
  inventory is unaffected by which backend renders the narrative.

## Pipeline

```
Discovery: GitLab · GitHub · Bitbucket · Gitea/Forgejo · file
                            │
                            ▼
              shallow clone ──► terraform-docs per .tf dir
                            │
        ┌───────────────────┴──────────────────────────┐
        ▼                                              ▼
required_providers              LLM (narrative summary)
parsed from HCL                 Bedrock · Anthropic · Vertex
        │                       Azure OpenAI · OpenAI · Ollama
        │                                              │
        └─────────────► aggregate ◄────────────────────┘
                            │
                            ▼
              render to chosen publisher
                            │
                            ▼
Publisher: Confluence (ADF) · Notion · GitHub Wiki
           Markdown · HTML · JSON
                            │
                            ▼
Notifications (info/warn/error) — multi-channel fanout
Slack · Teams · email · SNS · PagerDuty · Opsgenie · Discord
Slack-incoming · RocketChat · Mattermost · generic webhook
stdout/JSONL
```

## Where to next

- **Just want to see what it produces?** Run the
  [zero-credentials demo](quickstart.md#zero-credentials-demo) — three small
  public Terraform repos, no AWS account, no Confluence space, real output
  in under a minute.
- **Setting up your fleet?** Start at [Quick start](quickstart.md), then dig
  into the [backends](backends/index.md) you want to wire up.
- **Operating it long-term?** See
  [Running on a schedule](operations/runtime.md) (k8s CronJob via Helm,
  GitHub Actions, plain cron) and
  [Cutting a release](operations/releases.md).
- **Reading the code?** [Architecture](reference/architecture.md) explains
  the load-bearing patterns; [Configuration schema](reference/configuration.md)
  documents every `config.yaml` field.
