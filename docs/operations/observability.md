# Observability

iac-cartographer runs as a scheduled batch job, so its observability surface
is built for "did the last run succeed, how long did it take, what did it
cost" — not request-rate dashboards. Two opt-in mechanisms cover non-AWS
deployments without changing the defaults.

## Structured JSON logging

By default, logs are human-readable text. Set `IAC_CARTOGRAPHER_LOG_FORMAT=json`
to emit one JSON object per log line instead — suitable for shipping to Loki,
CloudWatch Logs Insights, Datadog, etc.

```bash
IAC_CARTOGRAPHER_LOG_FORMAT=json iac-cartographer --once --config ./config.yaml
```

Each line carries `ts` (ISO-8601 UTC), `level`, `logger`, `msg`, any
structured `extra=` fields attached at the call site (repo names, durations,
counts), and `exc` for exceptions. The same secret-redaction filter that
applies to text logs applies to JSON — credentials never reach either sink.

`IAC_CARTOGRAPHER_LOG_FORMAT` unset or set to anything other than `json`
keeps the text formatter. Switching back is a one-env-var rollback.

## OpenTelemetry metrics (optional)

AWS deployments already get metrics via CloudWatch (the `put_metric_data`
path, unchanged). For GCP / Azure / on-prem, an optional OTLP exporter sends
the same signals to any OpenTelemetry collector.

It's **off by default** and gated two ways:

1. Install the extra: `pip install 'iac-cartographer[otel]'`
2. Set an endpoint: `IAC_CARTOGRAPHER_OTEL_EXPORTER_OTLP_ENDPOINT`
   (or the standard `OTEL_EXPORTER_OTLP_ENDPOINT`).

When the endpoint is unset OR the `[otel]` extra isn't installed, every
metric call is a silent no-op (the OTel SDK is imported lazily; a missing
dep logs once at DEBUG and falls back to the null exporter). The CloudWatch
path is unaffected either way — OTLP is purely additive.

### Metrics emitted

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `iac_cartographer.runs` | counter | — | Pipeline run heartbeat (one per `--once`). |
| `iac_cartographer.repo.duration` | histogram | — | Per-repo clone + extract + narrate seconds. |
| `iac_cartographer.llm.tokens` | counter | `backend`, direction | LLM token usage by backend. |
| `iac_cartographer.publish.outcomes` | counter | `outcome` | Publish results (created / updated / unchanged / failed). |

```bash
pip install 'iac-cartographer[otel]'
export OTEL_EXPORTER_OTLP_ENDPOINT="http://otel-collector:4318"
iac-cartographer --once --config ./config.yaml
```

### Rollback

Unset the endpoint env var (or uninstall the `[otel]` extra) and metrics
silently stop exporting — no code change, no restart logic. The run itself
is unaffected; observability is never on the critical path.
