# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Going forward, entries are generated automatically by
[`release-please`](https://github.com/googleapis/release-please) from
[Conventional Commits](https://www.conventionalcommits.org/) on `main`. The
section below records everything that shipped before automation was wired up.

## [0.1.18](https://github.com/vakaobr/iac-cartographer/compare/v0.1.17...v0.1.18) (2026-06-02)


### Added

* **diagnose:** per-secret required/not-active report + LLM probe cost line ([#117](https://github.com/vakaobr/iac-cartographer/issues/117)) ([8f2fef6](https://github.com/vakaobr/iac-cartographer/commit/8f2fef657b09c8f967284dbade939933f2f8dd7a))

## [0.1.17](https://github.com/vakaobr/iac-cartographer/compare/v0.1.16...v0.1.17) (2026-06-02)


### Added

* **graph:** Mermaid resource-dependency diagram embedded on every page ([#115](https://github.com/vakaobr/iac-cartographer/issues/115)) ([2aebd26](https://github.com/vakaobr/iac-cartographer/commit/2aebd265c75f8ad0adcce60452fc0167f72a7166))

## [0.1.16](https://github.com/vakaobr/iac-cartographer/compare/v0.1.15...v0.1.16) (2026-06-02)


### Added

* **state-backend:** parse and surface terraform { backend "..." {} } posture ([#113](https://github.com/vakaobr/iac-cartographer/issues/113)) ([1f1ec45](https://github.com/vakaobr/iac-cartographer/commit/1f1ec45c512b6e25df8be64456c4fc5a58627498))

## [0.1.15](https://github.com/vakaobr/iac-cartographer/compare/v0.1.14...v0.1.15) (2026-06-02)


### Added

* **cli:** --repos accepts @path/to/file.txt for newline-delimited lists ([#107](https://github.com/vakaobr/iac-cartographer/issues/107)) ([6c8e585](https://github.com/vakaobr/iac-cartographer/commit/6c8e58506f30f4fda9de740e395eaf2ea35991f5))
* **cli:** add --output-dir override for markdown/html/json publishers ([#109](https://github.com/vakaobr/iac-cartographer/issues/109)) ([484dced](https://github.com/vakaobr/iac-cartographer/commit/484dced669a80db4a08192cee99ffafc0ff85ffa))
* **notifications:** Discord channel — optional thread_id support ([#111](https://github.com/vakaobr/iac-cartographer/issues/111)) ([f53ba25](https://github.com/vakaobr/iac-cartographer/commit/f53ba25baa9eecdcb376e955b324b3e47733087e))
* **notifications:** stdout channel — add human-readable text format ([#110](https://github.com/vakaobr/iac-cartographer/issues/110)) ([3175034](https://github.com/vakaobr/iac-cartographer/commit/3175034ffef8b8d047d9b32f8895b957117245fd))

## [0.1.14](https://github.com/vakaobr/iac-cartographer/compare/v0.1.13...v0.1.14) (2026-06-02)


### Documentation

* **readme:** roadmap — broader IaC support (state backend, Mermaid, Terragrunt, Ansible, TFC/Terrakube overlays) ([#93](https://github.com/vakaobr/iac-cartographer/issues/93)) ([8c84ba1](https://github.com/vakaobr/iac-cartographer/commit/8c84ba12754aaa7f7cebdef79ec96a01a537800e))

## [0.1.13](https://github.com/vakaobr/iac-cartographer/compare/v0.1.12...v0.1.13) (2026-05-29)


### Fixed

* **renderer:** exclude LLM narrative from banner-SHA ([#86](https://github.com/vakaobr/iac-cartographer/issues/86)) ([f3d1fa5](https://github.com/vakaobr/iac-cartographer/commit/f3d1fa575854557c77557e773e3aef983fc930aa))

## [0.1.12](https://github.com/vakaobr/iac-cartographer/compare/v0.1.11...v0.1.12) (2026-05-29)


### Fixed

* **llm:** pin temperature=0 across all backends for narrative determinism ([#84](https://github.com/vakaobr/iac-cartographer/issues/84)) ([1b84a4b](https://github.com/vakaobr/iac-cartographer/commit/1b84a4b2c299fd45e68d38610af641c0825a6653))

## [0.1.11](https://github.com/vakaobr/iac-cartographer/compare/v0.1.10...v0.1.11) (2026-05-28)


### Fixed

* **discovery:** replace GitHub Code Search with /orgs + git-tree probe ([#82](https://github.com/vakaobr/iac-cartographer/issues/82)) ([7f31da1](https://github.com/vakaobr/iac-cartographer/commit/7f31da16769fcbd9f04eeb43d7c36f05e2269191))

## [0.1.10](https://github.com/vakaobr/iac-cartographer/compare/v0.1.9...v0.1.10) (2026-05-27)


### Added

* add Bitbucket clone auth via HTTP Basic ([#80](https://github.com/vakaobr/iac-cartographer/issues/80)) ([9dcd229](https://github.com/vakaobr/iac-cartographer/commit/9dcd2294c9ec6896476250cb7ded4d307932ed9e))

## [0.1.9](https://github.com/vakaobr/iac-cartographer/compare/v0.1.8...v0.1.9) (2026-05-27)


### Chores

* release 0.1.9 ([0405dfe](https://github.com/vakaobr/iac-cartographer/commit/0405dfebfbc7fccf8cbbc0c58ca0049f41de7106))

## [0.1.8](https://github.com/vakaobr/iac-cartographer/compare/v0.1.7...v0.1.8) (2026-05-27)


### Refactored

* 1.0 API-freeze cleanup — rename pre-1.0 config/CLI names (aliased) ([#71](https://github.com/vakaobr/iac-cartographer/issues/71)) ([eb36a8e](https://github.com/vakaobr/iac-cartographer/commit/eb36a8e856df2ffe911f54822c02b1eb5849bd3d))

## [0.1.7](https://github.com/vakaobr/iac-cartographer/compare/v0.1.6...v0.1.7) (2026-05-27)


### Added

* **diagnose:** --probe-llm for an opt-in real LLM completion check ([#68](https://github.com/vakaobr/iac-cartographer/issues/68)) ([e4036dd](https://github.com/vakaobr/iac-cartographer/commit/e4036dde315b63ccaa65392224f6204fb7b40992))
* **discovery:** support self-hosted GitHub Enterprise Server ([#69](https://github.com/vakaobr/iac-cartographer/issues/69)) ([1ca9d0e](https://github.com/vakaobr/iac-cartographer/commit/1ca9d0ec24d787c2ca1c62f5b618798d8b01177c))
* **secrets:** load confluence / gitlab / github / slack lazily ([#67](https://github.com/vakaobr/iac-cartographer/issues/67)) ([5dcd9de](https://github.com/vakaobr/iac-cartographer/commit/5dcd9dea30386745b7904f3fba8f66cd7036a26e))


### Documentation

* sweep stale roadmap/future references after the feature wave ([#66](https://github.com/vakaobr/iac-cartographer/issues/66)) ([da933e1](https://github.com/vakaobr/iac-cartographer/commit/da933e115b147661c354d706484a3326b7249aaf))

## [0.1.6](https://github.com/vakaobr/iac-cartographer/compare/v0.1.5...v0.1.6) (2026-05-27)


### Added

* **diagnose:** add --live flag for real backend reachability checks ([#65](https://github.com/vakaobr/iac-cartographer/issues/65)) ([577a923](https://github.com/vakaobr/iac-cartographer/commit/577a923f547d31d722e77637e511454ac38f56e3))
* **observability:** structured JSON logging + optional OTLP metrics exporter ([#63](https://github.com/vakaobr/iac-cartographer/issues/63)) ([a2225f5](https://github.com/vakaobr/iac-cartographer/commit/a2225f5c88b3f9d1bea287987c6976310d34f580))


### Documentation

* **demo:** add HTML/JSON publisher and Ollama LLM demo variants ([#61](https://github.com/vakaobr/iac-cartographer/issues/61)) ([160a903](https://github.com/vakaobr/iac-cartographer/commit/160a903875b65890085524afd9ebb908e0d7e4a2))


### Refactored

* **models:** split monolithic models.py by subsystem ([#64](https://github.com/vakaobr/iac-cartographer/issues/64)) ([2a3ea83](https://github.com/vakaobr/iac-cartographer/commit/2a3ea83a784388e7cdd2afd4722bb42337fae789))

## [0.1.5](https://github.com/vakaobr/iac-cartographer/compare/v0.1.4...v0.1.5) (2026-05-27)


### Added

* **cli:** --diagnose pre-flight self-test of the active config ([#59](https://github.com/vakaobr/iac-cartographer/issues/59)) ([d25e0f2](https://github.com/vakaobr/iac-cartographer/commit/d25e0f260b707f85f6aacafed3641d6b4bec2b90))

## [0.1.4](https://github.com/vakaobr/iac-cartographer/compare/v0.1.3...v0.1.4) (2026-05-26)


### Added

* **aws:** ECS Fargate + EventBridge Scheduler runtime example (Terraform) ([#40](https://github.com/vakaobr/iac-cartographer/issues/40)) ([2ac870a](https://github.com/vakaobr/iac-cartographer/commit/2ac870a673c84942f054085800d3a9c5cd19edcc))
* **azure:** Container Apps Job runtime example (Terraform) ([#28](https://github.com/vakaobr/iac-cartographer/issues/28)) ([4ab9aa8](https://github.com/vakaobr/iac-cartographer/commit/4ab9aa80915df3e7d91b4da190d64f047d2fc41e))
* **cli:** --diff &lt;prev-output&gt; between-run change summary ([#44](https://github.com/vakaobr/iac-cartographer/issues/44)) ([e5a1a8f](https://github.com/vakaobr/iac-cartographer/commit/e5a1a8f3529a2014347323a66c2004932f81f9b3))
* **cli:** --lint subcommand for IaC hygiene + pre-commit hook ([#45](https://github.com/vakaobr/iac-cartographer/issues/45)) ([7237a91](https://github.com/vakaobr/iac-cartographer/commit/7237a91ffd00cc389dce829f4428a797a53f2d3c))
* **cli:** iac-cartographer init scaffolder ([#16](https://github.com/vakaobr/iac-cartographer/issues/16)) ([65b4974](https://github.com/vakaobr/iac-cartographer/commit/65b49749c6cf392855077ab3ff0ac820510c770e))
* **discovery:** Gitea / Forgejo discovery source ([#43](https://github.com/vakaobr/iac-cartographer/issues/43)) ([18069ba](https://github.com/vakaobr/iac-cartographer/commit/18069baa2466effef745177e9af3158d6704a5aa))
* **discovery:** pluggable discovery sources — Bitbucket + curated file ([#11](https://github.com/vakaobr/iac-cartographer/issues/11)) ([4b4ef50](https://github.com/vakaobr/iac-cartographer/commit/4b4ef50c7d4896a29a38a79fa3284a54c3d80336))
* **gcp:** Cloud Run Job runtime example (Terraform) ([#27](https://github.com/vakaobr/iac-cartographer/issues/27)) ([fe4f26f](https://github.com/vakaobr/iac-cartographer/commit/fe4f26f52ac655f636fbd03e393a8626ddf02704))
* **helm:** add Helm chart for k8s CronJob deployment ([#19](https://github.com/vakaobr/iac-cartographer/issues/19)) ([2722ff3](https://github.com/vakaobr/iac-cartographer/commit/2722ff3fa2f743bd6412e22eedc72a4dbe528643))
* **llm:** Azure OpenAI + OpenAI direct backends (GPT family) ([#31](https://github.com/vakaobr/iac-cartographer/issues/31)) ([36c0bfe](https://github.com/vakaobr/iac-cartographer/commit/36c0bfe97e4ce0870bcd6bac761c42da5c22efee))
* **llm:** Ollama backend (local LLM) ([#33](https://github.com/vakaobr/iac-cartographer/issues/33)) ([3907817](https://github.com/vakaobr/iac-cartographer/commit/390781780146a561f78c5dc606d1685e91bd7544))
* **llm:** pluggable LLM backend — Bedrock + Anthropic direct ([49c29ef](https://github.com/vakaobr/iac-cartographer/commit/49c29ef940742ceea0e1246f92eace28b7c0f5e7))
* **llm:** Vertex AI Claude backend ([#30](https://github.com/vakaobr/iac-cartographer/issues/30)) ([0e93e52](https://github.com/vakaobr/iac-cartographer/commit/0e93e52f28a6dca74ffd1e5f979f6637f2c9bf89))
* **notifications:** Discord webhook + stdout/JSONL channels (close-out) ([#39](https://github.com/vakaobr/iac-cartographer/issues/39)) ([43fcf78](https://github.com/vakaobr/iac-cartographer/commit/43fcf7852763e13b365a996a5cf99768cbc0d679))
* **notifications:** email (SMTP) + AWS SNS channels ([#37](https://github.com/vakaobr/iac-cartographer/issues/37)) ([084b3a9](https://github.com/vakaobr/iac-cartographer/commit/084b3a92b54c52e2ff9a22a20432545f0e7343ed))
* **notifications:** multi-channel dispatcher + back-compat Slack ([#35](https://github.com/vakaobr/iac-cartographer/issues/35)) ([ebe82e5](https://github.com/vakaobr/iac-cartographer/commit/ebe82e54c46d25c1f6b3bd1c375997a9c84b197c))
* **notifications:** PagerDuty + Opsgenie escalation channels ([#38](https://github.com/vakaobr/iac-cartographer/issues/38)) ([e76dd37](https://github.com/vakaobr/iac-cartographer/commit/e76dd37b876c9f58074ed8b0d047315acf98d92f))
* **notifications:** webhook-family channels (generic / slack_webhook / teams) ([#36](https://github.com/vakaobr/iac-cartographer/issues/36)) ([de20589](https://github.com/vakaobr/iac-cartographer/commit/de20589ccc3576a84da8b74abd7ec7ef4952ce87))
* **publishers:** GitHub Wiki publisher backend ([#42](https://github.com/vakaobr/iac-cartographer/issues/42)) ([b829ea5](https://github.com/vakaobr/iac-cartographer/commit/b829ea5d5311a7650c1f064790a3bf8085a0c1f3))
* **publishers:** machine-readable JSON publisher ([#21](https://github.com/vakaobr/iac-cartographer/issues/21)) ([8ff266e](https://github.com/vakaobr/iac-cartographer/commit/8ff266eb673a3c5d1894ec85154c85131e4fbfd2))
* **publishers:** Notion publisher backend ([#41](https://github.com/vakaobr/iac-cartographer/issues/41)) ([44171eb](https://github.com/vakaobr/iac-cartographer/commit/44171eb16e83d043ab6cfe7f368846f759949de9))
* **publishers:** pluggable publisher backend — Confluence + local Markdown ([#9](https://github.com/vakaobr/iac-cartographer/issues/9)) ([a2cc3bf](https://github.com/vakaobr/iac-cartographer/commit/a2cc3bf253bba9160b475e27cf06ad8fdf67e950))
* **publishers:** standalone HTML publisher (embedded CSS, no JS) ([#18](https://github.com/vakaobr/iac-cartographer/issues/18)) ([b6c3ffa](https://github.com/vakaobr/iac-cartographer/commit/b6c3ffa7cd85ad2e5fbaf53ac5da4298fc64c43c))
* **secrets:** pluggable secrets backend — env vars + dotenv + Vault ([#12](https://github.com/vakaobr/iac-cartographer/issues/12)) ([2e4d000](https://github.com/vakaobr/iac-cartographer/commit/2e4d0004d14cd935f695dc596d302b3f595fc302))


### Fixed

* **ci:** release-ghcr.yml needs contents:write to attach SBOM to release ([#54](https://github.com/vakaobr/iac-cartographer/issues/54)) ([6f1a356](https://github.com/vakaobr/iac-cartographer/commit/6f1a356a41e31cb8bf18d2266977a960b284ba69))


### Documentation

* Docs:  ([7237a91](https://github.com/vakaobr/iac-cartographer/commit/7237a91ffd00cc389dce829f4428a797a53f2d3c))
* Docs:  ([e5a1a8f](https://github.com/vakaobr/iac-cartographer/commit/e5a1a8f3529a2014347323a66c2004932f81f9b3))
* Docs:  ([b829ea5](https://github.com/vakaobr/iac-cartographer/commit/b829ea5d5311a7650c1f064790a3bf8085a0c1f3))
* Docs:  ([8ffc894](https://github.com/vakaobr/iac-cartographer/commit/8ffc894ba3445754d4cb55daa0d156c0768de12c))
* custom domain + optimized banner (PNG + WebP fallback) ([#23](https://github.com/vakaobr/iac-cartographer/issues/23)) ([dc14241](https://github.com/vakaobr/iac-cartographer/commit/dc1424165a6d8e47f4a81b4faae1069617ea7a01))
* **demo:** add zero-credentials clone-and-run demo ([#20](https://github.com/vakaobr/iac-cartographer/issues/20)) ([c873882](https://github.com/vakaobr/iac-cartographer/commit/c8738824c3dc64531012a09535f9da190adc7f7c))
* **examples:** new docs/examples/ section + docker-compose deployment ([#25](https://github.com/vakaobr/iac-cartographer/issues/25)) ([ea78967](https://github.com/vakaobr/iac-cartographer/commit/ea789679d1f3da49c4df87eb277bab5ad6ca4df3))
* mkdocs-material site + GitHub Pages workflow + banner ([#22](https://github.com/vakaobr/iac-cartographer/issues/22)) ([1554b4a](https://github.com/vakaobr/iac-cartographer/commit/1554b4a6c4b1637278d0f44b192acb215af3afc6))
* **readme:** refresh for pluggable backend reality ([#34](https://github.com/vakaobr/iac-cartographer/issues/34)) ([fea4a64](https://github.com/vakaobr/iac-cartographer/commit/fea4a646089ae367cad010f04413ff3e2ad481a7))
* refresh after notifications + Notion / GitHub Wiki / Gitea / AWS Terraform additions ([#47](https://github.com/vakaobr/iac-cartographer/issues/47)) ([b74d4d6](https://github.com/vakaobr/iac-cartographer/commit/b74d4d6078a3fb49980a76d018e83938d9c9e4d1))
* refresh Roadmap section — split Shipped + Coming next ([#24](https://github.com/vakaobr/iac-cartographer/issues/24)) ([f325cea](https://github.com/vakaobr/iac-cartographer/commit/f325cead89e576ef234fd06506d4af202677f960))
* **runtime:** K8s CronJob + GitHub Actions + cron deploy snippets ([#13](https://github.com/vakaobr/iac-cartographer/issues/13)) ([1f5b268](https://github.com/vakaobr/iac-cartographer/commit/1f5b268dda55c868cce81685f8cab6861a8ecdca))
* versioned docs site via mike + GitHub Pages branch deploy ([#46](https://github.com/vakaobr/iac-cartographer/issues/46)) ([fd34a49](https://github.com/vakaobr/iac-cartographer/commit/fd34a49432b9250df67f79fcc9b434a913becf85))


### Security

* hardening posture — SECURITY.md, reference doc, gitleaks + pip-audit CI ([#49](https://github.com/vakaobr/iac-cartographer/issues/49)) ([87d2da3](https://github.com/vakaobr/iac-cartographer/commit/87d2da3574543cbd037ec1c1a2d2fd034ba42cbb))

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
