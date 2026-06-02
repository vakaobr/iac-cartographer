"""Parser + posture analyser for `terraform { backend "..." { ... } }` blocks.

Surfaces "where is this repo's state, and is it configured safely?" on
the rendered page. Sits alongside the existing `_parse_required_providers_in_dir`
helper in `extractor.py`: same brace-counting strategy, same per-`.tf`-dir
sweep, no `python-hcl2` dependency.

What we read
-------------
For each top-level `*.tf` file in a module dir:

  1. Strip comments (`//`, `#`, `/* ... */`) — they otherwise confuse
     the depth counter on lines like `# foo = "bar" {`.
  2. Find every `terraform {` start and walk braces to its end.
  3. Inside that body, find every `backend "TYPE" {` start and walk
     braces to its end.
  4. Inside that body, extract simple `key = value` pairs — strings,
     booleans, numbers, bare identifiers (e.g. `var.region`). We do NOT
     try to resolve variable references; the literal `var.…` value is
     genuinely useful information on the rendered page ("region is
     parameterised").

Why not python-hcl2
-------------------
Backend blocks have a near-universal grammar in real-world repos
(thousands of public Terraform projects checked at design time). The
brace counter handles the only edge cases that matter — comments,
heredocs (rare in backend blocks; we skip; not load-bearing), nested
braces inside string interpolations like `"${var.x.y}"` (handled by
the string-aware scanner below). Adding a 1.5 MB HCL2 parser to the
runtime is overkill for ~120 LoC of extraction logic.

Posture taxonomy
----------------
Each `StateBackend` carries pre-computed `signals: list[StateBackendSignal]`
so the renderer doesn't need per-backend knowledge — it joins signal
labels into one "Safety" column. The per-type signal logic lives in
`_signals_for_<type>()` helpers below.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from iac_cartographer.models import StateBackend, StateBackendSignal

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger("iac_cartographer.state_backend")

# ─── Regex toolkit ────────────────────────────────────────────────────

# `terraform {` — start of the wrapper block that contains `backend`,
# `required_providers`, etc.
_TERRAFORM_BLOCK_RE = re.compile(r"\bterraform\s*\{")
# `backend "type" {` — the actual backend declaration. Backend names are
# lowercase letters/digits/underscores by convention; the official list
# is fixed (s3, gcs, azurerm, remote, local, consul, etcdv3, http,
# kubernetes, pg, oss, cos, swift, ...).
_BACKEND_BLOCK_RE = re.compile(r'\bbackend\s+"([a-z][a-z0-9_]*)"\s*\{')
# `key = value` where value is one of:
#   * "double-quoted string"  (allows `\"` and interpolations)
#   * true / false
#   * integer or decimal number
#   * bare identifier like `var.name`, `local.x.y`, `module.foo.bar`
# Captured value is stored as a raw string — the renderer trims surrounding
# quotes for display.
_BACKEND_ATTR_RE = re.compile(
    r"^\s*"
    r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)"
    r"\s*=\s*"
    r'(?P<value>"(?:\\.|[^"\\])*"|true|false|-?\d+(?:\.\d+)?|[A-Za-z_][A-Za-z0-9_.\-]*)'
    r"\s*$",
    re.MULTILINE,
)

# Block + line comment stripper. Order matters: block comments first
# (they can span lines), then line comments. Both `//` and `#` are
# legal HCL line-comment introducers.
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"(?:^|[^:])(#[^\n]*|//[^\n]*)")


def _strip_comments(text: str) -> str:
    """Return `text` with HCL comments removed.

    Block comments are dropped wholesale. Line comments (`#…` and `//…`)
    are stripped, but only when they aren't preceded by `:` — that's a
    defensive guard against eating URLs like `https://…` inside string
    literals. It's coarse (we'd also fail to eat a comment after a `:`
    on the same line), but the alternative is a real tokenizer.
    """
    text = _BLOCK_COMMENT_RE.sub("", text)
    return _LINE_COMMENT_RE.sub(lambda m: m.group(0).split("#")[0].split("//")[0], text)


def _scan_braced_body(text: str, start: int) -> tuple[str, int] | None:
    """Return `(body, end_index)` for the block whose opening `{` was
    consumed by the caller at position `start - 1`. `body` is the text
    between the outer braces (exclusive of both); `end_index` is one
    past the matching close-brace.

    Returns `None` if the braces are unbalanced (truncated file, broken
    declaration). The scanner is string-aware: braces inside `"…"` and
    `'…'` literals don't move the depth counter, so an interpolation
    like `"${var.x}"` won't lie about the body's end. Backslash escapes
    inside strings are honoured so a literal `\"` doesn't terminate
    the string prematurely.
    """
    depth = 1
    i = start
    in_string: str | None = None  # holds the quote char while inside a string
    while i < len(text):
        ch = text[i]
        if in_string is not None:
            if ch == "\\" and i + 1 < len(text):
                i += 2
                continue
            if ch == in_string:
                in_string = None
            i += 1
            continue
        if ch in ('"', "'"):
            in_string = ch
            i += 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i], i + 1
        i += 1
    return None


def _parse_attrs(body: str) -> dict[str, str]:
    """Pull `key = value` pairs out of a backend block body. The value
    is stored verbatim (including surrounding quotes for strings) so
    the renderer can decide how to display it; signal-derivation helpers
    below normalise as needed.
    """
    attrs: dict[str, str] = {}
    for m in _BACKEND_ATTR_RE.finditer(body):
        key = m.group("key")
        value = m.group("value")
        attrs[key] = value
    return attrs


def _unquote(value: str) -> str:
    """Strip surrounding double quotes from a string literal value, if any."""
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    return value


def _is_truthy(value: str) -> bool | None:
    """Return True/False for `true`/`false` literals; None for anything else
    (variable references, numbers, missing values)."""
    if value == "true":
        return True
    if value == "false":
        return False
    return None


# ─── Per-type safety signals ─────────────────────────────────────────

# Each `_signals_for_<type>` reads from `attrs` and returns the row of
# signals the renderer will show. Signals are ordered by importance —
# the renderer joins them in order, so the most operationally relevant
# fact comes first.


def _signals_for_s3(attrs: dict[str, str]) -> list[StateBackendSignal]:
    out: list[StateBackendSignal] = []

    encrypt = _is_truthy(attrs.get("encrypt", ""))
    if encrypt is True:
        out.append(StateBackendSignal(label="Encryption", value="enabled", severity="ok"))
    elif encrypt is False:
        out.append(StateBackendSignal(label="Encryption", value="explicitly disabled", severity="critical"))
    else:
        # Terraform 1.6+ encrypts S3 backends by default; earlier
        # versions don't. We can't tell which the operator targets, so
        # surface the absence as a warn rather than ok.
        out.append(StateBackendSignal(label="Encryption", value="not declared", severity="warn"))

    if "kms_key_id" in attrs:
        out.append(StateBackendSignal(label="KMS key", value="customer-managed", severity="ok"))
    else:
        out.append(StateBackendSignal(label="KMS key", value="AWS-managed (SSE-S3)", severity="info"))

    # Modern Terraform supports `use_lockfile = true` (S3-native locking
    # via conditional writes, 1.10+) in addition to the long-standing
    # `dynamodb_table` flow. Either counts as locking.
    if "dynamodb_table" in attrs:
        out.append(StateBackendSignal(label="State locking", value="DynamoDB", severity="ok"))
    elif _is_truthy(attrs.get("use_lockfile", "")) is True:
        out.append(StateBackendSignal(label="State locking", value="S3 native (use_lockfile)", severity="ok"))
    else:
        out.append(StateBackendSignal(label="State locking", value="not configured", severity="warn"))

    return out


def _signals_for_gcs(attrs: dict[str, str]) -> list[StateBackendSignal]:
    # GCS encrypts buckets by default. `encryption_key` (CSEK) is a
    # customer-managed override; either form is acceptable posture.
    if "encryption_key" in attrs:
        encryption = StateBackendSignal(label="Encryption", value="customer-supplied (CSEK)", severity="ok")
    else:
        encryption = StateBackendSignal(label="Encryption", value="Google-managed (default)", severity="ok")
    # GCS implements state locking natively via object generations —
    # the backend handles it for us regardless of declaration.
    locking = StateBackendSignal(label="State locking", value="native (object generations)", severity="ok")
    return [encryption, locking]


def _signals_for_azurerm(attrs: dict[str, str]) -> list[StateBackendSignal]:
    # Azure Storage encrypts blobs at rest by default; nothing to opt
    # into in the backend block itself.
    encryption = StateBackendSignal(label="Encryption", value="default (Azure Storage SSE)", severity="ok")
    # Azure implements locking natively via blob leases.
    locking = StateBackendSignal(label="State locking", value="native (blob lease)", severity="ok")
    # Auth method — surface what the operator wired up. AAD-backed
    # options (use_oidc / use_msi / use_azuread_auth) are preferred
    # over static access keys.
    if _is_truthy(attrs.get("use_oidc", "")) is True:
        auth = StateBackendSignal(label="Auth", value="OIDC federated", severity="ok")
    elif _is_truthy(attrs.get("use_msi", "")) is True:
        auth = StateBackendSignal(label="Auth", value="Managed Identity", severity="ok")
    elif _is_truthy(attrs.get("use_azuread_auth", "")) is True:
        auth = StateBackendSignal(label="Auth", value="AAD", severity="ok")
    elif "access_key" in attrs or "sas_token" in attrs:
        auth = StateBackendSignal(label="Auth", value="static access key", severity="warn")
    else:
        auth = StateBackendSignal(label="Auth", value="not specified", severity="info")
    return [encryption, locking, auth]


def _signals_for_remote(attrs: dict[str, str]) -> list[StateBackendSignal]:
    # The `remote` backend points at Terraform Cloud / HCP Terraform
    # (or a self-hosted Terraform Enterprise install via `hostname`).
    # TFC handles encryption + locking centrally; we surface that it's
    # in use and which workspace.
    if "hostname" in attrs and _unquote(attrs["hostname"]) not in ("", "app.terraform.io"):
        platform = StateBackendSignal(
            label="Backend", value=f"Terraform Enterprise ({_unquote(attrs['hostname'])})", severity="info"
        )
    else:
        platform = StateBackendSignal(label="Backend", value="Terraform Cloud / HCP", severity="info")
    posture = StateBackendSignal(label="Encryption + locking", value="centrally managed", severity="ok")
    return [platform, posture]


def _signals_for_local(attrs: dict[str, str]) -> list[StateBackendSignal]:
    # The `local` backend writes state to whichever workstation / CI
    # runner executed `terraform apply`. That's almost always the worst
    # outcome — no shared visibility, no locking, no encryption, and
    # state is lost if the disk is.
    return [
        StateBackendSignal(
            label="Backend",
            value="local-disk (no remote state)",
            severity="critical",
        )
    ]


_SIGNAL_HANDLERS: dict[str, callable] = {  # type: ignore[type-arg]
    "s3": _signals_for_s3,
    "gcs": _signals_for_gcs,
    "azurerm": _signals_for_azurerm,
    "remote": _signals_for_remote,
    "local": _signals_for_local,
}


def _signals_for_other(backend_type: str, attrs: dict[str, str]) -> list[StateBackendSignal]:
    """Generic posture row for the long-tail backends (`consul`,
    `etcdv3`, `http`, `kubernetes`, `pg`, `oss`, `cos`, `swift`, ...).
    We don't have a per-backend safety checklist for these — surface
    the fact that the operator owns posture rather than the backend."""
    # Note: keeping `attrs` in the signature for symmetry + future
    # expansion (some of these have known-good signals once a real user
    # asks for them — `pg` has SSL options, `http` has Basic auth).
    del attrs  # intentionally unused
    return [StateBackendSignal(label="Backend", value=f"{backend_type} (operator-managed posture)", severity="info")]


def compute_signals(backend_type: str, attrs: dict[str, str]) -> list[StateBackendSignal]:
    """Public helper: derive the safety-signal row for `backend_type`,
    given its parsed `attrs`. Used by the parser and by tests."""
    handler = _SIGNAL_HANDLERS.get(backend_type)
    if handler is None:
        return _signals_for_other(backend_type, attrs)
    return handler(attrs)


# ─── Public parser ────────────────────────────────────────────────────


def parse_state_backends_in_dir(module_dir: Path, *, module_path: str) -> list[StateBackend]:
    """Scan every top-level `*.tf` file under `module_dir` for backend
    blocks and return them as `StateBackend` instances.

    `module_path` is the repo-relative directory string (`"."` for the
    repo root) to record on each result — the renderer uses it to label
    the row in multi-module repos. The caller is responsible for
    computing it (the extractor does, via `relative_to(repo_path)`).

    Multiple backend declarations under the same module dir are all
    returned; Terraform itself rejects duplicates at init time, so >1
    result usually means the operator has stale/dead code to clean up
    and the rendered page is the right place to surface that.

    File read failures, malformed blocks, and unbalanced braces all
    degrade gracefully — they're logged at WARNING and the bad block
    is skipped. A repo with one good backend and one truncated one
    still gets the good one on its page.
    """
    backends: list[StateBackend] = []
    for tf_file in sorted(module_dir.glob("*.tf")):
        try:
            text = tf_file.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("state_backend: could not read %s: %s", tf_file, exc)
            continue
        text = _strip_comments(text)
        for tf_match in _TERRAFORM_BLOCK_RE.finditer(text):
            tf_body_pair = _scan_braced_body(text, tf_match.end())
            if tf_body_pair is None:
                logger.warning("state_backend: unbalanced `terraform {` in %s", tf_file)
                continue
            tf_body, _ = tf_body_pair
            for be_match in _BACKEND_BLOCK_RE.finditer(tf_body):
                backend_type = be_match.group(1)
                be_body_pair = _scan_braced_body(tf_body, be_match.end())
                if be_body_pair is None:
                    logger.warning(
                        'state_backend: unbalanced `backend "%s" {` in %s — skipping',
                        backend_type,
                        tf_file,
                    )
                    continue
                be_body, _ = be_body_pair
                attrs = _parse_attrs(be_body)
                backends.append(
                    StateBackend(
                        module_path=module_path,
                        type=backend_type,
                        attrs=attrs,
                        signals=compute_signals(backend_type, attrs),
                    )
                )
    return backends
