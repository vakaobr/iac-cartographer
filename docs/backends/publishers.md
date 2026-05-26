# Publishers

The publisher decides **where** the inventory ends up. Pick with
`publisher.kind`.

| Backend | When to use |
|---|---|
| `confluence` *(default)* | You already have Confluence; you want the inventory cross-linked with the rest of your wiki. |
| `markdown` | You run a static-site generator (mkdocs / Hugo / Docusaurus / Jekyll) and want to feed the rendered Markdown into its build. Or you commit the output to a docs repo so PRs show diffs. |
| `html` | You want **self-contained HTML files** with no build step — open in a browser, zip-and-email to a stakeholder, upload to S3 + CloudFront / GitHub Pages, print to PDF for an audit. Embedded CSS, no JS, no external fonts. |
| `json` | You want a **machine-readable feed** for Backstage catalog imports, internal CMDBs, dashboards, or custom drift-detection tooling. `index.json` carries one row per repo + aggregates; per-repo files carry the full inventory. |

All four use the same banner-SHA idempotency contract: on the next run
we compare the embedded SHA against the freshly-computed value and skip
the write when they match. Repos that change get rewritten; repos that
don't, don't.

## Confluence

```yaml
publisher:
  kind: confluence

confluence:
  site: "acme.atlassian.net"
  space_key: "DOCS"
  parent_page_id: "123456789"
  # or, for AWS deployments that store the ID in SSM:
  # parent_page_id_ssm_path: "/iac-cartographer/confluence-parent-id"
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
