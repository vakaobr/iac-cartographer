"""Bedrock Sonnet narrator — turn a (meta, summary, README, HCL) tuple into
a Pydantic-validated `BedrockNarrative`.

The hybrid extraction strategy (ADR-005) gives Sonnet a deterministic
skeleton so it can't hallucinate resources. We additionally enforce that
constraint at the call site by filtering `key_resources_explained` to
resource types that actually appear in the `TerraformSummary` — defense in
depth.

JSON-parse failures get exactly one retry with a stricter prompt. A second
failure returns `(None, in_tokens, out_tokens)` so the orchestrator can
publish a structurally-complete page with a `narrative=None` placeholder.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from iac_cartographer.aws import invoke_bedrock_model
from iac_cartographer.models import BedrockNarrative, ResourceExplanation
from iac_cartographer.prompts import RETRY_INSTRUCTION, SYSTEM_PROMPT

if TYPE_CHECKING:
    from iac_cartographer.models import BedrockConfig, RepoMetadata, TerraformSummary

logger = logging.getLogger("iac_cartographer.narrator")

# Per-input size caps. Sonnet 4.6 can handle a 1M context but cost scales
# linearly; capping inputs is the primary lever for predictable spend.
README_CAP_CHARS = 8000
HCL_CAP_CHARS = 30_000


def build_request(
    meta: RepoMetadata,
    summary: TerraformSummary,
    readme: str,
    hcl_concat: str,
    config: BedrockConfig,
    *,
    retry: bool = False,
) -> dict[str, Any]:
    """Assemble the Bedrock invoke_model body.

    The system block is marked `cache_control: {"type": "ephemeral"}` so Sonnet
    caches it across the ~15 per-repo invocations in a single run (~90% of
    system-prompt input tokens charged at cache-read rates, per Anthropic).
    """
    tf_docs_json = json.dumps(summary.model_dump(), separators=(",", ":"))
    # `<module-paths>` is technically redundant with `summary.module_paths`
    # inside `<tf-docs-json>`, but Sonnet picks up explicit, named blocks
    # far more reliably than fields buried in a JSON blob. Cheap to repeat;
    # the cache-control on the system prompt eats most of the token cost.
    module_paths_block = ", ".join(summary.module_paths) if summary.module_paths else "(single root-level module)"
    user_content: list[dict[str, str]] = [
        {"type": "text", "text": f"<repo>{meta.full_name}</repo>"},
        {"type": "text", "text": f"<module-paths>{module_paths_block}</module-paths>"},
        {"type": "text", "text": f"<tf-docs-json>{tf_docs_json}</tf-docs-json>"},
        {"type": "text", "text": f"<readme>{readme[:README_CAP_CHARS]}</readme>"},
        {
            "type": "text",
            "text": f"<terraform-snippets>{hcl_concat[:HCL_CAP_CHARS]}</terraform-snippets>",
        },
    ]
    if retry:
        user_content.append({"type": "text", "text": RETRY_INSTRUCTION})
    return {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": config.max_tokens,
        "system": [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user_content}],
    }


def summarize(
    meta: RepoMetadata,
    summary: TerraformSummary,
    readme: str,
    hcl_concat: str,
    config: BedrockConfig,
) -> tuple[BedrockNarrative | None, int, int]:
    """Invoke Bedrock; return (narrative-or-None, tokens_in, tokens_out).

    Never raises — Bedrock client errors (throttling, auth) are caught and
    surfaced as `(None, 0, 0)` so the orchestrator continues with the next
    repo. The error is logged with full context.
    """
    tokens_in = 0
    tokens_out = 0

    # First attempt
    narrative, tin, tout = _invoke_once(meta, summary, readme, hcl_concat, config, retry=False)
    tokens_in += tin
    tokens_out += tout
    if narrative is not None:
        return _enforce_resource_type_grounding(narrative, summary), tokens_in, tokens_out

    # One retry with a stricter "ONLY JSON" instruction
    logger.warning("narrator: %s — first attempt failed, retrying with stricter prompt", meta.full_name)
    narrative, tin, tout = _invoke_once(meta, summary, readme, hcl_concat, config, retry=True)
    tokens_in += tin
    tokens_out += tout
    if narrative is not None:
        return _enforce_resource_type_grounding(narrative, summary), tokens_in, tokens_out

    logger.error("narrator: %s — both attempts failed; falling back to narrative=None", meta.full_name)
    return None, tokens_in, tokens_out


def _invoke_once(
    meta: RepoMetadata,
    summary: TerraformSummary,
    readme: str,
    hcl_concat: str,
    config: BedrockConfig,
    *,
    retry: bool,
) -> tuple[BedrockNarrative | None, int, int]:
    """One invocation. Returns (narrative-or-None, tokens_in, tokens_out)."""
    body = build_request(meta, summary, readme, hcl_concat, config, retry=retry)
    try:
        response = invoke_bedrock_model(config.model_id, body)
    except Exception:
        logger.exception("narrator: %s — bedrock invoke failed", meta.full_name)
        return None, 0, 0

    usage = response.get("usage", {}) if isinstance(response, dict) else {}
    tokens_in = int(usage.get("input_tokens", 0) or 0)
    tokens_out = int(usage.get("output_tokens", 0) or 0)

    text = _extract_text(response)
    if text is None:
        logger.error("narrator: %s — bedrock returned no text content", meta.full_name)
        return None, tokens_in, tokens_out

    text = _strip_markdown_fences(text)

    try:
        narrative = BedrockNarrative.model_validate_json(text)
    except (ValidationError, json.JSONDecodeError) as exc:
        logger.warning(
            "narrator: %s — JSON parse / schema validation failed: %s",
            meta.full_name,
            str(exc)[:300],
        )
        return None, tokens_in, tokens_out

    return narrative, tokens_in, tokens_out


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)


def _strip_markdown_fences(text: str) -> str:
    """Strip surrounding ```json ... ``` (or plain ```) fences if present.

    Sonnet 4.5 routinely wraps JSON output in markdown code fences even when
    the system prompt explicitly asks for raw JSON; Sonnet 4.6 didn't. The
    fenced form is ````json\\n{...}\\n````. We
    only strip when both bookends are present; otherwise return text
    untouched so a partial fence doesn't get mangled into invalid JSON.
    """
    match = _FENCE_RE.match(text)
    return match.group(1) if match else text


def _extract_text(response: dict[str, Any]) -> str | None:
    """Pull the text content out of the Bedrock Claude response envelope.

    Claude on Bedrock returns `{"content": [{"type": "text", "text": "..."}, ...]}`.
    Concatenate text blocks (usually only one for a JSON-output task).
    """
    if not isinstance(response, dict):
        return None
    content = response.get("content", [])
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            t = block.get("text")
            if isinstance(t, str):
                parts.append(t)
    return "".join(parts) if parts else None


def _enforce_resource_type_grounding(narrative: BedrockNarrative, summary: TerraformSummary) -> BedrockNarrative:
    """Drop any `key_resources_explained` whose `resource_type` doesn't appear
    in `summary.resources`. Defense in depth against the model inventing
    types — the prompt already forbids this, but we still verify."""
    valid_types = {r.type for r in summary.resources}
    filtered = [e for e in narrative.key_resources_explained if e.resource_type in valid_types]
    if len(filtered) == len(narrative.key_resources_explained):
        return narrative
    dropped = len(narrative.key_resources_explained) - len(filtered)
    logger.warning("narrator: dropped %d hallucinated resource type(s) from narrative", dropped)
    return narrative.model_copy(update={"key_resources_explained": filtered})


def placeholder_narrative() -> BedrockNarrative:
    """For `--no-bedrock` mode: produce a deterministic placeholder so the
    renderer still has a `narrative` to consume."""
    return BedrockNarrative(
        purpose=("(Bedrock summarization disabled via --no-bedrock; structural facts below come from terraform-docs.)"),
        key_resources_explained=[],
        environments=[],
        owning_team_guess=None,
        notable_patterns=[],
    )


# AI-H1: trigger phrases that suggest indirect prompt injection has steered
# the model toward misleading narrative text. If detected, the orchestrator
# drops the narrative (page still renders with structural facts) and
# surfaces a WARN Slack notification so an operator can inspect the source
# repo for a malicious comment.
#
# Phrases are checked case-insensitively as substring matches against
# `purpose` + `notable_patterns` + `environments`. False-positive cost is one
# manual review per match. False-negative cost is a misleading Confluence
# page until the next weekly run.
#
# Curation principle (revised 2026-05-25 after the first end-to-end run):
# pick phrases that are unambiguously prompt-injection sentinels — language
# that only appears when the model has been told to break out of its task.
# Generic IaC adjectives like "deprecated", "disabled", "archived",
# "obsolete" were removed because they legitimately appear in many
# narratives (deprecated module, versioning_disabled, archived bucket,
# obsolete provider). The first pass flagged 6 of 33 repos as suspicious
# purely on those words, suppressing real content; that's worse than a
# narrower watchlist that catches actual injection language.
SUSPICIOUS_PHRASES: tuple[str, ...] = (
    # Explicit instruction-override patterns (most common injection forms)
    "ignore previous",
    "ignore prior",
    "ignore the above",
    "ignore all previous",
    "disregard previous",
    "disregard the above",
    # Role-confusion / meta-statements about the model
    "as an ai",
    "as a language model",
    "i am an ai",
    "i was instructed",
    "system prompt",
    "[system]",
    # Imperatives directed at the reader/operator
    "do not use this",
    "do not access",
    "do not visit",
    "click here",
    "stop using",
    # Hard decommissioning calls — legitimate use in a repo's purpose
    # statement is rare (purpose = what it does, not that it's gone),
    # so these stay even though they're closer to vocabulary.
    "scheduled for deletion",
    "scheduled for decommission",
    "decommissioned",
)


def detect_suspicious_phrases(narrative: BedrockNarrative) -> list[str]:
    """Return the suspicious phrases (lowercased) that appeared in the
    narrative's freeform text fields. Empty list = clean."""
    haystack_parts = [narrative.purpose, *narrative.notable_patterns, *narrative.environments]
    haystack = " ".join(haystack_parts).lower()
    return [phrase for phrase in SUSPICIOUS_PHRASES if phrase in haystack]


__all__ = [
    "SUSPICIOUS_PHRASES",
    "ResourceExplanation",  # re-export for convenience
    "build_request",
    "detect_suspicious_phrases",
    "placeholder_narrative",
    "summarize",
]
