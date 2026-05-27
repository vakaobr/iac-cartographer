"""Discovery subsystem config + credential models.

Co-located with the discovery sources so the "one concern, one package"
rule holds: the config that shapes discovery, and the credentials each
source needs, live next to the sources that consume them.

Re-exported from `iac_cartographer.models` for back-compat — existing
`from iac_cartographer.models import DiscoveryConfig` import sites keep
working unchanged.
"""

from __future__ import annotations

from pydantic import Field, model_validator

from iac_cartographer.models import _Strict


class DiscoveryConfig(_Strict):
    # GitLab group IDs whose subtree (incl. subgroups) should be scanned for
    # `*.tf` files. Empty list = skip GitLab.
    gitlab_group_ids: list[int] = Field(default_factory=list)
    # GitHub organisations to scan via `code search`. Empty list = skip GitHub.
    github_orgs: list[str] = Field(default_factory=list)
    # Bitbucket Cloud workspaces to enumerate. Empty list = skip Bitbucket.
    # The source lists every repo in the workspace (Bitbucket's public API
    # has no `extension:tf`-style filter on free plans) — combine with
    # `deny_repos` to narrow the scope.
    bitbucket_workspaces: list[str] = Field(default_factory=list)
    # Gitea / Forgejo organisations to enumerate. Empty list = skip.
    # Same source covers both platforms (Forgejo preserves Gitea API
    # compatibility). Like Bitbucket, the source lists every repo in
    # each org and lets the extractor filter — Gitea's code-search API
    # is per-repo only and many self-hosted instances disable the
    # indexer entirely, so org-wide enumeration is the portable path.
    gitea_orgs: list[str] = Field(default_factory=list)
    # Gitea / Forgejo base URL. REQUIRED when `gitea_orgs` is non-empty
    # — Gitea has no hosted-default-URL like GitHub or Bitbucket; every
    # deployment is self-hosted at a different domain.
    gitea_base_url: str = ""
    # Optional path to a YAML/JSON file containing a hand-curated list of
    # `RepoMetadata` records. Loaded as an additional `DiscoverySource`;
    # combine with the VCS-host fields or use standalone for air-gapped
    # runs. See `iac_cartographer/discovery/file.py` for the schema.
    repos_file: str | None = None
    # Glob patterns (against full_name) to exclude from publishing — e.g.
    # `*-archived`, `examples/*`, `vendor-*`.
    deny_repos: list[str] = Field(default_factory=list)
    # Optional override for the owning-team guess: full_name → team string.
    # Useful when team mapping isn't trivially derivable from the repo path.
    owner_overrides: dict[str, str] = Field(default_factory=dict)
    # Self-hosted GitLab base URL (without `/api/v4` suffix). Override to point
    # at gitlab.example.com; defaults to gitlab.com.
    gitlab_base_url: str = "https://gitlab.com"


# ─── Discovery credentials (one model per Secrets Manager entry) ───────────


class GitlabCredentials(_Strict):
    token: str


class GithubCredentials(_Strict):
    token: str


class GiteaCredentials(_Strict):
    """Gitea / Forgejo personal-access token — `iac-cartographer/gitea` secret.

    Generate at `<base_url>/-/user/settings/applications` →
    Generate New Token. Scopes needed: `read:organization` +
    `read:repository`. The same token works for the listing API
    (discovery) and the clone path (fetcher).
    """

    token: str


class BitbucketCredentials(_Strict):
    """Bitbucket Cloud credentials. Set EITHER `access_token` (recommended —
    workspace access tokens are scoped to one workspace) OR `username` +
    `app_password` (legacy form, still widely used).

    The model_validator below enforces the XOR so misconfigured secrets
    surface at load time instead of as a 401 mid-pipeline."""

    access_token: str | None = None
    username: str | None = None
    app_password: str | None = None

    @model_validator(mode="after")
    def _exactly_one_auth_mode(self) -> BitbucketCredentials:
        has_token = self.access_token is not None
        has_basic = self.username is not None and self.app_password is not None
        if has_token == has_basic:
            raise ValueError(
                "BitbucketCredentials: set EITHER access_token OR (username + app_password), not both/neither"
            )
        # If basic is partially set (only one of the two), surface it clearly.
        if not has_token and (self.username is None) != (self.app_password is None):
            raise ValueError("BitbucketCredentials: username and app_password must be set together")
        return self
