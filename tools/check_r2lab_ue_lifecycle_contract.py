#!/usr/bin/env python3
"""Validate the Sub 09 R2Lab UE lifecycle against the pinned original upstream."""
from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import subprocess
from pathlib import Path

from synthran.deployment_state import binding_identity, bindings_match_deployment, build_ue_map

ROOT = Path(__file__).resolve().parents[1]
EXECUTION_REFERENCE = ROOT / "third_party/sopnode-5g-ansible/EXECUTION_REFERENCE.json"
PROVISION = ROOT / "deployment/playbooks/provision_r2lab.yml"
CONNECT_PLAYBOOK = ROOT / "deployment/playbooks/connect_ues.yml"
SETUP_MAIN = ROOT / "deployment/roles/r2lab/ue/setup/tasks/main.yml"
SETUP_ONE = ROOT / "deployment/roles/r2lab/ue/setup/tasks/prepare_one.yml"
CONNECT_MAIN = ROOT / "deployment/roles/r2lab/ue/connect/tasks/main.yml"
CONNECT_QMI = ROOT / "deployment/roles/r2lab/ue/connect/tasks/qmi.yml"
VERIFY_MAIN = ROOT / "deployment/roles/synthran/r2lab_ue_verify/tasks/main.yml"
PROBE = ROOT / "deployment/scripts/probe_r2lab_ue.py"
PROFILE_VALIDATION = ROOT / "synthran/profile_validation.py"
DEPLOYMENT_STATE = ROOT / "synthran/deployment_state.py"


def fail(message: str) -> None:
    raise SystemExit(message)


def require(condition: bool, message: str) -> None:
    if not condition:
        fail(message)


def text(path: Path) -> str:
    require(path.is_file(), f"missing required file: {path}")
    return path.read_text(encoding="utf-8")


def git_head(path: Path) -> str:
    return subprocess.check_output(
        ["git", "-C", str(path), "rev-parse", "HEAD"], text=True
    ).strip()


def load_probe_module():
    spec = importlib.util.spec_from_file_location("synthran_r2lab_probe_contract", PROBE)
    require(spec is not None and spec.loader is not None, "cannot load R2Lab probe module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_reference(reference: Path) -> None:
    contract = json.loads(EXECUTION_REFERENCE.read_text(encoding="utf-8"))
    require(
        contract.get("repository") == "https://github.com/sopnode/5g_ansible",
        "Sub 09 lifecycle authority is not original sopnode/5g_ansible",
    )
    expected = contract.get("commit")
    require(git_head(reference) == expected, "5g-Ansible checkout does not match the execution pin")

    required_paths = set(contract.get("required_paths", []))
    upstream_paths = {
        "playbooks/test-ue-connect.yml",
        "roles/r2lab/ue/setup/tasks/main.yml",
        "roles/r2lab/ue/connect/tasks/main.yml",
        "roles/r2lab/ue/stop/tasks/main.yml",
    }
    require(
        upstream_paths <= required_paths,
        "execution reference no longer requires the R2Lab UE comparison surfaces",
    )

    setup = text(reference / "roles/r2lab/ue/setup/tasks/main.yml")
    for needle in (
        "Power OFF QHAT UEs",
        "Power OFF QFIT UEs",
        "Power ON QHAT UEs",
        "Power ON QFIT UEs",
        "SSH check",
        "qhat-init",
        "Stop UEs after init",
        "qhat-check",
    ):
        require(needle in setup, f"original-upstream setup surface changed: {needle}")
    require(
        "failed_ues: \"{{ all_ues | difference(reachable_ues) }}\"" in setup,
        "original-upstream ping-derived failed_ues shape changed; re-audit local SSH hardening",
    )
    require(
        "'qhat-init'" in setup,
        "original-upstream setup no longer invokes bare qhat-init; re-audit PREPARE ownership",
    )

    connect = text(reference / "roles/r2lab/ue/connect/tasks/main.yml")
    for needle in (
        "stop.sh; start.sh -F {{ current_dnn }}",
        "quectel-CM",
        "QMI: detect AT port",
        "ci_ctl_qtel.py",
        "Retrieve wwan0 IP",
        "ip route replace {{ upf_ip }} dev wwan0",
    ):
        require(needle in connect, f"original-upstream attach surface changed: {needle}")

    playbook = text(reference / "playbooks/test-ue-connect.yml")
    require("r2lab/ue/connect" in playbook, "upstream test-ue-connect lost the UE connect role")
    require("ignore_errors: true" in playbook, "upstream UE playbook is no longer fail-open; re-audit adapter")
    require("groups['qhats'] + groups['qfits'] + groups['phones']" in playbook, "upstream UE selection shape changed")


def check_local_structure() -> None:
    provision = text(PROVISION)
    for forbidden in (
        "network_profile_file",
        "qhat-init",
        "qhat-check",
        "ci_ctl_qtel.py",
        "stop.sh",
    ):
        require(forbidden not in provision, f"provision_r2lab.yml reintroduced UE mutation: {forbidden}")
    require("r2lab/ue/setup" in provision, "provisioning no longer delegates PREPARE to r2lab/ue/setup")
    require("any_errors_fatal: true" in provision, "R2Lab PREPARE orchestration must fail closed")

    setup = text(SETUP_MAIN)
    for needle in (
        "synthran_ue_map",
        "selected_qhats",
        "selected_qfits",
        "Wait for selected QHAT power-off completion",
        "qhat_off_status.finished",
        "rhubarbe_status_contract=0:ON,1:OFF,255:failure",
        "expected_rc=1",
        "Require every selected QHAT to reach the OFF terminal state",
        "Wait for every selected UE SSH endpoint",
        "timeout: 120",
        "include_tasks: prepare_one.yml",
    ):
        require(needle in setup, f"selected UE PREPARE contract lost: {needle}")
    require(
        setup.index("Wait for selected QHAT power-off completion")
        < setup.index("Allow selected UE power rails to settle after confirmed power-off")
        < setup.index("Power ON selected QHAT UEs"),
        "selected QHAT lifecycle can power ON before the asynchronous OFF transition completes",
    )
    require(
        "(item.rc | default(255) | int) == 1" in setup,
        "selected QHAT OFF completion no longer enforces Rhubarbe rc=1 terminal state",
    )
    require("ignore_errors: true" not in setup, "selected UE power/readiness path became fail-open")
    require("failed_ues" not in setup, "ping-only failed_ues authority was reintroduced")

    prepare = text(SETUP_ONE)
    for needle in (
        "synthran_r2lab_ue.tunnel.host",
        "synthran_r2lab_ue.tunnel.interface == 'wwan0'",
        "in ['EMPTY', 'FFFFFF']",
        "(synthran_r2lab_ue.sst | int) == 1",
        "qhat-init --mode={{ synthran_prepare_mode }}",
        "--dnn={{ synthran_prepare_dnn }} --nssai={{ synthran_prepare_nssai }}",
        "stop.sh",
        "ci_ctl_qtel.py",
        "qhat-check",
        "r2lab-ue-{{ synthran_prepare_device }}-prepare.log",
        "Require selected UE preparation to complete",
    ):
        require(needle in prepare, f"per-UE PREPARE contract lost: {needle}")
    require("retries:" not in prepare, "PREPARE added a helper retry/recovery loop")

    connect_playbook = text(CONNECT_PLAYBOOK)
    require("loop: \"{{ synthran_ue_map }}\"" in connect_playbook, "ATTACH no longer iterates exact selected contract entries")
    require("synthran_r2lab_ue: \"{{ item }}\"" in connect_playbook, "ATTACH no longer passes the selected identity directly")
    require("ignore_task_errors" not in connect_playbook, "ATTACH reintroduced fail-open compatibility mode")
    require("phone_ssh" not in connect_playbook, "dead phone compatibility path was reintroduced")

    attach = text(CONNECT_MAIN)
    for forbidden in (
        "network_profile_file",
        "fiveg.",
        "groups['phones']",
        "is_phone",
        "ignore_errors",
        "ignore_task_errors",
    ):
        require(forbidden not in attach, f"ATTACH reintroduced duplicate/dead authority: {forbidden}")
    for needle in (
        "synthran_r2lab_ue.tunnel.host",
        "current_dnn: \"{{ synthran_r2lab_ue.dnn }}\"",
        "current_slice_sd in ['EMPTY', 'FFFFFF']",
        "mbim_access_string",
        "_EMBB",
        "stop.sh; start.sh -F {{ mbim_access_string }}",
        "Validate or attach the selected QMI DNN with the upstream-style procedure",
        "when: ue_mode == 'qmi'",
        "r2lab-ue-{{ ue_item }}-attach.log",
        "ip route replace {{ upf_ip }} dev {{ ue_interface }}",
    ):
        require(needle in attach, f"selected ATTACH contract lost: {needle}")
    require(
        "retries: \"{{ 60 if ue_mode == 'qmi' else 5 }}\"" in attach,
        "existing MBIM/QMI address-acquisition windows changed; re-audit before treating retries as harmless",
    )

    qmi = text(CONNECT_QMI)
    for needle in (
        "pgrep -a -x quectel-CM",
        "synthran_qmi_manager_acceptable",
        "regex_escape",
        "current_dnn",
        "preexisting_user_plane={{ wwan0_up | bool }}",
        "not wwan0_up",
        "QMI AT control port",
        "ci_ctl_qtel.py",
        "Require the selected QMI attachment procedure to complete",
    ):
        require(needle in qmi, f"QMI fail-closed contract lost: {needle}")
    require("retries:" not in qmi, "QMI attachment added a retry/recovery loop")
    require(
        "not wwan0_up or synthran_qmi_existing.rc == 0" in qmi,
        "live QMI reuse no longer requires an existing selected-DNN manager",
    )

    verifier = text(VERIFY_MAIN)
    for forbidden in (
        "qhat-init",
        "start.sh",
        "stop.sh",
        "ci_ctl_qtel.py",
        "ip route replace",
        "quectel-CM -s",
    ):
        require(forbidden not in verifier, f"read-only UE verifier contains mutation: {forbidden}")
    for needle in (
        "probe_r2lab_ue.py",
        "ping",
        "-I",
        "r2lab-ue-{{ inventory_hostname }}-n6.log",
        "'user_plane':",
        "'verified': true",
        "'source_interface': synthran_r2lab_interface",
        "'source_address': synthran_r2lab_binding.address",
        "'target_address': synthran_r2lab_user_plane_address",
        "'target_kind': synthran_r2lab_user_plane_kind",
        "'target_interface': synthran_r2lab_user_plane_interface",
    ):
        require(needle in verifier, f"read-only UE verifier lost retained source-bound N6 proof: {needle}")

    probe = text(PROBE)
    for needle in (
        '"sst": str(contract["sst"])',
        '"sd": str(contract["sd"])',
        'contract_sd not in {"EMPTY", "FFFFFF"}',
        '"modem_verified": True',
    ):
        require(needle in probe, f"live UE evidence lost identity field or SD normalization: {needle}")

    state = text(DEPLOYMENT_STATE)
    for needle in (
        'str(item.get("sst"))',
        'str(item.get("sd"))',
        'prefix_lengths = {"oai": 24, "free5gc": 24, "open5gs": 16}',
        'user_plane.get("verified") is not True',
        'user_plane.get("source_interface") != _transport_value(contract, "interface")',
        'user_plane.get("source_address") != address',
        'user_plane.get("target_address") != expected_target',
    ):
        require(needle in state, f"deployment/live-binding identity contract lost: {needle}")

    validation = text(PROFILE_VALIDATION)
    for needle in (
        'name.startswith(("qhat", "qfit"))',
        'mode not in {"mbim", "qmi"}',
        'interface != "wwan0"',
        'session != 0',
    ):
        require(needle in validation, f"R2Lab UE catalog boundary changed: {needle}")


def fixture_contract(*, device: str, index: int, imsi: str, slice_name: str, sst: str, sd: str, dnn: str, cidr: str, mode: str) -> dict:
    return {
        "device": device,
        "index": index,
        "imsi": imsi,
        "slice": slice_name,
        "sst": sst,
        "sd": sd,
        "dnn": dnn,
        "address_cidr": cidr,
        "tunnel": {
            "host": device,
            "interface": "wwan0",
            "mode": mode,
            "mbim_session": 0 if mode == "mbim" else None,
        },
    }


def link(address: str) -> list[dict]:
    return [
        {
            "ifname": "wwan0",
            "flags": ["BROADCAST", "MULTICAST", "UP", "LOWER_UP"],
            "addr_info": [{"family": "inet", "scope": "global", "local": address}],
        }
    ]


def check_identity_fixtures() -> None:
    scenario = {
        "deployment": {
            "platform": "r2lab",
            "ran": "srsran",
            "core": "oai",
            "ues": ["qhat01", "qhat03", "qhat20"],
        }
    }
    profile = {
        "plmn": {"mcc": "001", "mnc": "01"},
        "slices": [
            {"name": "slice1", "sst": "1", "sd": "EMPTY", "dnn": "internet", "ip_prefix": "12.1.1"},
            {"name": "slice2", "sst": "1", "sd": "100000", "dnn": "streaming", "ip_prefix": "14.1.1"},
        ],
        "ues": {
            "qhat01": {"imsi_suffix": "0000000006", "slice": "slice1", "mode": "mbim", "interface": "wwan0", "mbim_session": 0},
            "qhat03": {"imsi_suffix": "0000000008", "slice": "slice2", "mode": "mbim", "interface": "wwan0", "mbim_session": 0},
            "qhat20": {"imsi_suffix": "0000000009", "slice": "slice1", "mode": "qmi", "interface": "wwan0"},
        },
    }
    mapping = build_ue_map(scenario, profile)
    require([item["device"] for item in mapping] == ["qhat01", "qhat03", "qhat20"], "selected UE order changed")
    require(mapping[0]["sd"] == "EMPTY" and mapping[0]["tunnel"]["mbim_session"] == 0, "empty-SD MBIM identity changed")
    require(mapping[1]["sd"] == "100000" and mapping[1]["dnn"] == "streaming", "non-empty-SD MBIM identity changed")
    require(mapping[2]["tunnel"]["mode"] == "qmi" and mapping[2]["tunnel"]["mbim_session"] is None, "QMI identity changed")
    require(mapping[0]["address_cidr"] == "12.1.1.0/24", "OAI UE pool must preserve all three configured prefix octets")
    require(mapping[1]["address_cidr"] == "14.1.1.0/24", "OAI slice2 UE pool width changed")

    open5gs_scenario = copy.deepcopy(scenario)
    open5gs_scenario["deployment"]["core"] = "open5gs"
    open5gs_mapping = build_ue_map(open5gs_scenario, profile)
    require(open5gs_mapping[0]["address_cidr"] == "12.1.1.0/16", "Open5GS retained /16 session pool changed")

    free5gc_scenario = copy.deepcopy(scenario)
    free5gc_scenario["deployment"]["core"] = "free5gc"
    free5gc_mapping = build_ue_map(free5gc_scenario, profile)
    require(free5gc_mapping[0]["address_cidr"] == "12.1.1.0/24", "Free5GC retained /24 UE pool changed")

    altered = copy.deepcopy(mapping[1])
    altered["sd"] = "200000"
    require(binding_identity(mapping[1]) != binding_identity(altered), "S-NSSAI drift does not change binding identity")

    probe = load_probe_module()
    empty = fixture_contract(
        device="qhat01", index=1, imsi="001010000000006", slice_name="slice1",
        sst="1", sd="EMPTY", dnn="internet", cidr="12.1.1.0/24", mode="mbim",
    )
    empty_binding = probe.verify_observations(
        empty,
        link("12.1.1.11"),
        '+CGDCONT: 1,"IP","internet","0.0.0.0",0,0\n',
        "Subscriber ID: '001010000000006'\n",
        "Session ID: '0'\nActivation state: 'activated'\n",
        "IP [0]: '12.1.1.11/24'\n",
    )
    require(empty_binding["sst"] == "1" and empty_binding["sd"] == "EMPTY", "empty-SD proof omitted S-NSSAI")

    ffffff = copy.deepcopy(empty)
    ffffff["sd"] = "FFFFFF"
    ffffff_binding = probe.verify_observations(
        ffffff,
        link("12.1.1.11"),
        '+CGDCONT: 1,"IP","internet","0.0.0.0",0,0\n',
        "Subscriber ID: '001010000000006'\n",
        "Session ID: '0'\nActivation state: 'activated'\n",
        "IP [0]: '12.1.1.11/24'\n",
    )
    require(
        ffffff_binding["sd"] == "FFFFFF",
        "FFFFFF default-SD sentinel was incorrectly treated as a real slice SD",
    )

    sliced = fixture_contract(
        device="qhat03", index=2, imsi="001010000000008", slice_name="slice2",
        sst="1", sd="100000", dnn="streaming", cidr="14.1.1.0/24", mode="mbim",
    )
    sliced_modem = (
        '+CGDCONT: 1,"IP","streaming","0.0.0.0",0,0\n'
        '+CGDCONT: 4,"IP","streaming_EMBB100000","0.0.0.0",0,0,0,0,,,,,,,,,1,"01.100000",,,,0\n'
    )
    sliced_binding = probe.verify_observations(
        sliced,
        link("14.1.1.12"),
        sliced_modem,
        "Subscriber ID: '001010000000008'\n",
        "Session ID: '0'\nActivation state: 'activated'\n",
        "IP [0]: '14.1.1.12/24'\n",
    )
    require(sliced_binding["sst"] == "1" and sliced_binding["sd"] == "100000", "slice-qualified proof omitted S-NSSAI")

    wrong_slice = copy.deepcopy(sliced)
    wrong_slice["sd"] = "200000"
    try:
        probe.verify_observations(
            wrong_slice,
            link("14.1.1.12"),
            sliced_modem,
            "Subscriber ID: '001010000000008'\n",
            "Session ID: '0'\nActivation state: 'activated'\n",
            "IP [0]: '14.1.1.12/24'\n",
        )
    except ValueError:
        pass
    else:
        fail("R2Lab probe accepted a different SD from the observed modem context")

    qmi = fixture_contract(
        device="qhat20", index=3, imsi="001010000000009", slice_name="slice1",
        sst="1", sd="EMPTY", dnn="internet", cidr="12.1.1.0/24", mode="qmi",
    )
    qmi_binding = probe.verify_observations(
        qmi,
        link("12.1.1.13"),
        'IMSI: 001010000000009\n+QCFG: "usbnet",0\n+CGDCONT: 1,"IP","internet","0.0.0.0",0,0\n',
        manager="123 /usr/local/bin/quectel-CM -s internet -4\n",
    )
    require(qmi_binding["mode"] == "qmi" and qmi_binding["mbim_session"] is None, "QMI proof changed session semantics")
    require(qmi_binding["sst"] == "1" and qmi_binding["sd"] == "EMPTY", "QMI proof omitted S-NSSAI")

    proved = copy.deepcopy(empty_binding)
    proved["user_plane"] = {
        "verified": True,
        "method": "icmp_echo",
        "source_interface": "wwan0",
        "source_address": "12.1.1.11",
        "target_address": "12.1.1.1",
        "observed_at": "2026-09-17T12:00:00Z",
    }
    require(
        bindings_match_deployment({"platform": "r2lab", "ues": [empty]}, [proved]),
        "matching R2Lab binding plus source-bound N6 proof was rejected",
    )
    missing_n6 = copy.deepcopy(proved)
    missing_n6.pop("user_plane")
    require(
        not bindings_match_deployment({"platform": "r2lab", "ues": [empty]}, [missing_n6]),
        "R2Lab binding without retained N6 proof was accepted",
    )
    wrong_source = copy.deepcopy(proved)
    wrong_source["user_plane"]["source_interface"] = "eth0"
    require(
        not bindings_match_deployment({"platform": "r2lab", "ues": [empty]}, [wrong_source]),
        "R2Lab binding accepted a user-plane proof from the management interface",
    )
    wrong_target = copy.deepcopy(proved)
    wrong_target["user_plane"]["target_address"] = "12.1.1.2"
    require(
        not bindings_match_deployment({"platform": "r2lab", "ues": [empty]}, [wrong_target]),
        "R2Lab binding accepted a user-plane proof to a non-selected UPF target",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", required=True, type=Path)
    args = parser.parse_args()
    check_reference(args.reference)
    check_local_structure()
    check_identity_fixtures()
    print("R2Lab UE lifecycle contract OK")


if __name__ == "__main__":
    main()
