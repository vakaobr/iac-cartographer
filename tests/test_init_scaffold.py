"""Tests for the `iac-cartographer init` scaffolder."""

from __future__ import annotations

import stat
from typing import TYPE_CHECKING

import pytest
import yaml

from iac_cartographer.cli import main as cli_main
from iac_cartographer.init_scaffold import InitError, write_scaffold
from iac_cartographer.models import AppConfig

if TYPE_CHECKING:
    from pathlib import Path


# ─── write_scaffold ────────────────────────────────────────────────────────


def test_scaffold_env_markdown_writes_both_files(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    env = tmp_path / ".env"
    written = write_scaffold(
        config_path=cfg,
        env_path=env,
        secrets_backend="env",
        publisher_kind="markdown",
        llm_backend="anthropic",
    )
    assert cfg in written
    assert env in written
    assert cfg.exists()
    assert env.exists()


def test_scaffold_aws_backend_does_not_write_env_file(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    env = tmp_path / ".env"
    written = write_scaffold(
        config_path=cfg,
        env_path=env,
        secrets_backend="aws",
        publisher_kind="confluence",
        llm_backend="bedrock",
    )
    assert written == [cfg]
    assert not env.exists()


def test_scaffold_vault_backend_does_not_write_env_file(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    env = tmp_path / ".env"
    written = write_scaffold(
        config_path=cfg,
        env_path=env,
        secrets_backend="vault",
        publisher_kind="markdown",
        llm_backend="anthropic",
    )
    assert written == [cfg]
    assert not env.exists()


def test_scaffold_output_validates_against_app_config(tmp_path: Path) -> None:
    """Every combination of backends produces a config that parses through
    `AppConfig.model_validate` cleanly. Future schema drift will fail this
    test before it can ship a broken scaffold."""
    matrix = [
        ("aws", "confluence", "bedrock"),
        ("aws", "markdown", "anthropic"),
        ("env", "confluence", "anthropic"),
        ("env", "markdown", "anthropic"),
        ("vault", "confluence", "bedrock"),
        ("vault", "markdown", "bedrock"),
    ]
    for i, (secrets, publisher, llm) in enumerate(matrix):
        cfg = tmp_path / f"{i}.yaml"
        env = tmp_path / f"{i}.env"
        write_scaffold(
            config_path=cfg,
            env_path=env,
            secrets_backend=secrets,  # type: ignore[arg-type]
            publisher_kind=publisher,  # type: ignore[arg-type]
            llm_backend=llm,  # type: ignore[arg-type]
        )
        parsed = yaml.safe_load(cfg.read_text())
        # AppConfig.model_validate raises on schema mismatch.
        AppConfig.model_validate(parsed)


def test_scaffold_env_file_is_mode_600(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    env = tmp_path / ".env"
    write_scaffold(
        config_path=cfg,
        env_path=env,
        secrets_backend="env",
        publisher_kind="markdown",
        llm_backend="anthropic",
    )
    mode = stat.S_IMODE(env.stat().st_mode)
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_scaffold_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("preexisting content\n", encoding="utf-8")
    with pytest.raises(InitError, match="refusing to overwrite"):
        write_scaffold(
            config_path=cfg,
            env_path=None,
            secrets_backend="aws",
            publisher_kind="confluence",
            llm_backend="bedrock",
        )
    # Preexisting content is preserved.
    assert cfg.read_text() == "preexisting content\n"


def test_scaffold_force_overwrites_existing_file(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("preexisting content\n", encoding="utf-8")
    write_scaffold(
        config_path=cfg,
        env_path=None,
        secrets_backend="aws",
        publisher_kind="confluence",
        llm_backend="bedrock",
        force=True,
    )
    assert "preexisting content" not in cfg.read_text()
    assert "iac-cartographer" in cfg.read_text()


def test_scaffold_creates_parent_dirs(tmp_path: Path) -> None:
    cfg = tmp_path / "deep" / "nested" / "config.yaml"
    write_scaffold(
        config_path=cfg,
        env_path=None,
        secrets_backend="aws",
        publisher_kind="markdown",
        llm_backend="anthropic",
    )
    assert cfg.exists()


def test_scaffold_anthropic_env_file_contains_anthropic_secret(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    env = tmp_path / ".env"
    write_scaffold(
        config_path=cfg,
        env_path=env,
        secrets_backend="env",
        publisher_kind="markdown",
        llm_backend="anthropic",
    )
    assert "IAC_CARTOGRAPHER_SECRET_ANTHROPIC" in env.read_text()


def test_scaffold_bedrock_env_file_omits_anthropic_secret(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    env = tmp_path / ".env"
    write_scaffold(
        config_path=cfg,
        env_path=env,
        secrets_backend="env",
        publisher_kind="confluence",
        llm_backend="bedrock",
    )
    # Bedrock auth is via the AWS credential chain — no env-var secret needed.
    assert "IAC_CARTOGRAPHER_SECRET_ANTHROPIC" not in env.read_text()


# ─── CLI integration ───────────────────────────────────────────────────────


def test_cli_init_writes_files_in_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    rc = cli_main(["--init"])
    assert rc == 0
    assert (tmp_path / "iac-cartographer.config.yaml").exists()
    assert (tmp_path / "iac-cartographer.env").exists()


def test_cli_init_with_custom_paths(tmp_path: Path) -> None:
    cfg = tmp_path / "my-config.yaml"
    env = tmp_path / "my.env"
    rc = cli_main(
        [
            "--init",
            "--config-path",
            str(cfg),
            "--env-path",
            str(env),
            "--secrets-backend",
            "env",
            "--publisher",
            "markdown",
            "--llm",
            "anthropic",
        ]
    )
    assert rc == 0
    assert cfg.exists()
    assert env.exists()


def test_cli_init_refuses_overwrite_returns_2(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("existing\n", encoding="utf-8")
    rc = cli_main(
        ["--init", "--config-path", str(cfg), "--secrets-backend", "aws"],
    )
    assert rc == 2
    assert cfg.read_text() == "existing\n"


def test_cli_init_force_overrides_overwrite_check(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("existing\n", encoding="utf-8")
    rc = cli_main(
        [
            "--init",
            "--config-path",
            str(cfg),
            "--secrets-backend",
            "aws",
            "--force",
        ]
    )
    assert rc == 0
    assert "existing" not in cfg.read_text()


def test_cli_init_and_once_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli_main(["--init", "--once"])
    assert exc_info.value.code == 2  # argparse mutex group violation


def test_cli_init_vault_backend_skips_env_file(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    env = tmp_path / ".env"
    rc = cli_main(
        [
            "--init",
            "--config-path",
            str(cfg),
            "--env-path",
            str(env),
            "--secrets-backend",
            "vault",
        ]
    )
    assert rc == 0
    assert cfg.exists()
    assert not env.exists()
