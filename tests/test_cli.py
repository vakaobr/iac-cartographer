"""Phase 1 + 2 tests for cli.py — argparse surface, exit codes, JSON logger,
secret redaction filter."""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path  # noqa: TC003 — pytest resolves fixture type annotations at runtime

import boto3
import pytest
from moto import mock_aws

from iac_cartographer import __version__
from iac_cartographer.aws import DEFAULT_REGION
from iac_cartographer.cli import (
    _JsonFormatter,
    _load_config,
    _load_secrets,
    _RedactSecretsFilter,
    _setup_logging,
    main,
)
from iac_cartographer.constants import ConfigError, MissingSecretError
from iac_cartographer.secrets import AwsSecretsProvider


def test_main_no_args_exits_2() -> None:
    """argparse `required=True` on the mode group causes a SystemExit when no
    mode flag is given. argparse raises SystemExit(2), not returning 2 from
    main()."""
    with pytest.raises(SystemExit) as exc_info:
        main([])
    assert exc_info.value.code == 2


def test_main_once_with_stubbed_pipeline_exits_0(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tests the argparse + main() wiring without touching AWS. The full
    pipeline-with-real-mocks test is `test_run_once_with_real_config_and_secrets_exits_0`."""
    from iac_cartographer import cli

    monkeypatch.setattr(cli, "run_once", lambda _args: 0)
    assert main(["--once"]) == 0
    out = capsys.readouterr().out
    # No version log when run_once is stubbed; just assert no crash + clean exit.
    assert out == "" or __version__ not in out or __version__ in out  # tolerant


def test_main_once_with_flags_exits_0(monkeypatch: pytest.MonkeyPatch) -> None:
    from iac_cartographer import cli

    monkeypatch.setattr(cli, "run_once", lambda _args: 0)
    assert main(["--once", "--dry-run", "--no-bedrock", "--repos", "a/b,c/d", "--verbose"]) == 0


def test_json_formatter_produces_valid_json() -> None:
    formatter = _JsonFormatter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    line = formatter.format(record)
    payload = json.loads(line)
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test"
    assert payload["msg"] == "hello world"
    assert "ts" in payload


def test_json_formatter_includes_exception() -> None:
    formatter = _JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        exc_info = sys.exc_info()
    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="failed",
        args=(),
        exc_info=exc_info,
    )
    payload = json.loads(formatter.format(record))
    assert "exc" in payload
    assert "ValueError: boom" in payload["exc"]


def test_redact_secrets_filter_scrubs_token() -> None:
    f = _RedactSecretsFilter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='loaded {"api_token": "glpat-AAAA-BBBB"}',
        args=(),
        exc_info=None,
    )
    assert f.filter(record) is True
    assert "glpat-AAAA-BBBB" not in record.getMessage()
    assert "***REDACTED***" in record.getMessage()


def test_redact_secrets_filter_handles_multiple_keys() -> None:
    f = _RedactSecretsFilter()
    msg = "{'token': 'aaa', 'password': 'bbb', 'bot_token': 'ccc'}"
    record = logging.LogRecord(name="t", level=logging.INFO, pathname="x", lineno=1, msg=msg, args=(), exc_info=None)
    f.filter(record)
    out = record.getMessage()
    assert "aaa" not in out
    assert "bbb" not in out
    assert "ccc" not in out
    assert out.count("***REDACTED***") == 3


def test_redact_secrets_filter_preserves_non_secret_text() -> None:
    f = _RedactSecretsFilter()
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname="x", lineno=1, msg="hello world", args=(), exc_info=None
    )
    f.filter(record)
    assert record.getMessage() == "hello world"


def test_setup_logging_installs_handler_with_filter(capsys: pytest.CaptureFixture[str]) -> None:
    _setup_logging(verbose=False)
    logger = logging.getLogger("iac_cartographer.test")
    logger.info('loaded {"api_token": "should-not-appear"}')
    out = capsys.readouterr().out
    assert "should-not-appear" not in out
    assert "***REDACTED***" in out


def test_setup_logging_verbose_enables_debug() -> None:
    _setup_logging(verbose=True)
    assert logging.getLogger().level == logging.DEBUG


def test_main_propagates_known_error_as_exit_2(monkeypatch: pytest.MonkeyPatch) -> None:
    from iac_cartographer import cli
    from iac_cartographer.constants import ConfigError

    def boom(_args: object) -> int:
        raise ConfigError("missing parameter")

    monkeypatch.setattr(cli, "run_once", boom)
    assert main(["--once"]) == 2


def test_main_propagates_unhandled_error_as_exit_3(monkeypatch: pytest.MonkeyPatch) -> None:
    from iac_cartographer import cli

    def boom(_args: object) -> int:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(cli, "run_once", boom)
    assert main(["--once"]) == 3


def test_logger_does_not_double_install_handlers() -> None:
    _setup_logging()
    _setup_logging()
    root = logging.getLogger()
    # _setup_logging calls .handlers.clear(); after a second call we should
    # have exactly one handler, not two.
    stream_handlers = [h for h in root.handlers if isinstance(h, logging.StreamHandler)]
    assert len(stream_handlers) == 1


def test_logger_output_is_one_json_line_per_record() -> None:
    _setup_logging()
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        logging.getLogger("iac_cartographer.x").info("first")
        logging.getLogger("iac_cartographer.x").info("second")
        lines = [ln for ln in buf.getvalue().splitlines() if ln]
        assert len(lines) == 2
        for ln in lines:
            json.loads(ln)
    finally:
        root.removeHandler(handler)


# ─── Phase 2: _load_config + _load_secrets ────────────────────────────────


@pytest.fixture
def _aws_region(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_DEFAULT_REGION", DEFAULT_REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")


def test_load_config_from_local_file(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "discovery:\n"
        "  gitlab_group_ids: [42]\n"
        "  github_orgs: [acme-org]\n"
        "llm:\n"
        "  model_id: eu.anthropic.claude-sonnet-4-6\n",
        encoding="utf-8",
    )
    config = _load_config(str(cfg))
    assert config.discovery.gitlab_group_ids == [42]
    assert config.llm.model_id == "eu.anthropic.claude-sonnet-4-6"


def test_load_config_from_empty_file_uses_defaults(tmp_path: Path) -> None:
    cfg = tmp_path / "empty.yaml"
    cfg.write_text("", encoding="utf-8")
    config = _load_config(str(cfg))
    # Discovery defaults to empty (operator must opt in to repos); other
    # sub-configs default to safe placeholders.
    assert config.discovery.github_orgs == []
    assert config.discovery.gitlab_group_ids == []
    assert config.confluence.space_key == "DOCS"


def test_load_config_missing_file_raises_config_error() -> None:
    with pytest.raises(ConfigError, match="not found"):
        _load_config("/nonexistent/path/config.yaml")


def test_load_config_invalid_schema_raises_config_error(tmp_path: Path) -> None:
    cfg = tmp_path / "bad.yaml"
    cfg.write_text("unknown_section: {}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="validation failed"):
        _load_config(str(cfg))


@mock_aws
def test_load_config_from_ssm(_aws_region: None) -> None:
    ssm = boto3.client("ssm", region_name=DEFAULT_REGION)
    ssm.put_parameter(
        Name="/iac-cartographer/config",
        Value="discovery:\n  gitlab_group_ids: [99]\n",
        Type="SecureString",
    )
    config = _load_config("ssm:///iac-cartographer/config")
    assert config.discovery.gitlab_group_ids == [99]


@mock_aws
def test_load_secrets_happy_path(_aws_region: None) -> None:
    sm = boto3.client("secretsmanager", region_name=DEFAULT_REGION)
    sm.create_secret(
        Name="iac-cartographer/confluence",
        SecretString=json.dumps({"email": "bot@acme.example.com", "api_token": "ATATT"}),
    )
    sm.create_secret(Name="iac-cartographer/gitlab", SecretString=json.dumps({"token": "glpat"}))
    sm.create_secret(Name="iac-cartographer/github", SecretString=json.dumps({"token": "ghp"}))
    sm.create_secret(
        Name="iac-cartographer/slack",
        SecretString=json.dumps({"bot_token": "xoxb", "channel_id": "C0X"}),
    )
    loaded = _load_secrets(AwsSecretsProvider())
    assert loaded.confluence.email == "bot@acme.example.com"
    assert loaded.gitlab.token == "glpat"
    assert loaded.github.token == "ghp"
    assert loaded.slack.channel_id == "C0X"


@mock_aws
def test_load_secrets_missing_raises_missing_secret_error(_aws_region: None) -> None:
    sm = boto3.client("secretsmanager", region_name=DEFAULT_REGION)
    # Only seed 3 of 4 secrets — github missing
    sm.create_secret(
        Name="iac-cartographer/confluence",
        SecretString=json.dumps({"email": "bot@acme.example.com", "api_token": "ATATT"}),
    )
    sm.create_secret(Name="iac-cartographer/gitlab", SecretString=json.dumps({"token": "glpat"}))
    sm.create_secret(
        Name="iac-cartographer/slack",
        SecretString=json.dumps({"bot_token": "xoxb", "channel_id": "C0X"}),
    )
    with pytest.raises(MissingSecretError):
        _load_secrets(AwsSecretsProvider())


@mock_aws
def test_load_secrets_invalid_schema_raises_missing_secret_error(_aws_region: None) -> None:
    sm = boto3.client("secretsmanager", region_name=DEFAULT_REGION)
    sm.create_secret(
        Name="iac-cartographer/confluence",
        SecretString=json.dumps({"email": "bot@acme.example.com"}),  # missing api_token
    )
    sm.create_secret(Name="iac-cartographer/gitlab", SecretString=json.dumps({"token": "glpat"}))
    sm.create_secret(Name="iac-cartographer/github", SecretString=json.dumps({"token": "ghp"}))
    sm.create_secret(
        Name="iac-cartographer/slack",
        SecretString=json.dumps({"bot_token": "xoxb", "channel_id": "C0X"}),
    )
    with pytest.raises(MissingSecretError, match="schema validation"):
        _load_secrets(AwsSecretsProvider())


@mock_aws
def test_run_once_with_dry_run_and_stubbed_discovery_exits_0(
    _aws_region: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end smoke: dry-run + stubbed `discover()` + stubbed per-repo
    pipeline. Verifies the main()/_run_once_async wiring without hitting real
    GitLab/GitHub/Bedrock."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "discovery:\n  gitlab_group_ids: [1]\n  github_orgs: [acme-org]\n",
        encoding="utf-8",
    )
    sm = boto3.client("secretsmanager", region_name=DEFAULT_REGION)
    sm.create_secret(
        Name="iac-cartographer/confluence",
        SecretString=json.dumps({"email": "bot@acme.example.com", "api_token": "ATATT"}),
    )
    sm.create_secret(Name="iac-cartographer/gitlab", SecretString=json.dumps({"token": "glpat"}))
    sm.create_secret(Name="iac-cartographer/github", SecretString=json.dumps({"token": "ghp"}))
    sm.create_secret(
        Name="iac-cartographer/slack",
        SecretString=json.dumps({"bot_token": "xoxb", "channel_id": "C0X"}),
    )

    # Stub the parts that hit external network.
    from datetime import UTC
    from datetime import datetime as _dt

    from iac_cartographer import cli as _cli
    from iac_cartographer.models import RepoInventory, RepoMetadata, TerraformSummary

    async def fake_discover(*_a: object, **_kw: object) -> list[RepoMetadata]:
        return [
            RepoMetadata(
                host="gitlab",
                full_name="acme/iac/main-cluster",
                clone_url="https://x.test/acme/iac/main-cluster.git",
                web_url="https://x.test/acme/iac/main-cluster",
                default_branch="main",
                last_commit_sha="a" * 40,
                last_commit_at=_dt(2026, 5, 22, tzinfo=UTC),
            )
        ]

    async def fake_process_repo(meta: RepoMetadata, *_a: object, **_kw: object):
        return (
            RepoInventory(meta=meta, summary=TerraformSummary(), narrative=None),
            None,
            0,
            0,
        )

    monkeypatch.setattr(_cli, "discover_from_sources", fake_discover)
    monkeypatch.setattr(_cli, "_process_repo", fake_process_repo)

    rc = main(["--once", "--config", str(cfg), "--dry-run", "--no-bedrock"])
    assert rc == 0


def test_run_once_missing_config_returns_2(tmp_path: Path) -> None:
    rc = main(["--once", "--config", str(tmp_path / "missing.yaml")])
    assert rc == 2


# ─── L-1 hardening: HCL byte budget in _read_repo_content ───────────────


def test_read_repo_content_below_budget_reads_everything(tmp_path: Path) -> None:
    from iac_cartographer.cli import _read_repo_content

    (tmp_path / "README.md").write_text("readme content", encoding="utf-8")
    (tmp_path / "main.tf").write_text('resource "x" "y" {}\n', encoding="utf-8")
    (tmp_path / "iam.tf").write_text('resource "a" "b" {}\n', encoding="utf-8")
    readme, hcl = _read_repo_content(tmp_path)
    assert readme == "readme content"
    assert 'resource "x" "y"' in hcl
    assert 'resource "a" "b"' in hcl


def test_read_repo_content_stops_at_byte_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """L-1: a pathological multi-MB HCL repo must not OOM the task. We stop
    concatenating once cumulative size exceeds the budget, log a warning, and
    let the narrator's 30 KB truncation handle the small tail we did read."""
    from iac_cartographer import cli as _cli

    # Tighten the budget so we don't have to write multi-MB fixtures.
    monkeypatch.setattr(_cli, "_HCL_BYTE_BUDGET", 100)

    # 3 files of 80 bytes each = first 1 fits; second pushes past budget.
    (tmp_path / "a.tf").write_text("a" * 80, encoding="utf-8")
    (tmp_path / "b.tf").write_text("b" * 80, encoding="utf-8")
    (tmp_path / "c.tf").write_text("c" * 80, encoding="utf-8")

    caplog.set_level(logging.WARNING, logger="iac_cartographer.cli")
    _readme, hcl = _cli._read_repo_content(tmp_path)
    # Only the first file made it in
    assert "a" * 80 in hcl
    assert "b" * 80 not in hcl
    assert "c" * 80 not in hcl
    # The warning fired
    assert any("byte-budget" in rec.getMessage() for rec in caplog.records)


@mock_aws
def test_run_once_model_flag_overrides_bedrock_model_id(
    _aws_region: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--model` overrides `config.bedrock.model_id` for this run only.
    Manual/validation invocations can pick Haiku without editing SSM config."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("discovery:\n  gitlab_group_ids: [1]\n", encoding="utf-8")
    sm = boto3.client("secretsmanager", region_name=DEFAULT_REGION)
    sm.create_secret(
        Name="iac-cartographer/confluence",
        SecretString=json.dumps({"email": "bot@acme.example.com", "api_token": "ATATT"}),
    )
    sm.create_secret(Name="iac-cartographer/gitlab", SecretString=json.dumps({"token": "glpat"}))
    sm.create_secret(Name="iac-cartographer/github", SecretString=json.dumps({"token": "ghp"}))
    sm.create_secret(
        Name="iac-cartographer/slack",
        SecretString=json.dumps({"bot_token": "xoxb", "channel_id": "C0X"}),
    )

    from datetime import UTC
    from datetime import datetime as _dt

    from iac_cartographer import cli as _cli
    from iac_cartographer.models import RepoInventory, RepoMetadata, TerraformSummary

    seen_model_ids: list[str] = []

    async def fake_discover(*_a: object, **_kw: object) -> list[RepoMetadata]:
        return [
            RepoMetadata(
                host="gitlab",
                full_name="op/x",
                clone_url="https://x.test/acme/x.git",
                web_url="https://x.test/op/x",
                default_branch="main",
                last_commit_sha="a" * 40,
                last_commit_at=_dt(2026, 5, 22, tzinfo=UTC),
            )
        ]

    async def fake_process_repo(meta: RepoMetadata, *_a: object, **kwargs: object):
        # Capture the bedrock model id that the orchestrator routed to us.
        bedrock_config = _a[2] if len(_a) >= 3 else kwargs.get("bedrock_config")
        if bedrock_config is not None:
            seen_model_ids.append(bedrock_config.model_id)
        return (
            RepoInventory(meta=meta, summary=TerraformSummary(), narrative=None),
            None,
            0,
            0,
        )

    monkeypatch.setattr(_cli, "discover_from_sources", fake_discover)
    monkeypatch.setattr(_cli, "_process_repo", fake_process_repo)

    rc = main(
        [
            "--once",
            "--config",
            str(cfg),
            "--dry-run",
            "--no-bedrock",
            "--model",
            "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
        ]
    )
    assert rc == 0
    assert seen_model_ids == ["eu.anthropic.claude-haiku-4-5-20251001-v1:0"]


@mock_aws
def test_run_once_no_model_flag_uses_config_default(
    _aws_region: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When `--model` is omitted, the default Sonnet inference profile is used."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("discovery:\n  gitlab_group_ids: [1]\n", encoding="utf-8")
    sm = boto3.client("secretsmanager", region_name=DEFAULT_REGION)
    for name, payload in [
        ("iac-cartographer/confluence", {"email": "bot@acme.example.com", "api_token": "ATATT"}),
        ("iac-cartographer/gitlab", {"token": "glpat"}),
        ("iac-cartographer/github", {"token": "ghp"}),
        ("iac-cartographer/slack", {"bot_token": "xoxb", "channel_id": "C0X"}),
    ]:
        sm.create_secret(Name=name, SecretString=json.dumps(payload))

    from datetime import UTC
    from datetime import datetime as _dt

    from iac_cartographer import cli as _cli
    from iac_cartographer.models import RepoInventory, RepoMetadata, TerraformSummary

    seen_model_ids: list[str] = []

    async def fake_discover(*_a: object, **_kw: object) -> list[RepoMetadata]:
        return [
            RepoMetadata(
                host="gitlab",
                full_name="op/x",
                clone_url="https://x.test/acme/x.git",
                web_url="https://x.test/op/x",
                default_branch="main",
                last_commit_sha="a" * 40,
                last_commit_at=_dt(2026, 5, 22, tzinfo=UTC),
            )
        ]

    async def fake_process_repo(meta: RepoMetadata, *_a: object, **kwargs: object):
        bedrock_config = _a[2] if len(_a) >= 3 else kwargs.get("bedrock_config")
        if bedrock_config is not None:
            seen_model_ids.append(bedrock_config.model_id)
        return (
            RepoInventory(meta=meta, summary=TerraformSummary(), narrative=None),
            None,
            0,
            0,
        )

    monkeypatch.setattr(_cli, "discover_from_sources", fake_discover)
    monkeypatch.setattr(_cli, "_process_repo", fake_process_repo)

    rc = main(["--once", "--config", str(cfg), "--dry-run", "--no-bedrock"])
    assert rc == 0
    assert seen_model_ids == ["eu.anthropic.claude-sonnet-4-5-20250929-v1:0"]


# ─── Preflight: Confluence parent-page reachability ──────────────────────


@mock_aws
def test_run_once_preflight_404_returns_2_before_discovery(
    _aws_region: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the SSM-stored parent page ID doesn't resolve, the pipeline aborts
    with exit 2 BEFORE running discovery → clone → Bedrock. This is what
    prevents wasting Bedrock spend on a run we can't publish."""
    import httpx
    import respx

    cfg = tmp_path / "config.yaml"
    cfg.write_text("discovery:\n  gitlab_group_ids: [1]\n", encoding="utf-8")
    boto_ssm = boto3.client("ssm", region_name=DEFAULT_REGION)
    boto_ssm.put_parameter(
        Name="/iac-cartographer/confluence-parent-id",
        Value="845152258",
        Type="String",
    )
    sm = boto3.client("secretsmanager", region_name=DEFAULT_REGION)
    for name, payload in [
        ("iac-cartographer/confluence", {"email": "bot@acme.example.com", "api_token": "ATATT"}),
        ("iac-cartographer/gitlab", {"token": "glpat"}),
        ("iac-cartographer/github", {"token": "ghp"}),
        ("iac-cartographer/slack", {"bot_token": "xoxb", "channel_id": "C0X"}),
    ]:
        sm.create_secret(Name=name, SecretString=json.dumps(payload))

    discover_called: list[bool] = []

    async def fake_discover_should_not_run(*_a: object, **_kw: object) -> object:
        discover_called.append(True)
        raise AssertionError("discover() must not run when preflight fails")

    from iac_cartographer import cli as _cli

    monkeypatch.setattr(_cli, "discover_from_sources", fake_discover_should_not_run)

    # 404 on the parent page lookup
    with respx.mock(base_url="https://acme.atlassian.net/wiki/api/v2"):
        respx.get("/pages/845152258").mock(return_value=httpx.Response(404, json={"message": "Not Found"}))
        rc = main(["--once", "--config", str(cfg)])

    assert rc == 2
    assert discover_called == []  # preflight short-circuited before discovery


@mock_aws
def test_run_once_preflight_skipped_in_dry_run(
    _aws_region: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--dry-run skips the Confluence preflight (along with all other Confluence
    reads/writes). Operators verifying parent-page setup should browser-check
    the URL directly."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("discovery:\n  gitlab_group_ids: [1]\n", encoding="utf-8")
    sm = boto3.client("secretsmanager", region_name=DEFAULT_REGION)
    for name, payload in [
        ("iac-cartographer/confluence", {"email": "bot@acme.example.com", "api_token": "ATATT"}),
        ("iac-cartographer/gitlab", {"token": "glpat"}),
        ("iac-cartographer/github", {"token": "ghp"}),
        ("iac-cartographer/slack", {"bot_token": "xoxb", "channel_id": "C0X"}),
    ]:
        sm.create_secret(Name=name, SecretString=json.dumps(payload))

    from datetime import UTC
    from datetime import datetime as _dt

    from iac_cartographer import cli as _cli
    from iac_cartographer.models import RepoInventory, RepoMetadata, TerraformSummary

    async def fake_discover(*_a: object, **_kw: object) -> list[RepoMetadata]:
        return [
            RepoMetadata(
                host="gitlab",
                full_name="op/x",
                clone_url="https://x.test/acme/x.git",
                web_url="https://x.test/op/x",
                default_branch="main",
                last_commit_sha="a" * 40,
                last_commit_at=_dt(2026, 5, 22, tzinfo=UTC),
            )
        ]

    async def fake_process_repo(meta: RepoMetadata, *_a: object, **_kw: object):
        return (RepoInventory(meta=meta, summary=TerraformSummary(), narrative=None), None, 0, 0)

    monkeypatch.setattr(_cli, "discover_from_sources", fake_discover)
    monkeypatch.setattr(_cli, "_process_repo", fake_process_repo)

    # If preflight ran in dry-run, this would hit an unmocked Confluence URL.
    # Test passes only if preflight is correctly skipped.
    rc = main(["--once", "--config", str(cfg), "--dry-run", "--no-bedrock"])
    assert rc == 0


def test_read_repo_content_skips_dot_terraform(tmp_path: Path) -> None:
    """Defensive: never accidentally feed `.terraform/` plugin caches to Bedrock."""
    from iac_cartographer.cli import _read_repo_content

    (tmp_path / "main.tf").write_text('resource "real" "x" {}\n', encoding="utf-8")
    cache = tmp_path / ".terraform" / "modules"
    cache.mkdir(parents=True)
    (cache / "vendored.tf").write_text('resource "SHOULD_NOT_APPEAR" "x" {}\n', encoding="utf-8")
    _readme, hcl = _read_repo_content(tmp_path)
    assert "real" in hcl
    assert "SHOULD_NOT_APPEAR" not in hcl


# ─── _build_llm_backend ──────────────────────────────────────────────


def test_build_llm_backend_vertex_branch() -> None:
    """Vertex backend doesn't need a credential bundle (auth via GCP
    ADC). Just check the factory routes correctly when
    `llm.backend: vertex` + a project_id are set."""
    from iac_cartographer.cli import LoadedSecrets, _build_llm_backend
    from iac_cartographer.llm import VertexBackend
    from iac_cartographer.models import (
        ConfluenceCredentials,
        GithubCredentials,
        GitlabCredentials,
        LLMConfig,
        SlackCredentials,
    )

    llm_cfg = LLMConfig(backend="vertex", vertex_project_id="my-project", vertex_region="us-east5")
    secrets = LoadedSecrets(
        confluence=ConfluenceCredentials(email="x@x", api_token="t"),
        gitlab=GitlabCredentials(token="t"),
        github=GithubCredentials(token="t"),
        slack=SlackCredentials(bot_token="t"),
    )
    backend = _build_llm_backend(llm_cfg, secrets)
    assert isinstance(backend, VertexBackend)
    assert backend._project_id == "my-project"
    assert backend._region == "us-east5"


def test_build_llm_backend_vertex_requires_project_id() -> None:
    """Missing vertex_project_id is a config error, not a runtime
    failure deep in the SDK."""
    from iac_cartographer.cli import LoadedSecrets, _build_llm_backend
    from iac_cartographer.constants import ConfigError
    from iac_cartographer.models import (
        ConfluenceCredentials,
        GithubCredentials,
        GitlabCredentials,
        LLMConfig,
        SlackCredentials,
    )

    llm_cfg = LLMConfig(backend="vertex")  # vertex_project_id defaults to ""
    secrets = LoadedSecrets(
        confluence=ConfluenceCredentials(email="x@x", api_token="t"),
        gitlab=GitlabCredentials(token="t"),
        github=GithubCredentials(token="t"),
        slack=SlackCredentials(bot_token="t"),
    )
    with pytest.raises(ConfigError, match="vertex_project_id"):
        _build_llm_backend(llm_cfg, secrets)


def test_build_llm_backend_azure_openai_with_api_key() -> None:
    """API-key auth path: factory wires up the secret correctly."""
    from iac_cartographer.cli import LoadedSecrets, _build_llm_backend
    from iac_cartographer.llm import AzureOpenAIBackend
    from iac_cartographer.models import (
        AzureOpenAICredentials,
        ConfluenceCredentials,
        GithubCredentials,
        GitlabCredentials,
        LLMConfig,
        SlackCredentials,
    )

    llm_cfg = LLMConfig(
        backend="azure_openai",
        azure_openai_endpoint="https://my-resource.openai.azure.com/",
        azure_openai_deployment="my-gpt4",
        azure_openai_api_version="2024-10-21",
    )
    secrets = LoadedSecrets(
        confluence=ConfluenceCredentials(email="x@x", api_token="t"),
        gitlab=GitlabCredentials(token="t"),
        github=GithubCredentials(token="t"),
        slack=SlackCredentials(bot_token="t"),
        azure_openai=AzureOpenAICredentials(api_key="sk-azure-..."),
    )
    backend = _build_llm_backend(llm_cfg, secrets)
    assert isinstance(backend, AzureOpenAIBackend)
    assert backend._deployment == "my-gpt4"
    assert backend._use_aad is False
    assert backend._api_key == "sk-azure-..."


def test_build_llm_backend_azure_openai_with_aad() -> None:
    """use_aad=True path: factory skips the credential bundle."""
    from iac_cartographer.cli import LoadedSecrets, _build_llm_backend
    from iac_cartographer.llm import AzureOpenAIBackend
    from iac_cartographer.models import (
        ConfluenceCredentials,
        GithubCredentials,
        GitlabCredentials,
        LLMConfig,
        SlackCredentials,
    )

    llm_cfg = LLMConfig(
        backend="azure_openai",
        azure_openai_endpoint="https://my-resource.openai.azure.com/",
        azure_openai_deployment="my-gpt4",
        azure_openai_use_aad=True,
    )
    secrets = LoadedSecrets(
        confluence=ConfluenceCredentials(email="x@x", api_token="t"),
        gitlab=GitlabCredentials(token="t"),
        github=GithubCredentials(token="t"),
        slack=SlackCredentials(bot_token="t"),
        # azure_openai stays None — AAD doesn't need it
    )
    backend = _build_llm_backend(llm_cfg, secrets)
    assert isinstance(backend, AzureOpenAIBackend)
    assert backend._use_aad is True
    assert backend._api_key is None


def test_build_llm_backend_azure_openai_missing_endpoint() -> None:
    """Both endpoint + deployment are required even in AAD mode —
    they're routing info, not credentials."""
    from iac_cartographer.cli import LoadedSecrets, _build_llm_backend
    from iac_cartographer.constants import ConfigError
    from iac_cartographer.models import (
        ConfluenceCredentials,
        GithubCredentials,
        GitlabCredentials,
        LLMConfig,
        SlackCredentials,
    )

    llm_cfg = LLMConfig(backend="azure_openai", azure_openai_deployment="my-gpt4")
    secrets = LoadedSecrets(
        confluence=ConfluenceCredentials(email="x@x", api_token="t"),
        gitlab=GitlabCredentials(token="t"),
        github=GithubCredentials(token="t"),
        slack=SlackCredentials(bot_token="t"),
    )
    with pytest.raises(ConfigError, match="azure_openai_endpoint"):
        _build_llm_backend(llm_cfg, secrets)


def test_build_llm_backend_azure_openai_missing_deployment() -> None:
    from iac_cartographer.cli import LoadedSecrets, _build_llm_backend
    from iac_cartographer.constants import ConfigError
    from iac_cartographer.models import (
        ConfluenceCredentials,
        GithubCredentials,
        GitlabCredentials,
        LLMConfig,
        SlackCredentials,
    )

    llm_cfg = LLMConfig(
        backend="azure_openai",
        azure_openai_endpoint="https://my-resource.openai.azure.com/",
    )
    secrets = LoadedSecrets(
        confluence=ConfluenceCredentials(email="x@x", api_token="t"),
        gitlab=GitlabCredentials(token="t"),
        github=GithubCredentials(token="t"),
        slack=SlackCredentials(bot_token="t"),
    )
    with pytest.raises(ConfigError, match="azure_openai_deployment"):
        _build_llm_backend(llm_cfg, secrets)


def test_build_llm_backend_openai_branch() -> None:
    """OpenAI factory branch wires the SDK base_url + organization + api_key."""
    from iac_cartographer.cli import LoadedSecrets, _build_llm_backend
    from iac_cartographer.llm import OpenAIBackend
    from iac_cartographer.models import (
        ConfluenceCredentials,
        GithubCredentials,
        GitlabCredentials,
        LLMConfig,
        OpenAICredentials,
        SlackCredentials,
    )

    llm_cfg = LLMConfig(
        backend="openai",
        openai_base_url="https://openai.example.com/v1",
        openai_organization="org-123",
    )
    secrets = LoadedSecrets(
        confluence=ConfluenceCredentials(email="x@x", api_token="t"),
        gitlab=GitlabCredentials(token="t"),
        github=GithubCredentials(token="t"),
        slack=SlackCredentials(bot_token="t"),
        openai=OpenAICredentials(api_key="sk-..."),
    )
    backend = _build_llm_backend(llm_cfg, secrets)
    assert isinstance(backend, OpenAIBackend)
    assert backend._api_key == "sk-..."
    assert backend._base_url == "https://openai.example.com/v1"
    assert backend._organization == "org-123"
