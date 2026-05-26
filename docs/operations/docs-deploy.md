# Docs deployment (versioned via `mike`)

The docs site at [iac-cartographer.andersonleite.me](https://iac-cartographer.andersonleite.me/)
is **versioned**. The mkdocs-material header carries a dropdown
showing every published version; older versions stay reachable at
their own URL after a new release lands.

## Versioning convention

| Alias | Updates on | Points at |
|---|---|---|
| `latest` | git tag `v*` | The most recent stable release. Default landing page. |
| `dev` | every push to `main` | The current main-branch state — unreleased, may be ahead of `latest`. |
| `vX.Y.Z` | git tag `v*` | The exact tagged version. Never overwritten once published. |

So a fresh clone reads from `latest`; a contributor checking
behaviour on the current `main` reads from `dev`; an operator pinning
to `v0.1.0` reads from `v0.1.0/` and keeps reading it after `v0.2.0`
ships.

`mkdocs-material` shows a banner on any non-latest version warning
that it's older or a dev-build — set via `extra.version.warning: true`
in `mkdocs.yml`.

## How the workflow publishes

The `Deploy docs site` workflow (`.github/workflows/docs.yml`) wraps
[mike](https://github.com/jimporter/mike) — a small tool that
commits versioned mkdocs builds to the `gh-pages` branch and
maintains an alias index.

Three deploy paths:

1. **`push` to `main`** → `mike deploy --push --update-aliases dev`.
   Overwrites the `dev/` subdirectory and the `dev` alias.
2. **`push` of `git tag v*`** →
   ```
   mike deploy --push --update-aliases <version> latest
   mike set-default --push latest
   ```
   Creates the `vX.Y.Z/` subdirectory, repoints `latest`, and marks
   `latest` as the default so the bare URL serves it.
3. **`workflow_dispatch`** — manual run lets an operator hand-publish
   any version label. Useful for backfilling a missed tag or staging
   a release candidate.

All three paths serialize via the same concurrency group
(`docs-deploy`) so concurrent main-pushes + tag-pushes can't race on
the `gh-pages` branch.

## Reading the version selector

Top-right of every docs page: dropdown showing every published
version. Clicking switches to that version's URL
(`/v0.1.0/operations/diff/` instead of
`/latest/operations/diff/`). The selector also lists aliases
(`latest`, `dev`) alongside the version numbers so it's clear which
one you're on.

The `extra.version.warning` config emits a yellow callout at the top
of any non-default version — "You're reading docs for version X.Y.Z.
The latest is …" with a link to switch.

## One-time bootstrap

The first `mike deploy` creates the `gh-pages` branch. Until that
branch exists, GitHub Pages may serve whatever was deployed
previously (via the older `actions/deploy-pages` artifact path).

After this workflow first runs, **the operator must change the
GitHub Pages source** in repo settings to "Deploy from a branch:
`gh-pages` / (root)". One-time switch:

1. Settings → Pages.
2. Under "Build and deployment", change Source from "GitHub Actions"
   to "Deploy from a branch".
3. Branch: `gh-pages`. Folder: `/ (root)`. Save.

For a brand-new fork, the bootstrap order is:

```bash
# 1. From a local clone with [docs] extra installed:
pip install -e ".[docs]"

# 2. Configure git for the commits mike will create:
git config user.name "you"
git config user.email "you@example.com"

# 3. Seed the first version + the latest alias:
mike deploy --push 0.1.0 latest
mike set-default --push latest

# 4. Flip the GitHub Pages source to gh-pages (in repo Settings).
```

After that, the CI workflow takes over.

## Removing a version

Sometimes a release has to be unpublished (bad release, broken docs):

```bash
mike delete --push v0.2.0
```

If `latest` was pointing at it, repoint first:

```bash
mike delete --push --update-aliases v0.2.0
mike set-default --push latest
```

`mike list` shows every version currently in `gh-pages`. `mike serve`
runs a local preview against the versioned build.

## Custom domain

The `CNAME` file at the root of `gh-pages` carries the custom domain
(`iac-cartographer.andersonleite.me`). `mike` preserves files
outside the per-version subdirectories on subsequent deploys, so
the `CNAME` file survives version updates as long as it's committed
to `gh-pages` directly (not regenerated from `docs/`).

If a deploy ever wipes the CNAME (theoretical — `mike` has never
done this for us in practice), re-add it via:

```bash
git fetch origin gh-pages
git checkout gh-pages
echo "iac-cartographer.andersonleite.me" > CNAME
git add CNAME
git commit -m "Restore CNAME"
git push origin gh-pages
```

## Tradeoffs vs the previous setup

The previous deploy path used `actions/deploy-pages@v4` — single
version, overwrites every push, no history. The switch to `mike`:

- ✅ Adds the version selector + URL stability per release.
- ✅ Lets contributors and operators verify behaviour on a specific
  released version (`/v0.1.0/`) without cloning the tag.
- ⚠️ Adds a `gh-pages` branch to the repo (~100kb per version after
  gzip on a small docs site). Not a real cost.
- ⚠️ The `Deploy docs site` job needs `contents: write` instead of
  the OIDC `pages: write` — slightly broader permission.
- ⚠️ Operators forking the project for their own deployment have to
  bootstrap the first version + flip the Pages source. Documented
  above.
