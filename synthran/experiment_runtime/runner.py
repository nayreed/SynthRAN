#!/usr/bin/env python3
"""Accepted-testbed runtime for SynthRAN physical experiments."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import yaml

from synthran.experiments import load_scenario, remap_gateways, scientific_settings
from synthran.scenario import load_scenario as load_testbed
from synthran.testbed_attachment import (
    attach_active_deployment,
    prove_experiment_eligible,
    requirements_from_deployment,
)

EXPERIMENT_ANSIBLE = ROOT / "synthran/experiment_runtime/ansible"


def _attachment(requirements: dict | None = None) -> dict:
    """Resolve historical accepted-testbed state without mutating authority state."""

    return attach_active_deployment(requirements)


def _active_private_dir(
    requirements: dict | None = None,
    *,
    expected_deployment_hash: str | None = None,
) -> Path:
    attachment = _attachment(requirements)
    if (
        expected_deployment_hash is not None
        and attachment["deployment_hash"] != expected_deployment_hash
    ):
        raise ValueError(
            "the accepted deployment changed after experiment preparation; "
            "refusing to attach the experiment to different infrastructure"
        )
    private = Path(attachment["private_execution_dir"]).resolve()
    os.environ["SYNTHRAN_PRIVATE_DIR"] = str(private)
    return private


def _configure_source(source: Path) -> dict:
    """Load either a full experiment scenario or standalone scientific settings."""
    raw = yaml.safe_load(source.read_text())
    if not isinstance(raw, dict):
        raise ValueError("experiment configuration must be a mapping")
    if "deployment" in raw:
        return load_scenario(source)

    for section in ("model", "mqtt", "devices"):
        if not isinstance(raw.get(section), dict):
            raise ValueError(f"experiment settings require mapping: {section}")
    if not raw["devices"]:
        raise ValueError("devices must define at least one sensor")

    logical_gateways = []
    for name, device in raw["devices"].items():
        if not isinstance(device, dict):
            raise ValueError("devices must map sensor names to configurations")
        gateway = device.get("gateway", name)
        device["gateway"] = gateway
        if gateway not in logical_gateways:
            logical_gateways.append(gateway)
    raw["deployment"] = {"ues": logical_gateways}

    trace = raw["model"].get("energy", {}).get("trace")
    if trace and not str(trace).startswith("builtin:"):
        raw["model"]["energy"]["trace"] = str((source.resolve().parent / trace).resolve())
    for device in raw["devices"].values():
        trace = device.get("energy", {}).get("trace")
        if trace and not str(trace).startswith("builtin:"):
            device["energy"]["trace"] = str((source.resolve().parent / trace).resolve())
    return raw


def configure(config: Path, source: Path) -> None:
    original = _configure_source(source)
    selected = load_testbed(config)
    remap_gateways(original, selected["deployment"]["ues"])
    settings = config.with_name("selected-experiment.yml")
    settings.write_text(
        yaml.safe_dump(scientific_settings(original), sort_keys=False)
    )
    selected["experiment"]["config"] = str(settings.resolve())
    selected.pop("_source_directory", None)
    config.write_text(yaml.safe_dump(selected, sort_keys=False))


def _write_attachment_snapshot(run: Path, attachment: dict) -> None:
    snapshot = {
        key: value
        for key, value in attachment.items()
        if key != "private_execution_dir"
    }
    (run / "accepted-testbed.json").write_text(
        json.dumps(snapshot, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _expected_attachment_hash(run: Path) -> str:
    path = run / "accepted-testbed.json"
    if not path.is_file():
        raise FileNotFoundError(
            "experiment run has no accepted-testbed snapshot; prepare the experiment first"
        )
    value = json.loads(path.read_text(encoding="utf-8"))
    deployment_hash = value.get("deployment_hash")
    if not isinstance(deployment_hash, str) or not deployment_hash:
        raise ValueError("accepted-testbed snapshot has no deployment hash")
    return deployment_hash


def prepare(
    config: Path, run: Path, prepared: Path | None, resume: Path | None
) -> None:
    from synthran.runtime import ensure

    ensure("experiment", run / "experiment-bootstrap.log")
    from synthran.mqtt_auth import write_credentials
    from synthran.workload.bundle import import_bundle
    from synthran.workload.trace import generate

    scenario = load_scenario(config)
    run.mkdir(parents=True, exist_ok=True)
    testbed = load_testbed(config)
    requirements = requirements_from_deployment(testbed["deployment"])
    attachment = _attachment(requirements)
    private = Path(attachment["private_execution_dir"]).resolve()
    os.environ["SYNTHRAN_PRIVATE_DIR"] = str(private)
    _write_attachment_snapshot(run, attachment)
    write_credentials(private)

    settings = run / "experiment-input.yml"
    settings.write_text(
        yaml.safe_dump(scientific_settings(scenario), sort_keys=False)
    )
    testbed["experiment"]["config"] = str(settings.resolve())
    testbed.pop("_source_directory", None)
    config.write_text(yaml.safe_dump(testbed, sort_keys=False))
    snapshot = run / "experiment-scenario.yml"
    scenario.pop("_source_directory", None)
    scenario.pop("experiment", None)
    snapshot.write_text(yaml.safe_dump(scenario, sort_keys=False))
    if resume:
        prepared = resume / "model"
    if prepared:
        import_bundle(prepared, run / "model", snapshot)
    else:
        generate(snapshot, run / "model")
    for relative in (
        "__init__.py",
        "cli.py",
        "workload/__init__.py",
        "workload/replay.py",
        "workload/bundle.py",
        "workload/cleanup.py",
    ):
        destination = run / "runtime/synthran" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "synthran" / relative, destination)
    mqtt = scenario["mqtt"]
    manifest = json.loads((run / "model/source-manifest.json").read_text())
    variables = {
        "run_dir": str(run),
        "mqtt_start_delay_seconds": mqtt.get("start_delay_seconds", 30),
        "mqtt_broker_address": mqtt.get("broker_address"),
        "mqtt_port": mqtt.get("port", 1883),
        "mqtt_qos": mqtt.get("qos", 1),
        "mqtt_topic_prefix": mqtt.get("topic_prefix", "synthran"),
        "mqtt_drain_seconds": mqtt.get("drain_seconds", 60),
        "mqtt_max_inflight": mqtt.get("max_inflight", 20),
        "mqtt_max_queued": mqtt.get("max_queued", 10000),
        "synthran_replay_horizon_seconds": manifest["duration_seconds"],
    }
    (run / "experiment-vars.yml").write_text(yaml.safe_dump(variables, sort_keys=False))


def _run_playbook(
    run: Path,
    playbook: Path,
    *,
    private: Path | None = None,
    extra_vars_file: Path | None = None,
) -> None:
    if private is None:
        private = _active_private_dir(
            expected_deployment_hash=_expected_attachment_hash(run)
        )
    else:
        private = private.resolve()
        os.environ["SYNTHRAN_PRIVATE_DIR"] = str(private)

    environment = dict(os.environ)
    environment["ANSIBLE_CONFIG"] = str(private / "ansible/ansible.cfg")
    environment["ANSIBLE_ROLES_PATH"] = os.pathsep.join(
        [str(EXPERIMENT_ANSIBLE / "roles"), str(private / "ansible/roles")]
    )
    environment["SYNTHRAN_PRIVATE_DIR"] = str(private)
    secrets_file = private / "experiment-secrets.yml"
    if not secrets_file.is_file():
        raise FileNotFoundError("private MQTT credentials are missing; prepare the experiment first")
    executable = Path(sys.executable).with_name("ansible-playbook")
    command = [
        str(executable),
        "-i",
        str(private / "inventory.yml"),
        "-e",
        "@" + str(private / "ansible/group_vars/all/all.yml"),
        "-e",
        "@" + str(private / "deployment-vars.yml"),
        "-e",
        "@" + str(run / "experiment-vars.yml"),
        "-e",
        "@" + str(secrets_file),
    ]
    if extra_vars_file is not None:
        command.extend(["-e", "@" + str(extra_vars_file)])
    command.append(str(playbook))
    subprocess.run(command, cwd=ROOT, env=environment, check=True)


def _refresh_experiment_eligibility(run: Path) -> dict:
    expected_hash = _expected_attachment_hash(run)
    attachment = _attachment({"deployment_hash": expected_hash})
    private = Path(attachment["private_execution_dir"]).resolve()
    evidence = run / "experiment-eligibility-evidence.json"
    cluster = run / "experiment-eligibility-cluster.json"
    variables = {
        "run_dir": str(run),
        "synthran_live_evidence_file": str(evidence.resolve()),
        "synthran_live_cluster_file": str(cluster.resolve()),
    }
    vars_file = run / "experiment-eligibility-vars.yml"
    vars_file.write_text(yaml.safe_dump(variables, sort_keys=False), encoding="utf-8")
    _run_playbook(
        run,
        private / "ansible/playbooks/verify_live_testbed.yml",
        private=private,
        extra_vars_file=vars_file,
    )
    eligible = prove_experiment_eligible(
        evidence,
        {"deployment_hash": expected_hash},
        attachment=attachment,
        cluster_snapshot_path=cluster,
        max_age_seconds=120,
    )
    snapshot = {
        key: value
        for key, value in eligible.items()
        if key != "private_execution_dir"
    }
    (run / "experiment-eligible.json").write_text(
        json.dumps(snapshot, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return eligible


def run_workload(run: Path) -> None:
    eligible = _refresh_experiment_eligibility(run)
    _run_playbook(
        run,
        EXPERIMENT_ANSIBLE / "playbooks/mqtt.yml",
        private=Path(eligible["private_execution_dir"]),
    )


def cleanup(run: Path) -> None:
    _run_playbook(run, EXPERIMENT_ANSIBLE / "playbooks/cleanup.yml")


def finalize(run: Path) -> None:
    from synthran.results import reconcile

    publishers = run / "publisher.jsonl"
    sources = [
        source
        for source in sorted(run.glob("publisher-*.jsonl"))
        if source != publishers
    ]
    partial = run / "partial-software-publishers.jsonl"
    if partial.is_file():
        sources.append(partial)

    seen: set[str] = set()
    with publishers.open("w") as stream:
        for source in sources:
            for line in source.read_text().splitlines():
                if not line.strip() or line in seen:
                    continue
                seen.add(line)
                stream.write(line + "\n")
    summary = reconcile(
        run / "model/events.jsonl",
        publishers,
        run / "broker.jsonl",
        run / "summary.json",
        run / "experiment-scenario.yml",
        True,
    )
    print(json.dumps(summary, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "phase", choices=("configure", "prepare", "run", "cleanup", "finalize")
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--prepared-workload", type=Path)
    parser.add_argument("--resume-from", type=Path)
    parser.add_argument("--source-config", type=Path)
    args = parser.parse_args()
    if args.phase == "configure":
        if not args.source_config:
            parser.error("configure requires --source-config")
        configure(args.config, args.source_config)
    else:
        if not args.run_dir:
            parser.error(f"{args.phase} requires --run-dir")
        run = args.run_dir.resolve()
        if args.phase == "prepare":
            prepare(args.config, run, args.prepared_workload, args.resume_from)
        elif args.phase == "run":
            run_workload(run)
        elif args.phase == "cleanup":
            cleanup(run)
        else:
            finalize(run)


if __name__ == "__main__":
    main()
