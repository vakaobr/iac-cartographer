# GitHub Actions

Scheduled iac-cartographer runs via a GitHub Actions workflow. The
runnable workflow file lives at
[`examples/runtime/github-actions.yml`](https://github.com/vakaobr/iac-cartographer/blob/main/examples/runtime/github-actions.yml).

**When to pick this:** lightweight setup with no infrastructure to own;
secrets via repo Actions secrets; runs on GitHub-hosted runners. Best
for small fleets (20-30 repos) where the run completes under the
GHA job timeout.

## Setup

Drop the workflow at `.github/workflows/iac-cartographer.yml` in any
repo. The workflow doesn't need access to the source code beyond the
PyPI-installed package, so this can be the iac-cartographer fork
itself or a separate dedicated repo (e.g. an internal "ops" repo).

```bash
mkdir -p .github/workflows
curl -fsSL https://raw.githubusercontent.com/vakaobr/iac-cartographer/main/examples/runtime/github-actions.yml \
  -o .github/workflows/iac-cartographer.yml
# Edit the inline config block (heredoc inside the workflow).
$EDITOR .github/workflows/iac-cartographer.yml
git add .github/workflows/iac-cartographer.yml
git commit -m "ci: scheduled iac-cartographer runs"
git push
```

## Required secrets

Set in *Repo Settings → Secrets and variables → Actions → Secrets*:

| Secret name | Format |
|---|---|
| `IAC_CARTOGRAPHER_SECRET_CONFLUENCE` | `{"email":"...","api_token":"..."}` |
| `IAC_CARTOGRAPHER_SECRET_GITLAB` | `{"token":"glpat-..."}` |
| `IAC_CARTOGRAPHER_SECRET_GITHUB` | `{"token":"ghp_..."}` |
| `IAC_CARTOGRAPHER_SECRET_SLACK` | `{"bot_token":"xoxb-..."}` |
| `IAC_CARTOGRAPHER_SECRET_ANTHROPIC` *(if `llm.backend: anthropic`)* | `{"api_key":"sk-ant-..."}` |

Plus an optional **Variable** (non-secret):

| Variable name | Format |
|---|---|
| `IAC_CARTOGRAPHER_PARAM_CONFLUENCE_PARENT_ID` | `"123456789"` |

## Schedule + manual trigger

The workflow's `on:` block defines both:

```yaml
on:
  schedule:
    - cron: "0 6 * * 1"     # 06:00 UTC every Monday
  workflow_dispatch:
    inputs:
      dry_run:
        description: "Skip Confluence writes (validation run)"
        required: false
        default: "false"
```

Manual triggers run from *Actions → iac-cartographer → Run workflow*.
The `dry_run` input is useful for validating config changes without
touching Confluence.

## Permissions model

The workflow runs with `permissions: contents: read` only — it doesn't
need write access to the repo it's defined in. All external access
(Confluence, GitLab API, GitHub API, Slack, Anthropic) is via the
Actions secrets, not the workflow token.

## Cost

GHA's free tier on a public repo is unlimited; on a private repo
you get 2,000 minutes/month on the free plan. A typical run for ~50
repos takes 3-5 minutes, so weekly runs are well within the free
allotment.

## When this isn't the right fit

- **Long runs (> 6 hours).** GHA jobs are capped at 6 hours. For fleets
  > 1000 repos with slow upstream APIs, switch to k8s.
- **Network egress restrictions.** GHA runners have public IPs;
  Confluence + GitLab APIs that allowlist by IP won't work without
  self-hosted runners or static-IP routing.
- **Secret hygiene mandates.** Actions secrets are encrypted but
  scoped to the repo. Compliance regimes that require Vault / KMS /
  HSM-rooted credentials should use the Helm chart with
  ExternalSecrets instead.
