#!/usr/bin/env python3
"""Validate Sub 10 accepted-testbed and experiment-eligibility contracts."""
from __future__ import annotations

import copy
import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from synthran.acceptance import bind_prerequisite_evidence
from synthran.cluster_identity import selected_cluster_runtime
from synthran.deployment_identity import build_implementation_identity, write_execution_manifest
from synthran.deployment_state import content_hash
from synthran.testbed_attachment import (
    AttachmentError,
    attach_active_deployment,
    prove_experiment_eligible,
)

ROOT = Path(__file__).resolve().parents[1]


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def require_failure(action, message: str) -> None:
    try:
        action()
    except AttachmentError:
        return
    raise AssertionError(message)


def pod(name: str, labels: dict[str, str], container: str, digest: str) -> dict:
    return {
        "name": name,
        "labels": labels,
        "phase": "Running",
        "ready": True,
        "containers": [{
            "kind": "container",
            "name": container,
            "configured_image": f"example/{container}:ci",
            "runtime_image_id": "containerd://" + digest,
            "ready": True,
        }],
    }


def check_oai_split_ran_ownership() -> None:
    deployment = {"core": "oai", "ran": "oai", "topology": {"namespace": "oai"}}
    releases = ["oai-5g-basic", "oai-cu", "oai-du", "oai-cu-cp", "oai-cu-up"]
    digest = "sha256:" + "a" * 64
    snapshot = {
        "namespace": "oai",
        "pods": [
            pod(name + "-pod", {"app.kubernetes.io/instance": name}, name, digest)
            for name in releases
        ],
        "helm_releases": [
            {
                "name": name,
                "status": "deployed",
                "chart": name + "-1.0.0",
                "app_version": "ci",
                "values_sha256": "1" * 64,
            }
            for name in releases
        ],
    }
    runtime = selected_cluster_runtime(deployment, snapshot)
    expected = {f"helm:{name}" for name in releases}
    assert {item["owner"] for item in runtime["workloads"]} == expected
    assert {item["name"] for item in runtime["helm_releases"]} == set(releases)


def deployment_fixture() -> dict:
    return {
        "core": "open5gs",
        "ran": "srsran",
        "platform": "rfsim",
        "radio_unit": "rfsim",
        "reservation_mode": "create",
        "host_preparation": "fresh",
        "pos_image": "ubuntu-jammy",
        "network_profile": "ci",
        "network_profile_hash": "sha256:" + "9" * 64,
        "bridge_enabled": True,
        "nodes": {"core": "f2", "ran": "f3", "broker": "f2"},
        "topology": {"namespace": "open5gs"},
        "slices": [{
            "name": "default",
            "sst": "1",
            "sd": "000001",
            "dnn": "internet",
            "ip_prefix": "12.1.1",
        }],
        "ues": [{
            "device": "uesim01",
            "index": 1,
            "imsi": "001010000000001",
            "slice": "default",
            "sst": "1",
            "sd": "000001",
            "dnn": "internet",
            "address_cidr": "12.1.1.0/24",
            "user_plane_target": "12.1.1.1",
            "tunnel": {"namespace": "open5gs", "interface": "tun_srsue1"},
        }],
    }


def cluster_fixture(configuration_hash: str) -> dict:
    digests = ["sha256:" + char * 64 for char in "abc"]
    return {
        "schema_version": 3,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "namespace": "open5gs",
        "cluster_attestation": {"configuration_hash": configuration_hash},
        "pods": [
            pod("open5gs-amf", {"nf": "amf"}, "amf", digests[0]),
            pod("srsran-gnb", {"app": "srsran", "component": "gnb"}, "gnb", digests[1]),
            pod("srsran-ue", {"app": "srsran", "component": "ue"}, "ue", digests[2]),
            {
                "name": "unrelated-debug",
                "labels": {"app": "unrelated"},
                "phase": "Pending",
                "ready": False,
                "containers": [{
                    "kind": "container",
                    "name": "debug",
                    "configured_image": "example/debug:latest",
                    "runtime_image_id": "",
                    "ready": False,
                }],
            },
        ],
        "helm_releases": [
            {
                "name": "srsran-gnb",
                "status": "deployed",
                "chart": "srsran-gnb-0.1.0",
                "app_version": "ci",
                "values_sha256": "1" * 64,
            },
            {
                "name": "srsran-ue",
                "status": "deployed",
                "chart": "srsran-ue-0.1.0",
                "app_version": "ci",
                "values_sha256": "2" * 64,
            },
            {
                "name": "unrelated",
                "status": "failed",
                "chart": "unrelated-1.0.0",
                "app_version": "ci",
                "values_sha256": "0" * 64,
            },
        ],
    }


def stage_execution(private: Path, deployment: dict, result: Path) -> None:
    staged = private / "ansible"
    shutil.copytree(ROOT / "deployment", staged)
    (staged / "reference").mkdir(parents=True)
    shutil.copyfile(
        ROOT / "third_party/sopnode-5g-ansible/EXECUTION_REFERENCE.json",
        staged / "reference/EXECUTION_REFERENCE.json",
    )
    write_execution_manifest(deployment, staged, result / "execution-manifest.json")


def main() -> None:
    check_oai_split_ran_ownership()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        private = root / "private"
        result = root / "deployment-result"
        private.mkdir()
        result.mkdir()
        (private / "inventory.yml").write_text("all: {}\n", encoding="utf-8")
        (private / "deployment-vars.yml").write_text("{}\n", encoding="utf-8")
        (private / "network-profile.yml").write_text("plmn: {mcc: '001', mnc: '01'}\n", encoding="utf-8")
        (private / "ssh-known-hosts").write_text("ci-host ssh-ed25519 test-key\n", encoding="utf-8")

        deployment = deployment_fixture()
        configuration_hash = content_hash(deployment)
        snapshot = cluster_fixture(configuration_hash)
        cluster_runtime = selected_cluster_runtime(deployment, snapshot)
        stage_execution(private, deployment, result)
        write_json(result / "provenance/cluster.json", snapshot)
        write_json(
            result / "bootstrap-evidence.json",
            {"host_preparation": "fresh", "nodes": [], "cluster_nodes": {}},
        )
        write_json(
            result / "transport-evidence.json",
            {"schema_version": 1, "deployment_hash": configuration_hash},
        )

        implementation = build_implementation_identity({"deployment": deployment}, result, private)
        assert implementation["cluster_runtime"] == cluster_runtime
        prerequisites = bind_prerequisite_evidence(result, configuration_hash)
        deployment_hash = content_hash(
            {"deployment": deployment, "implementation": implementation}
        )
        implementation_hash = content_hash(implementation)
        now = datetime.now(timezone.utc).isoformat()

        identity = result / "accepted.json"
        evidence = result / "live-deployment-evidence.json"
        endpoint = root / "endpoint.json"
        write_json(identity, {
            "schema_version": 2,
            "acceptance_schema_version": 1,
            "status": "accepted-testbed",
            "configuration_hash": configuration_hash,
            "deployment_hash": deployment_hash,
            "implementation_identity_sha256": implementation_hash,
            "implementation": implementation,
            "prerequisite_evidence": prerequisites,
            "deployment": deployment,
            "accepted_at": now,
            "acceptance_evidence": {
                "observed_at": now,
                "implementation_identity_sha256": implementation_hash,
                "prerequisite_evidence": prerequisites,
            },
        })

        binding = {
            "device": "uesim01",
            "index": 1,
            "imsi": "001010000000001",
            "slice": "default",
            "sst": "1",
            "sd": "000001",
            "dnn": "internet",
            "namespace": "open5gs",
            "interface": "tun_srsue1",
            "address": "12.1.1.2",
            "software_verified": True,
            "user_plane": {
                "verified": True,
                "method": "icmp_echo",
                "source_interface": "tun_srsue1",
                "source_address": "12.1.1.2",
                "target_address": "12.1.1.1",
                "observed_at": now,
            },
        }
        accepted_evidence = {
            "schema_version": 2,
            "configuration_hash": configuration_hash,
            "deployment_hash": deployment_hash,
            "implementation_identity_sha256": implementation_hash,
            "cluster_identity_verified": True,
            "bindings": [binding],
            "observed_at": now,
        }
        write_json(evidence, accepted_evidence)
        write_json(endpoint, {
            "schema_version": 2,
            "status": "accepted-testbed",
            "configuration_hash": configuration_hash,
            "deployment_hash": deployment_hash,
            "run_id": "ci-deployment",
            "identity_file": str(identity),
            "evidence_file": str(evidence),
            "private_execution_dir": str(private),
            "result_dir": str(result),
            "published_at": now,
        })

        requirements = {
            "core": "open5gs",
            "ran": ["srsran", "oai"],
            "platform": "rfsim",
            "radio_unit": "rfsim",
            "reservation_mode": "create",
            "host_preparation": "fresh",
            "nodes": {"core": "f2", "ran": "f3"},
            "ue_devices": ["uesim01"],
            "exact_ue_devices": True,
            "ues": [{"device": "uesim01", "dnn": "internet", "interface": "tun_srsue1"}],
            "slices": [{"name": "default", "dnn": "internet"}],
        }
        attached = attach_active_deployment(requirements, endpoint_path=endpoint)
        assert attached["status"] == "accepted-testbed-attached"
        assert attached["experiment_eligible"] is False
        assert attached["mode"] == "read_only"
        assert attached["deployment"]["reservation_mode"] == "create"
        assert attached["deployment"]["host_preparation"] == "fresh"
        assert attached["deployment"]["pos_image"] == "ubuntu-jammy"

        require_failure(
            lambda: attach_active_deployment({"ran": "oai"}, endpoint_path=endpoint),
            "incompatible accepted deployment was not rejected",
        )

        bad_evidence = copy.deepcopy(accepted_evidence)
        bad_evidence["implementation_identity_sha256"] = "sha256:" + "0" * 64
        write_json(evidence, bad_evidence)
        require_failure(
            lambda: attach_active_deployment(endpoint_path=endpoint),
            "wrong implementation evidence was accepted",
        )
        write_json(evidence, accepted_evidence)

        staged_cfg = private / "ansible/ansible.cfg"
        original = staged_cfg.read_text(encoding="utf-8")
        staged_cfg.write_text(original + "\n# tampered\n", encoding="utf-8")
        require_failure(
            lambda: attach_active_deployment(endpoint_path=endpoint),
            "tampered retained execution context was accepted",
        )
        staged_cfg.write_text(original, encoding="utf-8")

        private_vars = private / "deployment-vars.yml"
        original = private_vars.read_text(encoding="utf-8")
        private_vars.write_text("tampered: true\n", encoding="utf-8")
        require_failure(
            lambda: attach_active_deployment(endpoint_path=endpoint),
            "tampered private deployment variables were accepted",
        )
        private_vars.write_text(original, encoding="utf-8")

        bootstrap = result / "bootstrap-evidence.json"
        original = bootstrap.read_text(encoding="utf-8")
        bootstrap.write_text('{"tampered": true}\n', encoding="utf-8")
        require_failure(
            lambda: attach_active_deployment(endpoint_path=endpoint),
            "tampered predecessor evidence was accepted",
        )
        bootstrap.write_text(original, encoding="utf-8")

        unrelated_changed = copy.deepcopy(snapshot)
        unrelated_changed["pods"][3]["name"] = "unrelated-changed"
        unrelated_changed["helm_releases"][2]["status"] = "pending-install"
        assert selected_cluster_runtime(deployment, unrelated_changed) == cluster_runtime

        fresh_evidence = root / "experiment-eligibility-evidence.json"
        fresh_cluster = root / "experiment-eligibility-cluster.json"
        write_json(fresh_evidence, accepted_evidence)
        write_json(fresh_cluster, unrelated_changed)

        original = private_vars.read_text(encoding="utf-8")
        private_vars.write_text("tampered-after-attach: true\n", encoding="utf-8")
        require_failure(
            lambda: prove_experiment_eligible(
                fresh_evidence,
                requirements,
                attachment=attached,
                cluster_snapshot_path=fresh_cluster,
                max_age_seconds=120,
            ),
            "eligibility accepted private-input drift after attachment",
        )
        private_vars.write_text(original, encoding="utf-8")

        eligible = prove_experiment_eligible(
            fresh_evidence,
            requirements,
            endpoint_path=endpoint,
            max_age_seconds=120,
        )
        assert eligible["status"] == "experiment-eligible"
        assert eligible["experiment_eligible"] is True

        drifted = copy.deepcopy(snapshot)
        drifted["pods"][1]["containers"][0]["runtime_image_id"] = (
            "containerd://sha256:" + "d" * 64
        )
        write_json(fresh_cluster, drifted)
        require_failure(
            lambda: prove_experiment_eligible(
                fresh_evidence,
                requirements,
                endpoint_path=endpoint,
                max_age_seconds=120,
            ),
            "selected runtime image drift was not rejected",
        )

        write_json(fresh_cluster, snapshot)
        stale = copy.deepcopy(accepted_evidence)
        stale["observed_at"] = "2000-01-01T00:00:00+00:00"
        write_json(fresh_evidence, stale)
        require_failure(
            lambda: prove_experiment_eligible(
                fresh_evidence,
                requirements,
                endpoint_path=endpoint,
                max_age_seconds=120,
            ),
            "stale experiment eligibility evidence was accepted",
        )

    print("accepted-testbed and experiment-eligibility contracts OK")


if __name__ == "__main__":
    main()
