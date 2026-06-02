"""`iac-cartographer init` — first-time-setup scaffolder.

Writes a minimal, validated `config.yaml` tailored to the operator's chosen
backends, plus an optional `.env` template for the `env` secrets backend.
The output is opinionated about defaults but every field is annotated so
the operator knows what to edit.

The generated config is always run through `AppConfig.model_validate` before
being written to disk, so we can't ship a scaffold that doesn't parse — a
schema drift in a future release would fail the init command instead of
producing a broken-on-first-run config.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

import yaml

from iac_cartographer.constants import CartographerError
from iac_cartographer.models import AppConfig

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger("iac_cartographer.init_scaffold")


SecretsBackend = Literal["aws", "env", "vault"]
PublisherKind = Literal["confluence", "markdown"]
LLMBackend = Literal["bedrock", "anthropic"]


class InitError(CartographerError):
    """Raised when the scaffolder refuses to proceed (target file exists, …)."""


def write_scaffold(
    *,
    config_path: Path,
    env_path: Path | None,
    secrets_backend: SecretsBackend = "env",
    publisher_kind: PublisherKind = "markdown",
    llm_backend: LLMBackend = "anthropic",
    force: bool = False,
) -> list[Path]:
    """Write a starter config (and optional `.env`) to disk.

    Returns the list of files actually written. Raises `InitError` if a
    target file exists and `force` is False.
    """
    written: list[Path] = []

    config_text = _render_config(
        secrets_backend=secrets_backend,
        publisher_kind=publisher_kind,
        llm_backend=llm_backend,
    )

    # Self-test: parse what we just generated through the real schema.
    # A future field renamed without updating this scaffolder would fail
    # right here instead of producing a config that breaks first-run.
    parsed = yaml.safe_load(config_text) or {}
    try:
        AppConfig.model_validate(parsed)
    except Exception as exc:  # pragma: no cover — guard against future drift
        raise InitError(
            f"iac-cartographer init produced a config that doesn't validate "
            f"against AppConfig — this is a bug in the scaffolder: {exc}"
        ) from exc

    _write_if_safe(config_path, config_text, force=force)
    written.append(config_path)

    if secrets_backend == "env" and env_path is not None:
        env_text = _render_env_template(llm_backend=llm_backend)
        _write_if_safe(env_path, env_text, force=force)
        # Restrict to owner-only — this file holds placeholders for tokens.
        env_path.chmod(0o600)
        written.append(env_path)

    return written


def print_next_steps(
    written: list[Path],
    *,
    secrets_backend: SecretsBackend,
    publisher_kind: PublisherKind,
) -> None:
    """Emit a human-readable summary + suggested next commands."""
    print()
    print("Scaffold written:")
    for p in written:
        print(f"  • {p}")
    print()
    print("Next steps:")
    print()
    print(f"  1. Edit {written[0]} — fill in the discovery scope, Confluence")
    print("     space, and publisher settings (every placeholder is marked")
    print("     `REPLACE_ME-...`).")
    print()
    if secrets_backend == "env" and len(written) > 1:
        print(f"  2. Edit {written[1]} — paste your real credentials over the")
        print("     `REPLACE_ME-...` placeholders. The file is mode 600.")
        print(f"     Then source it before the run: `set -a; . {written[1]}; set +a`")
        print()
        print(f"  3. Dry-run: iac-cartographer --once --dry-run --config {written[0]}")
    elif secrets_backend == "aws":
        print("  2. Seed AWS Secrets Manager + SSM Parameter Store with the")
        print("     credentials listed in the README §'Seed credentials' table.")
        print()
        print(f"  3. Dry-run: iac-cartographer --once --dry-run --config {written[0]}")
    elif secrets_backend == "vault":
        print("  2. Set VAULT_TOKEN in your environment, then write the same")
        print("     credentials into Vault KV v2:")
        print("        vault kv put secret/iac-cartographer/gitlab token=glpat-...")
        print()
        print(f"  3. Dry-run: iac-cartographer --once --dry-run --config {written[0]}")
    print()
    if publisher_kind == "confluence":
        print("  4. When dry-run looks good, drop `--dry-run` to publish for real.")
    else:
        print("  4. When dry-run looks good, drop `--dry-run` to write Markdown files.")
    print()


# ─── internals ─────────────────────────────────────────────────────────────


def _write_if_safe(path: Path, text: str, *, force: bool) -> None:
    if path.exists() and not force:
        raise InitError(f"refusing to overwrite existing file: {path} (pass --force to override)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    logger.info("init: wrote %s (%d bytes)", path, len(text))


def _render_config(
    *,
    secrets_backend: SecretsBackend,
    publisher_kind: PublisherKind,
    llm_backend: LLMBackend,
) -> str:
    """Hand-rolled YAML so we get keyed sections + comments in the output.

    PyYAML's serializer would strip comments and reorder keys — not what we
    want for a scaffold the operator will read top-to-bottom and edit."""
    lines: list[str] = [
        "# iac-cartographer — config generated by `iac-cartographer init`.",
        "#",
        "# Every `REPLACE_ME-...` value is a placeholder you MUST edit before",
        "# the first non-dry-run. See examples/config.example.yaml for the full",
        "# annotated reference of every available field.",
        "",
        _render_discovery_section(),
        "",
        _render_secrets_section(secrets_backend),
        "",
        _render_llm_section(llm_backend),
        "",
        _render_publisher_section(publisher_kind),
        "",
        _render_slack_section(),
    ]
    return "\n".join(lines).rstrip() + "\n"


def _render_discovery_section() -> str:
    return 
        "discovery:\n"
        "  # At least one source must be configured. Empty values skip that\n"
        "  # backend entirely. See `examples/config.example.yaml` for Bitbucket\n"
        "  # and curated-file sources.\n"
        "  gitlab_group_ids: []      # e.g. [15, 42]\n"
        '  github_orgs: []           # e.g. ["acme-org"]\n'
        '  deny_repos: []            # glob patterns to skip, e.g. "*-archived"\n'
    


def _render_secrets_section(backend: SecretsBackend) -> str:
    if backend == "aws":
        return 'secrets:\n  backend: "aws"\n  aws_region: "eu-central-1"   # boto3 region for Secrets Manager + SSM\n'
    if backend == "env":
        return 
            "secrets:\n"
            '  backend: "env"\n'
            "  # Credentials read from IAC_CARTOGRAPHER_SECRET_<NAME> env vars.\n"
            "  # The generated .env file at this path is loaded before the run.\n"
            '  env_dotenv_path: "./iac-cartographer.env"\n'
        
    if backend == "vault":
        return 
            "secrets:\n"
            '  backend: "vault"\n'
            '  vault_addr: "REPLACE_ME-https://vault.example.com"\n'
            '  vault_mount: "secret"\n'
            '  vault_path_prefix: "iac-cartographer/"\n'
            "  # Auth uses the VAULT_TOKEN env var — set it before running.\n"
        
    # pragma: no cover — Literal exhausts the choices
    raise InitError(f"unknown secrets backend: {backend!r}")


def _render_llm_section(backend: LLMBackend) -> str:
    if backend == "bedrock":
        return 
            "llm:\n"
            '  backend: "bedrock"\n'
            '  model_id: "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"\n'
            "  max_tokens: 4096\n"
            '  bedrock_region: "eu-central-1"\n'
        
    if backend == "anthropic":
        return 
            "llm:\n"
            '  backend: "anthropic"\n'
            '  model_id: "claude-sonnet-4-5-20250929"\n'
            "  max_tokens: 4096\n"
            '  anthropic_base_url: "https://api.anthropic.com"\n'
        
    # pragma: no cover
    raise InitError(f"unknown llm backend: {backend!r}")


def _render_publisher_section(kind: PublisherKind) -> str:
    if kind == "confluence":
        return (
            "publisher:\n"
            '  kind: "confluence"\n'
            "\n"
            "confluence:\n"
            '  site: "REPLACE_ME-your-org.atlassian.net"\n'
            '  space_key: "REPLACE_ME-DOCS"\n'
            "  # Set parent_page_id directly OR leave it null and use the\n"
            "  # parameter ref below (resolved via the active secrets backend:\n"
            "  # SSM path / env var / Vault path).\n"
            '  parent_page_id: "REPLACE_ME-123456789"\n'
            '  # parent_page_id_ref: "/iac-cartographer/confluence-parent-id"\n'
        )
    if kind == "markdown":
        return (
            "publisher:\n"
            '  kind: "markdown"\n'
            "\n"
            "markdown:\n"
            "  # Directory the renderer writes <output_dir>/index.md and\n"
            "  # <output_dir>/repos/<slug>.md into. Safe to point at a docs/\n"
            "  # subtree of a static-site generator (mkdocs/Hugo/Docusaurus).\n"
            '  output_dir: "./iac-inventory"\n'
        )
    # pragma: no cover
    raise InitError(f"unknown publisher kind: {kind!r}")


def _render_slack_section() -> str:
    return (
        "slack:\n"
        "  # Set to a real channel to enable run-summary notifications. The\n"
        "  # `iac-cartographer/slack` secret must be loadable in any case (the\n"
        "  # notifier is constructed eagerly); set its bot_token to an empty\n"
        "  # string if you don't intend to use Slack at all.\n"
        '  channel: "#REPLACE_ME-alerts"\n'
    )


def _render_env_template(*, llm_backend: LLMBackend) -> str:
    """Generate the matching `.env` template for the `env` secrets backend."""
    lines = [
        "# iac-cartographer — env template generated by `iac-cartographer init`.",
        "#",
        "# Replace every REPLACE_ME-... with the real value. File mode is 0600.",
        "# Load with: set -a; . ./iac-cartographer.env; set +a",
        "",
        "# Confluence Cloud API credentials (legacy unscoped token; see README).",
        'IAC_CARTOGRAPHER_SECRET_CONFLUENCE=\'{"email":"REPLACE_ME-bot@example.com","api_token":"REPLACE_ME-ATATT..."}\'',
        "",
        "# GitLab + GitHub tokens for discovery + clone.",
        'IAC_CARTOGRAPHER_SECRET_GITLAB=\'{"token":"REPLACE_ME-glpat-..."}\'',
        'IAC_CARTOGRAPHER_SECRET_GITHUB=\'{"token":"REPLACE_ME-ghp_..."}\'',
        "",
        "# Slack bot token for run-summary notifications.",
        'IAC_CARTOGRAPHER_SECRET_SLACK=\'{"bot_token":"REPLACE_ME-xoxb-..."}\'',
        "",
    ]
    if llm_backend == "anthropic":
        lines.extend(
            [
                "# Anthropic API key for `llm.backend: anthropic`.",
                'IAC_CARTOGRAPHER_SECRET_ANTHROPIC=\'{"api_key":"REPLACE_ME-sk-ant-..."}\'',
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"
