#!/usr/bin/env python3
"""Validate Sub 07 runtime-image, log, N2, and RFSIM ownership invariants."""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG_MAIN = ROOT / "deployment/roles/5g/srsRAN/config/tasks/main.yml"
DEPLOY_MAIN = ROOT / "deployment/roles/5g/srsRAN/deploy/tasks/main.yml"
PREPARE_RFSIM_CHART = ROOT / "deployment/roles/5g/srsRAN/config/tasks/prepare_rfsim_chart.yml"
LEGACY_DEPLOY_PATCH = ROOT / "deployment/roles/5g/srsRAN/deploy/tasks/patch_charts.yml"
RFSIM_VERIFY = ROOT / "deployment/roles/5g/srsRAN/deploy/tasks/verify_rfsim_ue_runtime.yml"
RFSIM_HARDEN = ROOT / "deployment/roles/5g/srsRAN/deploy/tasks/harden_rfsim_ue_runtime.yml"
RFSIM_CONFIGMAP = ROOT / "deployment/roles/5g/srsRAN/config/templates/srsue_configmap.yaml.j2"
LEGACY_DEPLOY_CONFIGMAP = ROOT / "deployment/roles/5g/srsRAN/deploy/templates/srsue_configmap.yaml.j2"
START_BROKER = ROOT / "deployment/roles/5g/srsRAN/deploy/tasks/start_broker.yml"
VERIFY_LOGGING = ROOT / "deployment/roles/5g/srsRAN/deploy/tasks/verify_logging.yml"
HEALTH = ROOT / "deployment/roles/5g/srsRAN/deploy/tasks/verify_ran_health.yml"
IMAGE_DIGEST = ROOT / "synthran/image_digest.py"
RFSIM_RENDER = ROOT / "tools/check_srsran_rfsim_render.py"
CHART_DEFAULTS = ROOT / "deployment/roles/5g/srsRAN/common/defaults/main.yml"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def text(path: Path) -> str:
    require(path.is_file(), f"missing required file: {path}")
    return path.read_text(encoding="utf-8")


def named_task(tasks: list[dict], name: str) -> dict:
    for task in tasks:
        if task.get("name") == name:
            return task
    raise SystemExit(f"missing required task: {name}")


def main() -> None:
    require(not RFSIM_HARDEN.exists(), "duplicate deploy-stage RFSIM hardening owner returned")
    require(not LEGACY_DEPLOY_PATCH.exists(), "deploy-stage RFSIM chart mutation owner returned")
    require(not LEGACY_DEPLOY_CONFIGMAP.exists(), "deploy-owned RFSIM chart template returned")

    config = text(CONFIG_MAIN)
    for needle in (
        "Require one sequential deployment-contract entry per selected RFSIM UE",
        "synthran_srsue_image_source",
        "synthran_srsue_image_reference",
        "synthran.image_digest",
        'repository: "{{ synthran_srsue_image_reference }}"',
        'tag: ""',
        "prepare_rfsim_chart.yml",
        "harden_runtime.yml",
    ):
        require(needle in config, f"RFSIM/config ownership lost: {needle}")
    for forbidden in ("ue_indices:", "synthran_srsue_image_repository_override"):
        require(forbidden not in config, f"obsolete RFSIM parallel state returned: {forbidden}")

    deploy = text(DEPLOY_MAIN)
    reference_marker = "synthran_srsran_reference_deploy_srsues"
    verify_marker = "verify_rfsim_ue_runtime.yml"
    require(reference_marker in deploy, "reference-owned RFSIM UE lifecycle is no longer invoked")
    require(verify_marker in deploy, "live RFSIM UE runtime verification is no longer invoked")
    require(
        deploy.index(reference_marker) < deploy.index(verify_marker),
        "RFSIM live verification must follow reference-owned UE deployment",
    )
    for forbidden in ("patch_charts.yml", "harden_runtime.yml", "prepare_rfsim_chart.yml"):
        require(forbidden not in deploy, f"deploy role regained chart mutation ownership: {forbidden}")

    prepared = text(PREPARE_RFSIM_CHART)
    require(
        ".Values.image.repository" in prepared and ".Values.image.tag" not in prepared,
        "RFSIM deployment template must consume the digest-qualified repository directly",
    )
    for forbidden in (
        "add_route.sh",
        "python3-pip",
        "python3-venv",
        "CAP_NET_ADMIN",
        "apt update",
        "apt-get update",
        "apt install",
        "apt-get install",
        "iputils-ping",
        "iperf3",
    ):
        require(forbidden not in prepared, f"obsolete/buggy RFSIM chart code returned: {forbidden}")
    require(
        prepared.count("LOG=/var/log/gnu_multi_ue.log") == 1,
        "GNU Radio startup must declare exactly one authoritative log path",
    )
    require(
        'exec python3 /srsran/config/multi_ue_scenario.py --nof-ues "$UE_COUNT" >> "$LOG" 2>&1'
        in prepared,
        "GNU Radio broker must write through the script-owned log exactly once",
    )
    require('tee "$LOG"' not in prepared, "GNU Radio script must not add a second log writer")
    require("GNU Radio broker ready" in prepared, "RFSIM broker lost explicit readiness marker")

    configmap = text(RFSIM_CONFIGMAP)
    for forbidden in (
        "add_route.sh",
        "12.1.0.0/16",
        "14.1.0.0/16",
        "/proc/1/fd/1",
    ):
        require(forbidden not in configmap, f"obsolete RFSIM UE behavior returned: {forbidden}")
    for needle in (
        'CONSOLE_LOG="/var/log/ue${UE_NUMBER}.console.log"',
        '>> "$CONSOLE_LOG" 2>&1 &',
        'filename = {log_file}',
        '"[slicing]\\n"',
        '"nssai-sst = {sst}\\n"',
        'slicing += "nssai-sd = {}\\n".format(int(cfg["sd"]))',
    ):
        require(needle in configmap, f"RFSIM UE log/slice ownership lost: {needle}")

    rfsim = text(RFSIM_VERIFY)
    for needle in (
        'label_selectors: "{{ srs_ue_label_selectors }}"',
        "synthran_srsue_live_pod.resources | length == 1",
        "synthran_srsue_live_pod.resources[0].metadata.name == ue_pod_name",
        "synthran_srsue_image_reference",
        "imageID",
        "synthran_srsue_live_digest",
        "synthran_srsue_expected_digest",
        "synthran_srsue_configured_images[0] == synthran_srsue_image_reference",
        "synthran_srsue_live_digest | length > 0",
        "Verify baked RFSIM runtime prerequisites",
        "command -v tmux",
        "command -v python3",
        "command -v ip",
        "from gnuradio import gr, zeromq, blocks",
        "tmux new-session -d -s ran",
        "tmux has-session -t ran",
        "runtime_prerequisites_verified",
        "tmux_session_verified",
        "srsran-rfsim-ue-runtime.json",
        "selected_ues",
    ):
        require(needle in rfsim, f"RFSIM live-image/runtime evidence contract lost: {needle}")
    require(
        "synthran_srsue_live_digest == synthran_srsue_expected_digest" not in rfsim,
        "RFSIM verifier incorrectly equates an OCI index digest with a platform-manifest imageID",
    )

    broker = text(START_BROKER)
    broker_tasks = yaml.safe_load(broker)
    for forbidden in (
        "pkill -9 python3",
        "pkill -9 srsue",
        "ue_indices",
        "add_route.sh",
        "gnb_pod_name_result",
        "tee /var/log/gnu_multi_ue.log",
        "tee /var/log/ue",
        "PDU Session Establishment successful",
        "-c gnb-logs",
    ):
        require(forbidden not in broker, f"buggy RFSIM startup/evidence code returned: {forbidden}")
    for needle in (
        "[m]ulti_ue_scenario.py",
        "GNU Radio broker ready",
        "synthran_gnb_logging_pod",
        ".console.log",
        "ip -4 -o addr show dev tun_srsue",
        "set -euo pipefail",
        "Require the gNB cell to activate",
        "Require the GNU Radio broker to start",
        "Require the gNB to establish NGAP with the AMF",
        "Require every configured UE to establish a PDU-session tunnel",
        "Require the GNU Radio broker to remain alive after UE establishment",
    ):
        require(needle in broker, f"RFSIM startup contract lost: {needle}")

    cleanup = named_task(
        broker_tasks, "Stop stale RFSIM UE and broker processes before a new baseline"
    )
    cleanup_command = cleanup.get("ansible.builtin.command", {})
    cleanup_argv = cleanup_command.get("argv", []) if isinstance(cleanup_command, dict) else []
    cleanup_script = "\n".join(str(value) for value in cleanup_argv)
    for needle in (
        "pkill -TERM -x srsue",
        "pkill -TERM -f '[m]ulti_ue_scenario.py'",
        "pkill -KILL -x srsue",
        "pkill -KILL -f '[m]ulti_ue_scenario.py'",
        "stale RFSIM UE or GNU Radio process survived targeted cleanup",
    ):
        require(needle in cleanup_script, f"targeted stale-process cleanup lost: {needle}")
    require("failed_when" not in cleanup, "stale RFSIM process cleanup must fail closed")
    require(
        cleanup_script.index("pkill -TERM -x srsue") < cleanup_script.index("pkill -KILL -x srsue"),
        "RFSIM cleanup must attempt graceful termination before targeted kill",
    )

    broker_start = named_task(broker_tasks, "Start GNU Radio broker in tmux window 'gnu'")
    require("failed_when" not in broker_start, "GNU Radio startup mutation must fail immediately")
    tunnel_wait = named_task(broker_tasks, "Wait for all configured UE tunnels")
    require(
        tunnel_wait.get("retries") == 60 and tunnel_wait.get("delay") == 2,
        "outer RFSIM tunnel wait must cover the 100-second in-pod startup window",
    )
    live_addresses = named_task(broker_tasks, "Read live UE tunnel addresses")
    require("failed_when" not in live_addresses, "final live tunnel evidence must be strict")

    logging = text(VERIFY_LOGGING)
    for needle in (
        "synthran_gnb_logging_pods.resources | length == 1",
        "/proc/[0-9]*/comm",
        "/usr/local/bin/gnb",
        "/srsran/config/srsran-gnb.yaml",
        "/var/log/gnb.log",
        "synthran_gnb_live_image_digest",
        "synthran_gnb_expected_image_digest",
        "synthran_gnb_configured_images[0] == synthran_srsran_image_reference",
        "synthran_gnb_live_image_digest | length > 0",
        "synthran_gnb_log_sidecar_configured_images[0] == synthran_log_sidecar_image",
    ):
        require(needle in logging, f"live gNB/log ownership proof lost: {needle}")
    require(
        "synthran_gnb_live_image_digest == synthran_gnb_expected_image_digest" not in logging,
        "generic gNB verifier incorrectly equates an OCI index digest with a platform-manifest imageID",
    )

    health = text(HEALTH)
    for needle in (
        "ss -H -n -A sctp state established",
        "synthran_srsran_live_n2",
        "imageID",
        "synthran_srsran_live_image_digest",
        "synthran_srsran_expected_image_digest",
        "synthran_srsran_health_final_images[0] == synthran_srsran_image_reference",
        "synthran_srsran_live_image_digest | length > 0",
        "configured_image_digest",
        "live_image_digest",
        "pod_spec_image",
        "synthran_gnb_logging_pod",
        "synthran_gnb_logging_config",
        "synthran_gnb_logging_cmdline",
        "log_line_start",
        "log_line_mid",
        "log_line_end",
    ):
        require(needle in health, f"physical live-state evidence lost: {needle}")
    require(
        "synthran_srsran_live_image_digest == synthran_srsran_expected_image_digest" not in health,
        "physical verifier incorrectly equates an OCI index digest with a platform-manifest imageID",
    )
    require(
        "synthran_srsran_live_n2.rc == 0" in health,
        "physical acceptance no longer requires live N2 SCTP state",
    )

    resolver = text(IMAGE_DIGEST)
    for needle in (
        "resolve_public_image",
        '"ghcr.io"',
        "_SUPPORTED_REGISTRIES",
        "WWW-Authenticate",
        "Docker-Content-Digest",
        "_SUPPORTED_AUTH_HOSTS",
    ):
        require(needle in resolver, f"public OCI resolver lost constrained behavior: {needle}")

    render = text(RFSIM_RENDER)
    for needle in (
        "PREPARE_RFSIM_CHART",
        "CONFIGMAP_TEMPLATE",
        '"helm"',
        '"template"',
        "_render_case(chart, args.ue_image.strip(), 1)",
        "_render_case(chart, args.ue_image.strip(), 3)",
        "_validate_generated_ue_configs",
        "nssai-sst",
        "nssai-sd",
        "zmqTxPort",
        "zmqRxPort",
        "imsi",
        "apn",
    ):
        require(needle in render, f"RFSIM rendered-fixture coverage lost: {needle}")

    defaults = yaml.safe_load(text(CHART_DEFAULTS))
    version = str(defaults.get("version", ""))
    require(
        len(version) == 40 and all(character in "0123456789abcdef" for character in version.lower()),
        "srsRAN downstream chart pin is no longer immutable",
    )

    print("srsRAN Sub 07 runtime contract OK")


if __name__ == "__main__":
    main()
