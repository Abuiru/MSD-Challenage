#!/usr/bin/env python3
"""
monitor.py — Proactive Storage Health Monitor

Reads a storage-API JSON payload (hardware nodes, volumes, replication
relationships), evaluates it against configurable thresholds, and emits:
  - a structured JSON result (machine-consumable)
  - a human-readable summary (console + mock Slack/Teams webhook payload)

Designed to run on a schedule (cron / systemd timer / container orchestrator)
and to fail loudly (non-zero exit code) when the run itself cannot be trusted,
while staying resilient to individual bad records within an otherwise valid
payload.

Exit codes:
  0  - Run completed. No CRITICAL findings. (WARNINGs may still be present.)
  1  - Run completed successfully, but at least one CRITICAL finding exists.
  2  - Run could not complete: input unreachable, unparseable, or config error.
"""

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

try:
    import yaml  # PyYAML
except ImportError:  # pragma: no cover
    yaml = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("storage-health-monitor")


# --------------------------------------------------------------------------
# Exit codes (also used as sys.exit values)
# --------------------------------------------------------------------------
class ExitCode:
    OK = 0
    CRITICAL_FOUND = 1
    RUN_FAILED = 2


class Severity(str, Enum):
    OK = "OK"
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass
class Finding:
    category: str          # "hardware" | "capacity" | "replication" | "data_quality"
    severity: str           # Severity value
    resource: str           # e.g. node_id / volume_name / source_volume
    message: str
    details: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# Config loading (file + environment variable overrides)
# --------------------------------------------------------------------------
DEFAULTS = {
    "expected_node_status": "Online",
    "capacity_warning_percent": 85.0,
    "capacity_critical_percent": 95.0,
    "replication_lag_warning_hours": 4.0,
    "replication_lag_critical_hours": 12.0,
}

ENV_OVERRIDES = {
    "expected_node_status": "MONITOR_EXPECTED_NODE_STATUS",
    "capacity_warning_percent": "MONITOR_CAPACITY_WARNING_PERCENT",
    "capacity_critical_percent": "MONITOR_CAPACITY_CRITICAL_PERCENT",
    "replication_lag_warning_hours": "MONITOR_REPLICATION_WARNING_HOURS",
    "replication_lag_critical_hours": "MONITOR_REPLICATION_CRITICAL_HOURS",
}


def load_config(config_path: Optional[str]) -> dict:
    """
    Load thresholds from a YAML/JSON config file, then apply any
    environment-variable overrides on top. Falls back to DEFAULTS if no
    config file is supplied or the file is missing individual keys.
    """
    cfg = dict(DEFAULTS)

    if config_path:
        if not os.path.isfile(config_path):
            raise FileNotFoundError(f"Config file not found: {config_path}")
        with open(config_path, "r") as fh:
            raw = fh.read()
        try:
            if config_path.endswith((".yaml", ".yml")):
                if yaml is None:
                    raise RuntimeError(
                        "PyYAML is not installed but a .yaml config was given. "
                        "Install pyyaml or supply a .json config."
                    )
                parsed = yaml.safe_load(raw) or {}
            else:
                parsed = json.loads(raw)
        except Exception as exc:
            raise ValueError(f"Could not parse config file '{config_path}': {exc}")

        thresholds = parsed.get("thresholds", parsed)  # allow flat or nested
        for key in DEFAULTS:
            if key in thresholds and thresholds[key] is not None:
                cfg[key] = thresholds[key]

        cfg["_notifications"] = parsed.get("notifications", {})
        cfg["_input"] = parsed.get("input", {})

    # Environment variables always take final precedence (12-factor style).
    for key, env_name in ENV_OVERRIDES.items():
        if env_name in os.environ:
            val = os.environ[env_name]
            cfg[key] = val if key == "expected_node_status" else float(val)

    return cfg


# --------------------------------------------------------------------------
# Resilient parsing helpers
# --------------------------------------------------------------------------
def parse_timestamp(raw: Any) -> Optional[datetime]:
    """
    Best-effort parse of a timestamp field into a timezone-aware datetime.
    Returns None (never raises) if the value can't be interpreted, so callers
    can flag/skip the record instead of crashing the whole run.
    """
    if not raw or not isinstance(raw, str):
        return None

    candidates = [raw, raw.replace(" ", "T")]
    formats = [
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d",
    ]

    for candidate in candidates:
        # fromisoformat handles the common "...Z" / offset cases in 3.11+;
        # guard with try/except for older runtimes and odd input.
        try:
            iso_candidate = candidate.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso_candidate)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
        for fmt in formats:
            try:
                dt = datetime.strptime(candidate, fmt)
                return dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def safe_get(d: dict, key: str) -> Any:
    return d.get(key) if isinstance(d, dict) else None


# --------------------------------------------------------------------------
# Evaluation logic — one function per failure condition in "The Ask"
# --------------------------------------------------------------------------
def evaluate_hardware(nodes: list, cfg: dict) -> list:
    findings = []
    expected = cfg["expected_node_status"]

    for node in nodes or []:
        node_id = safe_get(node, "node_id") or "<unknown-node-id>"
        status = safe_get(node, "status")

        if status is None:
            findings.append(Finding(
                category="data_quality",
                severity=Severity.WARNING,
                resource=node_id,
                message="Node record is missing 'status' field; flagged, not evaluated.",
                details={"raw_record": node},
            ))
            continue

        if status != expected:
            findings.append(Finding(
                category="hardware",
                severity=Severity.CRITICAL,
                resource=node_id,
                message=f"Node status is '{status}', expected '{expected}'.",
                details={"status": status, "model": safe_get(node, "model")},
            ))

    return findings


def evaluate_capacity(volumes: list, cfg: dict) -> list:
    findings = []
    warn = float(cfg["capacity_warning_percent"])
    crit = float(cfg["capacity_critical_percent"])

    for vol in volumes or []:
        name = safe_get(vol, "volume_name") or "<unknown-volume>"
        pct = safe_get(vol, "used_capacity_percent")

        if pct is None:
            findings.append(Finding(
                category="data_quality",
                severity=Severity.WARNING,
                resource=name,
                message="Volume record is missing 'used_capacity_percent'; flagged, not evaluated.",
                details={"raw_record": vol},
            ))
            continue

        try:
            pct = float(pct)
        except (TypeError, ValueError):
            findings.append(Finding(
                category="data_quality",
                severity=Severity.WARNING,
                resource=name,
                message=f"Volume 'used_capacity_percent' is not numeric ('{pct}'); flagged, not evaluated.",
                details={"raw_record": vol},
            ))
            continue

        if pct > crit:
            findings.append(Finding(
                category="capacity",
                severity=Severity.CRITICAL,
                resource=name,
                message=f"Capacity at {pct}% (> {crit}% critical threshold).",
                details={"used_capacity_percent": pct, "protocol": safe_get(vol, "protocol")},
            ))
        elif pct > warn:
            findings.append(Finding(
                category="capacity",
                severity=Severity.WARNING,
                resource=name,
                message=f"Capacity at {pct}% (> {warn}% warning threshold).",
                details={"used_capacity_percent": pct, "protocol": safe_get(vol, "protocol")},
            ))

    return findings


def evaluate_replication(relationships: list, cfg: dict, now: datetime) -> list:
    findings = []
    warn_hours = float(cfg["replication_lag_warning_hours"])
    crit_hours = float(cfg["replication_lag_critical_hours"])

    for rel in relationships or []:
        source = safe_get(rel, "source_volume") or "<unknown-source>"
        raw_ts = safe_get(rel, "last_successful_sync")
        parsed_ts = parse_timestamp(raw_ts)

        if parsed_ts is None:
            findings.append(Finding(
                category="data_quality",
                severity=Severity.WARNING,
                resource=source,
                message=f"Could not parse 'last_successful_sync' value ('{raw_ts}'); flagged, not evaluated.",
                details={"raw_record": rel},
            ))
            continue

        age_hours = (now - parsed_ts).total_seconds() / 3600.0

        if age_hours > crit_hours:
            findings.append(Finding(
                category="replication",
                severity=Severity.CRITICAL,
                resource=source,
                message=f"Last successful sync was {age_hours:.1f}h ago (> {crit_hours}h critical RPO threshold).",
                details={
                    "destination_target": safe_get(rel, "destination_target"),
                    "last_successful_sync": raw_ts,
                    "age_hours": round(age_hours, 2),
                },
            ))
        elif age_hours > warn_hours:
            findings.append(Finding(
                category="replication",
                severity=Severity.WARNING,
                resource=source,
                message=f"Last successful sync was {age_hours:.1f}h ago (> {warn_hours}h RPO threshold).",
                details={
                    "destination_target": safe_get(rel, "destination_target"),
                    "last_successful_sync": raw_ts,
                    "age_hours": round(age_hours, 2),
                },
            ))

    return findings


# --------------------------------------------------------------------------
# Output formatting
# --------------------------------------------------------------------------
def build_result(cluster_name: str, findings: list, run_time: datetime) -> dict:
    counts = {sev.value: 0 for sev in Severity}
    for f in findings:
        counts[f.severity] += 1

    overall = Severity.OK
    if counts[Severity.CRITICAL] > 0:
        overall = Severity.CRITICAL
    elif counts[Severity.WARNING] > 0:
        overall = Severity.WARNING

    return {
        "cluster_name": cluster_name,
        "run_timestamp_utc": run_time.isoformat(),
        "overall_severity": overall.value,
        "finding_counts": counts,
        "findings": [asdict(f) for f in findings],
    }


def human_summary(result: dict) -> str:
    lines = []
    lines.append(f"Storage Health Report — {result['cluster_name']}")
    lines.append(f"Run time (UTC): {result['run_timestamp_utc']}")
    lines.append(f"Overall status: {result['overall_severity']}")
    counts = result["finding_counts"]
    lines.append(
        f"Findings: {counts['CRITICAL']} CRITICAL, {counts['WARNING']} WARNING, "
        f"{counts['INFO']} INFO ({counts['OK']} OK)"
    )
    lines.append("")

    if not result["findings"]:
        lines.append("No issues detected. All nodes, volumes, and replication pairs healthy.")
        return "\n".join(lines)

    for sev in ("CRITICAL", "WARNING", "INFO"):
        matches = [f for f in result["findings"] if f["severity"] == sev]
        if not matches:
            continue
        lines.append(f"[{sev}]")
        for f in matches:
            lines.append(f"  - ({f['category']}) {f['resource']}: {f['message']}")
        lines.append("")

    return "\n".join(lines).rstrip()


def build_webhook_payload(result: dict, webhook_type: str) -> dict:
    """Mock Slack/Teams payload — printed/POSTed but not required to succeed."""
    text = human_summary(result)
    if webhook_type == "teams":
        return {
            "@type": "MessageCard",
            "@context": "https://schema.org/extensions",
            "summary": f"Storage Health Report - {result['overall_severity']}",
            "themeColor": {"CRITICAL": "FF0000", "WARNING": "FFA500", "OK": "00A000"}.get(
                result["overall_severity"], "808080"
            ),
            "title": f"Storage Health Report — {result['cluster_name']}",
            "text": text,
        }
    # default: Slack-style
    return {
        "text": f"*Storage Health Report — {result['cluster_name']}*\n```{text}```"
    }


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def load_payload(input_path: str) -> dict:
    if not os.path.isfile(input_path):
        raise FileNotFoundError(f"Input payload not found: {input_path}")
    with open(input_path, "r") as fh:
        raw = fh.read()
    return json.loads(raw)  # allowed to raise; caller treats as fatal


def run(input_path: str, config_path: Optional[str], output_json_path: Optional[str]) -> int:
    # 1. Load config — a broken config is a fatal, non-zero-exit condition.
    try:
        cfg = load_config(config_path)
    except Exception as exc:
        log.error("Failed to load configuration: %s", exc)
        return ExitCode.RUN_FAILED

    # 2. Load + parse the input payload — unreachable/unparseable is fatal.
    try:
        payload = load_payload(input_path)
    except FileNotFoundError as exc:
        log.error("Input source unreachable: %s", exc)
        return ExitCode.RUN_FAILED
    except json.JSONDecodeError as exc:
        log.error("Input payload is malformed / unparseable: %s", exc)
        return ExitCode.RUN_FAILED
    except Exception as exc:
        log.error("Unexpected error reading input payload: %s", exc)
        return ExitCode.RUN_FAILED

    cluster_name = payload.get("cluster_name", "<unknown-cluster>")

    # Replication lag is measured relative to the moment the API snapshot was
    # taken (payload["timestamp"]), not wall-clock "now". This makes the
    # monitor correct both live (fresh payload) and against archived/replayed
    # payloads used for testing. Falls back to real wall-clock time if the
    # payload has no usable timestamp of its own.
    now = parse_timestamp(payload.get("timestamp")) or datetime.now(timezone.utc)

    # 3. Evaluate — individual bad records are handled inside each evaluator
    #    and turned into WARNING data_quality findings rather than exceptions.
    findings = []
    try:
        findings += evaluate_hardware(payload.get("hardware_nodes", []), cfg)
        findings += evaluate_capacity(payload.get("volumes", []), cfg)
        findings += evaluate_replication(payload.get("replication_relationships", []), cfg, now)
    except Exception as exc:
        # Belt-and-braces: a truly unexpected bug in evaluation logic is
        # still a failed run, not a false "all healthy" result.
        log.error("Unexpected error while evaluating payload: %s", exc)
        return ExitCode.RUN_FAILED

    result = build_result(cluster_name, findings, now)

    # 4. Emit structured JSON (machine-consumable).
    json_str = json.dumps(result, indent=2)
    if output_json_path:
        with open(output_json_path, "w") as fh:
            fh.write(json_str)
        log.info("Structured result written to %s", output_json_path)
    else:
        print(json_str)

    # 5. Emit human-readable summary + mock webhook payload.
    summary = human_summary(result)
    print("\n" + summary, file=sys.stderr)

    webhook_cfg = cfg.get("_notifications", {}) if isinstance(cfg, dict) else {}
    webhook_payload = build_webhook_payload(result, webhook_cfg.get("webhook_type", "slack"))
    log.info("Mock webhook payload:\n%s", json.dumps(webhook_payload, indent=2))

    if result["overall_severity"] == Severity.CRITICAL.value:
        return ExitCode.CRITICAL_FOUND
    return ExitCode.OK


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Proactive Storage Health Monitor")
    parser.add_argument(
        "--input",
        default=os.environ.get("MONITOR_INPUT_PATH", "storage_api_mock.json"),
        help="Path to the storage API JSON payload (default: storage_api_mock.json, "
             "or $MONITOR_INPUT_PATH).",
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("MONITOR_CONFIG_PATH", "config.yaml"),
        help="Path to the YAML/JSON config file (default: config.yaml, or $MONITOR_CONFIG_PATH).",
    )
    parser.add_argument(
        "--output-json",
        default=os.environ.get("MONITOR_OUTPUT_JSON_PATH"),
        help="If set, write the structured JSON result to this path instead of stdout.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    exit_code = run(args.input, args.config, args.output_json)
    log.info("Run finished with exit code %d", exit_code)
    sys.exit(exit_code)
