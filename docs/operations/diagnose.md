# Pre-flight self-test (`--diagnose`)

`iac-cartographer --diagnose` runs a series of offline probes against your
active config and reports a per-component checklist. It's the fastest way
to answer "is this config going to work?" without burning a real run —
and the first thing to reach for when a scheduled run misbehaves.

```bash
iac-cartographer --diagnose --config ./iac-cartographer.config.yaml
```

Example output:

```
iac-cartographer --diagnose
===================================

✓ terraform-docs  v0.20.0
✓ config          ./iac-cartographer.config.yaml
✓ optional-deps   all extras present (notion)
✓ discovery       github (1 org(s)) + gitlab (2 group(s))
✓ llm             bedrock → eu.anthropic.claude-sonnet-4-5-20250929-v1:0
✓ publisher       confluence → acme.atlassian.net
· notifications   no notification channels configured (silent dispatcher)

6 ok, 1 skip
All checks passed. Ready to run --once.
```

## What it checks

| Probe | Verdict logic |
|---|---|
| **terraform-docs** | On PATH? At the pinned `v0.20.0`? A different version warns (the JSON output schema is stable across 0.20.x / 0.24.x, but skew is worth flagging); missing is a hard failure. |
| **config** | The file (or `ssm://` parameter) parses and validates against the `AppConfig` schema. A failure here short-circuits everything below — there's nothing to probe against. |
| **optional-deps** | Every backend your config references has its `pip` extra installed. E.g. `publisher.kind: notion` needs `iac-cartographer[notion]`; `llm.backend: vertex` needs `[gcp]`. Missing extras fail with the exact `pip install` command to fix them. |
| **discovery** | At least one source is configured (the orchestrator otherwise fails mid-run). Gitea orgs without a `gitea_base_url` fail (every Gitea / Forgejo deploy is self-hosted). |
| **llm** | The chosen backend's required fields are populated — `vertex` needs `vertex_project_id`; `azure_openai` needs both `azure_openai_endpoint` and `azure_openai_deployment`. |
| **publisher** | The write target is reachable: Confluence/Notion/GitHub-Wiki required fields are non-placeholder; Markdown/HTML/JSON output directories are writable. |
| **notifications** | At least one channel, or an explicit "silent dispatcher" skip when `notifications:` is empty (legitimate for CI / air-gapped). |

## What it does *not* do

`--diagnose` makes **no live API calls**. It does not:

- authenticate against your LLM provider or estimate token cost,
- fetch the Confluence parent page or Notion page,
- hit your discovery sources' APIs,
- read secrets from Secrets Manager / Vault.

That keeps it fast (sub-second) and side-effect-free — safe to run
anywhere, including CI, without credentials. The deeper "can I actually
reach this backend with these credentials" check happens in the
publisher's own runtime preflight on the first real `--once` run.

## Exit codes

`--diagnose` is CI-friendly — gate a deploy on it:

| Code | Meaning |
|---|---|
| `0` | All checks OK (or OK + skipped). Ready to run. |
| `1` | Warnings only — runs will likely succeed but something's worth fixing (e.g. a `terraform-docs` version skew). |
| `2` | At least one hard failure — fix before running `--once`. |

```yaml
# Gate the scheduled run on a green diagnose in CI:
- run: iac-cartographer --diagnose --config ./config.yaml
- run: iac-cartographer --once --config ./config.yaml
```

The output goes to **stderr**, so it composes cleanly with `2>&1 | tee`
and doesn't get confused with normal program output on stdout.

## When to run it

- **Right after `--init`** — confirm the scaffold + your edits hold together
  before the first real run.
- **After editing config** — catch a typo'd field or a missing extra in a
  second, not after a 90-second run fails at the publish step.
- **When a scheduled run breaks** — the first triage step. `--diagnose`
  turns "it failed somewhere, grep the logs" into "the Gitea base URL is
  empty, fix that one line."
