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
