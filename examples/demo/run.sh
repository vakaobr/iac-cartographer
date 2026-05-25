#!/usr/bin/env bash
# Demo runner — clones three public Terraform repos, extracts their
# structure with terraform-docs, and writes the rendered Markdown
# inventory to ./demo-output/.
#
# Requirements:
#   * Python 3.12+ with iac-cartographer installed (`pip install iac-cartographer`
#     or `pip install -e .` from a checkout).
#   * terraform-docs on the PATH.
#   * git on the PATH (the fetcher shells out to `git clone --depth=1`).
#   * Network access to github.com.
#
# No real credentials needed: every IAC_CARTOGRAPHER_SECRET_* env var
# below is a stub value just sufficient for the Pydantic schema check.
# `--no-bedrock` skips the LLM entirely so no Anthropic / AWS Bedrock
# call is made.
#
# The Slack post at the end of the run will fail with the dummy token
# — that's expected, the failure is logged at WARN level and doesn't
# fail the run.

set -euo pipefail

# Resolve directory of this script — so the demo works from any CWD.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Dummy credentials — none of these will be transmitted to a real
# service. The Pydantic models just need *something* that parses.
export IAC_CARTOGRAPHER_SECRET_CONFLUENCE='{"email":"demo@example.com","api_token":"DEMO"}'
export IAC_CARTOGRAPHER_SECRET_GITLAB='{"token":"glpat-DEMO"}'
export IAC_CARTOGRAPHER_SECRET_GITHUB='{"token":"ghp_DEMO"}'
export IAC_CARTOGRAPHER_SECRET_SLACK='{"bot_token":"xoxb-DEMO"}'

cd "$REPO_ROOT"

echo "════════════════════════════════════════════════════════════════"
echo " iac-cartographer demo"
echo "════════════════════════════════════════════════════════════════"
echo " Cloning 3 public Terraform repos, extracting structure, and"
echo " writing the rendered inventory to ./demo-output/."
echo " Expected runtime: 30-90 seconds depending on network."
echo "════════════════════════════════════════════════════════════════"
echo

iac-cartographer --once --no-bedrock --config examples/demo/config.yaml "$@"

echo
echo "════════════════════════════════════════════════════════════════"
echo " Done. Open the result:"
echo "   • Overview: ./demo-output/index.md"
echo "   • Per-repo: ./demo-output/repos/*.md"
echo "════════════════════════════════════════════════════════════════"
