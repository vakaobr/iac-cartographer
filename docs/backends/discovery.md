# Discovery sources

Each non-empty field under `discovery:` activates one source. Sources run
concurrently; the orchestrator dedupes by `RepoMetadata.full_name`
(first-seen wins) and then applies `deny_repos` glob patterns to the
merged result.

At least one source must be configured — the orchestrator fails loud if
none are.

## GitLab

Blob-search `extension:tf` across each group (including subgroups). Works
against gitlab.com and self-hosted instances.

```yaml
discovery:
  gitlab_group_ids: [15, 42]
  gitlab_base_url: "https://gitlab.com"   # omit for the hosted default
```

Requires the `iac-cartographer/gitlab` secret (`{"token": "glpat-..."}`)
when the source is active.

## GitHub

Code-search `extension:tf` across each org. Self-hosted GitHub Enterprise
support is on the roadmap (the `api.github.com` base URL is currently
hardcoded).

```yaml
discovery:
  github_orgs: ["acme-org"]
```

Requires the `iac-cartographer/github` secret (`{"token": "ghp_..."}`).

## Bitbucket Cloud

Enumerate every repository under each configured workspace.

```yaml
discovery:
  bitbucket_workspaces: ["acme"]
```

Bitbucket Cloud's public API has no `extension:tf`-style filter on free
plans — this source lists every repo and lets the per-repo extractor
filter out the ones with no `.tf` files. Combine with `deny_repos` to
narrow large workspaces, or use the file source instead for a
hand-curated list.

Requires the `iac-cartographer/bitbucket` secret. Two auth forms:

=== "Workspace access token (recommended)"

    ```json
    {"access_token": "bbat-..."}
    ```

=== "App password (legacy)"

    ```json
    {"username": "...", "app_password": "..."}
    ```

The credential model enforces exactly-one-of via Pydantic validation, so
misconfiguration fails fast at startup.

## Gitea / Forgejo

Enumerate every repository under each configured org on a self-hosted
Gitea or Forgejo instance.

```yaml
discovery:
  gitea_orgs: ["acme"]
  gitea_base_url: "https://gitea.example.com"
```

One source covers both platforms — Forgejo forked from Gitea in 2022
and intentionally preserves API + auth-scheme compatibility.

Like Bitbucket, the source lists every repo in each org and lets the
per-repo extractor filter out the ones with no `.tf` files. Gitea's
code-search API is per-repo only, and many self-hosted instances
disable the indexer entirely — org-wide enumeration is the portable
path that works on every deployment regardless of indexer config.
Combine with `deny_repos` to narrow large orgs.

`gitea_base_url` is **required** — Gitea has no hosted-default URL
like GitHub or Bitbucket; every deployment is self-hosted at a
different domain.

Requires the `iac-cartographer/gitea` secret:

```json
{"token": "..."}
```

Generate the token at `<base_url>/-/user/settings/applications` →
Generate New Token. Scopes: `read:organization` + `read:repository`.
The same token powers the listing API (discovery) and the clone path
(fetcher splices it into the clone URL).

!!! note "Auth scheme: `token`, not `Bearer`"
    Gitea uses `Authorization: token <pat>` — the most common
    operator-side mistake when porting a config over from GitHub is
    leaving the `Bearer` prefix in place, which Gitea rejects with
    401.

## Curated file

Read a YAML or JSON list of `RepoMetadata` records from disk. No
network calls.

```yaml
discovery:
  repos_file: ./repos.yaml
```

File format (YAML):

```yaml
- host: github                                    # or "gitlab" / "bitbucket" / "other"
  full_name: acme/main-cluster
  clone_url: https://github.com/acme/main-cluster.git
  web_url: https://github.com/acme/main-cluster
  default_branch: main
  last_commit_sha: a1b2c3d4e5f6...
  last_commit_at: 2026-05-22T12:34:56Z
  last_commit_author: Alice <alice@example.com>   # optional
```

Use cases:

- **Air-gapped runs** without VCS API access.
- **Self-hosted VCS** without a first-party source (Gitea, Forgejo,
  Codeberg, …) — bring the metadata yourself.
- **Focused publish** of a curated subset of repos.

`host: "other"` covers anything that isn't one of the three first-party
hosts. The fetcher's `git clone --depth=1` doesn't care about the host
label, so any HTTPS-cloneable URL works.

See [`examples/demo/repos.yaml`](https://github.com/vakaobr/iac-cartographer/blob/main/examples/demo/repos.yaml)
for a working example.

## Filtering

`discovery.deny_repos` applies glob patterns against `full_name` to the
merged-and-deduped result:

```yaml
discovery:
  deny_repos:
    - "acme-org/*-archived"
    - "acme-org/examples-*"
    - "*/sandbox-*"
```

Standard fnmatch syntax — `*`, `?`, `[abc]`, etc.

## Mixing sources

Configure as many sources as you want; they all run concurrently. Useful
patterns:

- **VCS + curated file** — discover most repos automatically, pin
  hand-curated extras (e.g. self-hosted Gitea repos) via the file source.
- **Multiple GitLab groups + a GitHub org** — VCS-mixed environments
  during a migration.
- **File source only** — fully air-gapped, no VCS API access required.
