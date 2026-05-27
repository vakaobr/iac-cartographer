# Contributing

The full contribution guide lives in
[`CONTRIBUTING.md`](https://github.com/vakaobr/iac-cartographer/blob/main/CONTRIBUTING.md)
at the repo root. Quick highlights below.

## Setup

```bash
git clone https://github.com/vakaobr/iac-cartographer.git
cd iac-cartographer
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Install terraform-docs (pinned to v0.20.0 to match the project default).
brew install terraform-docs  # or download from https://terraform-docs.io
```

## CI gates (replicate locally before pushing)

```bash
ruff check .                                       # lint
ruff format --check .                              # format
pytest --cov=iac_cartographer --cov-fail-under=60  # tests + 60% coverage floor
```

Auto-fix what's fixable:

```bash
ruff check . --fix
ruff format .
```

## Architectural conventions

These are load-bearing patterns. Follow them when adding new features.

- **Pydantic v2 strict mode (`extra="forbid"`)** on every model.
  Schema drift should produce a loud validation error, not a silent
  partial parse.
- **One module per concern.** `cli.py` orchestrates; `aws.py` wraps
  boto3; `confluence.py` is the only file that knows ADF;
  `narrator.py` assembles prompts and validates responses; `llm.py`
  is the only file that talks to the six LLM SDKs. Each notification
  channel + each publisher backend lives in its own file under the
  matching package.
- **Config + credentials live beside their subsystem.** Each
  subsystem keeps its config + credential Pydantic models in a
  `config.py` inside its package (`discovery/config.py`,
  `publishers/config.py`, `secrets/config.py`,
  `notifications/config.py`; the single-module `llm` uses a sibling
  `llm_config.py`). `models.py` holds only the shared domain models +
  the `AppConfig` aggregator and **re-exports** every subsystem config
  so `from iac_cartographer.models import X` still resolves. Add new
  config/credential models to the right `config.py`, not to
  `models.py`.
- **Pure functions where possible.** The renderers are pure ADF /
  Markdown / HTML / JSON assembly. Tests don't need mock-heavy
  scaffolding because the seams are at module boundaries.
- **Idempotency via banner-SHA.** Anything that publishes externally
  compares a content SHA in the artifact's banner against the
  freshly-computed value and short-circuits on match.
- **Per-repo failure isolation.** A bad single repo must never sink
  the whole pipeline. Per-repo errors get captured into a dict and
  surfaced; never `raise` from inside `_process_repo`.
- **No SECRA-specific defaults.** This is an OSS project; any config
  value that mentions a specific org, tenant, or domain in the default
  path is almost certainly wrong.

## Test conventions

- Use `respx` for HTTP-level mocks (httpx). Avoid overlapping mocks on
  the same URL — `respx` matches by param-superset and
  order-of-definition; use `side_effect=[response1, response2]` for
  sequenced responses.
- Use `monkeypatch.setattr("iac_cartographer.<module>.subprocess.run", ...)`
  for shelling out to `terraform-docs` / `git`.
- Use `moto`'s `@mock_aws` (no per-service extras) for AWS clients.
  moto 5+ unified everything under one decorator.
- **No real network calls in tests.** The CI runs offline.

## Pull request checklist

Before opening a PR:

- [ ] `ruff check .` and `ruff format --check .` pass
- [ ] `pytest --cov-fail-under=60` passes locally
- [ ] New behaviour has a test
- [ ] README / examples / docs updated if user-facing config or CLI changed
- [ ] Commit message explains the WHY, not just the WHAT
- [ ] No org-specific identifiers in code or comments

## Adding a new backend

The four pluggable subsystems (discovery, LLM, publisher, secrets) all
follow the same pattern:

1. **Subclass the ABC** in the appropriate subpackage:
   - `DiscoverySource` in `iac_cartographer/discovery/`
   - `LLMBackend` in `iac_cartographer/llm.py`
   - `Publisher` in `iac_cartographer/publishers/`
   - `SecretsProvider` in `iac_cartographer/secrets/`
2. **Add a literal** to the discriminator in that subsystem's
   `config.py` (e.g. `publisher.kind` in `publishers/config.py`; the
   `llm` module uses `llm_config.py`). Add any new credential model
   to the same `config.py` — `models.py` re-exports it automatically.
3. **Add a branch** to the factory function (`_build_publisher` in
   `cli.py`, etc.).
4. **Write tests.** The existing implementations have good test
   coverage as templates.
5. **Document it.** Update `examples/config.example.yaml` + the
   relevant page in `docs/backends/`.

Every other module stays untouched. This is the test of whether the
ABC is doing its job.
