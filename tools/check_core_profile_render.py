#!/usr/bin/env python3
"""Render Sub 08 core adapters against selected profile fixtures."""

from __future__ import annotations

import argparse
import copy
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml
from jinja2 import Environment, StrictUndefined

ROOT = Path(__file__).resolve().parents[1]


class RenderError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RenderError(message)


def regex_replace(value: object, pattern: str, replacement: str) -> str:
    return re.sub(pattern, replacement, str(value))


def render(path: Path, **variables: object) -> str:
    environment = Environment(
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        autoescape=False,
    )
    environment.filters["regex_replace"] = regex_replace
    template = environment.from_string(path.read_text(encoding="utf-8"))
    return template.render(**variables)


def profile_fixture(repo: Path) -> dict:
    profile = yaml.safe_load(
        (repo / "deployment/group_vars/all/network_profile_default.yaml").read_text(
            encoding="utf-8"
        )
    )
    profile["ues"] = {
        "uesim01": {"imsi_suffix": "0000000001", "slice": "slice1"},
        "uesim02": {"imsi_suffix": "0000000002", "slice": "slice2"},
    }
    return profile


def topology_fixture() -> dict:
    return {
        "network": {"n3": {"route_cidr": "10.100.50.248/29"}},
        "transport": {
            "bridges": {"core": ["n3br", "n4br"], "ran": ["n3br", "n4br"]},
            "workload_interfaces": {
                "core": {"n2": "n3br", "n3": "n3br", "n4": "physical"},
                "ran": {"n2": "primary", "n3": "n3br"},
            },
            "chart_networks": {
                "n2": {"subnet_ip": "10.100.50.0", "cidr": 24},
                "n3": {"subnet_ip": "10.100.50.0", "cidr": 24},
            },
            "endpoints": {
                "amf_ngap_ip": "10.100.50.234",
                "branching_upf_n3_ip": "10.100.50.233",
                "branching_upf_n4_ip": "10.100.50.241",
                "anchor_upf1_n4_ip": "10.100.50.242",
                "anchor_upf2_n4_ip": "10.100.50.243",
            },
        },
    }


def expected_imsi(profile: dict, ue: dict) -> str:
    return f"{profile['plmn']['mcc']}{profile['plmn']['mnc']}{ue['imsi_suffix']}"


def expected_sd(value: object) -> str:
    text = str(value)
    return "ffffff" if text == "EMPTY" else text.lower().zfill(6)


def check_open5gs(repo: Path, profile: dict) -> None:
    template_root = repo / "deployment/roles/5g/open5gs/config/templates"
    topology = topology_fixture()

    amf = yaml.safe_load(
        render(
            template_root / "amf-configmap.yaml.j2",
            fiveg=profile,
            synthran_topology=topology,
        )
    )
    amf_cfg = amf["data"]["amfcfg.yaml"]
    require(profile["plmn"]["mcc"] in amf_cfg, "Open5GS AMF lost MCC")
    require(profile["plmn"]["mnc"] in amf_cfg, "Open5GS AMF lost MNC")
    require(profile["plmn"]["tac"] in amf_cfg, "Open5GS AMF lost TAC")
    require("10.100.50.234" in amf["data"]["wrapper.sh"], "Open5GS AMF lost selected NGAP endpoint")
    for entry in profile["slices"]:
        require(str(entry["sst"]) in amf_cfg, "Open5GS AMF lost selected SST")
        rendered_sd = "FFFFFF" if entry["sd"] == "EMPTY" else entry["sd"]
        require(rendered_sd in amf_cfg, "Open5GS AMF lost selected SD")

    for index, entry in enumerate(profile["slices"], start=1):
        smf = yaml.safe_load(
            render(
                template_root / "smf-configmap.yaml.j2",
                fiveg=profile,
                smf_name=f"smf{index}",
                smf_slice=entry["name"],
            )
        )
        smf_cfg = smf["data"]["smfcfg.yaml"]
        require(entry["dnn"] in smf_cfg, "Open5GS SMF lost selected DNN")
        require(entry["ip_prefix"] in smf_cfg, "Open5GS SMF lost selected UE pool")

        upf = yaml.safe_load(
            render(
                template_root / "upf-configmap.yaml.j2",
                upf_name=f"upf{index}",
                upf_slice_data=entry,
            )
        )
        upf_cfg = upf["data"]["upfcfg.yaml"]
        require(entry["dnn"] in upf_cfg, "Open5GS UPF lost selected DNN")
        require(entry["ip_prefix"] in upf_cfg, "Open5GS UPF lost selected UE pool")

    subscriber_script = render(
        template_root / "generate-data-fiveg.py.j2", profile=profile
    )
    compile(subscriber_script, "rendered-open5gs-subscribers.py", "exec")
    require("os.umask(0o077)" in subscriber_script, "Open5GS subscriber output is not private")
    require("os.chmod(SUBSCRIBER_FILE, 0o600)" in subscriber_script, "Open5GS subscriber file mode is not enforced")
    for name, ue in profile["ues"].items():
        require(name in subscriber_script, "Open5GS selected UE name was not rendered")
        require(ue["imsi_suffix"] in subscriber_script, "Open5GS selected IMSI suffix was not rendered")
    require("unselected-sentinel" not in subscriber_script, "Open5GS rendered an unselected UE")


def patch_free5gc_source(repo: Path, downstream: Path, staged: Path) -> None:
    shutil.copytree(
        downstream,
        staged,
        symlinks=True,
        ignore=shutil.ignore_patterns(".git"),
    )
    patch_root = repo / "deployment/roles/5g/free5gc/config/files"
    patchers = (
        "patch_amf_n2_nad.py",
        "patch_iupf_n3_nad.py",
        "patch_nads.py",
        "patch_upf_wrapper.py",
    )
    for _ in range(2):
        for patcher in patchers:
            subprocess.run(
                [sys.executable, str(patch_root / patcher), str(staged)],
                check=True,
                stdout=subprocess.DEVNULL,
            )


def check_ovs_nads(rendered: str) -> None:
    ovs_plugins: list[tuple[str, dict]] = []
    for document in yaml.safe_load_all(rendered):
        if not isinstance(document, dict) or document.get("kind") != "NetworkAttachmentDefinition":
            continue
        config = document.get("spec", {}).get("config")
        if not isinstance(config, str):
            continue
        payload = json.loads(config)
        for plugin in payload.get("plugins", [payload]):
            if isinstance(plugin, dict) and plugin.get("type") == "ovs":
                ovs_plugins.append((document.get("metadata", {}).get("name", ""), plugin))

    require(ovs_plugins, "rendered Free5GC chart contains no OVS NAD plugins")
    for name, plugin in ovs_plugins:
        require(plugin.get("bridge") == "n3br", f"Free5GC OVS NAD {name} lost topology bridge")
        require("master" not in plugin, f"Free5GC OVS NAD {name} incorrectly retained master")
        ipam = plugin.get("ipam")
        require(isinstance(ipam, dict) and ipam.get("type") == "static", f"Free5GC OVS NAD {name} lost static IPAM")
        require("routes" not in ipam, f"Free5GC OVS NAD {name} retained an empty-gateway default route")


def check_free5gc_amf_config(rendered: str, profile: dict) -> None:
    matches: list[dict] = []
    for document in yaml.safe_load_all(rendered):
        if not isinstance(document, dict) or document.get("kind") != "ConfigMap":
            continue
        name = str(document.get("metadata", {}).get("name", ""))
        data = document.get("data", {})
        if "amfcfg.yaml" in data and "amf" in name.lower():
            matches.append(document)

    require(len(matches) == 1, f"expected exactly one rendered Free5GC AMF ConfigMap, got {len(matches)}")
    payload = yaml.safe_load(matches[0]["data"]["amfcfg.yaml"])
    configuration = payload["configuration"]

    require("s_nssai" not in configuration, "Free5GC AMF retained an Open5GS s_nssai field")
    require("network_name" not in configuration, "Free5GC AMF retained an Open5GS network_name field")
    require("amf_name" not in configuration, "Free5GC AMF retained an Open5GS amf_name field")
    require("t3512" not in configuration, "Free5GC AMF retained an Open5GS-style t3512 mapping")

    support = configuration["plmnSupportList"]
    require(len(support) == 1, "Free5GC AMF must render one selected PLMN support entry")
    selected_plmn = support[0]["plmnId"]
    require(selected_plmn["mcc"] == profile["plmn"]["mcc"], "Free5GC AMF MCC lost its selected digit string")
    require(selected_plmn["mnc"] == profile["plmn"]["mnc"], "Free5GC AMF MNC lost its selected digit string")
    snssai = support[0]["snssaiList"]
    actual = {(int(item["sst"]), str(item["sd"]).lower().zfill(6)) for item in snssai}
    expected = {(int(item["sst"]), expected_sd(item["sd"])) for item in profile["slices"]}
    require(actual == expected, "Free5GC AMF snssaiList does not match the selected slices")

    expected_dnns = [item["dnn"] for item in profile["slices"]]
    require(configuration["supportDnnList"] == expected_dnns, "Free5GC AMF supportDnnList does not match selected DNNs")
    require(configuration["security"]["integrityOrder"] == ["NIA2", "NIA1", "NIA0"], "Free5GC AMF integrityOrder schema drifted")
    require(configuration["security"]["cipheringOrder"] == ["NEA0", "NEA1", "NEA2"], "Free5GC AMF cipheringOrder schema drifted")
    require(configuration["networkName"]["full"] == "free5GC", "Free5GC AMF networkName schema drifted")
    require(configuration["t3512Value"] == 540, "Free5GC AMF t3512Value was not preserved")


def check_free5gc(repo: Path, downstream: Path, profile: dict) -> None:
    template_root = repo / "deployment/roles/5g/free5gc/config/templates"
    topology = topology_fixture()
    values_text = render(
        template_root / "free5gc-values-override.yaml.j2",
        profile=profile,
        core_node_name="core-ci",
        ran_node_name="ran-ci",
        nic_interface="eth0",
        synthran_topology=topology,
    )
    values = yaml.safe_load(values_text)
    require(values["global"]["n2network"]["type"] == "ovs", "Free5GC N2 did not render OVS")
    require(values["global"]["n3network"]["type"] == "ovs", "Free5GC N3 did not render OVS")
    require(values["global"]["n2network"]["masterIf"] == "n3br", "Free5GC N2 lost topology interface")
    require(values["global"]["n3network"]["masterIf"] == "n3br", "Free5GC N3 lost topology interface")
    require(values["global"]["n2network"]["gatewayIP"] == "", "Free5GC N2 should not inject a default route")
    require(values["global"]["n3network"]["gatewayIP"] == "", "Free5GC N3 should not inject a default route")

    serialized = values_text
    for entry in profile["slices"]:
        require(entry["dnn"] in serialized, "Free5GC values lost selected DNN")
        require(entry["ip_prefix"] in serialized, "Free5GC values lost selected UE pool")
        require(expected_sd(entry["sd"]) in serialized.lower(), "Free5GC values lost selected SD")
    for ue in profile["ues"].values():
        require(expected_imsi(profile, ue) in serialized, "Free5GC values lost selected IMSI")
    require("unselected-sentinel" not in serialized, "Free5GC values rendered an unselected UE")

    subscriber_script = render(
        template_root / "add_subscribers.py.j2", profile=profile
    )
    compile(subscriber_script, "rendered-free5gc-subscribers.py", "exec")
    for name, ue in profile["ues"].items():
        require(name in subscriber_script, "Free5GC selected UE name was not rendered")
        require(expected_imsi(profile, ue) in subscriber_script, "Free5GC selected IMSI was not rendered")

    with tempfile.TemporaryDirectory(prefix="synthran-core-render-") as temp_dir:
        temp = Path(temp_dir)
        staged = temp / "free5gc-source"
        patch_free5gc_source(repo, downstream, staged)
        values_file = temp / "values.yaml"
        values_file.write_text(values_text, encoding="utf-8")
        values_file.chmod(0o600)
        result = subprocess.run(
            [
                "helm",
                "template",
                "free5gc",
                str(staged / "charts/free5gc"),
                "--namespace",
                "free5gc",
                "-f",
                str(values_file),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        check_ovs_nads(result.stdout)
        check_free5gc_amf_config(result.stdout, profile)


def check_colocated_free5gc(repo: Path, profile: dict) -> None:
    values_text = render(
        repo / "deployment/roles/5g/free5gc/config/templates/free5gc-values-override.yaml.j2",
        profile=profile,
        core_node_name="same-ci",
        ran_node_name="same-ci",
        nic_interface="eth0",
        synthran_topology=topology_fixture(),
    )
    values = yaml.safe_load(values_text)
    require(values["global"]["n2network"]["type"] == "ipvlan", "colocated Free5GC N2 type changed")
    require(values["global"]["n2network"]["masterIf"] == "eth0", "colocated Free5GC N2 master changed")
    require(values["global"]["n2network"]["gatewayIP"] == "", "colocated Free5GC N2 gateway mutation was not rendered")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--free5gc-root", type=Path, required=True)
    args = parser.parse_args()

    repo = args.repo_root.resolve()
    downstream = args.free5gc_root.resolve()
    profile = profile_fixture(repo)

    try:
        check_open5gs(repo, copy.deepcopy(profile))
        check_free5gc(repo, downstream, copy.deepcopy(profile))
        check_colocated_free5gc(repo, copy.deepcopy(profile))
    except (RenderError, KeyError, TypeError, ValueError, subprocess.CalledProcessError, yaml.YAMLError, json.JSONDecodeError) as exc:
        print(f"core profile render contract FAILED: {exc}", file=sys.stderr)
        return 1

    print("core profile render contract OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
