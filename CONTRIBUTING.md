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
