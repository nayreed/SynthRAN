"""Reconcile Ambient-IoT model output with 5G/MQTT delivery evidence."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

import yaml

from ..acceptance import validate_live_evidence
from ..deployment_identity import validate_current_cluster_runtime
from ..deployment_state import bindings_match_deployment
from .metrics import measurements


def _read(path: str | Path) -> list[dict]:
    source = Path(path)
    if not source.exists():
        return []
    return [
        json.loads(line)
        for line in source.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _configured_devices(expected: str | Path, scenario: str | Path | None) -> list[str]:
    """Return the scenario device order, including devices with no decoded events."""
    source = (
        Path(scenario) if scenario else Path(expected).parent / "resolved-scenario.yml"
    )
    if not source.exists():
        return []
    data = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    if data.get("devices"):
        return list(data["devices"])
    deployment_devices = data.get("deployment", {}).get("ues", [])
    if deployment_devices:
        return list(dict.fromkeys(str(device) for device in deployment_devices))
    return list(dict.fromkeys(str(device) for device in data.get("devices", {})))


def _deployment_evidence(expected: str | Path) -> dict:
    """Verify the accepted deployment and the eligibility snapshot used for this run."""

    run = Path(expected).parent.parent
    identity_path = run / "deployment-fingerprint.json"
    evidence_path = run / "experiment-eligibility-evidence.json"
    cluster_path = run / "experiment-eligibility-cluster.json"
    decision_path = run / "experiment-eligible.json"
    required = (identity_path, evidence_path, cluster_path, decision_path)
    missing = [path.name for path in required if not path.is_file()]
    if missing:
        return {
            "verified": False,
            "reason": "experiment eligibility evidence is incomplete: " + ", ".join(missing),
        }

    try:
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        decision = json.loads(decision_path.read_text(encoding="utf-8"))
        validate_live_evidence(identity_path, evidence_path, max_age_seconds=None)
        validate_current_cluster_runtime(identity, cluster_path)
    except (ValueError, json.JSONDecodeError, OSError) as exc:
        return {
            "verified": False,
            "reason": f"experiment eligibility evidence is invalid: {exc}",
        }

    deployment = identity.get("deployment", {})
    bindings = evidence.get("bindings", [])
    binding_verified = isinstance(bindings, list) and bindings_match_deployment(
        deployment, bindings
    )
    decision_verified = (
        decision.get("status") == "experiment-eligible"
        and decision.get("experiment_eligible") is True
        and decision.get("deployment_hash") == identity.get("deployment_hash")
        and decision.get("eligibility_observed_at") == evidence.get("observed_at")
    )
    status_valid = identity.get("status") == "accepted-testbed"
    verified = binding_verified and decision_verified and status_valid
    return {
        "verified": verified,
        "status": identity.get("status"),
        "eligibility_status": decision.get("status"),
        "deployment_hash": identity.get("deployment_hash"),
        "scenario_hash": identity.get("scenario_hash"),
        "cluster_identity_verified": evidence.get("cluster_identity_verified") is True,
        "binding_verified": binding_verified,
        "bindings": bindings,
        "eligibility_observed_at": evidence.get("observed_at"),
        "reason": (
            None
            if verified
            else "experiment eligibility evidence does not completely match the accepted deployment"
        ),
    }


def reconcile(
    expected,
    publisher,
    broker,
    output="summary.json",
    scenario=None,
    require_deployment_identity=False,
) -> dict:
    if not Path(expected).is_file():
        raise FileNotFoundError("the expected workload trace is required")
    manifest_path = Path(expected).parent / "source-manifest.json"
    manifest = {}
    if manifest_path.exists():
        from synthran.workload.bundle import validate_bundle

        if Path(expected).name != "events.jsonl":
            raise ValueError(
                "reconciliation must use the bundle's authoritative events.jsonl"
            )
        manifest = validate_bundle(Path(expected).parent)
    expected_rows = _read(expected)
    publisher_records = _read(publisher)
    broker_records = _read(broker)
    publisher_rows = [
        row
        for row in publisher_records
        if "event_id" in row and row.get("record_type", "publish") == "publish"
    ]
    broker_rows = [
        row
        for row in broker_records
        if "event_id" in row and row.get("record_type", "receipt") == "receipt"
    ]
    expected_by_id = {row["event_id"]: row for row in expected_rows}
    mismatches = []
    valid_receipts = []
    for row in broker_rows:
        event = expected_by_id.get(row["event_id"])
        if event and (
            row.get("device") != event["device"]
            or row.get("retained", False)
            or ("topic" in row and row["topic"] != event["topic"])
            or (
                "payload_sha256" in row
                and row["payload_sha256"]
                != hashlib.sha256(event["payload"].encode()).hexdigest()
            )
        ):
            mismatches.append(row["event_id"])
        else:
            valid_receipts.append(row)
    broker_rows = valid_receipts
    expected_ids = {row["event_id"] for row in expected_rows}
    attempted_ids = {row["event_id"] for row in publisher_rows}
    published_ids = {
        row["event_id"] for row in publisher_rows if row.get("accepted", True)
    }
    received_counts = Counter(row["event_id"] for row in broker_rows)
    received_ids = set(received_counts)
    acknowledged_ids = {
        row["event_id"]
        for row in publisher_records
        if row.get("acknowledged") and "event_id" in row
    }
    configured_devices = _configured_devices(expected, scenario)
    observed_devices = {
        row.get("device", "unknown")
        for row in expected_rows + publisher_rows + broker_rows
    }
    if not configured_devices:
        configured_devices = sorted(observed_devices)
    devices = configured_devices + sorted(observed_devices - set(configured_devices))
    per_device = {}
    for device in devices:
        model_ids = {
            row["event_id"] for row in expected_rows if row.get("device") == device
        }
        per_device[device] = {
            "ambient_iot_decoded": len(model_ids),
            "published": len(model_ids & published_ids),
            "broker_received": len(model_ids & received_ids),
            "transport_lost": len((model_ids & published_ids) - received_ids),
        }
    ambient_summary_path = Path(expected).parent / "ambient_iot" / "summary.json"
    ambient = (
        json.loads(ambient_summary_path.read_text(encoding="utf-8"))
        if ambient_summary_path.exists()
        else {"decoded": len(expected_ids)}
    )
    suppression_count = int(ambient.get("energy_or_protocol_suppressed", 0))
    opportunity_count = int(ambient.get("opportunities", 0))
    rf_loss_count = int(ambient.get("radio_collision_loss", 0)) + int(
        ambient.get("below_sensitivity_or_unheard", 0)
    )
    config_path = (
        Path(scenario) if scenario else Path(expected).parent / "resolved-scenario.yml"
    )
    configuration = (
        (yaml.safe_load(config_path.read_text()) or {}) if config_path.exists() else {}
    )
    summary = {
        "deployment_identity": _deployment_evidence(expected),
        "artifact_presence": {
            "expected": Path(expected).is_file(),
            "publisher": Path(publisher).is_file(),
            "receipts": Path(broker).is_file(),
        },
        "measurement": measurements(
            expected_rows,
            publisher_rows,
            broker_rows,
            [row for row in publisher_records if row.get("record_type") == "session"],
            manifest,
            configuration.get("measurement", {}),
            broker_records,
        ),
        "endpoint": "receiving-application callback entry",
        "legacy_transport_lost_semantics": "publication accepted but not observed by collection end; not proven radio loss",
        "ambient_iot": ambient,
        "five_g": {
            "input": len(expected_ids),
            "publication_attempted": len(expected_ids & attempted_ids),
            "submission_failed": sorted((expected_ids & attempted_ids) - published_ids),
            "published": len(expected_ids & published_ids),
            "acknowledged": len(expected_ids & acknowledged_ids),
            "received": len(expected_ids & received_ids),
            "not_received_by_collection_end": sorted(expected_ids - received_ids),
            "invalid_receipt_records": sum(
                row.get("record_type") == "invalid_receipt" for row in broker_records
            ),
            "receipt_integrity_mismatch_event_ids": mismatches,
            "receipt_payloads_verified": bool(broker_rows)
            and all("payload_sha256" in row for row in broker_rows),
            "publisher_missing": sorted(expected_ids - published_ids),
            "transport_lost": sorted((expected_ids & published_ids) - received_ids),
            "unexpected_received": sorted(received_ids - expected_ids),
            "duplicate_receipts": sum(
                max(0, count - 1) for count in received_counts.values()
            ),
        },
        "per_device": per_device,
        "experimental_coverage": {
            "configured_devices": configured_devices,
            "devices_with_decoded_events": [
                device
                for device in configured_devices
                if per_device[device]["ambient_iot_decoded"] > 0
            ],
            "all_configured_devices_exercised": all(
                per_device[device]["ambient_iot_decoded"] > 0
                for device in configured_devices
            ),
            "rf_loss_observed": rf_loss_count > 0,
            "transport_loss_observed": bool(
                (expected_ids & published_ids) - received_ids
            ),
            "suppression_fraction": (
                suppression_count / opportunity_count if opportunity_count else 0.0
            ),
        },
    }
    if require_deployment_identity and not summary["deployment_identity"]["verified"]:
        raise ValueError(
            "result reconciliation refused: "
            + summary["deployment_identity"].get(
                "reason", "deployment identity was not proved"
            )
        )
    if output is not None:
        Path(output).write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary
