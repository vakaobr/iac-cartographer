"""Vendor-neutral observability surface for non-AWS deployments.

The tool's native telemetry is AWS CloudWatch (`iac_cartographer.aws.put_metric_data`).
That works only for AWS adopters. This module adds an *optional*, *additive*
OpenTelemetry (OTLP) metrics exporter so GCP / Azure / on-prem adopters can ship
the same run-level metrics to any OTLP-compatible collector (Grafana Alloy,
OpenTelemetry Collector, Datadog Agent, …).

Design contract:
  * **Default OFF.** Nothing initializes unless an OTLP endpoint env var is set
    AND the `[otel]` extra is installed.
  * **Lazy + fail-soft.** The OTel SDK is imported only on first use. When the
    deps are missing we log one DEBUG line and turn every metric call into a
    no-op for the rest of the process — same defence the Notion publisher and
    email channel use for their optional deps.
  * **Never load-bearing.** A metric must never crash, slow, or alter a run.
    Every public function swallows exceptions.

Endpoint selection (first match wins):
  1. ``IAC_CARTOGRAPHER_OTEL_EXPORTER_OTLP_ENDPOINT`` (tool-specific override)
  2. ``OTEL_EXPORTER_OTLP_ENDPOINT`` (the OTel-standard env var)

The metric set mirrors what the pipeline already tracks and emits to
CloudWatch, so dashboards stay comparable across exporters:

  * ``iac_cartographer.runs``                — counter, run heartbeat
  * ``iac_cartographer.repo.duration``       — histogram, per-repo seconds
  * ``iac_cartographer.llm.tokens``          — counter, attr ``backend`` + ``direction``
  * ``iac_cartographer.publish.outcomes``    — counter, attr ``outcome``
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("iac_cartographer.observability")

_ENDPOINT_ENV_VARS = (
    "IAC_CARTOGRAPHER_OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
)

_SERVICE_NAME = "iac-cartographer"


def _resolve_endpoint() -> str | None:
    """Return the configured OTLP endpoint, or None when the feature is off."""
    for name in _ENDPOINT_ENV_VARS:
        value = os.environ.get(name)
        if value:
            return value
    return None


class _NullMetrics:
    """No-op implementation — every method silently does nothing.

    Installed whenever OTel is disabled (endpoint unset) or unavailable
    (deps missing). Keeps call sites branch-free.
    """

    enabled = False

    def run_started(self) -> None: ...

    def record_repo_duration(self, seconds: float) -> None: ...

    def record_llm_tokens(self, backend: str, tokens_in: int, tokens_out: int) -> None: ...

    def record_publish_outcome(self, outcome: str, count: int = 1) -> None: ...

    def shutdown(self) -> None: ...


class _OtelMetrics:
    """Live OTLP exporter wrapper. Constructed only when deps + endpoint exist."""

    enabled = True

    def __init__(self, endpoint: str) -> None:
        # Imports are local + already-guarded by the factory; keep them here so
        # the module imports cleanly without the `[otel]` extra.
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource

        reader = PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=endpoint))
        self._provider = MeterProvider(
            resource=Resource.create({SERVICE_NAME: _SERVICE_NAME}),
            metric_readers=[reader],
        )
        meter = self._provider.get_meter(_SERVICE_NAME)

        self._runs = meter.create_counter(
            "iac_cartographer.runs",
            unit="1",
            description="Pipeline run heartbeat.",
        )
        self._repo_duration = meter.create_histogram(
            "iac_cartographer.repo.duration",
            unit="s",
            description="Per-repo clone+extract+narrate duration.",
        )
        self._llm_tokens = meter.create_counter(
            "iac_cartographer.llm.tokens",
            unit="1",
            description="LLM token usage by backend and direction.",
        )
        self._publish_outcomes = meter.create_counter(
            "iac_cartographer.publish.outcomes",
            unit="1",
            description="Publish outcome counts by result.",
        )
        logger.info("observability: OTLP metrics exporter active (endpoint=%s)", endpoint)

    def run_started(self) -> None:
        self._runs.add(1)

    def record_repo_duration(self, seconds: float) -> None:
        self._repo_duration.record(seconds)

    def record_llm_tokens(self, backend: str, tokens_in: int, tokens_out: int) -> None:
        if tokens_in:
            self._llm_tokens.add(tokens_in, {"backend": backend, "direction": "in"})
        if tokens_out:
            self._llm_tokens.add(tokens_out, {"backend": backend, "direction": "out"})

    def record_publish_outcome(self, outcome: str, count: int = 1) -> None:
        if count:
            self._publish_outcomes.add(count, {"outcome": outcome})

    def shutdown(self) -> None:
        try:
            self._provider.shutdown()
        except Exception:
            logger.debug("observability: OTLP provider shutdown failed", exc_info=True)


_NULL = _NullMetrics()
_metrics: _NullMetrics | _OtelMetrics | None = None


def _build_metrics() -> _NullMetrics | _OtelMetrics:
    endpoint = _resolve_endpoint()
    if not endpoint:
        return _NULL
    try:
        return _OtelMetrics(endpoint)
    except ImportError:
        logger.debug(
            "observability: OTLP endpoint set but the `[otel]` extra is not installed; "
            "metrics export disabled (pip install 'iac-cartographer[otel]')"
        )
        return _NULL
    except Exception:
        logger.debug("observability: OTLP exporter init failed; metrics export disabled", exc_info=True)
        return _NULL


def get_metrics() -> _NullMetrics | _OtelMetrics:
    """Return the process-wide metrics handle, initializing it on first call.

    Idempotent and thread-safe enough for our single-shot batch model: the
    first caller builds the exporter; everyone after reuses it.
    """
    global _metrics
    if _metrics is None:
        _metrics = _build_metrics()
    return _metrics


def reset_for_tests() -> None:
    """Clear the cached handle so tests can re-evaluate env state. Test-only."""
    global _metrics
    _metrics = None


# ─── thin fan-out helpers ─────────────────────────────────────────────────
# Call sites use these so a metric is recorded once and fans out to whichever
# exporters are active. The CloudWatch path stays in cli.py (it owns the AWS
# namespace + dry-run gating); these add the OTLP fan-out leg without coupling
# the two. Every helper is fail-soft.


def _safe(fn: str, *args: Any, **kwargs: Any) -> None:
    try:
        getattr(get_metrics(), fn)(*args, **kwargs)
    except Exception:
        logger.debug("observability: metric call %s failed", fn, exc_info=True)


def run_started() -> None:
    _safe("run_started")


def record_repo_duration(seconds: float) -> None:
    _safe("record_repo_duration", seconds)


def record_llm_tokens(backend: str, tokens_in: int, tokens_out: int) -> None:
    _safe("record_llm_tokens", backend, tokens_in, tokens_out)


def record_publish_outcome(outcome: str, count: int = 1) -> None:
    _safe("record_publish_outcome", outcome, count)


def shutdown() -> None:
    _safe("shutdown")
