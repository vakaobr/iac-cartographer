# Publishers

The publisher decides **where** the inventory ends up. Pick with
`publisher.kind`.

| Backend | When to use |
|---|---|
| `confluence` *(default)* | You already have Confluence; you want the inventory cross-linked with the rest of your wiki. |
| `markdown` | You run a static-site generator (mkdocs / Hugo / Docusaurus / Jekyll) and want to feed the rendered Markdown into its build. Or you commit the output to a docs repo so PRs show diffs. |
| `html` | You want **self-contained HTML files** with no build step — open in a browser, zip-and-email to a stakeholder, upload to S3 + CloudFront / GitHub Pages, print to PDF for an audit. Embedded CSS, no JS unless a resource graph is present (Mermaid CDN). |
| `json` | You want a **machine-readable feed** for Backstage catalog imports, internal CMDBs, dashboards, or custom drift-detection tooling. `index.json` carries one row per repo + aggregates; per-repo files carry the full inventory. |
| `notion` | You already have Notion; you want the inventory as sub-pages under a parent you control. Compact bullet-list style (no tables). |
| `github_wiki` | You want the inventory committed to a repo's wiki — searchable via GitHub's search, accessible to anyone with repo read. |

All publishers use the same banner-SHA idempotency contract: on the next
run we compare the embedded SHA against the freshly-computed value and
skip the write when they match. Repos that change get rewritten; repos
that don't, don't.

## What appears on a rendered child page

The per-repo deep-dive page is the same shape across every publisher
(allowing for format-specific rendering — tables in Confluence /
Markdown / HTML, bullet lists in Notion):

1. **Banner-SHA callout** — idempotency anchor + "auto-generated, do not edit" warning + the timestamp of the run.
2. **Purpose** — one-paragraph LLM narrative explaining what the repo provisions (placeholder when `--no-llm` was passed).
3. **Environments** / **Owning team (guess)** / **Notable patterns** — additional LLM-derived sections when available.
4. **Module layout** — relative paths of every directory containing `*.tf` files (multi-module repos surface their `env/dev` + `env/prod` shape here).
5. **State backend** *(since 1.0)* — backend type (`s3` / `gcs` / `azurerm` / `remote` / `local` / etc.) plus precomputed posture signals: encryption status, KMS / customer-managed key, state locking (DynamoDB / `use_lockfile` / native), auth method. `local` backends surface a `[CRIT]` signal — see the `iac_cartographer.state_backend` module for the full per-type checklist.
6. **Live state** *(since 1.1, optional)* — present only when `live_state.backend != "none"`. Workspace name + URL, current run status, last successful apply timestamp, drift, live resource count (with a `⚠ declared=N` divergence marker when the platform-reported count doesn't match what terraform-docs parsed). Backed by the read-only `iac_cartographer.overlays.live_state` overlay — currently with [Terraform Cloud / HCP / TFE](../reference/configuration.md#live_state) and [Terrakube](terrakube.md) backends. Excluded from the banner-SHA so external state changes don't republish the page on every run.
7. **Providers** / **Modules** / **Resources by type** — structural facts from terraform-docs.
8. **Resource graph** *(since 1.0)* — a Mermaid `graph TD` diagram with provider nodes (stadium shape) and resource nodes (rectangles), grouped by provider via explicit edges. Chunked into multiple diagrams when the resource count exceeds `graph.max_nodes_per_graph` (default 25). Confluence + GitHub-flavoured Markdown render Mermaid natively; the HTML publisher loads the Mermaid CDN bundle in `<head>` only when a graph is present.
9. **Inputs** / **Outputs** — terraform-docs-derived variable + output tables.

The overview page lists every repo with cross-links to its child page,
plus aggregate stats (top providers, total resource count, etc.).

## Confluence

```yaml
publisher:
  kind: confluence

confluence:
  site: "acme.atlassian.net"
  space_key: "DOCS"
  parent_page_id: "123456789"
  # or, for backend-stored references (AWS SSM path / Vault path / env var):
  # parent_page_id_ref: "/iac-cartographer/confluence-parent-id"
  # (pre-1.0 spelling `parent_page_id_ssm_path` still works for the 1.x line —
  #  deprecated; removed in 2.0)
```

Requires the `iac-cartographer/confluence` secret:

```json
{"email": "bot@example.com", "api_token": "ATATT..."}
```

The token must be a **legacy unscoped** API token (the plain "Create
API token" form at id.atlassian.com, not "Create API token with
scopes" — the latter requires an installed OAuth app on the workspace).

The parent page is the inventory's overview; child pages live under
it. You pre-create the parent page once; iac-cartographer updates it on
every run and adds/updates one child per discovered repo.

## Markdown

```yaml
publisher:
  kind: markdown

markdown:
  output_dir: "./iac-inventory"
```

Layout:

```
./iac-inventory/
├── index.md                              # overview / index page
└── repos/
    ├── acme-org__main-cluster.md         # one file per discovered repo
    ├── acme-org__auth-service.md         # full_name slugged with "__"
    └── ...
```

Banner SHA lives in the first line as `<!-- iac-cartographer-sha: <sha> -->`.

Pairs naturally with mkdocs / Hugo / Docusaurus / Jekyll — point
`output_dir` at the static-site generator's source directory.

## HTML

```yaml
publisher:
  kind: html

html:
  output_dir: "./iac-inventory-html"
```

Same layout as Markdown but each file is fully self-contained:

- CSS embedded in a `<style>` block (no external fonts, no `<link>` tags).
- No JavaScript, no external dependencies, no build step.
- Dark mode automatic via CSS `prefers-color-scheme`.
- `@media print` block tightens layout for audit-PDF export.

Banner SHA lives in a `<meta name="iac-cartographer-sha">` tag in the
document head.

## JSON

```yaml
publisher:
  kind: json

json:
  output_dir: "./iac-inventory-json"
```

`index.json` is sized for catalog-import use cases — one row per repo
with summary fields plus `aggregates.{repo_count,total_resources,top_providers}`.
Per-repo files carry the full `RepoInventory` payload (providers,
modules, resources, inputs, outputs, narrative).

Top-level shape:

```json
{
  "iac_cartographer": {
    "schema_version": "1",
    "sha": "abc12345",
    "updated_at": "2026-05-25T...",
    "generator": "iac-cartographer",
    "generator_url": "https://github.com/vakaobr/iac-cartographer"
  },
  "aggregates": { "repo_count": 42, ... },
  "repos": [ {"full_name": "...", ...}, ... ]
}
```

`schema_version` lets consumers pin to a major version and warn on
drift. Additive changes (new optional fields) don't require a bump.

## Notion

Publishes each repo as a sub-page of a configured Notion parent page,
plus an "Overview" sub-page carrying the aggregate summary + links
to every repo's deep-dive page.

```yaml
publisher:
  kind: notion

notion:
  # UUID of the parent Notion page. Operator pre-creates the page and
  # shares it with the integration via the page's Connections menu.
  parent_page_id: "11111111-1111-1111-1111-111111111111"
```

**Requires `pip install 'iac-cartographer[notion]'`** — the official
`notion-client` SDK is lazy-imported on first publish so the base
install doesn't pay for it. If the dep is missing the publisher
raises at `__aenter__` with a clear pip-install hint.

Credentials live in the `iac-cartographer/notion` secret as
`{"integration_token": "secret_..."}`. Create the integration at
[notion.so/profile/integrations](https://www.notion.so/profile/integrations)
→ "+ New integration" → internal type → copy the secret.
**Important:** an integration only sees pages it's been shared with —
open the parent page in Notion, click `…` → Connections → add the
integration.

### Banner-SHA idempotency

The very first block on every page we publish is a 🔖 callout with
plain text `iac-cartographer SHA: <hex>`. The next run reads the
first block, parses the SHA, and short-circuits the rewrite when it
matches — same contract as the Confluence / Markdown / HTML / JSON
publishers, just embedded in a Notion-native carrier (callout vs
ADF version-string vs HTML comment vs JSON field).

### Block-replacement caveat

Notion's API has no "replace page body" operation. Updates go through:

1. List the page's existing block children.
2. Delete each one (archive=True).
3. Append the new blocks.

This means each update sends ~2N HTTP calls (N deletes + N inserts).
For a typical iac-cartographer page (~15 blocks) that's ~30 calls
per update — not free, but acceptable at the once-per-week cadence
the runtime is designed for. The banner-SHA short-circuit means
unchanged pages skip the rewrite entirely.

### Notion-specific quirks

- **Cross-page links** use the relative-URL form `/{page_uuid_no_dashes}`.
  Rendered via rich-text `link` annotations on the overview's bullet
  list.
- **Title is the only built-in property** on regular (page-parent)
  sub-pages. Custom properties exist only when the parent is a
  database — operators who want a "Last Updated" or "Provider count"
  column should switch their parent to a Notion database; that's a
  follow-up the publisher could grow if there's demand.
- **Block content caps at 2000 chars** per rich-text run. The
  renderer truncates aggressively (1900 chars) to stay below the
  cap against pathological narrative outputs.

## GitHub Wiki

Writes the inventory as Markdown files to a repo's GitHub Wiki —
operators who already use GitHub for code + issue tracking get a
zero-extra-platform docs surface, browsable at
`github.com/<owner>/<repo>/wiki`.

```yaml
publisher:
  kind: github_wiki

github_wiki:
  owner: "acme-org"                                # GitHub user / org
  repo: "infrastructure"                           # Repo whose wiki to publish to
  commit_author_name: "iac-cartographer"           # default; override for bot identity
  commit_author_email: "iac-cartographer@noreply"  # default
```

Wiki publishing is **git-based**, not API-based. There is no GitHub
REST API for editing wiki content (that endpoint was deprecated
years ago); the canonical path is to clone the wiki repository at
`<owner>/<repo>.wiki.git`, rewrite the Markdown files in the
working tree, and `git commit` + `git push`. The publisher handles
all of that — operators just need to make sure:

1. The repo's wiki is **enabled** in Settings → Features → Wikis.
2. The wiki has at least one page (visit `/wiki` once and click
   "Create the first page"). Without that, `<repo>.wiki.git`
   doesn't yet exist on the remote and the clone fails.
3. The token in `iac-cartographer/github` has `public_repo` (public
   targets) or `repo` (private targets) — same token + scope the
   GitHub discovery source uses.

### Reusing the `iac-cartographer/github` secret

This publisher does **not** need its own credential entry. The
existing `iac-cartographer/github` secret (created for the GitHub
discovery source) carries the same token format — it's loaded once
at startup and handed to whichever component needs it.

### Layout

```
Home.md                            # GitHub's default wiki landing page
acme-org__main-cluster.md          # one file per discovered repo
acme-org__auth-service.md          # full_name slugged with "__"
…
```

Slashes in `full_name` become `__` in the filename — `acme-org/main-cluster`
→ `acme-org__main-cluster.md`. GitHub Wiki renders the file as a
page titled `acme-org__main-cluster` (clickable from the sidebar).
This matches the local-markdown publisher's slug convention so the
Markdown body is byte-identical between the two.

### Banner-SHA idempotency

Same shape as the local-markdown publisher: an HTML comment at the
top of each file (`<!-- iac-cartographer-sha: <hex> -->`). The
next run reads the file, parses the SHA, and skips the rewrite when
it matches. Unchanged files leave the wiki repo untouched — no
zombie commits with empty diffs.

### Commit behaviour

- All file rewrites happen in the clone's working tree during
  `publish_child` / `publish_overview` calls.
- At `__aexit__`, the publisher runs `git add -A` + checks
  `git diff --cached --quiet`. If the tree matches HEAD (every
  repo was a SHA-match short-circuit), commit + push are skipped
  entirely.
- Otherwise one commit goes out per run, message
  `iac-cartographer: update inventory`. The author identity is
  set per-commit (not via global `git config`), so the host's
  git config stays clean.

### Operator's commit history

Wiki commits are visible in the wiki's git history — visible to
anyone with read access to the repo. This is intentional: it's
auditable evidence of when the inventory last refreshed and what
changed each run. For deployments where this matters, set
`commit_author_name` / `commit_author_email` to a recognisable
bot identity (e.g. `github-actions[bot]@users.noreply.github.com`).

### When to use this vs the local-markdown publisher

| Use this when | Use `markdown` when |
|---|---|
| Your team's docs live on GitHub already; an extra `/wiki` tab is the discoverable home. | You run a static-site generator (mkdocs / Hugo / Docusaurus) and want to feed the rendered output into its build. |
| You want the inventory always-live without running a docs-build CI job. | You want to commit the rendered output to a docs repo and have PRs show diffs before publishing. |
| You're fine with GitHub-hosted Markdown rendering (no custom CSS / theming / search). | You need a custom theme, full-text search, or other capabilities only a real static-site generator offers. |
