"""Structural extraction via the `terraform-docs` CLI.

For each cloned repo we walk the tree, find every directory containing at
least one `.tf` file (skipping `.git/`, `.terraform/`, `vendor/`, and
similar noise), then run `terraform-docs json .` in each one and aggregate
the resulting per-module JSON via `_build_summary`. A
`resource_counts_by_type` map is computed for the renderer.

Why the Python walk (instead of `terraform-docs --recursive`):
  * `terraform-docs json .` at the repo root only captures `.tf` files at
    the root, leaving entire repos blank when the Terraform lives under
    `terraform/`, `env/{prod,staging}/`, `environments/…` (the common
    layout for any non-trivial IaC repo).
  * `terraform-docs --recursive` is not usable for our aggregation: with
    `--recursive-path=modules` it errors on flat repos missing that dir;
    with `--recursive-path=.` it requires `--output-file` (per-module file
    writes, not stdout aggregation).
The walk-and-aggregate path side-steps both quirks and works on every
repo layout we have seen in the wild.

`terraform-docs` does NOT require `terraform init` — it uses its own HCL
parser. That's the whole reason we picked it.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from iac_cartographer.constants import ExtractionError
from iac_cartographer.models import (
    ModuleRef,
    OutputRef,
    ProviderRef,
    ResourceRef,
    StateBackend,
    TerraformSummary,
    VariableRef,
)
from iac_cartographer.state_backend import parse_state_backends_in_dir

logger = logging.getLogger("iac_cartographer.extractor")

TERRAFORM_DOCS_TIMEOUT_S = 60
# Safety cap on the number of `.tf`-containing dirs we'll process per repo.
# A normal IaC repo has under 50 module dirs; if discovery sees more than
# this, something is wrong (vendored copy of a giant tree, accidental
# checkout of node_modules-style content, etc.) and we'd rather skip the
# overflow than spend minutes on terraform-docs invocations.
MAX_TF_DIRS_PER_REPO = 200
# Directories we never descend into. `.terraform` holds provider caches
# (post `terraform init` — shouldn't be in the repo but we've seen
# accidental check-ins). `vendor` is a Go convention. `.venv` / `venv` /
# `node_modules` are language ecosystems' dep dumps. `.git` is obvious.
_SKIP_DIRS: frozenset[str] = frozenset(
    {".git", ".terraform", "vendor", ".venv", "venv", "node_modules", "__pycache__", ".idea", ".vscode"}
)


def run_terraform_docs(repo_path: Path) -> TerraformSummary:
    """Run terraform-docs on every `.tf`-containing dir in `repo_path` and
    aggregate the result into a single `TerraformSummary`.

    Raises `ExtractionError` only on whole-pipeline failures (missing binary,
    no .tf files found anywhere). Per-dir failures (rc!=0, timeout, invalid
    JSON) are logged and dropped — one bad submodule shouldn't blank out
    an entire repo's inventory.
    """
    if not repo_path.exists() or not repo_path.is_dir():
        raise ExtractionError(f"extractor: path is not a directory: {repo_path}")

    tf_dirs = _find_tf_dirs(repo_path)
    if not tf_dirs:
        # Discovery vouched that `.tf` files exist somewhere in this repo,
        # but our walk can't find any (could be an exotic extension, or
        # files only under one of the `_SKIP_DIRS` we exclude). Return an
        # empty summary rather than raising — the page still renders with
        # whatever narrative Bedrock produces from the README.
        logger.warning("extractor: no .tf-containing directories found under %s", repo_path)
        return TerraformSummary()

    if len(tf_dirs) > MAX_TF_DIRS_PER_REPO:
        logger.warning(
            "extractor: %s contains %d .tf-dirs (cap=%d); processing first %d only",
            repo_path,
            len(tf_dirs),
            MAX_TF_DIRS_PER_REPO,
            MAX_TF_DIRS_PER_REPO,
        )
        tf_dirs = tf_dirs[:MAX_TF_DIRS_PER_REPO]

    per_module: list[dict[str, Any]] = []
    for module_dir in tf_dirs:
        content = _run_terraform_docs_once(module_dir, repo_path)
        if content is not None:
            rel = module_dir.relative_to(repo_path).as_posix() or "."
            # Parse state-backend blocks alongside terraform-docs output.
            # terraform-docs intentionally doesn't surface backend config,
            # so this is the only way to get it onto the page. Failures
            # here log + drop the bad block (per `parse_state_backends_in_dir`)
            # so a malformed declaration doesn't sink the rest of the run.
            state_backends = parse_state_backends_in_dir(module_dir, module_path=rel)
            per_module.append({"path": rel, "content": content, "state_backends": state_backends})

    if not per_module:
        # Every per-dir invocation failed (or returned empty). Don't raise
        # the orchestrator into the all-failed path for one repo with a
        # weird Terraform layout — log loudly and return an empty summary.
        logger.warning("extractor: every terraform-docs invocation under %s failed or returned empty", repo_path)
        return TerraformSummary()

    logger.info(
        "extractor: %s — aggregated %d .tf-dir(s) into one summary",
        repo_path.name,
        len(per_module),
    )
    return _build_summary(per_module)


def _find_tf_dirs(repo_path: Path) -> list[Path]:
    """Return every directory under `repo_path` that contains at least one
    `*.tf` file (top-level files at that depth, not nested). Skips
    `_SKIP_DIRS` and any directory whose name starts with `.` apart from
    the repo root itself.

    Sorted for deterministic ordering across runs (banner-SHA stability).
    """
    found: set[Path] = set()
    for tf_file in repo_path.rglob("*.tf"):
        # Skip any path that goes through a banned directory.
        if any(
            part in _SKIP_DIRS or (part.startswith(".") and part != ".")
            for part in tf_file.relative_to(repo_path).parts[:-1]
        ):
            continue
        if not tf_file.is_file():
            continue
        found.add(tf_file.parent)
    return sorted(found)


# Module-level override config — written once per process. Some of our
# repos ship a per-module `.terraform-docs.yml` that sets
# `recursive.enabled: true`, which makes `terraform-docs json .` fail
# with "value of '--output-file' cannot be empty with '--recursive'".
# We force-disable recursion by passing `--config` to this file, which
# takes precedence over the in-tree config and lets us aggregate every
# submodule's JSON into stdout as a single object.
_OVERRIDE_CONFIG_PATH: Path | None = None
_OVERRIDE_CONFIG_BODY = "formatter: json\nrecursive:\n  enabled: false\n"


def _terraform_docs_override_config() -> Path:
    """Lazily materialise the override config file and return its path."""
    global _OVERRIDE_CONFIG_PATH
    if _OVERRIDE_CONFIG_PATH is None or not _OVERRIDE_CONFIG_PATH.exists():
        fd, name = tempfile.mkstemp(prefix="iac-cartographer-tfdocs-", suffix=".yml")
        path = Path(name)
        # mkstemp gave us an os-level fd; close it before writing via
        # Path.open() to avoid a double-open of the same path on Windows
        # (we're on Linux/Fargate but the lint rule is platform-neutral).
        import os

        os.close(fd)
        path.write_text(_OVERRIDE_CONFIG_BODY, encoding="utf-8")
        _OVERRIDE_CONFIG_PATH = path
    return _OVERRIDE_CONFIG_PATH


# `terraform-docs json` strips the `source` field from `providers[]` and
# from `requirements[]` — both v0.20.x and v0.24.x verified. The only way
# to recover the source for the Confluence table is to parse the HCL
# directly. We do this with a small brace-counting scanner rather than a
# full HCL parser: we only need the `required_providers { ... }` block,
# its shape is near-universal across real-world repos, and adding
# python-hcl2 as a dep would be overkill. For any pattern this misses,
# the page falls back to the curated inference map
# (renderer.infer_provider_source) which still labels the row
# "(not declared)" — a strict superset of the pre-patch behaviour, never
# worse.
_REQUIRED_PROVIDERS_START_RE = re.compile(r"required_providers\s*\{")
# An individual provider entry inside the block, e.g.
#   aws = {
#     source  = "hashicorp/aws"
#     version = ">= 6.0"
#   }
_PROVIDER_ENTRY_RE = re.compile(
    r"(?P<name>[A-Za-z_][A-Za-z0-9_-]*)\s*=\s*\{(?P<body>[^{}]*)\}",
    re.DOTALL,
)
_SOURCE_FIELD_RE = re.compile(r'source\s*=\s*"([^"]+)"')
_VERSION_FIELD_RE = re.compile(r'version\s*=\s*"([^"]+)"')


def _extract_required_providers_body(text: str) -> list[str]:
    """Return every `required_providers { ... }` block's body (just the
    contents between the outer braces). Uses a depth-1 brace counter
    because a regex `.*?` would terminate at the first inner closing
    brace (the brace of an individual provider entry like `aws = {…}`)."""
    bodies: list[str] = []
    for start_match in _REQUIRED_PROVIDERS_START_RE.finditer(text):
        depth = 1
        i = start_match.end()
        body_start = i
        while i < len(text) and depth > 0:
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            i += 1
        if depth == 0:
            # `i` is now one past the matching close-brace; the body is
            # everything up to (but not including) that close-brace.
            bodies.append(text[body_start : i - 1])
    return bodies


def _parse_required_providers_in_dir(module_dir: Path) -> dict[str, dict[str, str | None]]:
    """Scan every `*.tf` file in `module_dir` for `required_providers` blocks
    and return `{provider_name: {"source": ..., "version": ...}}`.

    Returns an empty dict if no block is found. Each value's `source` /
    `version` is either the literal string from the HCL or `None` when the
    field wasn't declared. Multiple blocks in the same dir are merged;
    later entries don't overwrite earlier ones (defensive — should be a
    no-op in practice since the block is conventionally declared once)."""
    result: dict[str, dict[str, str | None]] = {}
    for tf_file in sorted(module_dir.glob("*.tf")):
        try:
            text = tf_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for block_body in _extract_required_providers_body(text):
            for entry_match in _PROVIDER_ENTRY_RE.finditer(block_body):
                name = entry_match.group("name")
                if name in result:
                    continue
                body = entry_match.group("body")
                src_m = _SOURCE_FIELD_RE.search(body)
                ver_m = _VERSION_FIELD_RE.search(body)
                result[name] = {
                    "source": src_m.group(1) if src_m else None,
                    "version": ver_m.group(1) if ver_m else None,
                }
    return result


def _enrich_providers_with_parsed_sources(
    data: dict[str, Any],
    parsed: dict[str, dict[str, str | None]],
) -> None:
    """Mutate `data["providers"]` in place, filling in `source` (and
    `version` when missing) from the HCL-parsed `required_providers` map.
    Never overwrites a value already present — terraform-docs is the
    source of truth when it actually emits the field."""
    for p in data.get("providers", []) or []:
        if not isinstance(p, dict):
            continue
        name = p.get("name")
        if not name or name not in parsed:
            continue
        entry = parsed[name]
        if not p.get("source") and entry.get("source"):
            p["source"] = entry["source"]
        if not p.get("version") and entry.get("version"):
            p["version"] = entry["version"]


def _run_terraform_docs_once(module_dir: Path, repo_path: Path) -> dict[str, Any] | None:
    """Invoke `terraform-docs json .` in `module_dir`, then patch the result
    with `source`/`version` parsed from local `required_providers` blocks.

    Returns the merged JSON dict on success, `None` on failure (logged,
    not raised). `repo_path` is only used for clean log messages — paths
    logged as relative-to-repo rather than as 30-char tempdir prefixes.
    """
    # `--config` forces our override (recursive: enabled: false) so that
    # repos shipping their own `.terraform-docs.yml` with recursive=true
    # don't break our invocation — see `_terraform_docs_override_config`.
    cmd = ["terraform-docs", "--config", str(_terraform_docs_override_config()), "json", "."]
    rel = module_dir.relative_to(repo_path).as_posix() or "."
    try:
        result = subprocess.run(  # noqa: S603 — args are static
            cmd,
            cwd=module_dir,
            check=False,
            capture_output=True,
            timeout=TERRAFORM_DOCS_TIMEOUT_S,
            text=True,
        )
    except FileNotFoundError as exc:
        # `terraform-docs` binary missing — this is a whole-pipeline
        # problem, not per-module. Surface it so the orchestrator records
        # the repo as failed instead of producing a misleading partial.
        raise ExtractionError(f"terraform-docs binary not found on PATH: {exc}") from exc
    except subprocess.TimeoutExpired:
        logger.warning("extractor: terraform-docs timed out in %s/%s — skipping", repo_path.name, rel)
        return None

    if result.returncode != 0:
        logger.warning(
            "extractor: terraform-docs rc=%d in %s/%s — skipping (stderr: %s)",
            result.returncode,
            repo_path.name,
            rel,
            (result.stderr or "").strip()[:200],
        )
        return None

    raw = result.stdout.strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("extractor: invalid JSON from terraform-docs in %s/%s: %s", repo_path.name, rel, exc)
        return None

    if not isinstance(data, dict):
        return None

    # Backfill the `source` (and any missing `version`) field on each
    # provider from the HCL we parse directly — terraform-docs' JSON
    # formatter drops these even when `required_providers` is correctly
    # declared. Without this step every page reads `(not declared)` even
    # for repos with picture-perfect HCL.
    parsed = _parse_required_providers_in_dir(module_dir)
    if parsed:
        _enrich_providers_with_parsed_sources(data, parsed)

    return data


def _build_summary(data: dict[str, Any] | list[dict[str, Any]]) -> TerraformSummary:
    """Collapse a terraform-docs JSON document (single module or recursive list)
    into a single `TerraformSummary`."""
    # `--recursive` returns a list of `{path, content: {...}}` entries OR (older
    # versions) a flat single-module dict. Handle both.
    modules_data: list[dict[str, Any]]
    if isinstance(data, dict) and "content" not in data and "modules" not in data and "resources" in data:
        # Flat single-module shape — wrap it.
        modules_data = [{"path": ".", "content": data}]
    elif isinstance(data, dict) and "content" in data:
        modules_data = [data]
    elif isinstance(data, list):
        modules_data = data
    else:
        # Brand-new shape we don't know — assume it's already the merged content.
        modules_data = [{"path": ".", "content": data if isinstance(data, dict) else {}}]

    providers: list[ProviderRef] = []
    modules: list[ModuleRef] = []
    resources: list[ResourceRef] = []
    inputs: list[VariableRef] = []
    outputs: list[OutputRef] = []
    requirements: dict[str, str] = {}

    seen_providers: set[tuple[str, str | None, str | None]] = set()
    seen_modules: set[tuple[str, str]] = set()
    seen_resources: set[tuple[str, str, str]] = set()
    seen_inputs: set[str] = set()
    seen_outputs: set[str] = set()
    module_paths: list[str] = []
    state_backends: list[StateBackend] = []
    seen_backend_keys: set[tuple[str, str, tuple[tuple[str, str], ...]]] = set()

    for module in modules_data:
        # Capture the path field per-module so the page renders the actual
        # repo layout and the narrator prompt can feed it to Sonnet. Skip the
        # implicit `"."` we inject for single-module shapes — it's not useful
        # information ("the root is a module" is the trivial case).
        path = module.get("path") if isinstance(module, dict) else None
        if isinstance(path, str) and path and path != "." and path not in module_paths:
            module_paths.append(path)
        content = module.get("content", module) if isinstance(module, dict) else {}
        if not isinstance(content, dict):
            continue
        for p in content.get("providers", []) or []:
            key = (p.get("name", ""), p.get("alias"), p.get("source"))
            if key in seen_providers or not p.get("name"):
                continue
            seen_providers.add(key)
            providers.append(
                ProviderRef(
                    name=p["name"],
                    source=p.get("source"),
                    version=p.get("version"),
                    alias=p.get("alias"),
                )
            )
        for m in content.get("modules", []) or []:
            key2 = (m.get("name", ""), m.get("source", ""))
            if key2 in seen_modules or not m.get("name") or not m.get("source"):
                continue
            seen_modules.add(key2)
            modules.append(ModuleRef(name=m["name"], source=m["source"], version=m.get("version")))
        for r in content.get("resources", []) or []:
            r_type = r.get("type", "")
            r_name = r.get("name", "")
            r_mode = r.get("mode", "managed")
            if not r_type or not r_name:
                continue
            key3 = (r_type, r_name, r_mode)
            if key3 in seen_resources:
                continue
            seen_resources.add(key3)
            resources.append(
                ResourceRef(
                    type=r_type,
                    name=r_name,
                    mode=r_mode if r_mode in ("managed", "data") else "managed",
                    provider=r.get("provider"),
                )
            )
        for v in content.get("inputs", []) or []:
            if not v.get("name") or v["name"] in seen_inputs:
                continue
            seen_inputs.add(v["name"])
            inputs.append(
                VariableRef(
                    name=v["name"],
                    type=_safe_type(v.get("type")),
                    description=v.get("description"),
                    default=v.get("default"),
                    required=v.get("required", False),
                )
            )
        for o in content.get("outputs", []) or []:
            if not o.get("name") or o["name"] in seen_outputs:
                continue
            seen_outputs.add(o["name"])
            outputs.append(OutputRef(name=o["name"], description=o.get("description")))
        for req in content.get("requirements", []) or []:
            name = req.get("name")
            ver = req.get("version")
            if name and ver:
                requirements[name] = ver
        # State backends — parsed outside terraform-docs (it intentionally
        # doesn't surface backend config). The per-module dict gets these
        # attached in `run_terraform_docs()`; aggregate + dedupe here.
        # Dedupe key: (module_path, type, frozen attrs). Different runs of
        # the same `.tf` file would otherwise be reported as duplicates if
        # multiple `terraform { backend ... }` blocks were declared (rare,
        # but legal — Terraform itself rejects at init, we just surface).
        for backend in module.get("state_backends", []) if isinstance(module, dict) else []:
            if not isinstance(backend, StateBackend):
                continue
            key4 = (backend.module_path, backend.type, tuple(sorted(backend.attrs.items())))
            if key4 in seen_backend_keys:
                continue
            seen_backend_keys.add(key4)
            state_backends.append(backend)

    counts: dict[str, int] = dict(Counter(r.type for r in resources))

    return TerraformSummary(
        providers=providers,
        requirements=requirements,
        modules=modules,
        resources=resources,
        inputs=inputs,
        outputs=outputs,
        resource_counts_by_type=counts,
        state_backends=sorted(state_backends, key=lambda b: (b.module_path, b.type)),
        module_paths=sorted(module_paths),
    )


def _safe_type(t: object) -> str | None:
    """terraform-docs sometimes emits `type` as a nested dict (object/list types).
    Coerce to a string so the Pydantic schema (which expects `str | None`)
    doesn't blow up; lossy but acceptable for narrative purposes."""
    if t is None:
        return None
    if isinstance(t, str):
        return t
    return json.dumps(t, separators=(",", ":"))[:200]
