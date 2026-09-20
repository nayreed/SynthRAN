#!/usr/bin/env python3
"""Issue #124 Tasks 4-6 bootstrap reconciliation safety contract."""

from __future__ import annotations

import inspect
from pathlib import Path

import yaml

from synthran import host_preparation, reservation

ROOT = Path(__file__).resolve().parents[1]


def fail(message: str) -> None:
    raise SystemExit(message)


def require(condition: bool, message: str) -> None:
    if not condition:
        fail(message)


def require_text(text: str, needle: str, context: str) -> None:
    if needle not in text:
        fail(f"{context}: missing required marker {needle!r}")


def forbid_text(text: str, needle: str, context: str) -> None:
    if needle in text:
        fail(f"{context}: forbidden marker {needle!r}")


def main() -> int:
    bootstrap = host_preparation.PREPARATION_CONTRACT["bootstrap"]
    require(bootstrap["implementation"] == "active", "bootstrap policy is not active")
    require(host_preparation.is_executable("bootstrap"), "bootstrap must be executable")
    require(not bootstrap["allows_image_staging"], "bootstrap must not allow image staging")
    require(not bootstrap["allows_allocation_reclaim"], "bootstrap must not reclaim allocation")
    require(not bootstrap["allows_pos_reset"], "bootstrap must not use fresh POS reset")
    require(bootstrap["allows_reboot"], "bootstrap must allow bounded reboot")
    require(
        not bootstrap["silent_escalation_to_fresh"],
        "bootstrap must never silently escalate to fresh",
    )

    prepare_source = inspect.getsource(reservation.prepare_hosts)
    require_text(prepare_source, '"mode": "bootstrap"', "reservation.prepare_hosts")
    require_text(prepare_source, '"allocation": "retained"', "reservation.prepare_hosts")
    require_text(prepare_source, '"image": "retained"', "reservation.prepare_hosts")
    bootstrap_segment = prepare_source.split("if mode == BOOTSTRAP:", 1)[1].split(
        'image = reservation.get("image"', 1
    )[0]
    for forbidden in (
        "pos nodes image",
        "allocations free",
        "allocations allocate",
        "pos nodes reset",
        "_probe_allocation_for_fresh",
        "_reclaim_allocation_for_fresh",
    ):
        forbid_text(bootstrap_segment, forbidden, "bootstrap reservation segment")

    classify_path = ROOT / "deployment/playbooks/bootstrap_classify.yml"
    classify = classify_path.read_text(encoding="utf-8")
    yaml.safe_load(classify)
    for marker in (
        "healthy",
        "repairable-in-place",
        "reboot-required",
        "requires-fresh",
        "existing-cluster-unhealthy",
        "boot-profile-drift",
        "sop-bootstrap-classification-cluster.json",
        "sop-bootstrap-classification-{{ inventory_hostname }}.json",
        "Re-run with host_preparation=fresh",
        "Apply required POS boot parameters without image staging",
        "Require explicit POS authority before bootstrap boot mutation",
        "Reboot current OS to activate corrected boot parameters",
        "Require corrected boot profile after bootstrap reboot",
        "image_staged': false",
        "allocation_reclaimed': false",
        "pos_reset_used': false",
    ):
        require_text(classify, marker, "bootstrap_classify.yml")
    for forbidden in (
        "pos nodes image",
        "pos allocations free",
        "pos allocations allocate",
        "pos nodes reset",
    ):
        forbid_text(classify, forbidden, "bootstrap_classify.yml")

    require(
        classify.index("Retain per-host bootstrap classification before mutation")
        < classify.index("Block bootstrap state that requires a fresh image"),
        "classification evidence must be retained before requires-fresh enforcement",
    )
    require(
        classify.index("Require explicit POS authority before bootstrap boot mutation")
        < classify.index("Apply required POS boot parameters without image staging"),
        "boot mutation must follow explicit POS authority",
    )

    bootstrap_nodes_path = ROOT / "deployment/playbooks/bootstrap_nodes.yml"
    bootstrap_nodes = bootstrap_nodes_path.read_text(encoding="utf-8")
    yaml.safe_load(bootstrap_nodes)
    for marker in (
        "synthran_host_preparation in ['fresh', 'bootstrap']",
        "synthran_bootstrap_cluster_action == 'rebuild'",
        "synthran_bootstrap_cluster_action == 'reuse'",
        "Reconcile CNI plugin binaries for bootstrap reuse",
        "Reconcile CNI DHCP service for bootstrap reuse",
        "setup/k8s/cluster_create",
        "setup/k8s/cluster_join",
        "setup/ovs",
        "setup/k8s/k8s_setup",
        "setup/k8s/k8s_cpu_tuning",
        "bootstrap_classification",
        "bootstrap_cluster_action",
    ):
        require_text(bootstrap_nodes, marker, "bootstrap_nodes.yml")

    site = (ROOT / "deployment/playbooks/site.yml").read_text(encoding="utf-8")
    preflight = site.index("preflight_nodes.yml")
    classify_at = site.index("bootstrap_classify.yml")
    r2lab = site.index("provision_r2lab.yml")
    require(
        preflight < classify_at < r2lab,
        "bootstrap classification and boot reconciliation must precede R2Lab mutation",
    )

    preflight_text = (
        ROOT / "deployment/playbooks/preflight_nodes.yml"
    ).read_text(encoding="utf-8")
    for marker in (
        "Probe Kubernetes node identity",
        "Probe generated kubelet configuration",
        "Probe Kubernetes administrator identity",
        "Probe Kubernetes etcd identity",
        "Resolve missing required boot parameters",
    ):
        require_text(preflight_text, marker, "preflight_nodes.yml")

    print("Issue #124 Tasks 4-6 bootstrap reconciliation contract OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
