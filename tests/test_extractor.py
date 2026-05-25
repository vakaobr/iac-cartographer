"""Phase 4 tests for iac_cartographer.extractor — terraform-docs subprocess mocked."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path  # noqa: TC003 — pytest resolves fixture type annotations at runtime

import pytest

from iac_cartographer.constants import ExtractionError
from iac_cartographer.extractor import _build_summary, _safe_type, run_terraform_docs

# ─── _build_summary ──────────────────────────────────────────────────────


def test_build_summary_flat_module_shape() -> None:
    data = {
        "providers": [{"name": "aws", "source": "hashicorp/aws", "version": ">= 6.0"}],
        "modules": [{"name": "vpc", "source": "terraform-aws-modules/vpc/aws", "version": "5.0.0"}],
        "resources": [
            {"type": "aws_iam_role", "name": "task", "mode": "managed"},
            {"type": "aws_iam_role_policy", "name": "task_policy", "mode": "managed"},
            {"type": "aws_caller_identity", "name": "current", "mode": "data"},
        ],
        "inputs": [{"name": "region", "type": "string", "default": "eu-central-1", "required": False}],
        "outputs": [{"name": "role_arn", "description": "Task role ARN"}],
        "requirements": [{"name": "aws", "version": ">= 6.0"}, {"name": "terraform", "version": ">= 1.10.0"}],
    }
    summary = _build_summary(data)
    assert {p.name for p in summary.providers} == {"aws"}
    assert summary.modules[0].source == "terraform-aws-modules/vpc/aws"
    assert summary.resource_counts_by_type == {
        "aws_iam_role": 1,
        "aws_iam_role_policy": 1,
        "aws_caller_identity": 1,
    }
    assert summary.requirements == {"aws": ">= 6.0", "terraform": ">= 1.10.0"}
    assert summary.outputs[0].name == "role_arn"


def test_build_summary_recursive_list_shape() -> None:
    data = [
        {
            "path": "modules/iam",
            "content": {
                "resources": [{"type": "aws_iam_role", "name": "task", "mode": "managed"}],
            },
        },
        {
            "path": "modules/ecs",
            "content": {
                "resources": [{"type": "aws_ecs_task_definition", "name": "main", "mode": "managed"}],
            },
        },
    ]
    summary = _build_summary(data)
    assert summary.resource_counts_by_type == {
        "aws_iam_role": 1,
        "aws_ecs_task_definition": 1,
    }


def test_build_summary_deduplicates_across_modules() -> None:
    data = [
        {"path": "a", "content": {"resources": [{"type": "aws_iam_role", "name": "r1"}]}},
        {"path": "b", "content": {"resources": [{"type": "aws_iam_role", "name": "r1"}]}},
    ]
    summary = _build_summary(data)
    # Same type+name+mode → counted once
    assert summary.resource_counts_by_type == {"aws_iam_role": 1}


def test_build_summary_handles_nested_data_type_for_variable() -> None:
    data = {
        "inputs": [
            {
                "name": "complex_var",
                "type": {"object": {"a": "string", "b": "number"}},
                "required": True,
            }
        ],
    }
    summary = _build_summary(data)
    assert summary.inputs[0].name == "complex_var"
    assert summary.inputs[0].required is True
    # Type was lossily serialized to a JSON string
    assert summary.inputs[0].type is not None
    assert isinstance(summary.inputs[0].type, str)
    assert "object" in summary.inputs[0].type


def test_build_summary_skips_invalid_resource_mode() -> None:
    data = {
        "resources": [
            {"type": "aws_iam_role", "name": "good", "mode": "managed"},
            {"type": "aws_iam_role", "name": "weird", "mode": "BOGUS"},
        ],
    }
    summary = _build_summary(data)
    # Both included; weird is coerced to "managed"
    assert all(r.mode == "managed" for r in summary.resources)


def test_build_summary_empty_returns_empty_summary() -> None:
    summary = _build_summary({})
    assert summary.providers == []
    assert summary.resources == []
    assert summary.resource_counts_by_type == {}


# ─── _safe_type ──────────────────────────────────────────────────────────


def test_safe_type_string_passthrough() -> None:
    assert _safe_type("string") == "string"


def test_safe_type_none_returns_none() -> None:
    assert _safe_type(None) is None


def test_safe_type_dict_serialized() -> None:
    out = _safe_type({"object": {"a": "string"}})
    assert out is not None
    assert "object" in out
    assert "a" in out


def test_safe_type_truncates_long_payload() -> None:
    huge = {"x": "y" * 1000}
    out = _safe_type(huge)
    assert out is not None
    assert len(out) <= 200


# ─── run_terraform_docs ──────────────────────────────────────────────────


def _seed_tf(repo: Path, *rel_dirs: str) -> None:
    """Create empty `main.tf` files in the given relative dirs (or at the
    root if `rel_dirs` is empty) so `_find_tf_dirs` has something to find."""
    targets = [repo] if not rel_dirs else [repo / d for d in rel_dirs]
    for t in targets:
        t.mkdir(parents=True, exist_ok=True)
        (t / "main.tf").write_text("# stub for tests\n", encoding="utf-8")


def test_run_terraform_docs_happy_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _seed_tf(tmp_path)
    payload = {
        "resources": [{"type": "aws_iam_role", "name": "task"}],
        "providers": [{"name": "aws"}],
    }

    def fake_run(*_args: object, **_kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr("iac_cartographer.extractor.subprocess.run", fake_run)
    summary = run_terraform_docs(tmp_path)
    assert summary.resource_counts_by_type == {"aws_iam_role": 1}
    assert summary.providers[0].name == "aws"


# ─── _parse_required_providers_in_dir + provider source enrichment ───


def test_parse_required_providers_extracts_source_and_version(tmp_path: Path) -> None:
    """Direct test of the HCL parser — recover source + version from a
    standard `terraform { required_providers { ... } }` block."""
    from iac_cartographer.extractor import _parse_required_providers_in_dir

    (tmp_path / "providers.tf").write_text(
        """
terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0"
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = ">= 5.0"
    }
  }
}

provider "aws" {}
""",
        encoding="utf-8",
    )
    parsed = _parse_required_providers_in_dir(tmp_path)
    assert parsed == {
        "aws": {"source": "hashicorp/aws", "version": ">= 6.0"},
        "cloudflare": {"source": "cloudflare/cloudflare", "version": ">= 5.0"},
    }


def test_parse_required_providers_returns_empty_on_no_block(tmp_path: Path) -> None:
    """Repos that declare providers without a `required_providers` block
    (just bare `provider "x" {}`) yield no map — the renderer's
    `infer_provider_source` fallback then kicks in."""
    from iac_cartographer.extractor import _parse_required_providers_in_dir

    (tmp_path / "main.tf").write_text('provider "cloudflare" {}\n', encoding="utf-8")
    assert _parse_required_providers_in_dir(tmp_path) == {}


def test_parse_required_providers_handles_missing_source_or_version(tmp_path: Path) -> None:
    """A `required_providers` entry can omit source OR version
    independently; record `None` for the missing field."""
    from iac_cartographer.extractor import _parse_required_providers_in_dir

    (tmp_path / "p.tf").write_text(
        """
terraform {
  required_providers {
    aws = {
      source = "hashicorp/aws"
    }
    random = {
      version = ">= 3.0"
    }
  }
}
""",
        encoding="utf-8",
    )
    parsed = _parse_required_providers_in_dir(tmp_path)
    assert parsed == {
        "aws": {"source": "hashicorp/aws", "version": None},
        "random": {"source": None, "version": ">= 3.0"},
    }


def test_run_terraform_docs_enriches_provider_source_from_hcl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """End-to-end: terraform-docs JSON returns provider WITHOUT source
    (its formatter strips the field), but we recover it from a local
    `required_providers` block. Regression for the 2026-05-25 production
    discovery that *every* page was reading "(not declared)" because
    terraform-docs JSON drops source even on perfectly-declared repos."""
    _seed_tf(tmp_path)
    (tmp_path / "providers.tf").write_text(
        """
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0"
    }
  }
}
""",
        encoding="utf-8",
    )

    # Simulate terraform-docs's actual JSON output for this repo: providers
    # appear but with no `source` field (matches v0.20.x and v0.24.x).
    payload = {"providers": [{"name": "aws", "alias": None, "version": ">= 6.0"}]}

    def fake_run(*_args: object, **_kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr("iac_cartographer.extractor.subprocess.run", fake_run)
    summary = run_terraform_docs(tmp_path)
    aws = next(p for p in summary.providers if p.name == "aws" and p.alias is None)
    # terraform-docs reported no source, but our HCL parser filled it in.
    assert aws.source == "hashicorp/aws"
    assert aws.version == ">= 6.0"


def test_run_terraform_docs_never_overwrites_declared_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """If terraform-docs ever does emit a source (future version, or a
    formatter quirk), the parser MUST NOT overwrite it. Belt-and-braces."""
    _seed_tf(tmp_path)
    (tmp_path / "providers.tf").write_text(
        'terraform { required_providers { aws = { source = "PARSED/wrong" } } }\n',
        encoding="utf-8",
    )

    payload = {"providers": [{"name": "aws", "source": "TFDOCS/correct", "version": ">= 6"}]}

    def fake_run(*_args: object, **_kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr("iac_cartographer.extractor.subprocess.run", fake_run)
    summary = run_terraform_docs(tmp_path)
    assert summary.providers[0].source == "TFDOCS/correct"  # not overwritten


def test_run_terraform_docs_populates_module_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`TerraformSummary.module_paths` must reflect every `.tf`-containing
    dir the extractor visited (sorted, relative-to-repo). Mirrors the
    op-infrastructure layout that lost `staging` from env detection on
    2026-05-25 — surfacing the dir list is the prompt-side fix."""
    _seed_tf(tmp_path, "terraform/env/dev", "terraform/env/staging", "terraform/env/prod")

    def fake_run(*_args: object, **_kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps({"resources": []}), stderr="")

    monkeypatch.setattr("iac_cartographer.extractor.subprocess.run", fake_run)
    summary = run_terraform_docs(tmp_path)
    assert summary.module_paths == [
        "terraform/env/dev",
        "terraform/env/prod",
        "terraform/env/staging",
    ]


def test_run_terraform_docs_module_paths_skips_root_single_module(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """For a flat single-module repo (root-only `.tf` files), `module_paths`
    is empty — the implicit `"."` we inject is not useful information."""
    _seed_tf(tmp_path)

    def fake_run(*_args: object, **_kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps({"resources": []}), stderr="")

    monkeypatch.setattr("iac_cartographer.extractor.subprocess.run", fake_run)
    summary = run_terraform_docs(tmp_path)
    assert summary.module_paths == []


def test_run_terraform_docs_aggregates_across_subdirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Regression for the 2026-05-25 production miss: repos like
    op/op-infrastructure store their Terraform under `terraform/env/{prod,
    staging}/` not at the root. Both subdirs must contribute to the summary.
    """
    _seed_tf(tmp_path, "terraform/env/prod", "terraform/env/staging")

    payloads = iter(
        [
            json.dumps({"resources": [{"type": "aws_iam_role", "name": "prod_task"}], "providers": [{"name": "aws"}]}),
            json.dumps(
                {"resources": [{"type": "aws_iam_role", "name": "staging_task"}], "providers": [{"name": "aws"}]}
            ),
        ]
    )

    def fake_run(*_args: object, **_kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=next(payloads), stderr="")

    monkeypatch.setattr("iac_cartographer.extractor.subprocess.run", fake_run)
    summary = run_terraform_docs(tmp_path)
    # Both `prod_task` and `staging_task` resources are surfaced (resource
    # mode dedupes by (type, name, mode) so distinct names are both kept).
    resource_names = {r.name for r in summary.resources}
    assert resource_names == {"prod_task", "staging_task"}


def test_run_terraform_docs_no_tf_files_returns_empty_summary(tmp_path: Path) -> None:
    """If `_find_tf_dirs` returns nothing, return an empty summary instead
    of raising — keeps the orchestrator running even on a misbehaving repo."""
    summary = run_terraform_docs(tmp_path)
    assert summary.resources == []
    assert summary.providers == []


def test_run_terraform_docs_skips_terraform_dot_dir(tmp_path: Path) -> None:
    """`.terraform/` (provider caches from `terraform init`) and other
    `_SKIP_DIRS` must never be descended into."""
    _seed_tf(tmp_path, ".terraform/modules/whatever", ".git/refs/heads", "vendor/cache")
    summary = run_terraform_docs(tmp_path)
    # No real tf-dirs → empty summary; no terraform-docs invocations.
    assert summary.resources == []


def test_run_terraform_docs_missing_binary_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _seed_tf(tmp_path)

    def fake_run(*_args: object, **_kw: object) -> None:
        raise FileNotFoundError("terraform-docs")

    monkeypatch.setattr("iac_cartographer.extractor.subprocess.run", fake_run)
    with pytest.raises(ExtractionError, match="not found"):
        run_terraform_docs(tmp_path)


def test_run_terraform_docs_timeout_skips_that_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Per-dir timeout now logs + skips — one slow submodule shouldn't
    blank out the whole repo's summary. Only every-dir-failed → empty."""
    _seed_tf(tmp_path)

    def fake_run(*_args: object, **_kw: object) -> None:
        raise subprocess.TimeoutExpired(cmd="terraform-docs", timeout=60)

    monkeypatch.setattr("iac_cartographer.extractor.subprocess.run", fake_run)
    summary = run_terraform_docs(tmp_path)
    assert summary.resources == []  # only dir timed out → empty summary, no raise


def test_run_terraform_docs_non_zero_exit_skips_that_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _seed_tf(tmp_path)

    def fake_run(*_args: object, **_kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="HCL parse error at line 42")

    monkeypatch.setattr("iac_cartographer.extractor.subprocess.run", fake_run)
    summary = run_terraform_docs(tmp_path)
    assert summary.resources == []  # only dir failed → empty summary


def test_run_terraform_docs_invalid_json_skips_that_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _seed_tf(tmp_path)

    def fake_run(*_args: object, **_kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="not json {{{", stderr="")

    monkeypatch.setattr("iac_cartographer.extractor.subprocess.run", fake_run)
    summary = run_terraform_docs(tmp_path)
    assert summary.resources == []  # only dir invalid → empty summary


def test_run_terraform_docs_one_bad_one_good_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """If 1 of 2 submodules fails (rc!=0) we still publish the good half —
    a single bad submodule should never erase the rest of the repo's inventory."""
    _seed_tf(tmp_path, "good", "bad")

    rcs = iter([0, 1])
    stdouts = iter([json.dumps({"resources": [{"type": "aws_iam_role", "name": "good_role"}]}), ""])

    def fake_run(*_args: object, **_kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=next(rcs), stdout=next(stdouts), stderr="oops")

    monkeypatch.setattr("iac_cartographer.extractor.subprocess.run", fake_run)
    summary = run_terraform_docs(tmp_path)
    assert [r.name for r in summary.resources] == ["good_role"]


def test_run_terraform_docs_empty_output_skips_that_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _seed_tf(tmp_path)

    def fake_run(*_args: object, **_kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="   \n", stderr="")

    monkeypatch.setattr("iac_cartographer.extractor.subprocess.run", fake_run)
    summary = run_terraform_docs(tmp_path)
    assert summary.resources == []


def test_run_terraform_docs_path_must_be_directory(tmp_path: Path) -> None:
    nonexistent = tmp_path / "does-not-exist"
    with pytest.raises(ExtractionError, match="not a directory"):
        run_terraform_docs(nonexistent)
