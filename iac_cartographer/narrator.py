"""Narrator — turn a (meta, summary, README, HCL) tuple into a
Pydantic-validated `BedrockNarrative`.

The hybrid extraction strategy gives the model a deterministic skeleton
so it can't hallucinate resources. We additionally enforce that constraint
at the call site by filtering `key_resources_explained` to resource types
that actually appear in the `TerraformSummary` — defense in depth.

JSON-parse failures get exactly one retry with a stricter prompt. A second
failure returns `(None, in_tokens, out_tokens)` so the orchestrator can
publish a structurally-complete page with a `narrative=None` placeholder.

LLM provider selection lives behind the `LLMBackend` ABC in `llm.py` —
this module only knows about prompts and Pydantic validation. Swap
backends at construction time, not in the narrator.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

from pydantic import ValidationError

from iac_cartographer.models import BedrockNarrative, ResourceExplanation
from iac_cartographer.prompts import RETRY_INSTRUCTION, SYSTEM_PROMPT

if TYPE_CHECKING:
    from iac_cartographer.llm import LLMBackend
    from iac_cartographer.models import BedrockConfig, RepoMetadata, TerraformSummary

logger = logging.getLogger("iac_cartographer.narrator")

# Per-input size caps. Modern Claude models can handle ~1M context but cost
# scales linearly; capping inputs is the primary lever for predictable spend.
README_CAP_CHARS = 8000
HCL_CAP_CHARS = 30_000


def build_user_blocks(
    meta: RepoMetadata,
    summary: TerraformSummary,
    readme: str,
    hcl_concat: str,
    *,
    retry: bool = False,
) -> list[dict[str, str]]:
    """Assemble the user-content blocks for one invocation.

    Returns a list of `{"type": "text", "text": "..."}` dicts in Anthropic
    Messages API shape. Backends pass this through to the provider; the
    cache-control behaviour on the system block belongs to the backend,
    not here."""
    tf_docs_json = json.dumps(summary.model_dump(), separators=(",", ":"))
    # `<module-paths>` is technically redundant with `summary.module_paths`
    # inside `<tf-docs-json>`, but the model picks up explicit, named blocks
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
    return user_content


def summarize(
    meta: RepoMetadata,
    summary: TerraformSummary,
    readme: str,
    hcl_concat: str,
    config: BedrockConfig,
    backend: LLMBackend,
) -> tuple[BedrockNarrative | None, int, int]:
    """Invoke the configured LLM backend; return (narrative-or-None,
    tokens_in, tokens_out).

    Never raises — backend errors (throttling, auth, network) are caught
    and surfaced as `(None, 0, 0)` so the orchestrator continues with the
    next repo. The error is logged with full context."""
    tokens_in = 0
    tokens_out = 0

    # First attempt
    narrative, tin, tout = _invoke_once(meta, summary, readme, hcl_concat, config, backend, retry=False)
    tokens_in += tin
    tokens_out += tout
    if narrative is not None:
        return _enforce_resource_type_grounding(narrative, summary), tokens_in, tokens_out

    # One retry with a stricter "ONLY JSON" instruction.
    logger.warning("narrator: %s — first attempt failed, retrying with stricter prompt", meta.full_name)
    narrative, tin, tout = _invoke_once(meta, summary, readme, hcl_concat, config, backend, retry=True)
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
    backend: LLMBackend,
    *,
    retry: bool,
) -> tuple[BedrockNarrative | None, int, int]:
    """One invocation. Returns (narrative-or-None, tokens_in, tokens_out)."""
    user_blocks = build_user_blocks(meta, summary, readme, hcl_concat, retry=retry)
    try:
        response = backend.invoke(
            model_id=config.model_id,
            system_prompt=SYSTEM_PROMPT,
            user_blocks=user_blocks,
            max_tokens=config.max_tokens,
        )
    except Exception:
        logger.exception("narrator: %s — LLM invoke failed", meta.full_name)
        return None, 0, 0

    if not response.text:
        logger.error("narrator: %s — LLM returned no text content", meta.full_name)
        return None, response.input_tokens, response.output_tokens

    text = _strip_markdown_fences(response.text)

    try:
        narrative = BedrockNarrative.model_validate_json(text)
    except (ValidationError, json.JSONDecodeError) as exc:
        logger.warning(
            "narrator: %s — JSON parse / schema validation failed: %s",
            meta.full_name,
            str(exc)[:300],
        )
        return None, response.input_tokens, response.output_tokens

    return narrative, response.input_tokens, response.output_tokens


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)


def _strip_markdown_fences(text: str) -> str:
    """Strip surrounding ```json ... ``` (or plain ```) fences if present.

    Sonnet 4.5 routinely wraps JSON output in markdown code fences even
    when the system prompt explicitly asks for raw JSON; Sonnet 4.6
    didn't. We only strip when both bookends are present; otherwise
    return text untouched so a partial fence doesn't get mangled into
    invalid JSON.
    """
    match = _FENCE_RE.match(text)
    return match.group(1) if match else text


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
    """Returned by the orchestrator when `--no-llm` is passed — used
    for local development to skip LLM costs."""
    return BedrockNarrative(
        purpose="(LLM narration skipped for this run — placeholder narrative.)",
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
# Curation principle: pick phrases that are unambiguously prompt-injection
# sentinels — language that only appears when the model has been told to
# break out of its task. Generic IaC adjectives like "deprecated",
# "disabled", "archived", "obsolete" were removed because they legitimately
# appear in many narratives.
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
    "HCL_CAP_CHARS",
    "README_CAP_CHARS",
    "SUSPICIOUS_PHRASES",
    "ResourceExplanation",  # re-export for convenience
    "build_user_blocks",
    "detect_suspicious_phrases",
    "placeholder_narrative",
    "summarize",
]
