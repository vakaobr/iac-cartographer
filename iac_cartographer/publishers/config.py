"""Publisher subsystem config + credential models.

Per-publisher config models (one per `PublisherConfig.kind`), the
`PublisherConfig` selector, and the publisher credential models live here,
beside the publisher implementations that consume them.

Re-exported from `iac_cartographer.models` for back-compat.
"""

from __future__ import annotations

from typing import Literal

from iac_cartographer.models import _Strict


class PublisherConfig(_Strict):
    """Selects WHERE the inventory gets published.

    Most fields are backend-specific and ignored when `kind` doesn't match.
    Adding a new publisher means:
      * Add a literal to the `kind` discriminator.
      * Add an `Publisher` subclass in `publishers/`.
      * Wire it in the cli's `_build_publisher` helper.
    """

    # Which publisher to use.
    #   "confluence" → publish ADF pages to Atlassian Confluence Cloud.
    #                  Uses `confluence:` config + the
    #                  `iac-cartographer/confluence` secret. Default.
    #   "markdown"   → write Markdown files to a local directory. Uses
    #                  the `markdown:` config. No credentials needed.
    #   "html"       → write self-contained HTML files (embedded CSS, no
    #                  external dependencies) to a local directory. Uses
    #                  the `html:` config. No credentials needed. Designed
    #                  for snapshots, S3/CloudFront hosting, audit PDFs.
    #   "json"       → write machine-readable JSON files to a local
    #                  directory. Uses the `json:` config. Designed as a
    #                  feed for Backstage catalogs, internal CMDBs,
    #                  dashboards, and custom drift-detection tooling.
    #   "notion"     → publish each repo as a Notion sub-page. Uses
    #                  the `notion:` config + the
    #                  `iac-cartographer/notion` secret. Requires
    #                  `pip install iac-cartographer[notion]`.
    #   "github_wiki" → git-push Markdown files to a repo's GitHub Wiki.
    #                   Uses the `github_wiki:` config + the existing
    #                   `iac-cartographer/github` secret (same token
    #                   that powers GitHub discovery).
    kind: Literal["confluence", "markdown", "html", "json", "notion", "github_wiki"] = "confluence"


class ConfluenceConfig(_Strict):
    # Atlassian Cloud site without protocol or trailing slash.
    # Example: "your-org.atlassian.net". The placeholder is obviously invalid
    # in production — Confluence requests will fail loudly with DNS errors
    # if left unset — but it keeps the model validatable for tests/dry-runs.
    site: str = "your-org.atlassian.net"
    # Confluence space key (e.g. "DOCS", "Engineering"). The parent page must
    # already exist in this space; iac-cartographer publishes child pages under it.
    space_key: str = "DOCS"
    # Logical name of the parameter holding the parent page's numeric ID
    # as a plain string. Resolved via the configured `SecretsProvider`
    # (`get_parameter()`):
    #   * AWS:   SSM Parameter Store path — same as the original
    #            behaviour (`/iac-cartographer/confluence-parent-id`).
    #   * env:   env var `IAC_CARTOGRAPHER_PARAM_CONFLUENCE_PARENT_ID`.
    #   * vault: `{mount}/data/iac-cartographer/confluence-parent-id`
    #            with the page ID stored under a `value` field.
    # The parent page is the overview; child pages live under it.
    parent_page_id_ssm_path: str = "/iac-cartographer/confluence-parent-id"

    # Optional direct override. When set, the page ID is taken verbatim
    # from here and `parent_page_id_ssm_path` is ignored. Use for
    # deployments where storing a non-secret integer ID in an external
    # parameter store is overkill (small teams, file-based config, etc.).
    parent_page_id: str | None = None


class MarkdownConfig(_Strict):
    """`publisher.kind == "markdown"` settings.

    Output layout under `output_dir`:

        output_dir/
        ├── index.md
        └── repos/
            └── <full_name_slugged>.md
    """

    output_dir: str = "./iac-inventory"


class HtmlConfig(_Strict):
    """`publisher.kind == "html"` settings.

    Output layout under `output_dir`:

        output_dir/
        ├── index.html
        └── repos/
            └── <full_name_slugged>.html

    Each file is self-contained (embedded CSS, no JS, no external fonts)
    so it works opened directly from disk, mailed as an attachment, or
    uploaded to S3 + CloudFront / GitHub Pages without a build step.
    """

    output_dir: str = "./iac-inventory-html"


class JsonConfig(_Strict):
    """`publisher.kind == "json"` settings.

    Output layout under `output_dir`:

        output_dir/
        ├── index.json
        └── repos/
            └── <full_name_slugged>.json

    The overview (`index.json`) is suitable as a feed for Backstage
    catalog imports, internal CMDBs, or dashboards — it includes a row
    per repo with key metadata + aggregate counts. The per-repo files
    carry the full `RepoInventory` payload (providers, modules,
    resources, inputs, outputs, narrative).

    Top-level `iac_cartographer.sha` field carries the banner-SHA so
    the publisher's idempotent-republish short-circuit works the same
    way as the Markdown / HTML / Confluence publishers.
    """

    output_dir: str = "./iac-inventory-json"


class NotionConfig(_Strict):
    """`publisher.kind == "notion"` settings.

    Layout: each repo becomes a sub-page of the configured parent
    Notion page; an "Overview" sub-page carries the aggregate summary.
    The banner-SHA is embedded in a 🔖 callout block at the top of
    every page (operator-visible, idempotency anchor).

    Operator pre-creates a Notion page, shares it with an internal
    integration via the page's Connections menu, and sets the
    integration token in the `iac-cartographer/notion` secret as
    `{"integration_token": "secret_..."}`.

    Requires `pip install 'iac-cartographer[notion]'` — the
    `notion-client` SDK is lazy-imported on first publish.
    """

    # UUID of the parent Notion page (the one the operator pre-creates
    # + shares with the integration). Strip dashes or keep them — both
    # forms are accepted by the Notion API.
    parent_page_id: str = ""


class GitHubWikiConfig(_Strict):
    """`publisher.kind == "github_wiki"` settings.

    Each repo is rendered as a Markdown page in the target repo's
    GitHub Wiki; `Home.md` carries the overview. The publisher
    clones the wiki repo to a temp dir, rewrites Markdown files
    in-place, then `git commit` + `git push` once at the end.
    SHA-in-HTML-comment idempotency at the top of each `.md` file
    means unchanged repos skip the disk write entirely.

    Reuses the existing `iac-cartographer/github` secret — operators
    using GitHub discovery already have the token configured. The
    token needs `public_repo` for public targets or `repo` for
    private targets; the wiki inherits the repo's collaborator
    permissions automatically.

    Pre-requisite: the wiki must exist. GitHub creates the
    `<owner>/<repo>.wiki.git` backing repo only when the first wiki
    page is created via the UI — visit
    `github.com/<owner>/<repo>/wiki` and create a placeholder page
    before pointing iac-cartographer at it.
    """

    # GitHub repo owner (org or user).
    owner: str = ""
    # GitHub repo name (without the `.wiki` suffix — we append that).
    repo: str = ""
    # Author identity on each commit. Override for service-account
    # deployments (e.g. `github-actions[bot]@users.noreply.github.com`).
    commit_author_name: str = "iac-cartographer"
    commit_author_email: str = "iac-cartographer@noreply"


# ─── Publisher credentials (one model per Secrets Manager entry) ───────────


class ConfluenceCredentials(_Strict):
    email: str
    api_token: str


class NotionCredentials(_Strict):
    """Notion integration token — `iac-cartographer/notion` secret.

    Create an internal integration at notion.so/profile/integrations
    → "+ New integration" → internal type → copy the secret. The
    integration's permissions are governed by which pages the
    operator shares with it (Connections menu on each page) — start
    by sharing the configured `notion.parent_page_id`.
    """

    integration_token: str
