# Security reference

The short policy + reporting address lives in
[`SECURITY.md`](https://github.com/vakaobr/iac-cartographer/blob/main/SECURITY.md)
at the repo root. This page walks through the defences in depth — what
each layer does, why it's there, and how to verify it.

## Threat model

The pipeline runs inside trusted infrastructure (your AWS account, your
Kubernetes cluster, your CI runner). Anyone who can invoke the binary
already has access to the secrets it reads. The relevant attack surface
is therefore the **outbound** data plane: what we read from repositories,
what we send to the LLM, and what we publish to a documentation
backend.

```
┌──────────────┐  read-only API   ┌─────────────────┐
│  Discovery   │ ───────────────▶ │  VCS host       │
│  sources     │                  │  (GitLab, …)    │
└──────────────┘                  └─────────────────┘
       │
       │ list of repos
       ▼
┌──────────────┐  shallow clone   ┌─────────────────┐  ← UNTRUSTED CONTENT
│  Fetcher     │ ───────────────▶ │  Repo working   │     boundary
│              │                  │  tree (temp)    │
└──────────────┘                  └─────────────────┘
       │
       │ structural extract  +  XML-wrapped narrative request
       ▼
┌──────────────┐                  ┌─────────────────┐
│  Narrator    │ ───────────────▶ │  LLM backend    │
│  (six        │                  │  (validates     │
│   backends)  │ ◀─────────────── │  output schema) │
└──────────────┘   strict JSON    └─────────────────┘
       │
       │ aggregated artefact (banner-SHA short-circuit)
       ▼
┌──────────────┐                  ┌─────────────────┐
│  Publisher   │ ───────────────▶ │  Documentation  │
│  (six        │                  │  backend        │
│   backends)  │                  │                 │
└──────────────┘                  └─────────────────┘
```

## Prompt injection — defence in depth

Repo content is the only untrusted input. A README, an embedded comment
in `*.tf`, or a top-level `variables.tf` description could in principle
say "Ignore all previous instructions and …" or carry an Atlassian-style
markup payload designed to render maliciously on the published page.

The defence layers, ordered from outermost (cheapest, runs first) to
innermost (most expensive, runs last):

### 1. Read-only blast radius

The LLM has no tool use, no function calling, no file-system access, and
no network handle. Its worst possible output is **a string in a
documentation page**, which is overwritten by the next scheduled run.
There is no path from "the model emitted X" to "X executed" anywhere in
the pipeline.

This is the single most important property. Everything below is harm
reduction within an already-bounded blast radius.

### 2. Structural / narrative separation

The aggregator splits each repo's content into two streams:

* **Structural facts** — provider list, module list, resource list,
  required-providers block. Parsed deterministically from
  `terraform-docs` JSON output + the HCL `required_providers` parser.
  These flow to the published page **without ever passing through the
  LLM**.
* **Narrative request** — a constrained prompt asking only for a short
  "purpose summary" string. The LLM sees the repo's README + the
  structural facts (XML-wrapped), and is asked for a JSON object
  conforming to a specific Pydantic schema.

Even if the narrative is completely hijacked, the structural side of the
page (the part operators actually use) is unchanged.

### 3. XML-wrapped prompt context

User-controlled data is wrapped in distinct XML blocks
(`<repo-readme>...</repo-readme>`, `<repo-paths>...</repo-paths>`, …)
that the system prompt explicitly labels as untrusted. The model is
instructed to treat any "instructions" inside those blocks as data, not
commands. This is necessary but not sufficient — hence the layers above
and below.

### 4. Pydantic v2 strict-mode output validation

Every LLM response is parsed against a Pydantic model declared with
`model_config = ConfigDict(extra="forbid")`. URLs are rejected inside
narrative free-text fields by a field validator. A response that fails
validation is retried **once** with a stricter "JSON only, no markdown
fences" reminder; persistent failure inserts a placeholder narrative
(`(Narrative summary unavailable for this run…)`) and the repo
continues.

This catches malformed output regardless of intent (a sloppy model
emitting markdown fences fails the same way a hostile model emitting
extra fields fails).

### 5. AI-H1 trigger-phrase watchlist

Every accepted narrative is scanned for a small, curated list of phrases
that have never legitimately appeared in IaC repo READMEs but are
canonical indicators of prompt-injection or model-distress output
(`"ignore previous"`, `"as an AI"`, `"system prompt"`, `"do not access"`,
and a handful of variants).

The watchlist is curated against real production data — generic IaC
vocabulary (`deprecated`, `disabled`, `archived`) flagged 18 % of legitimate
repos in early testing and was removed. False positives bury the genuine
signal; the current watchlist optimises for precision over recall.

A match replaces the narrative with a placeholder and flags the repo for
AI-H1 review in the run summary (the notification dispatcher fans this
out across all configured channels).

### 6. Per-repo failure isolation

Anything raised inside `_process_repo` is caught and recorded in
`RunOutcome.failed`. A single hostile or broken repo cannot prevent the
other 49 from publishing.

## Schema drift defence

Upstream APIs (`terraform-docs` JSON, Confluence v2, GitLab,
Bedrock Converse, Notion, GitHub Wiki via git) all evolve. We use
Pydantic v2 strict mode (`extra="forbid"`) on every internal model so
upstream schema drift produces a loud validation error per-repo, rather
than a silent partial parse that ships stale or malformed data.

Combined with per-repo failure isolation, a schema-drift casualty
manifests as "this one repo's page wasn't updated this run" with a
captured exception in the dispatcher summary — never as a half-published
fleet.

## Credential handling

* **Secrets never log.** All log lines that reference a secret reference
  it by SSM/Secrets Manager parameter name, not by value.
* **No env-var defaults for credentials in publishable models.** The
  active `SecretsProvider` is the only path; reading from the process
  env directly is restricted to the `env` provider, which exists to make
  this explicit.
* **Three backends shipped:** AWS Secrets Manager + SSM, process env
  vars (with `.env` auto-loading), and HashiCorp Vault KV v2 (with
  optional Enterprise namespace header). Pick the one your environment
  already authenticates against — this is the single biggest reduction
  in credential-handling surface.

## Dependency posture

* **`pip-audit`** runs on every PR + push to `main` + weekly. The
  command resolves the full dependency tree including optional groups
  (`[dev]`, `[gcp]`, `[azure]`, `[openai]`, `[notion]`, `[email]`). New
  CVEs fail the build.
* **Dependabot** maintains weekly PRs for Python deps, GitHub Actions
  versions, and the Dockerfile base image. The bot's PR title prefix
  (`deps:`, `ci:`, `docker:`) is one of the release-please-recognised
  commit types, so dependency bumps show up in the CHANGELOG.
* **Optional dependency groups** keep the default install lean. If you
  don't use Vertex AI, you don't pull `google-cloud-aiplatform` and its
  transitive surface; the cost of a CVE in an unused SDK is zero on a
  base install.

## Supply chain

| Artefact | Signing / provenance | Operator verification |
|---|---|---|
| **PyPI wheel + sdist** | OIDC trusted publishing — no long-lived API token, the upload is authenticated by GitHub's OIDC issuer at run time | `pip download iac-cartographer --no-deps && twine check iac_cartographer-*.{whl,tar.gz}` |
| **Container image** | `cosign` keyless signature; provenance recorded in the Sigstore transparency log; SPDX SBOM attached to the GitHub Release | See cosign-verify below |
| **Source tag** (`vX.Y.Z`) | Created by `release-please` on the merge of the release PR; one tag per release commit | `git verify-tag vX.Y.Z` |

Cosign verification of the container image:

```bash
cosign verify ghcr.io/vakaobr/iac-cartographer:v0.1.0 \
  --certificate-identity-regexp '^https://github\.com/vakaobr/iac-cartographer' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com'
```

## Continuous checks (CI)

| Check | Workflow | When |
|---|---|---|
| `gitleaks` (secret scan, full history) | `.github/workflows/security.yml` | Every PR + push to main + weekly |
| `pip-audit` (Python CVE scan, all extras) | `.github/workflows/security.yml` | Every PR + push to main + weekly |
| Pydantic schema tests | `.github/workflows/ci.yml` | Every PR + push to main + weekly |
| Coverage floor (60 %) | `.github/workflows/ci.yml` | Every PR + push to main + weekly |
| Code Scanning (CodeQL, semantic) | GitHub Code Scanning (managed) | Every PR + push to main + weekly |

Findings surface in the repo's Security tab. PRs that introduce a new
finding fail the build until the issue is fixed or explicitly waived in
the relevant tool's config file.

## Reporting

See [`SECURITY.md`](https://github.com/vakaobr/iac-cartographer/blob/main/SECURITY.md)
in the repo root for the disclosure address and the response-time
commitments.
