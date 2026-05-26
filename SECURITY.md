# Security policy

## Supported versions

iac-cartographer is at `v0.1.x` — pre-1.0, no formal LTS branches. The
`main` branch is the only supported version; please run a recent commit
or the latest tagged release.

## Reporting a vulnerability

Please report security-sensitive issues **privately** by emailing
**falecom@andersonleite.me** rather than opening a public GitHub issue.

Include in your report:

* A description of the vulnerability and the attack scenario it enables.
* The minimum reproduction steps (or, if responsible, a proof-of-concept).
* The affected version / commit SHA.
* Your assessment of severity.

You can expect:

* An acknowledgement within 7 days.
* A first patch attempt or response within 30 days for High / Critical
  issues. Lower-severity issues may take longer; we'll keep you in the
  loop.
* Credit in the release notes for the fix (unless you ask to remain
  anonymous).

For a deeper walk-through of how each layer of defence works (prompt
injection, transitive deps, supply-chain signing), see
[the security reference page in the docs site](https://iac-cartographer.andersonleite.me/reference/security/)
(or [`docs/reference/security.md`](docs/reference/security.md) in the
repo).

## Threat model (summary)

The full version with mitigations and diagrams lives in the docs site
linked above. This is the one-screen summary.

The pipeline runs inside trusted infrastructure (your AWS account, your
k8s cluster, your CI runner). Anyone with the ability to invoke the
binary already has access to the secrets it reads. The relevant attack
surface is therefore the **outbound** data plane: what we read from
repositories, what we send to the LLM, and what we publish to a
documentation backend.

| Component | Trust assumption | Mitigation |
|---|---|---|
| **Discovery API tokens** (GitLab, GitHub, Bitbucket, Gitea) | Trusted — read-only scope sufficient | Use the narrowest token scope possible; tokens come from the active `SecretsProvider`, never from environment defaults |
| **Repo contents** (Terraform, READMEs) | **Untrusted** — anyone with commit access to a scanned repo can author the content the LLM reads | Layered prompt-injection defence: XML-wrapped prompt context, Pydantic v2 `extra="forbid"` validation, curated AI-H1 trigger-phrase watchlist, no LLM tool use, read-only blast radius (see docs) |
| **LLM provider** (Bedrock, Anthropic, Vertex, Azure OpenAI, OpenAI, Ollama) | Trusted endpoint, untrusted output | All six backends validate JSON output against a strict Pydantic schema; one retry on validation failure; placeholder narrative on persistent failure |
| **Publisher destination** (Confluence, Notion, GitHub Wiki, Markdown, HTML, JSON) | Trusted — pages are rewritten every run | A hostile narrative is overwritten by the next scheduled firing (worst case: one run cycle of bad documentation) |
| **Notification channels** (10 backends — Slack, Teams, email, SNS, PagerDuty, Opsgenie, Discord, webhooks, stdout) | Trusted — credentials come from the secrets provider | Per-channel failure isolation; one broken channel doesn't sink the run |

### Out of scope

* Backend-side ACL bypasses (the responsibility of Atlassian, Notion,
  GitHub, Slack, Microsoft, etc.).
* Cloud IAM misconfigurations on the deployment account (operator's
  responsibility).
* Vulnerabilities in transitive dependencies (file these directly with
  the upstream project; we'll bump as soon as a patched version lands).
  We do run `pip-audit` weekly + on every PR to surface known CVEs.

## Supply chain

| Artefact | Provenance | Verify with |
|---|---|---|
| **PyPI wheel + sdist** (`iac-cartographer`) | OIDC trusted publishing from `release-pypi.yml` — no long-lived API token | `pip download iac-cartographer --no-deps && twine check iac_cartographer-*.{whl,tar.gz}` |
| **Container image** (`ghcr.io/vakaobr/iac-cartographer:vX.Y.Z`) | Built + pushed by `release-ghcr.yml`; cosign keyless signature recorded in the Sigstore transparency log; SPDX SBOM attached to the GitHub Release | See the cosign-verify command below |
| **Source tags** (`vX.Y.Z`) | Created by `release-please` on merge of the release PR; tag points at the version-bump commit | `git verify-tag vX.Y.Z` (after fetching tags) |

Cosign verification of the container image:

```bash
cosign verify ghcr.io/vakaobr/iac-cartographer:v0.1.0 \
  --certificate-identity-regexp '^https://github\.com/vakaobr/iac-cartographer' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com'
```

## Continuous security checks

Run by `.github/workflows/security.yml` on every PR + every push to
`main` + weekly on Mondays at 09:00 UTC:

* **`gitleaks`** — secret scan over the working tree + full history.
  Configured via `.gitleaks.toml`; false positives go in
  `.gitleaksignore`.
* **`pip-audit`** — Python dependency CVE scan against the resolved
  dependency tree (including optional groups: `[dev]`, `[gcp]`,
  `[azure]`, `[openai]`, `[notion]`, `[email]`).
* **CodeQL** (managed separately by GitHub Code Scanning) — semantic
  Python analysis on the same schedule.

Findings are visible under the repo's Security tab; new CVEs fail the
build on PRs so they can't merge without a fix or an explicit waiver in
`.pip-audit.toml`.
