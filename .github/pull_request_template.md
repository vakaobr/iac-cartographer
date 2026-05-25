<!--
  Thanks for sending a PR! A few quick notes:

  * For non-trivial changes, please open an issue first so we can agree
    on the shape before you invest time in code.
  * Keep PRs focused on a single concern — easier to review, easier to
    revert.
  * Tests are required for new behaviour (the project sits around 87 %
    coverage; please don't drop the floor).
-->

## What does this change?

<!-- One or two sentences. The WHY, not just the WHAT. -->

## Why does it need to change?

<!-- The use case, bug symptom, or roadmap item this addresses. -->

## How was it tested?

<!--
  - [ ] Added unit tests for the new behaviour
  - [ ] Ran `pytest --cov-fail-under=60` locally
  - [ ] Manually verified against a real Confluence / Bedrock setup
        (optional, but mention if you did)
-->

## Related issues / PRs

<!-- "Fixes #123", "Closes #456", "Related to #789", or "n/a". -->

## Checklist

- [ ] `ruff check .` passes
- [ ] `ruff format --check .` passes
- [ ] `pytest --cov-fail-under=60` passes
- [ ] User-facing changes are reflected in README / examples
- [ ] No SECRA-specific or other organisation-specific identifiers in code or comments
- [ ] Commit message explains the WHY, not just the WHAT
