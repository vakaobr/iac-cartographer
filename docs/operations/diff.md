# Between-run diff (`--diff`)

`iac-cartographer --diff <prev-output>` computes a **structural diff**
between the current run's inventory and a prior run's JSON output,
then prints a Markdown summary to stdout and attaches a one-line
summary to the end-of-run notification.

Slack readers stop seeing just `iac-cartographer: run complete — 42
repos published` and start seeing:

> iac-cartographer: run complete — 42 repos published
> _Diff vs prior run:_ 3 new, 1 archived, 2 changed; 36 unchanged

## Workflow

The diff feature consumes the JSON publisher's output, so the
workflow has two pieces:

1. **Baseline run** — run iac-cartographer with `publisher.kind: json`
   pointing somewhere persistent. This produces the snapshot the next
   run will diff against.
2. **Subsequent runs** — pass `--diff <path-to-baseline>` on the
   command line. The path is the same `json.output_dir` from the
   baseline run.

The publisher being used on the *current* run doesn't matter — the
diff reads the JSON snapshot from disk independently. A common
deployment runs **both** publishers in sequence (Confluence for
humans + JSON for the diff snapshot), but a JSON-only deployment
works too.

```bash
# Baseline (one-off, or part of every run)
iac-cartographer --once

# Subsequent runs: compare against the prior JSON output
iac-cartographer --once --diff ./iac-inventory-json
```

## Output shape

```
## Inventory diff

**Added (3):** acme-org/new-svc, acme-org/another-svc, acme-org/edge-cache
**Removed (1):** acme-org/old-svc
**Changed (2):**
  - acme-org/main-cluster: provider aws bumped (>= 5.0 → >= 6.0); +2 resources
  - acme-org/auth-service: module terraform-aws-vpc bumped (4.0.0 → 5.0.0)

37 unchanged.
```

When the inventory is identical to the prior run (no adds, no removes,
no structural changes), the renderer emits a one-line "No changes."
summary so downstream grep / regex consumers stay deterministic.

## What counts as a "structural change"

A repo is considered **changed** when at least one of these is true
between snapshots:

- A provider was added, removed, or had its version constraint bumped.
- A module was added, removed, or had its version constraint bumped.
- The total resource count (summed across all types) moved.

Narrative-only changes (the LLM rewords the `purpose` paragraph)
do **NOT** count as structural — they'd flood the diff with noise
since the model picks slightly different words each run. The
banner-SHA short-circuit on the publisher side already filters
narrative-only re-renders out of the actual republish path, so this
keeps the diff aligned with what an operator would actually see
change in their docs.

A repo where only the resource *distribution* shifted (e.g. 5
`aws_instance` → 5 `aws_lambda_function`, same total of 5) without
provider or module changes also doesn't count as structural — the
total is what shows up in dashboards and ages well.

## First-run baseline

Pass `--diff <path-that-doesnt-yet-exist>` and the diff renders
every current repo as `added`. Useful as the "initial inventory"
summary for a new deployment — `42 new; 0 unchanged` is a clean
starting line.

## Programmatic access

The diff is also available as a Python API for tooling that wants
to react to changes programmatically (CI gates, custom
notifications, drift dashboards):

```python
from iac_cartographer.diff import (
    compute_diff,
    load_prior_inventories,
    render_diff_markdown,
)

prior = load_prior_inventories("./iac-inventory-json")
diff = compute_diff(prior, current_inventories)

if diff.removed_repos:
    raise SystemExit("Repos were archived since last run — manual review required")

print(render_diff_markdown(diff))
```

The `InventoryDiff` model is JSON-serializable via Pydantic
(`diff.model_dump(mode="json")`) for downstream consumers that want
the structured form.

## Tips

- **Use the JSON publisher as a sidecar.** If you're already publishing
  to Confluence, add a JSON publisher pointed at a persistent
  directory (committed to the docs repo, or written to an S3 bucket
  mounted on the next run). That gives you the diff anchor "for
  free" alongside the human-facing inventory.
- **Persist the JSON output across container runs.** ECS Fargate
  tasks have ephemeral storage; mount an EFS volume or write to S3
  if you want diff data to survive container restarts.
- **Combine with a notification filter.** `notifications: [{kind:
  pagerduty, levels: [error]}]` won't fire on diff messages (they
  ride on `info` posts), but a chat channel with `levels: [info,
  warn, error]` gets the diff summary every run. Pair with a
  separate `kind: slack` for an "infra-inventory-changes" channel
  if you want to silo diff posts away from other notifications.
