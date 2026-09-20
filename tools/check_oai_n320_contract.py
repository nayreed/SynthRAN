#!/usr/bin/env python3
"""Static contract for issue #55: OAI R2Lab N320 adaptation ownership."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]

DEFAULTS = ROOT / "deployment/roles/5g/oai/common/defaults/main.yml"
SETUP = ROOT / "deployment/roles/5g/oai/setup/tasks/main.yml"
N320_SOURCE = ROOT / "deployment/roles/5g/oai/setup/tasks/r2lab_n320.yml"
RAN = ROOT / "deployment/roles/5g/oai/ran/tasks/main.yml"
N320_CHART = ROOT / "deployment/roles/5g/oai/ran/tasks/r2lab_n320_chart.yml"
N320_READINESS = ROOT / "deployment/roles/5g/oai/ran/tasks/r2lab_n320_readiness.yml"
N320_ATTEST = ROOT / "deployment/roles/5g/oai/ran/tasks/r2lab_n320_attest.yml"
N320_NAD = ROOT / "deployment/roles/5g/oai/ran/files/r2lab_n320_ipvlan_nad.yaml"
LEGACY_SWAP = ROOT / "deployment/roles/5g/oai/ran/n3xx_ip_swap"


def fail(message: str) -> None:
    raise SystemExit(message)


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def require(haystack: str, needle: str, context: str) -> None:
    if needle not in haystack:
        fail(f"{context}: missing required contract text: {needle!r}")


def forbid(haystack: str, needle: str, context: str) -> None:
    if needle in haystack:
        fail(f"{context}: forbidden contract text remains: {needle!r}")


def helper_pin(defaults: str) -> str:
    match = re.search(r'^tag_oai5g_rru:\s*["\']([0-9a-fA-F]{40})["\']\s*$', defaults, re.M)
    if not match:
        fail("OAI defaults: immutable 40-hex tag_oai5g_rru pin is missing")
    return match.group(1).lower()


def git_head(path: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(path), "rev-parse", "HEAD"], text=True
        ).strip().lower()
    except (OSError, subprocess.CalledProcessError) as error:
        fail(f"cannot read helper Git revision at {path}: {error}")


def shell_function(script: str, name: str) -> str:
    match = re.search(
        rf"(?ms)^{re.escape(name)}\(\)\s*\{{\s*$(.*?)^\}}\s*$",
        script,
    )
    if not match:
        fail(f"pinned helper: cannot locate shell function {name}()")
    return match.group(1)


def validate_helper(helper: Path, pin: str) -> None:
    actual_head = git_head(helper)
    if actual_head != pin:
        fail(f"pinned helper drift: expected {pin}, found {actual_head}")

    n320_env = text(helper / "rru/n320.env")
    require(n320_env, 'IP_GNB_RU="dhcp"', "pinned helper n320.env")
    require(n320_env, 'MTU_GNB_RU="9000"', "pinned helper n320.env")
    require(
        n320_env,
        "mgmt_addr=192.168.235.106,addr=192.168.235.106",
        "pinned helper n320.env",
    )

    gnb_ifs = text(helper / "demo_charts/values/nf-ifs/oai-gnb.yaml")
    du_ifs = text(helper / "demo_charts/values/nf-ifs/oai-du.yaml")
    require(gnb_ifs, '- name: "ru"', "pinned helper gNB interface fragment")
    require(gnb_ifs, "type: macvlan", "pinned helper gNB RU interface")
    require(du_ifs, '- name: "ru"', "pinned helper DU interface fragment")
    require(du_ifs, "type: macvlan", "pinned helper DU RU interface")

    # The helper also carries replacement NAD templates, but they do not add an
    # ipvlan case. The exact staged GitLab chart is still validated at runtime
    # by SynthRAN with `helm template`; this check only proves the helper itself
    # has no native ipvlan renderer that makes the local adapter redundant.
    for relative in (
        "demo_charts/templates/oai-gnb/nad.yaml",
        "demo_charts/templates/oai-du/nad.yaml",
    ):
        nad = text(helper / relative)
        require(nad, 'eq .type "macvlan"', f"pinned helper {relative}")
        require(nad, 'eq .type "vlan"', f"pinned helper {relative}")
        forbid(nad, 'eq .type "ipvlan"', f"pinned helper {relative}")

    prepare = text(helper / "testing/prepare-demo-oai.sh")
    configure_branch = re.search(
        r"elif \[\[ \"\$action\" = 'configure' \]\]; then\s+configure_all_scripts",
        prepare,
    )
    if configure_branch is None:
        fail("pinned helper prepare-demo-oai.sh: configure-only branch changed")

    demo = text(helper / "demo-oai.sh")
    require(demo, 'export RAN_TAG="2026.w26"', "pinned helper OAI image tag")
    launch = shell_function(demo, "start-gnb")
    require(launch, "helm -n $NS install oai-gnb", "pinned helper start-gnb")
    require(launch, "helm -n $NS install oai-cu", "pinned helper start-gnb")
    require(launch, "helm -n $NS install oai-cu-cp", "pinned helper start-gnb")
    require(launch, "helm -n $NS install oai-cu-up", "pinned helper start-gnb")
    require(launch, "helm install -n $NS oai-du", "pinned helper start-gnb")
    require(launch, "kubectl -n $NS wait pod", "pinned helper start-gnb")
    forbid(launch, "set -e", "pinned helper start-gnb")


def validate_local_contract(pin: str) -> None:
    defaults = text(DEFAULTS)
    setup = text(SETUP)
    source = text(N320_SOURCE)
    ran = text(RAN)
    chart = text(N320_CHART)
    readiness = text(N320_READINESS)
    attest = text(N320_ATTEST)
    nad = text(N320_NAD)

    expected_defaults = {
        'oai_r2lab_n320_ru_parent: "r2lab_usrp"',
        'oai_r2lab_n320_ru_ip: "192.168.235.120"',
        'oai_r2lab_n320_ru_prefix: "24"',
        'oai_r2lab_n320_ru_mtu: "9216"',
        'oai_r2lab_n320_sfp0_ip: "192.168.235.105"',
        'oai_r2lab_n320_sfp1_ip: "192.168.235.106"',
        'oai_r2lab_n320_sdr_ip: "{{ oai_r2lab_n320_sfp0_ip }}"',
        'oai_r2lab_n320_ssh_user: "root"',
        'oai_r2lab_n320_ru_cni_type: "ipvlan"',
        'oai_r2lab_n320_ru_cni_mode: "l2"',
    }
    for needle in expected_defaults:
        require(defaults, needle, "OAI defaults")
    if helper_pin(defaults) != pin:
        fail("OAI defaults helper pin changed during validation")

    require(setup, "ansible.builtin.include_tasks: r2lab_n320.yml", "OAI setup")
    include_index = setup.index("ansible.builtin.include_tasks: r2lab_n320.yml")
    gate_window = setup[include_index : include_index + 300]
    for gate in (
        "platform == 'r2lab'",
        "ran == 'oai'",
        "rru == 'n320'",
    ):
        require(gate_window, gate, "OAI setup N320 gate")

    for needle in (
        "IF_NAME_GNB_RU=",
        "IP_GNB_RU=",
        "MTU_GNB_RU=",
        "mgmt_addr={{ oai_r2lab_n320_sdr_ip }},addr={{ oai_r2lab_n320_sdr_ip }}",
        "oai-du-cucpup.yaml",
        "oai-du.yaml",
        "oai-gnb.yaml",
    ):
        require(source, needle, "N320 source adapter")
    forbid(source, "n300", "N320 source adapter")

    require(chart, "r2lab_n320_ipvlan_nad.yaml", "N320 chart adapter")
    require(chart, "helm template", "N320 chart adapter")
    require(chart, "test \"$count\" -eq 1", "N320 chart adapter")
    forbid(chart, "kubectl apply", "N320 chart adapter")
    require(nad, 'eq .type "ipvlan"', "N320 Helm NAD")
    forbid(nad, "macvlan", "N320 Helm NAD")
    forbid(nad, "vlanId", "N320 Helm NAD")

    for needle in (
        "oai_expected_ran_releases",
        "Prove the R2Lab N320 Helm lifecycle starts clean",
        "Reject explicit Helm installation failures",
        "'INSTALLATION FAILED'",
        "platform == 'r2lab' and",
        "rru == 'n320' and",
        "oai_start_wait_timeout | bool",
        "r2lab_n320_readiness.yml",
        "Verify N3xx gNB RF-device readiness",
        "r2lab_n320_attest.yml",
    ):
        require(ran, needle, "OAI RAN lifecycle")
    require(
        ran,
        "A nonzero result is tolerated only for the\n      evidence-backed R2Lab N320 readiness timeout",
        "OAI RAN lifecycle",
    )
    readiness_index = ran.index("ansible.builtin.include_tasks: r2lab_n320_readiness.yml")
    readiness_window = ran[readiness_index : readiness_index + 220]
    for gate in (
        "platform == 'r2lab'",
        "rru == 'n320'",
    ):
        require(readiness_window, gate, "OAI RAN N320 readiness gate")

    for needle in (
        "BatchMode=yes",
        "UserKnownHostsFile=${N320_KNOWN_HOSTS}",
        "StrictHostKeyChecking=accept-new",
        "net.ipv4.conf.${scope}.arp_ignore=1",
        "net.ipv4.conf.${scope}.arp_announce=2",
        'test "$remote_sfp0_ip" = "$N320_SFP0_IP"',
        'test "$remote_sfp1_ip" = "$N320_SFP1_IP"',
        'test "$sfp0_neighbor" = "$remote_sfp0_mac"',
        'test "$sfp1_neighbor" = "$remote_sfp1_mac"',
        "ip neigh del",
        "oai-n320-network-readiness.json",
    ):
        require(readiness, needle, "N320 pre-launch readiness")
    forbid(readiness, "StrictHostKeyChecking=no", "N320 pre-launch readiness")
    forbid(readiness, "UserKnownHostsFile=/dev/null", "N320 pre-launch readiness")
    forbid(readiness, "rhubarbe-pdu", "N320 pre-launch readiness")
    forbid(readiness, "/etc/sysctl", "N320 pre-launch readiness")
    forbid(readiness, "/data/network", "N320 pre-launch readiness")
    forbid(readiness, "systemctl restart systemd-networkd", "N320 pre-launch readiness")

    for needle in (
        "time.sleep(10)",
        "pod identity changed during stability window",
        "restart counters changed during stability window",
        "N320 radio container restarted during startup",
        "RU [0-9]+ rf device ready",
        "Received NGSetupResponse from AMF",
        "sha256:",
        "helper_revision': tag_oai5g_rru",
        "charts_revision': synthran_oai_charts_revision",
        "selected_transport': synthran_topology.transport",
        "selected_network': synthran_topology.network",
        "prelaunch_network': oai_n320_network_readiness.stdout | from_json",
    ):
        require(attest, needle, "N320 runtime attestation")


def validate_legacy_absence() -> None:
    if LEGACY_SWAP.exists():
        fail("legacy generic n3xx_ip_swap role must not exist on the #55 branch")

    role_root = ROOT / "deployment/roles/5g/oai"
    for path in role_root.rglob("*"):
        if not path.is_file():
            continue
        body = path.read_text(encoding="utf-8", errors="replace")
        forbid(body, "n3xx_ip_swap", f"legacy scan {path.relative_to(ROOT)}")
        forbid(body, "deploy_nr_ue", f"legacy scan {path.relative_to(ROOT)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--helper",
        type=Path,
        required=True,
        help="Exact checkout of the pinned sopnode/oai5g-rru helper",
    )
    parser.add_argument(
        "--section",
        choices=("all", "helper", "local", "legacy"),
        default="all",
        help="Contract section to validate",
    )
    args = parser.parse_args()

    pin = helper_pin(text(DEFAULTS))
    helper = args.helper.resolve()

    if args.section in ("all", "helper"):
        validate_helper(helper, pin)
        print(f"OAI N320 helper prerequisites OK: helper={pin}")
    if args.section in ("all", "local"):
        validate_local_contract(pin)
        print("OAI N320 local adaptation/acceptance contract OK")
    if args.section in ("all", "legacy"):
        validate_legacy_absence()
        print("OAI N320 legacy-path absence contract OK")


if __name__ == "__main__":
    main()
