#!/usr/bin/env bash
# Entrypoint for the GitHub Action wrapper around iac-cartographer.
#
# Reads the action's inputs from $INPUT_* environment variables (GitHub
# populates these automatically from `with:`), assembles the matching
# iac-cartographer CLI args, then execs the binary.
#
# Inputs:
#   INPUT_CONFIG       — required, path to config.yaml
#   INPUT_MODE         — once | lint  (default: once)
#   INPUT_DRY_RUN      — true | false (default: false)
#   INPUT_VERBOSE      — true | false (default: false)
#   INPUT_REPOS        — optional CSV repo filter
#   INPUT_MODEL        — optional LLM model override
#   INPUT_DIFF         — optional path to prior JSON snapshot for --diff
#   INPUT_LINT_PATH    — directory to lint when mode=lint (required then)
#   INPUT_LINT_FORMAT  — text | json | github  (default: github)
#   INPUT_FAIL_ON      — error | warn  (default: error)
#   INPUT_EXTRA_ARGS   — escape hatch for any other CLI flag

set -euo pipefail

# Args we'll build up and pass to iac-cartographer.
args=()

mode="${INPUT_MODE:-once}"

case "$mode" in
  once)
    args+=(--once)

    if [[ -n "${INPUT_CONFIG:-}" ]]; then
      args+=(--config "$INPUT_CONFIG")
    else
      echo "::error::input 'config' is required when mode=once" >&2
      exit 2
    fi

    [[ "${INPUT_DRY_RUN:-false}" == "true" ]]  && args+=(--dry-run)
    [[ "${INPUT_VERBOSE:-false}" == "true" ]]  && args+=(--verbose)
    [[ -n "${INPUT_REPOS:-}" ]]                && args+=(--repos "$INPUT_REPOS")
    [[ -n "${INPUT_MODEL:-}" ]]                && args+=(--model "$INPUT_MODEL")
    [[ -n "${INPUT_DIFF:-}" ]]                 && args+=(--diff "$INPUT_DIFF")
    ;;

  lint)
    if [[ -z "${INPUT_LINT_PATH:-}" ]]; then
      echo "::error::input 'lint-path' is required when mode=lint" >&2
      exit 2
    fi
    args+=(--lint "$INPUT_LINT_PATH")
    args+=(--format "${INPUT_LINT_FORMAT:-github}")
    args+=(--fail-on "${INPUT_FAIL_ON:-error}")
    ;;

  *)
    echo "::error::unknown mode '$mode' (valid: once, lint)" >&2
    exit 2
    ;;
esac

# Escape hatch: free-form extra args appended verbatim. Split on whitespace
# (callers needing literal spaces in a value should use INPUT_REPOS / etc.).
if [[ -n "${INPUT_EXTRA_ARGS:-}" ]]; then
  # shellcheck disable=SC2206  # intentional word-splitting
  extra=( $INPUT_EXTRA_ARGS )
  args+=( "${extra[@]}" )
fi

echo "::group::iac-cartographer ${args[*]}"
exec iac-cartographer "${args[@]}"
