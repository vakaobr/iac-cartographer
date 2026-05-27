"""Tests for the OTLP observability surface.

The `[otel]` extra is NOT installed in CI, so every test here runs offline:
either the endpoint env var is unset (no-op path) or we inject a fake OTel SDK
into sys.modules to exercise the live path without the real dependency.
"""

from __future__ import annotations

import logging
import sys
import types
from typing import TYPE_CHECKING

import pytest

from iac_cartographer import observability

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    """Clear the cached metrics handle + endpoint env between tests."""
    observability.reset_for_tests()
    yield
    observability.reset_for_tests()


def _clear_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in observability._ENDPOINT_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


# ─── no-op paths ──────────────────────────────────────────────────────────


def test_noop_when_endpoint_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_endpoints(monkeypatch)
    m = observability.get_metrics()
    assert m.enabled is False
    # None of these should raise.
    observability.run_started()
    observability.record_repo_duration(1.0)
    observability.record_llm_tokens("bedrock", 10, 20)
    observability.record_publish_outcome("updated", 3)
    observability.shutdown()


def test_noop_when_deps_missing(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setenv("IAC_CARTOGRAPHER_OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    # Ensure the import fails even if otel happens to be installed locally.
    monkeypatch.setitem(sys.modules, "opentelemetry", None)
    with caplog.at_level(logging.DEBUG, logger="iac_cartographer.observability"):
        m = observability.get_metrics()
    assert m.enabled is False
    assert any("otel" in r.message.lower() for r in caplog.records)


def test_standard_otel_env_var_also_works(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_endpoints(monkeypatch)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    monkeypatch.setitem(sys.modules, "opentelemetry", None)
    # Endpoint resolves, but deps "missing" -> no-op (no crash).
    assert observability._resolve_endpoint() == "http://collector:4318"
    assert observability.get_metrics().enabled is False


def test_tool_specific_env_var_takes_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IAC_CARTOGRAPHER_OTEL_EXPORTER_OTLP_ENDPOINT", "http://specific:4318")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://standard:4318")
    assert observability._resolve_endpoint() == "http://specific:4318"


# ─── live path with a faked SDK ─────────────────────────────────────────────


class _FakeInstrument:
    def __init__(self) -> None:
        self.adds: list[tuple[float, dict[str, str] | None]] = []
        self.records: list[float] = []

    def add(self, value: float, attributes: dict[str, str] | None = None) -> None:
        self.adds.append((value, attributes))

    def record(self, value: float) -> None:
        self.records.append(value)


class _FakeMeter:
    def __init__(self) -> None:
        self.instruments: dict[str, _FakeInstrument] = {}

    def _get(self, name: str) -> _FakeInstrument:
        return self.instruments.setdefault(name, _FakeInstrument())

    def create_counter(self, name: str, **_: object) -> _FakeInstrument:
        return self._get(name)

    def create_histogram(self, name: str, **_: object) -> _FakeInstrument:
        return self._get(name)


class _FakeProvider:
    def __init__(self, **_: object) -> None:
        self.meter = _FakeMeter()
        self.shutdown_called = False

    def get_meter(self, _name: str) -> _FakeMeter:
        return self.meter

    def shutdown(self) -> None:
        self.shutdown_called = True


def _install_fake_otel(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Inject a minimal fake OTel SDK so _OtelMetrics constructs offline."""
    captured: dict[str, object] = {}

    otel = types.ModuleType("opentelemetry")
    metrics_mod = types.ModuleType("opentelemetry.metrics")
    otel.metrics = metrics_mod  # type: ignore[attr-defined]

    sdk = types.ModuleType("opentelemetry.sdk")
    sdk_metrics = types.ModuleType("opentelemetry.sdk.metrics")
    sdk_metrics_export = types.ModuleType("opentelemetry.sdk.metrics.export")
    sdk_resources = types.ModuleType("opentelemetry.sdk.resources")
    exporter_pkg = types.ModuleType("opentelemetry.exporter")
    exporter_otlp = types.ModuleType("opentelemetry.exporter.otlp")
    exporter_proto = types.ModuleType("opentelemetry.exporter.otlp.proto")
    exporter_http = types.ModuleType("opentelemetry.exporter.otlp.proto.http")
    exporter_metric = types.ModuleType("opentelemetry.exporter.otlp.proto.http.metric_exporter")

    def make_provider(**kwargs: object) -> _FakeProvider:
        provider = _FakeProvider(**kwargs)
        captured["provider"] = provider
        return provider

    sdk_metrics.MeterProvider = make_provider  # type: ignore[attr-defined]
    sdk_metrics_export.PeriodicExportingMetricReader = lambda *a, **k: ("reader", a, k)  # type: ignore[attr-defined]
    sdk_resources.SERVICE_NAME = "service.name"  # type: ignore[attr-defined]
    sdk_resources.Resource = types.SimpleNamespace(create=lambda d: d)  # type: ignore[attr-defined]
    exporter_metric.OTLPMetricExporter = lambda *a, **k: ("exporter", k)  # type: ignore[attr-defined]

    for name, mod in {
        "opentelemetry": otel,
        "opentelemetry.metrics": metrics_mod,
        "opentelemetry.sdk": sdk,
        "opentelemetry.sdk.metrics": sdk_metrics,
        "opentelemetry.sdk.metrics.export": sdk_metrics_export,
        "opentelemetry.sdk.resources": sdk_resources,
        "opentelemetry.exporter": exporter_pkg,
        "opentelemetry.exporter.otlp": exporter_otlp,
        "opentelemetry.exporter.otlp.proto": exporter_proto,
        "opentelemetry.exporter.otlp.proto.http": exporter_http,
        "opentelemetry.exporter.otlp.proto.http.metric_exporter": exporter_metric,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)

    return captured


def test_live_path_records_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IAC_CARTOGRAPHER_OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    captured = _install_fake_otel(monkeypatch)

    m = observability.get_metrics()
    assert m.enabled is True

    observability.run_started()
    observability.record_repo_duration(3.5)
    observability.record_llm_tokens("anthropic", 100, 50)
    observability.record_publish_outcome("updated", 2)
    observability.record_publish_outcome("failed", 0)  # zero is skipped

    provider = captured["provider"]
    meter = provider.meter  # type: ignore[attr-defined]
    assert meter.instruments["iac_cartographer.runs"].adds == [(1, None)]
    assert meter.instruments["iac_cartographer.repo.duration"].records == [3.5]

    token_adds = meter.instruments["iac_cartographer.llm.tokens"].adds
    assert (100, {"backend": "anthropic", "direction": "in"}) in token_adds
    assert (50, {"backend": "anthropic", "direction": "out"}) in token_adds

    publish_adds = meter.instruments["iac_cartographer.publish.outcomes"].adds
    assert (2, {"outcome": "updated"}) in publish_adds
    assert all(a[0] != 0 for a in publish_adds)

    observability.shutdown()
    assert provider.shutdown_called is True  # type: ignore[attr-defined]


def test_get_metrics_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_endpoints(monkeypatch)
    first = observability.get_metrics()
    second = observability.get_metrics()
    assert first is second
