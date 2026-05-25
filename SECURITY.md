# Security policy

## Supported versions

iac-cartographer is at `v0.1.x` — pre-1.0, no formal LTS branches. The
`main` branch is the only supported version; please run a recent commit.

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
  issues. Lower-severity issues may take longer; we'll keep you in the loop.
* Credit in the release notes for the fix (unless you ask to remain
  anonymous).

## Threat model

The current threat model assumes:

* **The pipeline is run inside trusted infrastructure** (your AWS account,
  K8s cluster, etc.). Anyone who can invoke the binary already has access
  to the secrets it reads.
* **The repositories being scanned are at least partially trusted.** We do
  apply defense-in-depth against indirect prompt injection from repo
  contents (XML-wrapped prompt context, Pydantic-validated LLM output,
  trigger-phrase watchlist, no tool-use, read-only blast radius), but a
  determined adversary with commit access to a scanned repo could still
  influence the Confluence narrative for that one repo.
* **The Confluence destination is a controlled space** — pages are
  rewritten on every run, so a stale or hostile narrative is overwritten
  by the next scheduled firing (worst case: one week of bad documentation).

Out of scope:

* Confluence-side ACL bypasses (Atlassian's responsibility).
* AWS IAM mis-configurations on the deployment account (operator's
  responsibility).
* Vulnerabilities in transitive dependencies (file these directly with
  the upstream project; we'll bump as soon as a patched version lands).
