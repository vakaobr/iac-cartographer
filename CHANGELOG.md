# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Going forward, entries are generated automatically by
[`release-please`](https://github.com/googleapis/release-please) from
[Conventional Commits](https://www.conventionalcommits.org/) on `main`. The
section below records everything that shipped before automation was wired up.

## [Unreleased]

## [0.1.3] — 2026-05-26

GitHub Marketplace publish + PyPI verified-metadata polish. v0.1.2
shipped the action successfully but Marketplace rejected the listing
on a description-length cap; PyPI showed the project page with an
"Unverified details" notice on metadata it couldn't verify against the
trusted publisher.

### Fixed

* **`action.yml` description** trimmed to 120 chars to satisfy the
  GitHub Marketplace 125-char cap. The long-form description still
  lives in README + pyproject.toml — `action.yml` only carries the
  catalogue-line version.
* **`pyproject.toml [project.urls]`** expanded — every label now points
  at a `github.com/vakaobr/iac-cartographer` path, which PyPI
  auto-verifies because it matches the OIDC trusted-publisher repo.
  Adds `Documentation`, `Source`, `Changelog`, `Releases` to the
  existing `Homepage`, `Repository`, `Issues` set. The "Unverified
  details" banner on the PyPI project page should clear on the next
  publish.
* **`release-ghcr.yml` workflow permissions** bumped from
  `contents: read` to `contents: write`. The image build + push +
  cosign signing all worked on v0.1.2, but the final SBOM-attach-to-
  release step failed with "Resource not accessible by integration"
  because the SBOM action PATCHes the GitHub Release and needs
  contents-write to do so.

## [0.1.2] — 2026-05-26

Release-pipeline shakedown plus the GitHub Marketplace action. The v0.1.1
tag exercised the workflows end-to-end and surfaced three issues; this
release fixes them, then adds the marketplace wrapper so adopters can
`uses: vakaobr/iac-cartographer@v0.1.2` from any workflow.

### Added

* **GitHub Action wrapper** — [`action.yml`](action.yml) at the repo
  root publishes a reusable marketplace action backed by the existing
  container image (Docker-based action; rebuilds from `Dockerfile` at
  invocation time, so the entrypoint + image always match the pinned
  ref). Inputs: `config`, `mode` (`once` / `lint`), `dry-run`,
  `verbose`, `repos`, `model`, `diff`, `lint-path`, `lint-format`,
  `fail-on`, `extra-args`. Ships [`scripts/action-entrypoint.sh`](scripts/action-entrypoint.sh)
  inside the image; non-action invocations still use the existing
  `iac-cartographer` ENTRYPOINT.
* **Marketplace usage example** —
  [`examples/runtime/github-action-marketplace.yml`](examples/runtime/github-action-marketplace.yml)
  shows the shortest possible scheduled-refresh workflow + the secret
  shape for the `env` secrets backend.

### Fixed

* `pyproject.toml` description trimmed from 595 → 427 chars to satisfy PyPI's
  512-char `summary` limit (the v0.1.1 upload failed with a 400 Bad Request
  from upload.pypi.org).
* `.github/workflows/security.yml` — `pip-audit --strict` no longer trips
  over the editable install of the project itself; the workflow now installs
  the package non-editably so the `--strict` warning-as-error path stays
  intact for yanked-version detection.

### Documentation

* README quick-start step 2 is no longer Confluence-specific — covers all six
  publishers (Confluence, Notion, GitHub Wiki, Markdown, HTML, JSON).
* [`examples/runtime/README.md`](examples/runtime/README.md) rewritten to
  link every subdirectory (`docker-compose/`, `aws-ecs-fargate/`,
  `gcp-cloud-run-job/`, `azure-container-apps-job/`) that landed in
  earlier PRs but wasn't surfaced in the index.

## [0.1.1] — 2026-05-26

First PyPI publish. Functionally a superset of 0.1.0 with the post-0.1.0
additions below; cut primarily to exercise the release pipeline
end-to-end (PyPI trusted publishing, GHCR cosign signing, GitHub
Release artefact attachment).

### Added

* `--diff <prev-output>` mode — between-run structural diff against a
  prior JSON-publisher snapshot. Adds / removes / provider bumps /
  module bumps / resource-count deltas. Prints Markdown to stdout and
  rides on the end-of-run Slack post as a one-liner ([#44]).
* CHANGELOG.md (Keep a Changelog format) + release-please automation:
  release-please maintains a single open "release PR" on main that bumps
  the version + updates CHANGELOG from Conventional Commits; merging it
  cuts the `vX.Y.Z` tag that drives PyPI + GHCR publishes ([#48]).

### Documentation

* Refreshed every doc surface after the notifications / Notion /
  GitHub Wiki / Gitea / AWS Terraform additions ([#47]).

[#44]: https://github.com/vakaobr/iac-cartographer/pull/44
[#47]: https://github.com/vakaobr/iac-cartographer/pull/47
[#48]: https://github.com/vakaobr/iac-cartographer/pull/48

## [0.1.0] — 2026-05-26

Initial public release. Five pluggable seams (discovery, LLM, publisher,
secrets, notifications) plus the distribution + onboarding wins below.

### Added

#### Discovery — five sources

* GitLab groups (subgroups walked automatically; self-hosted-aware).
* GitHub organisations.
* Bitbucket Cloud workspaces ([#11]).
* Gitea / Forgejo organisations (self-hosted; covers Codeberg too) ([#43]).
* Curated YAML / JSON file (`repos_file`) for handpicked inventories ([#11]).

#### LLM — six backends

* AWS Bedrock (default; cross-region inference profile via `eu.anthropic.*`).
* Anthropic API direct ([#49c29ef]).
* Vertex AI — Claude on GCP ([#30]).
* Azure OpenAI — GPT family on Azure ([#31]).
* OpenAI direct + OpenAI-compatible gateways (LiteLLM, vLLM, …) ([#31]).
* Ollama — local LLM ([#33]).

#### Publishers — six backends

* Confluence (Atlassian Cloud v2 API, ADF body, banner-SHA in HTML comment).
* Local Markdown ([#9]).
* Standalone HTML — embedded CSS, no JS ([#18]).
* Machine-readable JSON ([#21]).
* Notion — block API, callout banner-SHA ([#41]).
* GitHub Wiki — git-based, reuses the markdown renderer ([#42]).

#### Secrets — three backends

* AWS Secrets Manager + SSM Parameter Store (default).
* Process env vars with `.env` auto-loading ([#12]).
* HashiCorp Vault KV v2 (with optional Enterprise namespace header) ([#12]).

#### Notifications — multi-channel dispatcher (ten channels)

* Multi-channel dispatcher with per-level routing (info / warn / error) and
  per-channel failure isolation; back-compatible with the legacy single-Slack
  block ([#35]).
* Webhook family — generic JSON POST, Slack incoming webhook, Microsoft Teams
  Adaptive Card v1.4 ([#36]).
* Email (SMTP via `aiosmtplib`) + AWS SNS ([#37]).
* PagerDuty (Events API v2) + Opsgenie (Alerts API; US + EU regions) ([#38]).
* Discord (Incoming Webhook) + stdout / JSONL (CI + air-gapped) ([#39]).

#### Distribution + onboarding

* PyPI release workflow with OIDC trusted publishing, tag-driven, version-match
  guard ([#14]).
* GHCR container image (`ghcr.io/vakaobr/iac-cartographer`) with cosign keyless
  signing + SPDX SBOM on every tag ([#15]).
* Multi-arch container builds (`linux/amd64` + `linux/arm64`) ([#26]).
* Helm chart for k8s CronJob deployments with workload-identity bindings ([#19]).
* `iac-cartographer --init` scaffolder — interactive starter `config.yaml` +
  `.env` for any backend combination ([#16]).
* Zero-credentials demo — `./examples/demo/run.sh` produces real Markdown
  output without any tokens ([#20]).
* Docs site — mkdocs-material at
  [iac-cartographer.andersonleite.me](https://iac-cartographer.andersonleite.me/)
  ([#22], [#23]).
* Runtime examples for AWS ECS Fargate + EventBridge Scheduler ([#40]), GCP
  Cloud Run Jobs + Cloud Scheduler ([#27]), and Azure Container Apps Jobs
  ([#28]).
* docker-compose deployment recipe ([#25]).

### Security

* Layered defence against indirect prompt injection from repo content:
  XML-wrapped prompt context, Pydantic v2 strict-mode (`extra="forbid"`)
  output validation, curated AI-H1 trigger-phrase watchlist, no LLM tool use,
  read-only blast radius.
* OIDC trusted publishing on the PyPI release path — no long-lived token to
  rotate.
* Cosign keyless signatures on every container release.

[#9]: https://github.com/vakaobr/iac-cartographer/pull/9
[#11]: https://github.com/vakaobr/iac-cartographer/pull/11
[#12]: https://github.com/vakaobr/iac-cartographer/pull/12
[#14]: https://github.com/vakaobr/iac-cartographer/pull/14
[#15]: https://github.com/vakaobr/iac-cartographer/pull/15
[#16]: https://github.com/vakaobr/iac-cartographer/pull/16
[#18]: https://github.com/vakaobr/iac-cartographer/pull/18
[#19]: https://github.com/vakaobr/iac-cartographer/pull/19
[#20]: https://github.com/vakaobr/iac-cartographer/pull/20
[#21]: https://github.com/vakaobr/iac-cartographer/pull/21
[#22]: https://github.com/vakaobr/iac-cartographer/pull/22
[#23]: https://github.com/vakaobr/iac-cartographer/pull/23
[#25]: https://github.com/vakaobr/iac-cartographer/pull/25
[#26]: https://github.com/vakaobr/iac-cartographer/pull/26
[#27]: https://github.com/vakaobr/iac-cartographer/pull/27
[#28]: https://github.com/vakaobr/iac-cartographer/pull/28
[#30]: https://github.com/vakaobr/iac-cartographer/pull/30
[#31]: https://github.com/vakaobr/iac-cartographer/pull/31
[#33]: https://github.com/vakaobr/iac-cartographer/pull/33
[#35]: https://github.com/vakaobr/iac-cartographer/pull/35
[#36]: https://github.com/vakaobr/iac-cartographer/pull/36
[#37]: https://github.com/vakaobr/iac-cartographer/pull/37
[#38]: https://github.com/vakaobr/iac-cartographer/pull/38
[#39]: https://github.com/vakaobr/iac-cartographer/pull/39
[#40]: https://github.com/vakaobr/iac-cartographer/pull/40
[#41]: https://github.com/vakaobr/iac-cartographer/pull/41
[#42]: https://github.com/vakaobr/iac-cartographer/pull/42
[#43]: https://github.com/vakaobr/iac-cartographer/pull/43

[Unreleased]: https://github.com/vakaobr/iac-cartographer/compare/v0.1.3...HEAD
[0.1.3]: https://github.com/vakaobr/iac-cartographer/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/vakaobr/iac-cartographer/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/vakaobr/iac-cartographer/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/vakaobr/iac-cartographer/releases/tag/v0.1.0
