#!/usr/bin/env python3
"""Contract checks for issue #124 Tasks 2-3 SOP preflight ordering."""

from __future__ import annotations

from pathlib import Path

import yaml

from synthran import reservation

ROOT = Path(__file__).resolve().parents[1]


def fail(message: str) -> None:
    raise SystemExit(message)


def require(condition: bool, message: str) -> None:
    if not condition:
        fail(message)


def require_text(text: str, needle: str, context: str) -> None:
    if needle not in text:
        fail(f"{context}: missing required marker {needle!r}")


def main() -> int:
    site_path = ROOT / "deployment/playbooks/site.yml"
    site = site_path.read_text(encoding="utf-8")
    provision = site.index("provision_nodes.yml")
    preflight = site.index("preflight_nodes.yml")
    r2lab = site.index("provision_r2lab.yml")
    bootstrap = site.index("bootstrap_nodes.yml")
    require(
        provision < preflight < r2lab < bootstrap,
        "site.yml must validate nodes, preflight SOP state, then mutate R2Lab, then bootstrap",
    )

    path = ROOT / "deployment/playbooks/preflight_nodes.yml"
    text = path.read_text(encoding="utf-8")
    yaml.safe_load(text)

    for marker in (
        "Wait for the selected SOP node before preflight",
        "Probe containerd service",
        "Probe kubelet service",
        "Probe CNI DHCP binary",
        "Probe CNI DHCP daemon",
        "Probe OVS database service",
        "Probe OVS switching service",
        "Probe authoritative OVS bridges",
        "Probe IPv4 forwarding",
        "Probe bridge IPv4 filtering",
        "Probe bridge IPv6 filtering",
        "Read live kernel command line",
        "Probe Kubernetes control plane readiness",
        "Probe preserved RAN node registration",
        "Probe NetworkAttachmentDefinition API",
        "Retain per-host SOP preflight evidence before enforcement",
        "Retain control-plane SOP preflight evidence before enforcement",
        "Require preserved containerd",
        "Require preserved kubelet",
        "Require preserved CNI DHCP prerequisites",
        "Require preserved OVS services",
        "Require preserved authoritative OVS bridges",
        "Require preserved Kubernetes networking sysctls",
        "Require preserved Kubernetes control plane",
    ):
        require_text(text, marker, "preflight_nodes.yml")

    require_text(text, "{{ run_dir }}/sop-preflight-", "preflight_nodes.yml")
    require_text(text, "{{ run_dir }}/sop-preflight-cluster.json", "preflight_nodes.yml")
    require_text(text, "failed_when: false", "preflight_nodes.yml")
    require_text(text, "synthran_host_preparation == 'preserve'", "preflight_nodes.yml")

    evidence_at = text.index("Retain per-host SOP preflight evidence before enforcement")
    enforce_at = text.index("Enforce selected SOP host preflight policy before physical testbed mutation")
    require(evidence_at < enforce_at, "per-host preflight evidence must be retained before enforcement")
    cluster_evidence_at = text.index("Retain control-plane SOP preflight evidence before enforcement")
    cluster_enforce_at = text.index("Enforce preserved Kubernetes control-plane preflight before physical testbed mutation")
    require(
        cluster_evidence_at < cluster_enforce_at,
        "control-plane preflight evidence must be retained before enforcement",
    )

    for forbidden in ("r2lab/cleanup", "r2lab/rru", "r2lab/ue/setup"):
        require(forbidden not in text, f"preflight must not mutate R2Lab resources: {forbidden}")

    group_vars = yaml.safe_load(
        (ROOT / "deployment/group_vars/all/all.yml").read_text(encoding="utf-8")
    )
    common = list(group_vars["synthran_boot_common_tokens"])
    ran = list(group_vars["synthran_boot_ran_tokens"])
    expected_generic = reservation.REFERENCE_BOOT_PARAMETERS.split()
    expected_ran = reservation.REFERENCE_BOOT_RAN_PARAMETERS.split()
    require(common == expected_generic, "generic preflight boot tokens drifted from POS reservation profile")
    require(len(common + ran) == len(set(common + ran)), "preflight boot-token lists contain duplicates")
    require(
        set(common + ran) == set(expected_ran),
        "RAN preflight boot tokens drifted from POS reservation profile",
    )

    r2lab_text = (ROOT / "deployment/playbooks/provision_r2lab.yml").read_text(encoding="utf-8")
    for mutation in (
        "Cleanup selected R2Lab resources before provisioning",
        "Power ON RRU on R2Lab",
        "Prepare selected UEs on R2Lab",
    ):
        require_text(r2lab_text, mutation, "provision_r2lab.yml")

    print("Issue #124 Tasks 2-3 SOP preflight ordering contract OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
