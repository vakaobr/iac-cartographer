# Lint mode (`--lint`)

`iac-cartographer --lint <path>` runs the same extraction pipeline
the scheduled-publish path uses, but instead of producing a published
page it applies a small set of IaC-hygiene rules and emits findings.
Different mode from the publish path:

- No discovery, no clone, no LLM, no publisher, no notifier.
- Operates on a single **local directory** the operator already has
  on disk (a CI checkout, a working tree, a pre-commit-hook invocation).
- Exits non-zero when findings exceed the configured threshold,
  so it's CI-gating-friendly.

```bash
iac-cartographer --lint ./infra
iac-cartographer --lint ./infra --format=json
iac-cartographer --lint ./infra --format=github  # GitHub Actions annotations
iac-cartographer --lint ./infra --fail-on=warn   # treat warnings as failures too
```

## Rules

| Rule ID | Severity | Triggers when |
|---|---|---|
| `provider-undeclared` | `error` | A provider is referenced in `resource "..."` blocks but missing from `terraform { required_providers { ... } }`. Terraform's own `init` already fails on this for any non-Hashicorp namespace — lint catches it before `init` runs. |
| `provider-unpinned` | `warn` | A declared provider has no `version = "..."` constraint. Each `init` may then pick a different version → silent drift. |
| `module-unpinned` | `warn` | A declared module has no `version` field. For registry modules this is high-risk (same `source` can resolve to any tag); for git-source modules with a ref (`?ref=v1.2.3`) the version parser extracts the ref and the lint passes. |
| `no-terraform-files` | `info` | The directory contains no `.tf` files at all. Not a problem in itself — surfaced so a misconfigured pre-commit hook fails loud rather than silently passing. |

## Output formats

### text (default)

Markdown-ish, one finding per line, colour-free, prefixed with the
severity in upper-case for `grep -E '^ERROR'`-friendly CI log
filtering.

```
## iac-cartographer lint — ./infra

**1 error(s), 2 warning(s), 0 info**

ERROR  [provider-undeclared]: provider 'cloudflare' is used but missing from `terraform { required_providers { ... } }`. ...
WARN   [provider-unpinned]: provider 'aws' (hashicorp/aws) has no version constraint — each `terraform init` may resolve a different version. Pin with `version = "~> X.Y"`.
WARN   [module-unpinned]: module 'vpc' (terraform-aws-modules/vpc/aws) has no version pin ...
```

### json

Stable schema for CI dashboards / custom gates / programmatic
consumers:

```json
{
  "schema": "iac-cartographer.lint.v1",
  "repo_path": "./infra",
  "summary": {"error": 1, "warn": 2, "info": 0},
  "findings": [
    {
      "rule_id": "provider-undeclared",
      "severity": "error",
      "path": "",
      "message": "..."
    },
    ...
  ]
}
```

`schema` lets consumers pin to a major version and warn on drift.
Additive changes (new optional fields) don't require a bump.

### github

GitHub Actions annotation format — `::warning file=...,title=...::message`.
One annotation per finding. When the linter runs inside a GitHub
Actions workflow these surface as PR review comments. Info findings
map to `::notice`; warn maps to `::warning`; error maps to `::error`.

Special chars in the message (`%`, `\r`, `\n`, `:`) are URL-encoded
to avoid breaking the annotation parser.

## Exit codes

| Code | When |
|---|---|
| 0 | No findings (or only `info`-level findings, with default `--fail-on=error`). |
| 1 | Warnings present AND `--fail-on=warn` (or info findings + `--fail-on=info`). |
| 2 | Errors present, regardless of `--fail-on` threshold. |
| 3 | Unhandled exception. |

## Pre-commit hook

The repo ships a `.pre-commit-hooks.yaml` so [pre-commit](https://pre-commit.com)
users can wire iac-cartographer into their commit gate:

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/vakaobr/iac-cartographer
    rev: v0.1.0   # pin to a release tag
    hooks:
      - id: iac-cartographer-lint
        args: [--fail-on=warn]
```

The hook runs once per commit with the working tree as `.`. It
pulls iac-cartographer into pre-commit's isolated environment via
`language: python`, but **`terraform-docs` must be on PATH**
(pre-commit doesn't install OS-level binaries). On macOS:

```bash
brew install terraform-docs
```

For CI runs without terraform-docs installed, use the Docker variant:

```yaml
hooks:
  - id: iac-cartographer-lint-docker
    args: [--fail-on=warn]
```

This runs `ghcr.io/vakaobr/iac-cartographer:latest` with the working
tree mounted at `/work`. The image ships terraform-docs
pre-installed, so the host doesn't need it.

## GitHub Actions usage

The `github` output format turns the linter into a PR-review-annotating
gate:

```yaml
# .github/workflows/iac-lint.yml
name: IaC lint
on: [pull_request]
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: |
          curl -sSLo terraform-docs.tar.gz "https://terraform-docs.io/dl/v0.20.0/terraform-docs-v0.20.0-$(uname)-amd64.tar.gz"
          tar -xzf terraform-docs.tar.gz
          sudo mv terraform-docs /usr/local/bin/
      - run: pip install iac-cartographer
      - run: iac-cartographer --lint ./infra --format=github --fail-on=warn
```

A failed run blocks the PR; annotations surface in the diff view so
reviewers see exactly which file / rule fired.

## Programmatic API

The lint engine is importable for custom tooling — drift dashboards,
multi-repo gates, IDE integrations:

```python
from iac_cartographer.lint import (
    Severity,
    compute_exit_code,
    render,
    run_lint,
)

report = run_lint("./infra")
if report.errors:
    print(f"{len(report.errors)} errors blocking deploy")
    print(render(report, "text"))
    raise SystemExit(compute_exit_code(report, Severity.ERROR))
```

`LintReport.findings`, `.errors`, `.warnings`, `.infos` are stable
Python attributes; the JSON shape is the stable wire format.

## Relationship to the publish path

- Lint and publish share the same **extractor** (`run_terraform_docs`) —
  what the linter sees IS what the publish path renders.
- Lint findings are not echoed onto published pages. Those pages
  carry the structural facts with `(not declared)` / `(unpinned)`
  markers; the linter promotes those markers to **exit-code-affecting
  findings** for CI / pre-commit use cases.
- Lint mode does **not** read your `config.yaml`, hit any external
  APIs, or load secrets. Safe to run from sandboxed CI runners.
