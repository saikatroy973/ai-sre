# Telemetry Analyst — observability playbook

Scope: logs, metrics, traces and alert policy. If no source is configured,
abstain explicitly; never describe data you did not read.

## Signals
- Golden signals: latency, traffic, errors, saturation. Use RED for request
  services and USE for resources.
- Metrics: counters (rate/increase), gauges (current value), histograms
  (quantiles from buckets; never average percentiles).
- Logs: structured JSON, level, timestamp, trace/span IDs, service/version;
  sample and redact secrets/PII.
- Traces: service graph, span status, error tags, sampling ratio; resource
  attributes identify the workload.

## Alerting
- Alert on symptoms that hurt users, not every possible cause; every page must
  have an action.
- Prefer multi-window burn-rate alerts on SLOs; short window pages, long window
  tickets.
- Link alerts to dashboards/runbooks; name alerts consistently.

## Query hygiene
- Bound time ranges and cardinality; aggregate before selecting.
- Distinguish absence of data from zero; check scrape/export health first.
- Correlate logs with traces by trace_id and with deploys by version label.

## Loki/LogQL pointers
- Stream selector on low-cardinality labels, then filter expressions.
- Use `|=` line filters, `| json` parsing, `rate()`/`count_over_time` with
  bounded ranges; avoid unbounded `.*` regex.

## Sources (retrieved 2026-11-07)
- Prometheus alerting practices: https://prometheus.io/docs/practices/alerting/
- Prometheus querying basics: https://prometheus.io/docs/prometheus/latest/querying/basics/
- OpenTelemetry signals — logs: https://opentelemetry.io/docs/concepts/signals/logs/
- OpenTelemetry signals — metrics: https://opentelemetry.io/docs/concepts/signals/metrics/
- OpenTelemetry signals — traces: https://opentelemetry.io/docs/concepts/signals/traces/
- Grafana Loki LogQL: https://grafana.com/docs/loki/latest/query/
