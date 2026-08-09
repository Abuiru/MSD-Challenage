# Proactive Storage Health Monitor — Challenge 2

An hourly, container-friendly health check for a multi-node storage cluster
replicating to AWS. It parses a storage-API JSON payload and flags:

1. **Hardware Health** — any node whose `status` isn't the expected value.
2. **Capacity Risk** — any volume above the warning/critical `used_capacity_percent` threshold.
3. **Replication Lag (RPO breach)** — any replication pair whose `last_successful_sync` is too old.

It is built to survive messy real-world input (missing fields, malformed
timestamps) without crashing, and to fail loudly — with a non-zero exit code —
when the *run itself* can't be trusted (bad input source, bad config).

## Repo layout

```
.
├── monitor.py                          # the monitor
├── config.yaml                         # tunable thresholds (edit this, not the code)
├── requirements.txt
├── Dockerfile
├── storage_api_mock.json               # sample input payload (from the case study appendix)
├── sample_output/
│   ├── alert_output_sample.json        # structured (machine-consumable) sample output
│   └── alert_output_sample.txt         # human-readable + mock webhook sample output
└── README.md
```

## Quick start (Docker)

```bash
# Build
docker build -t storage-health-monitor .

# Run with the bundled sample payload + default config
docker run --rm storage-health-monitor

# Run against your own payload, mounted read-only
docker run --rm \
  -v /path/to/real_payload.json:/app/payload.json:ro \
  storage-health-monitor --input /app/payload.json --config config.yaml

# Override a threshold at run time, no image rebuild needed
docker run --rm -e MONITOR_CAPACITY_WARNING_PERCENT=80 storage-health-monitor
```

## Quick start (local Python)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python3 monitor.py --input storage_api_mock.json --config config.yaml
```

## Configuration

Thresholds live in `config.yaml` (JSON is also accepted) and can be
overridden per-run with environment variables — useful for a scheduler that
shouldn't need to edit files on disk:

| Threshold                        | config.yaml key                  | Environment variable                     |
|-----------------------------------|-----------------------------------|-------------------------------------------|
| Expected node status              | `expected_node_status`            | `MONITOR_EXPECTED_NODE_STATUS`             |
| Capacity warning %                | `capacity_warning_percent`        | `MONITOR_CAPACITY_WARNING_PERCENT`         |
| Capacity critical %               | `capacity_critical_percent`       | `MONITOR_CAPACITY_CRITICAL_PERCENT`        |
| Replication lag warning (hours)   | `replication_lag_warning_hours`   | `MONITOR_REPLICATION_WARNING_HOURS`        |
| Replication lag critical (hours)  | `replication_lag_critical_hours`  | `MONITOR_REPLICATION_CRITICAL_HOURS`       |

CLI flags (`--input`, `--config`, `--output-json`) can also be set via
`MONITOR_INPUT_PATH`, `MONITOR_CONFIG_PATH`, `MONITOR_OUTPUT_JSON_PATH` — so a
container can be fully configured with `-e` flags alone.

Precedence: **environment variable > config file > built-in default.**

## Output

Every run produces two things:

1. **Structured JSON** (stdout, or written to `--output-json <path>`) —
   machine-consumable, one entry per finding, with `severity`, `category`,
   `resource`, `message`, and a `details` dict for drill-down. See
   `sample_output/alert_output_sample.json`.
2. **Human-readable summary + mock webhook payload** (stderr) — grouped by
   severity, plus a ready-to-POST Slack or Teams payload shape (`notifications.webhook_type`
   in config.yaml). See `sample_output/alert_output_sample.txt`. The webhook
   is only *built*, not actually sent, unless you wire `webhook_url` up to a
   real HTTP POST — kept as a mock per the assignment.

Severity levels: `CRITICAL` (breach of the critical threshold), `WARNING`
(breach of the warning threshold, or a record too malformed to evaluate
safely), `OK`.

## Resilient parsing

- A hardware node missing `status`, or a volume missing `used_capacity_percent`,
  is **not** dropped silently — it's reported as a `data_quality` / `WARNING`
  finding (see `node-C3` and `nas_archive_01` in the sample output) and the
  run continues.
- Timestamps are parsed with a small cascade of ISO-8601 and common
  alternate formats (e.g. `"2023-10-26 14:30"` with no `T`/`Z`), so a
  malformed-but-recognizable timestamp is still evaluated. If a timestamp
  truly can't be interpreted, that record is flagged (`WARNING`,
  `data_quality`) instead of crashing the run.
- Replication lag is measured against the payload's own snapshot
  `timestamp` field (falling back to real wall-clock time if that's absent),
  so replaying an older sample payload for testing gives correct,
  reproducible results instead of "everything is critical because the file
  is old."

## Exit codes

| Code | Meaning                                                              |
|------|------------------------------------------------------------------------|
| `0`  | Run completed. No CRITICAL findings (WARNINGs may still be present).   |
| `1`  | Run completed, but at least one CRITICAL finding was found.            |
| `2`  | Run **failed** — input unreachable/unparseable, or config invalid.     |

A scheduler/cron/orchestrator should treat `2` as "this run itself is
broken — investigate the pipeline," and `1` as "the run worked, but the
storage estate needs attention now."

## Testing against the sample payload

`storage_api_mock.json` is seeded to exercise every requirement:

- `node-A2` → Offline → **CRITICAL** hardware finding.
- `node-C3` → missing `status` → **WARNING** data-quality finding (not dropped, not crashed).
- `san_db_prod_01` → 97.8% → **CRITICAL** capacity finding (> 95%).
- `nas_projects` → 88.4% → **WARNING** capacity finding (> 85%).
- `nas_archive_01` → missing `used_capacity_percent` → **WARNING** data-quality finding.
- `nas_home_dirs` → last sync > 24h before the snapshot → **CRITICAL** RPO breach.
- `nas_projects` replication → malformed timestamp (`"2023-10-26 14:30"`) → parsed successfully by the fallback cascade, still evaluated (also lands in RPO breach given how stale it is), demonstrating "don't crash" without needing to skip data that's actually recoverable.

Run it and diff against `sample_output/` to verify behavior after any change.
