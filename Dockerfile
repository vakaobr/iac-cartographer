# iac-cartographer — container image for the scheduled batch pipeline.
#
# Runs `iac-cartographer --once` per invocation. Designed to be triggered by
# whatever scheduler your platform uses (EventBridge Scheduler on ECS, a
# Kubernetes CronJob, a GitHub Actions schedule, plain `cron`, ...).
# amd64 only — multi-arch buildx is an exercise for the reader.

FROM python:3.12-slim AS runtime

# terraform-docs is pinned to a specific release. Bump deliberately —
# unpinned versions could change the JSON output shape and break the
# extractor.
ARG TERRAFORM_DOCS_VERSION=v0.20.0

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System deps:
#   git              — `git clone --depth=1` of each discovered repo
#   ca-certificates  — TLS to GitLab / GitHub / Confluence / Slack / Bedrock
#   curl + tar       — fetch + extract pinned terraform-docs release
# We intentionally do NOT install terraform itself — terraform-docs has its
# own HCL parser.
# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git \
      ca-certificates \
      curl \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL \
      "https://terraform-docs.io/dl/${TERRAFORM_DOCS_VERSION}/terraform-docs-${TERRAFORM_DOCS_VERSION}-linux-amd64.tar.gz" \
      -o /tmp/terraform-docs.tar.gz \
 && tar -xzf /tmp/terraform-docs.tar.gz -C /usr/local/bin terraform-docs \
 && rm /tmp/terraform-docs.tar.gz \
 && chmod +x /usr/local/bin/terraform-docs \
 && terraform-docs --version \
 # Strip curl after install — only needed at build time.
 && apt-get purge -y --auto-remove curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (cached layer when only source code changes).
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

# Then the source code. `pip install -e .` requires the source tree to be
# present, so we do a real install here that picks up the entrypoint.
COPY iac_cartographer/ iac_cartographer/
RUN pip install --no-cache-dir -e .

# Non-root user — not strictly required by every runtime, but good hygiene.
RUN useradd --create-home --shell /bin/bash --uid 10001 cartographer \
 && chown -R cartographer:cartographer /app
USER cartographer

# Default command — your scheduler can override with `--repos a,b,c` or
# `--model <inference-profile>` via containerOverrides / args.
ENTRYPOINT ["iac-cartographer"]
CMD ["--once"]
