#!/usr/bin/env bash
# Demo runner — clones three public Terraform repos, extracts their
# structure with terraform-docs, and writes the rendered inventory to a
# local directory.
#
# Variants (all zero-credentials):
#   ./run.sh                       Markdown publisher, placeholder LLM (default)
#   ./run.sh --publisher markdown  same as default
#   ./run.sh --publisher html      standalone HTML  → ./demo-output-html/
#   ./run.sh --publisher json      machine-readable → ./demo-output-json/
#   ./run.sh --llm ollama          REAL narratives via a local Ollama server
#                                  (Markdown) → ./demo-output-ollama/
#
# Requirements:
#   * Python 3.12+ with iac-cartographer installed (`pip install iac-cartographer`
#     or `pip install -e .` from a checkout).
#   * terraform-docs on the PATH.
#   * git on the PATH (the fetcher shells out to `git clone --depth=1`).
#   * Network access to github.com.
#   * For `--llm ollama` only: a running Ollama server with a model pulled
#     (https://ollama.com; `ollama pull llama3.1:8b`). If none is reachable
#     the run prints a clear message and skips — it does not hang or crash.
#
# No real credentials needed: every IAC_CARTOGRAPHER_SECRET_* env var
# below is a stub value just sufficient for the Pydantic schema check.
# Every variant except `--llm ollama` passes `--no-llm`, which skips
# the LLM entirely so no Anthropic / AWS Bedrock call is made. The
# `--llm ollama` variant talks to a LOCAL server only (no cloud).
#
# The Slack post at the end of the run will fail with the dummy token
# — that's expected, the failure is logged at WARN level and doesn't
# fail the run.

set -euo pipefail

# Resolve directory of this script — so the demo works from any CWD.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# ─── Parse the demo-specific flags ───────────────────────────────────
# Anything we don't recognise is forwarded to the CLI verbatim (kept in
# PASSTHRU) so `./run.sh --verbose` etc. still work.
PUBLISHER="markdown"
LLM="none"
PASSTHRU=()

while [ "$#" -gt 0 ]; do
  case "$1" in
    --publisher)
      PUBLISHER="${2:-}"
      shift 2
      ;;
    --publisher=*)
      PUBLISHER="${1#*=}"
      shift
      ;;
    --llm)
      LLM="${2:-}"
      shift 2
      ;;
    --llm=*)
      LLM="${1#*=}"
      shift
      ;;
    -h|--help)
      sed -n '2,30p' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *)
      PASSTHRU+=("$1")
      shift
      ;;
  esac
done

# ─── Map flags → config file + LLM behaviour ─────────────────────────
# `--llm ollama` wins over `--publisher` for config selection because it
# needs the ollama-specific llm: block; it always publishes Markdown.
NO_LLM="--no-llm"
case "$LLM" in
  none)
    case "$PUBLISHER" in
      markdown) CONFIG="examples/demo/config.yaml";        OUTPUT_DIR="./demo-output";      OUTPUT_INDEX="index.md" ;;
      html)     CONFIG="examples/demo/config-html.yaml";   OUTPUT_DIR="./demo-output-html"; OUTPUT_INDEX="index.html" ;;
      json)     CONFIG="examples/demo/config-json.yaml";   OUTPUT_DIR="./demo-output-json"; OUTPUT_INDEX="index.json" ;;
      *)
        echo "error: unknown --publisher '$PUBLISHER' (expected: markdown | html | json)" >&2
        exit 2
        ;;
    esac
    ;;
  ollama)
    # Real local LLM — do NOT pass --no-llm.
    NO_LLM=""
    CONFIG="examples/demo/config-ollama.yaml"
    OUTPUT_DIR="./demo-output-ollama"
    OUTPUT_INDEX="index.md"
    ;;
  *)
    echo "error: unknown --llm '$LLM' (expected: none | ollama)" >&2
    exit 2
    ;;
esac

# Dummy credentials — none of these will be transmitted to a real
# service. The Pydantic models just need *something* that parses.
export IAC_CARTOGRAPHER_SECRET_CONFLUENCE='{"email":"demo@example.com","api_token":"DEMO"}'
export IAC_CARTOGRAPHER_SECRET_GITLAB='{"token":"glpat-DEMO"}'
export IAC_CARTOGRAPHER_SECRET_GITHUB='{"token":"ghp_DEMO"}'
export IAC_CARTOGRAPHER_SECRET_SLACK='{"bot_token":"xoxb-DEMO"}'

cd "$REPO_ROOT"

# ─── Ollama reachability pre-check (graceful degradation) ────────────
# Probe Ollama's /api/tags endpoint before running. If nothing answers,
# print a clear message and skip — never hang on a 5-minute timeout or
# crash mid-pipeline.
if [ "$LLM" = "ollama" ]; then
  OLLAMA_URL="${OLLAMA_HOST:-http://localhost:11434}"
  echo "Checking for a local Ollama server at ${OLLAMA_URL} ..."
  REACHABLE=0
  if command -v curl >/dev/null 2>&1; then
    if curl -fsS --max-time 3 "${OLLAMA_URL}/api/tags" >/dev/null 2>&1; then
      REACHABLE=1
    fi
  elif command -v wget >/dev/null 2>&1; then
    if wget -q -T 3 -O /dev/null "${OLLAMA_URL}/api/tags" 2>/dev/null; then
      REACHABLE=1
    fi
  else
    echo "warning: neither curl nor wget found — cannot pre-check Ollama." >&2
    echo "         Install Ollama (https://ollama.com) or run without --llm ollama." >&2
    exit 0
  fi

  if [ "$REACHABLE" -ne 1 ]; then
    echo
    echo "────────────────────────────────────────────────────────────────"
    echo " No Ollama server reachable at ${OLLAMA_URL}."
    echo
    echo " The --llm ollama variant needs a LOCAL Ollama server running"
    echo " with a model pulled. To set it up:"
    echo "   1. Install Ollama:  https://ollama.com"
    echo "   2. Pull a model:    ollama pull llama3.1:8b"
    echo "   3. Re-run:          ./examples/demo/run.sh --llm ollama"
    echo
    echo " Skipping (no cloud fallback by design). For placeholder"
    echo " narratives with no LLM at all, run:  ./examples/demo/run.sh"
    echo "────────────────────────────────────────────────────────────────"
    exit 0
  fi
  echo "Ollama is reachable — generating REAL narratives."
fi

echo "════════════════════════════════════════════════════════════════"
echo " iac-cartographer demo"
echo "════════════════════════════════════════════════════════════════"
echo " Cloning 3 public Terraform repos, extracting structure, and"
echo " writing the rendered inventory to ${OUTPUT_DIR}/."
echo "   publisher : ${PUBLISHER}"
if [ "$LLM" = "ollama" ]; then
  echo "   llm       : ollama (real local narratives)"
else
  echo "   llm       : none (--no-llm placeholder narratives)"
fi
echo " Expected runtime: 30-90 seconds depending on network."
echo "════════════════════════════════════════════════════════════════"
echo

# shellcheck disable=SC2086
iac-cartographer --once $NO_LLM --config "$CONFIG" "${PASSTHRU[@]+"${PASSTHRU[@]}"}"

echo
echo "════════════════════════════════════════════════════════════════"
echo " Done. Open the result:"
echo "   • Overview: ${OUTPUT_DIR}/${OUTPUT_INDEX}"
if [ "$PUBLISHER" = "json" ]; then
  echo "   • Per-repo: ${OUTPUT_DIR}/repos/*.json"
elif [ "$PUBLISHER" = "html" ]; then
  echo "   • Per-repo: ${OUTPUT_DIR}/repos/*.html"
else
  echo "   • Per-repo: ${OUTPUT_DIR}/repos/*.md"
fi
echo "════════════════════════════════════════════════════════════════"
