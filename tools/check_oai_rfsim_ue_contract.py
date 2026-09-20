#!/usr/bin/env python3
"""Contract regression checks for issue #114 OAI RFSIM UE authority."""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import yaml

from synthran.deployment_state import (
    build_manifest,
    build_ue_map,
    resolve_user_plane_targets,
)


ROOT = Path(__file__).resolve().parents[1]


class CheckError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CheckError(message)


def text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def physical_regression_contract() -> tuple[dict, list[dict]]:
    profile = yaml.safe_load(
        text("deployment/group_vars/all/network_profile_default.yaml")
    )
    catalog = yaml.safe_load(text("deployment/group_vars/all/ue_catalog.yaml"))["ues"]
    selected = ["uesim01", "uesim02", "uesim03"]
    assignments = {
        "uesim01": "slice1",
        "uesim02": "slice1",
        "uesim03": "slice2",
    }
    effective = copy.deepcopy(profile)
    effective["ues"] = {}
    for device in selected:
        entry = copy.deepcopy(catalog[device])
        entry.pop("platform")
        entry["slice"] = assignments[device]
        effective["ues"][device] = entry

    scenario = {
        "deployment": {
            "core": "oai",
            "ran": "oai",
            "platform": "rfsim",
            "network_profile": "default",
            "ues": selected,
            "ue_slices": assignments,
            "nodes": {
                "core": "sopnode-f2",
                "ran": "sopnode-f3",
                "broker": "sopnode-f2",
            },
        }
    }
    topology = {
        "namespace": "oai",
        "transport": {
            "user_plane_probe": {
                "kind": "oai-upf-tun0",
                "session_index": 0,
                "interface": "tun0",
            }
        },
    }
    ue_map = build_ue_map(scenario, effective)
    ue_map = resolve_user_plane_targets(
        scenario,
        effective,
        ue_map,
        topology,
    )
    manifest = build_manifest(
        scenario,
        effective,
        ue_map,
        topology=topology,
    )
    return manifest, ue_map


def check_resolved_mapping() -> None:
    _, ue_map = physical_regression_contract()
    require(
        [ue["device"] for ue in ue_map] == ["uesim01", "uesim02", "uesim03"],
        "physical regression UE order changed",
    )
    require(
        [ue["dnn"] for ue in ue_map] == ["internet", "internet", "streaming"],
        "selected DNN mapping is not 1/1/2 authoritative",
    )
    require(
        [ue["slice"] for ue in ue_map] == ["slice1", "slice1", "slice2"],
        "selected slice mapping is not 1/1/2 authoritative",
    )
    require(
        [ue["tunnel"]["pod_name_prefix"] for ue in ue_map]
        == ["oai-nr-ue-", "oai-nr-ue2-", "oai-nr-ue3-"],
        "OAI RFSIM release-slot identity changed",
    )
    require(
        [ue["user_plane_target"] for ue in ue_map]
        == ["12.1.1.1", "12.1.1.1", "12.1.1.1"],
        "OAI acceptance must use the shared live UPF tun0 endpoint for every DNN",
    )
    require(
        all(
            ue["user_plane_probe"]
            == {
                "kind": "oai-upf-tun0",
                "interface": "tun0",
                "session_index": 0,
                "address": "12.1.1.1",
            }
            for ue in ue_map
        ),
        "OAI user-plane probe identity is not sealed to the shared UPF tun0 endpoint",
    )
    require(
        ue_map[2]["user_plane_target"] != "14.1.1.1",
        "streaming PDU subnet must not manufacture a nonexistent OAI UPF .1 target",
    )


def load_probe_module():
    path = ROOT / "deployment/scripts/probe_software_ues.py"
    spec = importlib.util.spec_from_file_location("probe_software_ues", path)
    require(spec is not None and spec.loader is not None, "cannot load software UE probe")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_single_device_probe() -> None:
    manifest, ue_map = physical_regression_contract()
    discovered = [
        {
            "namespace": "oai",
            "pod": "oai-nr-ue-abc",
            "container": "nr-ue",
            "interface": "oaitun_ue1",
            "address": "12.1.1.11",
            "pod_labels": {},
        },
        {
            "namespace": "oai",
            "pod": "oai-nr-ue2-def",
            "container": "nr-ue",
            "interface": "oaitun_ue1",
            "address": "12.1.1.12",
            "pod_labels": {},
        },
        {
            "namespace": "oai",
            "pod": "oai-nr-ue3-ghi",
            "container": "nr-ue",
            "interface": "oaitun_ue1",
            "address": "14.1.1.11",
            "pod_labels": {},
        },
    ]
    probe = load_probe_module()
    probe._probe_user_plane = lambda candidate, ue: {
        "verified": True,
        "method": "icmp_echo",
        "source_interface": candidate["interface"],
        "source_address": candidate["address"],
        "target_address": ue["user_plane_target"],
        "target_kind": ue["user_plane_probe"]["kind"],
        "target_interface": ue["user_plane_probe"].get("interface"),
        "target_runtime": {
            "pod": "oai-upf-fixture",
            "container": "upf",
            "interface": "tun0",
            "address": ue["user_plane_target"],
        },
        "observed_at": "fixture",
    }
    binding = probe.resolve_bindings(
        manifest,
        discovered,
        device="uesim02",
    )
    require(len(binding) == 1, "single-device readiness probe did not stay single-device")
    require(binding[0]["device"] == "uesim02", "single-device probe selected wrong UE")
    require(binding[0]["dnn"] == "internet", "uesim02 readiness used positional DNN")
    require(binding[0]["address"] == "12.1.1.12", "uesim02 readiness used wrong slice CIDR")

    all_bindings = probe.resolve_bindings(manifest, discovered)
    require(
        [item["device"] for item in all_bindings]
        == [item["device"] for item in ue_map],
        "default final probe no longer validates the complete selected UE set",
    )


def check_yaml_task_files_parse() -> None:
    for path in (
        "deployment/roles/5g/oai/setup/tasks/rfsim_ue_contract.yml",
        "deployment/roles/5g/oai/setup/tasks/rfsim_ue_contract_one.yml",
        "deployment/roles/5g/oai/ran/tasks/main.yml",
        "deployment/roles/5g/oai/ran/tasks/rfsim_ue_start.yml",
        "deployment/roles/synthran/r2lab_ue_verify/tasks/main.yml",
    ):
        try:
            parsed = yaml.safe_load(text(path))
        except yaml.YAMLError as exc:
            raise CheckError(f"{path} is not valid YAML: {exc}") from exc
        require(isinstance(parsed, list), f"{path} must remain an Ansible task list")


def check_ansible_contract() -> None:
    setup_main = text("deployment/roles/5g/oai/setup/tasks/main.yml")
    all_vars = text("deployment/group_vars/all/all.yml")
    chart = text("deployment/roles/5g/oai/setup/tasks/rfsim_ue_contract.yml")
    chart_one = text("deployment/roles/5g/oai/setup/tasks/rfsim_ue_contract_one.yml")
    ran_main = text("deployment/roles/5g/oai/ran/tasks/main.yml")
    start = text("deployment/roles/5g/oai/ran/tasks/rfsim_ue_start.yml")
    final = text("deployment/playbooks/verify_live_testbed.yml")
    env = text("deployment/roles/5g/oai/config/templates/5g-env.sh.j2")
    inventory = text("synthran/inventory.py")
    topology = text("deployment/topology.yml")
    state = text("synthran/deployment_state.py")
    probe = text("deployment/scripts/probe_software_ues.py")
    r2lab = text("deployment/roles/synthran/r2lab_ue_verify/tasks/main.yml")

    require(
        "include_tasks: rfsim_ue_contract.yml" in setup_main,
        "OAI setup no longer binds rendered RFSIM charts to the selected contract",
    )
    require(
        "platform == 'rfsim'" in setup_main and "ran | lower == 'oai'" in setup_main,
        "RFSIM runtime/UE contract is no longer guarded from physical OAI",
    )
    for marker in (
        'synthran_oai_rfsim_runtime_release: "2026.w33"',
        'synthran_oai_rfsim_upstream_revision: "2b69bde6aeafe892cda1531a0f0cbba2e37792cd"',
        'synthran_oai_rfsim_prach_fix_revision: "568ed052348947dc5ece1d408887c50afe3c8e6a"',
        'synthran_oai_rfsim_gnb_repository: "docker.io/oaisoftwarealliance/oai-gnb"',
        'synthran_oai_rfsim_gnb_index_digest: "sha256:0dd2bd5507ccbbe08600a6df33a32f7861eaa4034c9d63750d5a3a8754686d16"',
        'synthran_oai_rfsim_ue_repository: "docker.io/oaisoftwarealliance/oai-nr-ue"',
        'synthran_oai_rfsim_ue_index_digest: "sha256:18b7a553088e98187de38e363ebe389ece45db6b54c0ce568c60f681762515c8"',
    ):
        require(marker in all_vars, f"reviewed RFSIM runtime pin lost marker: {marker}")

    require(
        'synthran_oai_rfsim_release: "2026.w33"' not in all_vars,
        "RFSIM runtime version must not reuse the per-UE Helm release variable",
    )

    for marker in (
        "synthran_ue_map | length <= 3",
        "roadmap #112",
        "authority': 'synthran_ue_map'",
        "oai-rfsim-ue-contract.json",
        ".nfimage.repository = strenv(SYNTHRAN_RFSIM_GNB_REPOSITORY)",
        ".nfimage.version = strenv(SYNTHRAN_RFSIM_GNB_VERSION)",
        "synthran_oai_rfsim_gnb_index_digest",
        "synthran_oai_rfsim_prach_fix_revision",
        "gnb_rendered",
    ):
        require(marker in chart, f"RFSIM chart authority/runtime contract lost marker: {marker}")

    for marker in (
        ".nfimage.repository = strenv(SYNTHRAN_RFSIM_UE_REPOSITORY)",
        ".nfimage.version = strenv(SYNTHRAN_RFSIM_UE_VERSION)",
        "synthran_oai_rfsim_ue_index_digest",
        ".config.fullImsi = strenv(SYNTHRAN_UE_IMSI)",
        ".config.dnn = strenv(SYNTHRAN_UE_DNN)",
        ".config.sst = strenv(SYNTHRAN_UE_SST)",
        ".config.sd = 16777215",
        ".config.sd = strenv(SYNTHRAN_UE_SD)",
        "synthran_oai_rfsim_rendered.fullImsi",
        "synthran_oai_rfsim_rendered.dnn",
        "synthran_oai_rfsim_rendered.sst",
        "synthran_oai_rfsim_rendered.sd",
    ):
        require(marker in chart_one, f"RFSIM chart readback contract lost marker: {marker}")

    require(
        "range(1, ue_count + 1)" not in ran_main,
        "OAI RFSIM launch detached from selected UE identities again",
    )
    require(
        'loop: "{{ synthran_ue_map }}"' in ran_main,
        "OAI RFSIM launch no longer iterates the sealed UE map",
    )
    for marker in (
        "helm status",
        "app.kubernetes.io/instance=",
        "synthran_oai_rfsim_pod_ready",
        "--device",
        "synthran_rfsim_ue.device",
        "synthran_oai_rfsim_probe.rc == 0",
        "synthran_oai_rfsim_ue_repository",
        "synthran_oai_rfsim_runtime_release",
        "synthran_oai_rfsim_ue_index_digest",
        "'runtime_release': synthran_oai_rfsim_runtime_release",
        "'index_digest': synthran_oai_rfsim_ue_index_digest",
        "'@sha256:'",
        "'image_id': synthran_oai_rfsim_status_container.imageID",
        "user_plane.verified",
        "oai-rfsim-{{ synthran_rfsim_ue.device }}-failure.log",
    ):
        require(marker in start, f"per-UE OAI RFSIM readiness lost marker: {marker}")

    for marker in (
        "oai-rfsim-runtime.json",
        "synthran_oai_rfsim_gnb_repository",
        "synthran_oai_rfsim_upstream_revision",
        "synthran_oai_rfsim_prach_fix_revision",
        "synthran_oai_rfsim_runtime_release",
        "synthran_oai_rfsim_gnb_index_digest",
        "synthran_oai_rfsim_ue_index_digest",
        "'@sha256:'",
    ):
        require(marker in ran_main, f"live RFSIM runtime attestation lost marker: {marker}")

    require(
        "synthran_oai_rfsim_" not in text("deployment/roles/5g/oai/setup/tasks/r2lab_n320.yml"),
        "RFSIM runtime override leaked into the physical N320 setup contract",
    )

    require(
        "probe_software_ues.py" in final and "--device" not in final,
        "final accepted-testbed probe is no longer an independent complete-set gate",
    )
    require(
        "UE_SLICE_MAP=(" in env,
        "OAI subscriber generation no longer receives selected UE/slice mapping",
    )
    for marker in (
        "resolve_user_plane_targets(c, profile, ue_map, topology)",
        "manifest = build_manifest(c, profile, ue_map, topology)",
    ):
        require(marker in inventory, f"inventory no longer seals topology-owned probe identity: {marker}")
    for marker in (
        "kind: oai-upf-tun0",
        "session_index: 0",
        "interface: tun0",
    ):
        require(marker in topology, f"OAI topology lost shared UPF probe marker: {marker}")
    for marker in (
        '"kind": "oai-upf-tun0"',
        '"interface": interface',
        '"kind": "session-network-gateway"',
        'entry["user_plane_target"] = target',
    ):
        require(marker in state, f"user-plane target contract lost marker: {marker}")
    for marker in (
        "_verify_oai_upf_probe",
        "app.kubernetes.io/name=oai-upf",
        "selected user-plane endpoint",
        '"target_kind": target_kind',
        '"target_interface": target_interface',
        '"target_runtime": target_runtime',
    ):
        require(marker in probe, f"software UE probe lost endpoint identity marker: {marker}")
    for marker in (
        "synthran_r2lab_user_plane_kind",
        "synthran_r2lab_user_plane_interface",
        "Attest the sealed OAI UPF endpoint",
        "'target_kind': synthran_r2lab_user_plane_kind",
        "'target_interface': synthran_r2lab_user_plane_interface",
    ):
        require(marker in r2lab, f"R2Lab evidence lost endpoint identity marker: {marker}")


def main() -> int:
    check_resolved_mapping()
    check_single_device_probe()
    check_yaml_task_files_parse()
    check_ansible_contract()
    print("OAI RFSIM selected-UE contract checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
