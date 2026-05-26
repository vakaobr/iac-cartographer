# Cutting a release

Releases are tag-driven. Two workflows fan out from a single tag push:

1. [`release-pypi.yml`](https://github.com/vakaobr/iac-cartographer/blob/main/.github/workflows/release-pypi.yml)
   — builds the sdist + wheel, uploads to PyPI via OIDC trusted
   publishing, attaches the dist files to a GitHub Release.
2. [`release-ghcr.yml`](https://github.com/vakaobr/iac-cartographer/blob/main/.github/workflows/release-ghcr.yml)
   — builds the multi-arch container image
   (`linux/amd64` + `linux/arm64` via buildx + QEMU), pushes to
   `ghcr.io/vakaobr/iac-cartographer` with a full semver tag fan-out,
   signs the image with cosign, attaches an SPDX SBOM to the GitHub
   Release.

## Maintainer setup (one-time)

### PyPI

1. Create the project on PyPI (or claim the name):
   <https://pypi.org/manage/projects/>
2. *Project settings → Publishing → Add a new pending publisher*:
   - Owner: `vakaobr`
   - Repository: `iac-cartographer`
   - Workflow: `release-pypi.yml`
   - Environment: `release`
3. Create a GitHub Environment named `release` (Repo Settings →
   Environments). Optionally add reviewers / wait timer for an
   approval gate before each upload.

### GHCR

Nothing to set up. The workflow uses the built-in `GITHUB_TOKEN` with
`packages: write` — no PAT to manage.

## Cutting a release

```bash
# 1. Bump pyproject.toml version on main
sed -i '' 's/^version = .*/version = "0.2.0"/' pyproject.toml
git commit -am "chore: bump version to 0.2.0"
git push origin main

# 2. Tag + push
git tag v0.2.0
git push origin v0.2.0
```

The PyPI workflow:

1. Builds sdist + wheel.
2. Verifies the tag suffix matches `pyproject.toml` (catches forgotten
   bumps as a CI failure, not a misversioned wheel).
3. Uploads to PyPI via OIDC trusted publishing.
4. Creates a GitHub Release with auto-generated release notes + dist
   files attached.

In parallel, the GHCR workflow builds + pushes the container image
with the full semver tag fan-out + cosign signature + SBOM
attestation.

## Container image tag fan-out

| Trigger | Tags produced |
|---|---|
| `git push origin v0.2.0` | `:v0.2.0`, `:0.2.0`, `:0.2`, `:0`, `:latest` |
| `git push origin main` | `:main`, `:main-<short-sha>` |
| `workflow_dispatch` | `:dispatch-<short-sha>` |

Pin to `:vX.Y.Z` in production manifests. `:latest` and `:main` are
convenience tags — `:latest` only moves on tag pushes, `:main` moves on
every main push.

## Verifying images

```bash
cosign verify ghcr.io/vakaobr/iac-cartographer:v0.2.0 \
  --certificate-identity-regexp '^https://github\.com/vakaobr/iac-cartographer' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com'
```

The signature binds to the image **digest**, not the tag, so a
retag-overwrite can't substitute a signed image with an unsigned one.

## Dry-runs

For PyPI: trigger the workflow manually
(*Actions → Release to PyPI → Run workflow → target: test*) to upload
to TestPyPI instead of PyPI.

For GHCR: trigger the workflow manually to publish a
`:dispatch-<short-sha>` tag without cutting a real release.
