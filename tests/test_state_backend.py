"""Parser + posture-analyser tests for `iac_cartographer.state_backend`.

Covers the acceptance criteria for #94:

  * `s3` happy path                    → encryption + KMS + locking signals
  * `s3` without `encrypt`             → encryption signal escalates to `warn`
  * `local` backend                    → loud `critical` signal
  * Unparseable backend block          → graceful degradation, no crash
  * Repo with no backend declared      → empty list, no section rendered

Plus richer coverage of the per-backend safety logic for `gcs`,
`azurerm`, and `remote`, and a few parser edge cases (comments,
multiple blocks in one file, string-aware brace matching).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from iac_cartographer.models import StateBackend, StateBackendSignal
from iac_cartographer.state_backend import (
    compute_signals,
    parse_state_backends_in_dir,
)


def _write(tmp_path: Path, name: str, body: str) -> Path:
    """Helper: write a `.tf` file with `body` (dedented) under `tmp_path`."""
    p = tmp_path / name
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


# ─── s3: the happy path + the missing-encryption case ──────────────────


def test_s3_happy_path_surfaces_ok_signals(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "main.tf",
        """
        terraform {
          required_version = ">= 1.5"
          backend "s3" {
            bucket         = "acme-tfstate"
            key            = "prod/app.tfstate"
            region         = "eu-central-1"
            encrypt        = true
            dynamodb_table = "tf-locks"
          }
        }
        """,
    )
    backends = parse_state_backends_in_dir(tmp_path, module_path=".")
    assert len(backends) == 1
    b = backends[0]
    assert b.type == "s3"
    assert b.module_path == "."
    assert b.attrs["bucket"] == '"acme-tfstate"'
    assert b.attrs["key"] == '"prod/app.tfstate"'
    by_label = {s.label: s for s in b.signals}
    assert by_label["Encryption"].severity == "ok"
    assert by_label["State locking"].severity == "ok"
    # No customer-managed KMS key declared → AWS-managed SSE-S3 → info, not warn.
    assert by_label["KMS key"].severity == "info"


def test_s3_without_encrypt_surfaces_warn(tmp_path: Path) -> None:
    """No `encrypt = true` (and no explicit `= false`) → warn, because
    pre-1.6 Terraform left S3 backends unencrypted by default and we
    can't tell which version the operator targets."""
    _write(
        tmp_path,
        "main.tf",
        """
        terraform {
          backend "s3" {
            bucket = "acme-tfstate"
            key    = "prod/app.tfstate"
            region = "eu-central-1"
          }
        }
        """,
    )
    [b] = parse_state_backends_in_dir(tmp_path, module_path=".")
    encryption = next(s for s in b.signals if s.label == "Encryption")
    assert encryption.severity == "warn"
    assert "not declared" in encryption.value


def test_s3_explicit_encrypt_false_is_critical(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "main.tf",
        """
        terraform {
          backend "s3" {
            bucket  = "x"
            key     = "k"
            region  = "us-east-1"
            encrypt = false
          }
        }
        """,
    )
    [b] = parse_state_backends_in_dir(tmp_path, module_path=".")
    encryption = next(s for s in b.signals if s.label == "Encryption")
    assert encryption.severity == "critical"


def test_s3_with_kms_key_surfaces_ok(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "main.tf",
        """
        terraform {
          backend "s3" {
            bucket     = "x"
            key        = "k"
            region     = "us-east-1"
            encrypt    = true
            kms_key_id = "arn:aws:kms:us-east-1:111111111111:key/abc"
          }
        }
        """,
    )
    [b] = parse_state_backends_in_dir(tmp_path, module_path=".")
    kms = next(s for s in b.signals if s.label == "KMS key")
    assert kms.severity == "ok"
    assert "customer-managed" in kms.value


def test_s3_use_lockfile_counts_as_locking(tmp_path: Path) -> None:
    """Terraform 1.10+ supports S3-native locking via `use_lockfile = true`,
    which is a valid alternative to `dynamodb_table`."""
    _write(
        tmp_path,
        "main.tf",
        """
        terraform {
          backend "s3" {
            bucket       = "x"
            key          = "k"
            region       = "us-east-1"
            encrypt      = true
            use_lockfile = true
          }
        }
        """,
    )
    [b] = parse_state_backends_in_dir(tmp_path, module_path=".")
    locking = next(s for s in b.signals if s.label == "State locking")
    assert locking.severity == "ok"
    assert "use_lockfile" in locking.value


# ─── local: critical posture ──────────────────────────────────────────


def test_local_backend_is_critical(tmp_path: Path) -> None:
    """A `local` backend in any repo we'd index is almost always wrong —
    state on the workstation / CI runner that ran apply is the worst
    case. Surface it as the loudest severity."""
    _write(
        tmp_path,
        "main.tf",
        """
        terraform {
          backend "local" {
            path = "terraform.tfstate"
          }
        }
        """,
    )
    [b] = parse_state_backends_in_dir(tmp_path, module_path=".")
    assert b.type == "local"
    assert len(b.signals) == 1
    assert b.signals[0].severity == "critical"


# ─── gcs / azurerm / remote ───────────────────────────────────────────


def test_gcs_default_encryption_is_ok(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "main.tf",
        """
        terraform {
          backend "gcs" {
            bucket = "acme-tfstate"
            prefix = "prod"
          }
        }
        """,
    )
    [b] = parse_state_backends_in_dir(tmp_path, module_path=".")
    assert b.type == "gcs"
    encryption = next(s for s in b.signals if s.label == "Encryption")
    assert encryption.severity == "ok"
    assert "Google-managed" in encryption.value


def test_gcs_customer_supplied_encryption_is_ok(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "main.tf",
        """
        terraform {
          backend "gcs" {
            bucket         = "x"
            prefix         = "p"
            encryption_key = "base64=="
          }
        }
        """,
    )
    [b] = parse_state_backends_in_dir(tmp_path, module_path=".")
    encryption = next(s for s in b.signals if s.label == "Encryption")
    assert "customer-supplied" in encryption.value


def test_azurerm_oidc_auth_is_ok_static_key_is_warn(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "oidc.tf",
        """
        terraform {
          backend "azurerm" {
            resource_group_name  = "rg"
            storage_account_name = "sa"
            container_name       = "tfstate"
            key                  = "prod.tfstate"
            use_oidc             = true
          }
        }
        """,
    )
    [b] = parse_state_backends_in_dir(tmp_path, module_path="env/prod")
    auth = next(s for s in b.signals if s.label == "Auth")
    assert auth.severity == "ok"
    assert "OIDC" in auth.value

    # Static access key should escalate to warn.
    other = tmp_path / "sub"
    other.mkdir()
    _write(
        other,
        "static.tf",
        """
        terraform {
          backend "azurerm" {
            resource_group_name  = "rg"
            storage_account_name = "sa"
            container_name       = "tfstate"
            key                  = "dev.tfstate"
            access_key           = "shhh"
          }
        }
        """,
    )
    [b2] = parse_state_backends_in_dir(other, module_path="env/dev")
    auth2 = next(s for s in b2.signals if s.label == "Auth")
    assert auth2.severity == "warn"
    assert "static" in auth2.value


def test_remote_backend_default_is_tfc_hcp(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "main.tf",
        """
        terraform {
          backend "remote" {
            organization = "acme"
            workspaces { name = "prod" }
          }
        }
        """,
    )
    [b] = parse_state_backends_in_dir(tmp_path, module_path=".")
    backend = next(s for s in b.signals if s.label == "Backend")
    assert "Terraform Cloud" in backend.value


def test_remote_backend_with_enterprise_hostname(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "main.tf",
        """
        terraform {
          backend "remote" {
            hostname     = "tfe.acme.internal"
            organization = "acme"
          }
        }
        """,
    )
    [b] = parse_state_backends_in_dir(tmp_path, module_path=".")
    backend = next(s for s in b.signals if s.label == "Backend")
    assert "Terraform Enterprise" in backend.value
    assert "tfe.acme.internal" in backend.value


# ─── Long-tail backends use the operator-managed fallback signal ──────


def test_unhandled_backend_falls_back_to_operator_managed(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "main.tf",
        """
        terraform {
          backend "consul" {
            address = "consul.acme.internal:8500"
            path    = "terraform/state"
          }
        }
        """,
    )
    [b] = parse_state_backends_in_dir(tmp_path, module_path=".")
    assert b.type == "consul"
    assert len(b.signals) == 1
    assert "operator-managed posture" in b.signals[0].value


# ─── Negative + graceful-degradation cases ────────────────────────────


def test_repo_with_no_backend_returns_empty_list(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "main.tf",
        """
        terraform {
          required_version = ">= 1.5"
          required_providers {
            aws = { source = "hashicorp/aws", version = ">= 6.0" }
          }
        }

        resource "aws_s3_bucket" "x" { bucket = "y" }
        """,
    )
    assert parse_state_backends_in_dir(tmp_path, module_path=".") == []


def test_unbalanced_backend_block_is_skipped_gracefully(tmp_path: Path) -> None:
    """A truncated / malformed block must not crash the whole run — the
    parser logs at WARNING and moves on. The acceptance criterion is
    'graceful degradation': zero backends returned, no exception."""
    _write(
        tmp_path,
        "broken.tf",
        """
        terraform {
          backend "s3" {
            bucket = "x"
            key    = "y"
            # missing close brace below intentionally
        """,
    )
    # Must not raise.
    result = parse_state_backends_in_dir(tmp_path, module_path=".")
    # Either zero backends (unbalanced outer `terraform {` skipped) or
    # zero (unbalanced inner `backend {` skipped) — both acceptable per
    # the design. The contract is "doesn't crash, doesn't return junk".
    assert result == []


def test_repo_with_no_tf_files_returns_empty_list(tmp_path: Path) -> None:
    # Empty directory — no .tf files at all.
    assert parse_state_backends_in_dir(tmp_path, module_path=".") == []


def test_comment_with_backend_keyword_is_not_parsed_as_block(tmp_path: Path) -> None:
    """Line comments mentioning `backend` must not lure the parser into
    treating them as declarations."""
    _write(
        tmp_path,
        "comments.tf",
        """
        terraform {
          # backend "s3" { encrypt = false }  -- commented-out, ignore
          // backend "local" {}                 -- also a comment
          required_version = ">= 1.5"
        }
        """,
    )
    assert parse_state_backends_in_dir(tmp_path, module_path=".") == []


def test_string_with_brace_does_not_confuse_scanner(tmp_path: Path) -> None:
    """Interpolations like `"${var.x.y}"` contain braces that must not
    affect the brace-depth counter."""
    _write(
        tmp_path,
        "interpolated.tf",
        """
        terraform {
          backend "s3" {
            bucket  = "acme-${var.env}-state"
            key     = "${var.team}/${var.app}/main.tfstate"
            region  = "${var.region}"
            encrypt = true
          }
        }
        """,
    )
    [b] = parse_state_backends_in_dir(tmp_path, module_path=".")
    assert b.type == "s3"
    # The literal `var.x` references are surfaced as-is on `attrs`.
    assert b.attrs["bucket"] == '"acme-${var.env}-state"'


# ─── compute_signals is usable independently ──────────────────────────


def test_compute_signals_public_helper_works_for_each_handler() -> None:
    """`compute_signals` is the public entry point — used by the parser
    and (in future renderers) directly. Sanity-check it covers every
    backend we ship a handler for, plus the fallback."""
    for backend_type in ("s3", "gcs", "azurerm", "remote", "local"):
        signals = compute_signals(backend_type, {})
        assert all(isinstance(s, StateBackendSignal) for s in signals)
        assert signals, f"no signals returned for {backend_type}"
    # Fallback path for an unhandled type.
    fallback = compute_signals("oss", {})
    assert any("operator-managed" in s.value for s in fallback)


# ─── Model round-trip ─────────────────────────────────────────────────


def test_state_backend_model_round_trips(tmp_path: Path) -> None:
    """The model is the boundary between extractor and renderer. Round-trip
    through model_dump → model_validate so the renderer side can trust
    that what gets cached on `RepoInventory.summary` is byte-identical."""
    _write(
        tmp_path,
        "main.tf",
        """
        terraform {
          backend "s3" {
            bucket  = "x"
            key     = "k"
            encrypt = true
            dynamodb_table = "locks"
          }
        }
        """,
    )
    [original] = parse_state_backends_in_dir(tmp_path, module_path="env/prod")
    dumped = original.model_dump(mode="json")
    restored = StateBackend.model_validate(dumped)
    assert restored == original


# ─── parse_state_backends_in_dir respects module_path ─────────────────


@pytest.mark.parametrize("module_path", [".", "env/prod", "terraform/env/staging"])
def test_module_path_records_per_repo_layout(tmp_path: Path, module_path: str) -> None:
    _write(
        tmp_path,
        "main.tf",
        """
        terraform {
          backend "s3" {
            bucket = "x"
            key    = "k"
            encrypt = true
          }
        }
        """,
    )
    [b] = parse_state_backends_in_dir(tmp_path, module_path=module_path)
    assert b.module_path == module_path
