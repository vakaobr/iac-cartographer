# Contributing to iac-cartographer

Thank you for considering a contribution! This project is small and the bar
for changes is "is this useful to more than one person?" — if the answer is
yes, the contribution is welcome.

## Code of conduct

Participation in this project is governed by the
[Contributor Covenant](CODE_OF_CONDUCT.md). By participating you agree to
uphold it. Please report unacceptable behaviour by emailing
falecom@andersonleite.me.

## Where to start

* **Roadmap items** are in the README under "Roadmap". Issues tagged
  [`good-first-issue`](https://github.com/vakaobr/iac-cartographer/issues?q=is%3Aopen+label%3Agood-first-issue)
  are scoped to land cleanly without deep familiarity with the codebase.
* **Bug reports**: open an issue with reproduction steps. A failing test is
  worth a thousand words.
* **Feature requests**: open an issue describing the use case BEFORE writing
  code. The maintainers may have a different shape in mind, and a chat
  upfront saves you from a big PR that needs reshaping.

## Development setup

```bash
git clone https://github.com/vakaobr/iac-cartographer.git
cd iac-cartographer
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Install terraform-docs (used by the extractor's subprocess call).
# Pin to v0.20.0 to match the project default; bumps need a deliberate test
# pass because terraform-docs's JSON output shape can shift.
brew install terraform-docs  # or download from https://terraform-docs.io
```

## Running checks

The CI runs three gates; replicate locally before pushing:

```bash
ruff check .                                          # lint
ruff format --check .                                 # format
pytest --cov=iac_cartographer --cov-fail-under=60     # tests + 60% coverage
```

Auto-fix what's fixable:

```bash
ruff check . --fix
ruff format .
```

## Architectural conventions

A few load-bearing patterns to follow:

* **Pydantic v2 strict mode (`extra="forbid"`)** on every model. Schema drift
  in upstream responses (terraform-docs, Confluence, GitLab, Bedrock) should
  produce a loud validation error, not a silent partial parse.
* **One module per concern.** `cli.py` orchestrates; `aws.py` wraps boto3;
  `confluence.py` is the only file that knows ADF; `narrator.py` is the only
  one that talks to Bedrock; and so on. Keep new functionality in the module
  whose name matches its concern, or open a new module.
* **Pure functions where possible.** The renderer is pure ADF assembly; the
  extractor takes a path and returns a `TerraformSummary`; tests don't need
  mock-heavy scaffolding because the seams are at module boundaries.
* **Idempotency via banner-SHA.** Anything that publishes externally compares
  a content SHA in the artefact's banner against the freshly-computed one
  and short-circuits on match. See `renderer.compute_sha` +
  `renderer.extract_banner_sha`.
* **Per-repo failure isolation.** A bad single repo must never sink the
  whole pipeline. Exit codes are documented in `cli.py`; per-repo errors get
  captured into a dict and surfaced (don't `raise`).
* **No SECRA-specific defaults.** This is an OSS project; any config value
  that mentions a specific org, tenant, or domain in the default path is
  almost certainly wrong.

## Adding tests

Every PR with code changes should include tests. The 60% coverage gate is a
floor — most modules sit above 90%. Patterns to follow:

* Use `respx` for HTTP-level mocks (httpx). Avoid overlapping mocks on the
  same URL — `respx` matches by param-superset and order-of-definition; use
  `side_effect=[response1, response2]` for sequenced responses.
* Use `monkeypatch.setattr("iac_cartographer.<module>.subprocess.run", ...)`
  for shelling out to `terraform-docs` / `git`.
* Use `moto`'s `@mock_aws` (no per-service extras) for AWS clients. moto 5+
  unified everything under one decorator.
* No real network calls in tests. The CI runs offline.

## Commit messages

Conventional Commits-ish but loose. Lead with a one-line summary in the
imperative ("Add ...", "Fix ...", "Refactor ..."); follow with a paragraph
or two explaining the WHY if it isn't obvious. Reference relevant issues
with `Fixes #123` / `Closes #456` so they auto-close when the PR merges.

## Pull request checklist

Before opening a PR:

- [ ] `ruff check .` and `ruff format --check .` pass
- [ ] `pytest --cov-fail-under=60` passes locally
- [ ] New behaviour has a test
- [ ] README / examples updated if user-facing config or CLI changed
- [ ] Commit message explains the WHY, not just the WHAT
- [ ] No SECRA-specific or other organisation-specific identifiers in code or comments

## Cutting a release (maintainers)

Releases are tag-driven. Two workflows fan out from a single tag push:

1. [`release-pypi.yml`](.github/workflows/release-pypi.yml) — builds the
   sdist + wheel and uploads them to PyPI on every tag matching `v*`,
   then attaches them to the corresponding GitHub Release.
2. [`release-ghcr.yml`](.github/workflows/release-ghcr.yml) — builds the
   container image (multi-arch: `linux/amd64` + `linux/arm64` via
   buildx + QEMU emulation) and pushes it to
   `ghcr.io/vakaobr/iac-cartographer` with a full semver tag fan-out,
   then signs the image with cosign and attaches an SPDX SBOM to the
   GitHub Release.

### PyPI side

Authentication uses **trusted publishing** (OIDC) — no token to manage,
but it requires a one-time setup on the PyPI side:

1. Create the project on PyPI (or claim the name): https://pypi.org/manage/projects/
2. Project settings → Publishing → "Add a new pending publisher"
   * Owner: `vakaobr`
   * Repository: `iac-cartographer`
   * Workflow: `release-pypi.yml`
   * Environment: `release`
3. In the GitHub repo settings, create an Environment named `release` with
   any reviewers / wait-timer / approval rules you want gating the upload.

### Container image side

| Trigger | Tags produced |
|---|---|
| `git push origin v0.2.0` | `:v0.2.0`, `:0.2.0`, `:0.2`, `:0`, `:latest` |
| `git push origin main` | `:main`, `:main-<short-sha>` |
| `workflow_dispatch` | `:dispatch-<short-sha>` |

Pin to `:vX.Y.Z` in production manifests; `:latest` and `:main` are convenience tags.

Authentication uses the built-in `GITHUB_TOKEN` with `packages: write` — no
PAT to manage. Images are signed with **cosign keyless signing** and the
signature is recorded in the Sigstore transparency log; adopters verify with:

```bash
cosign verify ghcr.io/vakaobr/iac-cartographer:v0.2.0 \
  --certificate-identity-regexp '^https://github\.com/vakaobr/iac-cartographer' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com'
```

An SPDX SBOM is also generated by the workflow and attached to the
corresponding GitHub Release (tag pushes only).

### Cutting a release

Releases are cut automatically by
[release-please](https://github.com/googleapis/release-please) from
[Conventional Commits](https://www.conventionalcommits.org/) on `main`.

You do not bump `pyproject.toml` or tag manually. Just write good commit
messages.

**How it works:**

1. Every push to `main` runs `.github/workflows/release-please.yml`.
2. release-please scans commits since the last release. If any are
   `feat:` / `fix:` / `feat!:`, it opens (or updates) a single
   pending PR titled something like
   *"chore(main): release 0.2.0"* that bumps the version + updates
   `CHANGELOG.md`.
3. Review the release PR. When you merge it, release-please creates the
   `vX.Y.Z` tag and the matching GitHub Release.
4. The tag push triggers `release-pypi.yml` (PyPI upload via OIDC) and
   `release-ghcr.yml` (container image + cosign + SBOM).

**Required secret: `RELEASE_PLEASE_TOKEN`.** Fine-grained PAT scoped to
this repo with `contents: read+write`, `pull-requests: read+write`,
`actions: read+write`, and `workflows: read+write`. Without it, the tag
push at step 3 uses the default `GITHUB_TOKEN`, which (by GitHub's
anti-loop design) cannot trigger downstream workflows — and step 4
silently fails to run. Rotate the PAT annually.

**Commit message conventions (enforced by release-please):**

| Prefix | Effect | Appears in CHANGELOG |
|---|---|---|
| `feat:` / `feat(scope):` | Minor bump | Yes — *Added* |
| `fix:` / `fix(scope):` | Patch bump | Yes — *Fixed* |
| `feat!:` / `BREAKING CHANGE:` in body | Major bump | Yes — *Breaking* |
| `perf:` | Patch bump | Yes — *Performance* |
| `deps:` | Patch bump | Yes — *Dependencies* |
| `docs:` | No bump | Yes — *Documentation* |
| `refactor:` | No bump | Yes — *Refactored* |
| `security:` | Patch bump | Yes — *Security* |
| `revert:` | Patch bump | Yes — *Reverted* |
| `ci:` / `chore:` / `build:` / `test:` / `style:` | No bump | No |

Until we hit `1.0.0`, breaking changes bump the minor (release-please's
`bump-minor-pre-major: true`).

**Dry-runs and one-offs:**

* PyPI TestPyPI dry-run: `Actions → Release to PyPI → Run workflow →
  target: test`.
* Force a release PR rebuild after closing one: `Actions → Release Please
  → Run workflow`.

**Hot-fix without release-please** (rare — only if release-please is
broken):

```bash
# Bump pyproject.toml + .release-please-manifest.json manually,
# then tag.
git tag v0.2.1 && git push origin v0.2.1
```

## Roadmap themes (Phase 2)

If you want to take on something larger, these are the in-flight themes
(open an issue first to claim one):

* **Pluggable publishers** — Confluence is hardcoded today. We want a
  `Publisher` ABC with implementations for GitHub Wiki, Notion, and
  local-Markdown.
* **Pluggable LLM backend** — AWS Bedrock is hardcoded. Anthropic direct API,
  OpenAI, and a local Ollama backend are obvious next steps.
* **Pluggable discovery** — GitLab + GitHub today; Bitbucket and a
  `--repos-from-file` source are the most-requested follow-ups.
* **Pluggable secrets/config** — AWS SSM + Secrets Manager today; env vars,
  HashiCorp Vault, and plain dotenv would unblock non-AWS adopters.

Thanks for reading this far. PRs welcome.
