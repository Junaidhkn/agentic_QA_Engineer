# LGTM (Mimir/Tempo/Loki) outage and backfill

Customer evidence telemetry (traces, logs, app metrics) flows through the OTLP gateway into
the collector (`deploy/otel/collector.yaml`) and on to Mimir/Tempo/Loki. Run results,
findings, diagnoses and their evidence summaries are in Postgres and are not affected.

## While LGTM is down

* The collector's exporters queue to disk (`file_storage/queue` for Tempo/Loki, the
  remote-write WAL for Mimir) and retry for up to **1 hour**. The queue directory
  (`OTEL_QUEUE_DIR`) must be on a persistent volume, not the task's ephemeral disk.
* Live run charts keep working (they come from runner progress events via Redis).
* Diagnoses started meanwhile have less evidence (no traces/logs); they say so in their
  evidence list rather than guessing.

## Steps

1. Restore the failing component (single-binary services, S3 backend `lgtm-data` bucket).
2. Watch the collector's `otelcol_exporter_queue_size` drain to 0 after recovery.
3. **Outage longer than 1 hour:** data older than the queue window is dropped. There is no
   replay source — say so in the incident. Affected diagnoses can be re-requested once
   telemetry is flowing again (new runs produce new evidence).
4. If disk filled up on the collector, raise the volume or queue size and restart it; the
   queue survives restarts.

## Platform metrics

Our own SLO metrics are scraped by `platform-collector` into tenant `platform`. During an
outage the SLO alerts in Mimir cannot fire — the CloudWatch alarms (ALB 5xx, RDS, Redis,
PrivateLink health) still page. Gaps in SLO history are accepted; note the window in the
monthly SLO report.
