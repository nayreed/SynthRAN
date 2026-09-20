#!/usr/bin/env python3
"""Issue #124 Task 12 consolidated host-preparation regression matrix."""

from __future__ import annotations

import inspect
import os
from pathlib import Path
import subprocess
import sys

from synthran import reservation

ROOT = Path(__file__).resolve().parents[1]


class CheckError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CheckError(message)


def run_checker(name: str) -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / name)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise CheckError(
            f"{name} failed while satisfying the #124 regression matrix:\n"
            f"{result.stdout}\n{result.stderr}"
        )


def preserve_regressions() -> None:
    site = (ROOT / "deployment/playbooks/site.yml").read_text(encoding="utf-8")
    preflight_at = site.index("preflight_nodes.yml")
    r2lab_at = site.index("provision_r2lab.yml")
    require(
        preflight_at < r2lab_at,
        "preserve preflight must execute before any R2Lab mutation",
    )

    preflight = (
        ROOT / "deployment/playbooks/preflight_nodes.yml"
    ).read_text(encoding="utf-8")
    for marker in (
        "Require preserved kubelet",
        "synthran_preflight_kubelet.rc == 0",
        "synthran_host_preparation == 'preserve'",
        "Retain per-host SOP preflight evidence before enforcement",
    ):
        require(marker in preflight, f"preserve missing-kubelet regression lost {marker!r}")

    calls: list[list[str]] = []

    def forbidden(argv, *, check=True, stdin=None, echo=False):
        calls.append(list(argv))
        raise CheckError(f"preserve attempted host mutation: {argv}")

    original = reservation.run
    reservation.run = forbidden
    try:
        result = reservation.prepare_hosts(
            {"host_preparation": "preserve", "image": "unused"},
            selected=["sopnode-f2", "sopnode-f3"],
            calendar={"status": "required-existing"},
        )
    finally:
        reservation.run = original
    require(result["mode"] == "preserve", "healthy preserve path lost preserve mode")
    require(result["mutations"] == [], "healthy preserve path claims mutations")
    require(not calls, f"healthy preserve path mutated POS: {calls}")

    bootstrap = (
        ROOT / "deployment/playbooks/bootstrap_nodes.yml"
    ).read_text(encoding="utf-8")
    for marker in (
        "Verify preserved containerd is active",
        "Verify preserved kubelet is active",
        "Verify preserved Kubernetes control plane is reachable",
        "Verify preserved NetworkAttachmentDefinition API",
    ):
        require(marker in bootstrap, f"healthy preserve verification lost {marker!r}")


def bootstrap_regressions() -> None:
    classify = (
        ROOT / "deployment/playbooks/bootstrap_classify.yml"
    ).read_text(encoding="utf-8")
    bootstrap = (
        ROOT / "deployment/playbooks/bootstrap_nodes.yml"
    ).read_text(encoding="utf-8")

    # Missing kubelet on a supported/otherwise valid host is represented as a
    # repairable prerequisite, while cluster identity determines rebuild/reuse.
    for marker in (
        "kubelet-missing-or-inactive",
        "repairable-in-place",
        "synthran_bootstrap_cluster_action == 'rebuild'",
        "setup/k8s/k8s_setup",
        "setup/k8s/cluster_create",
        "setup/k8s/cluster_join",
    ):
        require(
            marker in classify or marker in bootstrap,
            f"bootstrap missing-kubelet repair path lost {marker!r}",
        )

    prepare_source = inspect.getsource(reservation.prepare_hosts)
    bootstrap_segment = prepare_source.split("if mode == BOOTSTRAP:", 1)[1].split(
        'image = reservation.get("image"', 1
    )[0]
    for forbidden in (
        "_probe_allocation_for_fresh",
        "_reclaim_allocation_for_fresh",
        "pos nodes image",
        "pos nodes reset",
    ):
        require(
            forbidden not in bootstrap_segment,
            f"bootstrap repair path regained fresh POS mutation: {forbidden}",
        )

    # Unrecoverable existing cluster/OS/boot state must fail explicitly to fresh
    # before the site enters provision_r2lab.yml.
    for marker in (
        "requires-fresh",
        "Block bootstrap state that requires a fresh image",
        "Re-run with host_preparation=fresh",
        "No image staging or allocation",
    ):
        require(marker in classify, f"requires-fresh regression lost {marker!r}")


def fresh_regression() -> None:
    node = "sopnode-f3"
    calls: list[list[str]] = []
    old_attempts = os.environ.get("SYNTHRAN_POS_READY_ATTEMPTS")
    old_interval = os.environ.get("SYNTHRAN_POS_READY_INTERVAL_SECONDS")
    os.environ["SYNTHRAN_POS_READY_ATTEMPTS"] = "1"
    os.environ["SYNTHRAN_POS_READY_INTERVAL_SECONDS"] = "0"

    def fake(argv, *, check=True, stdin=None, echo=False):
        command = list(argv)
        calls.append(command)
        result = subprocess.CompletedProcess(command, 0, "", "")
        return result

    original = reservation.run
    reservation.run = fake
    try:
        result = reservation.prepare_hosts(
            {"host_preparation": "fresh", "image": "ci-image"},
            selected=[node],
            calendar={"status": "created"},
        )
    finally:
        reservation.run = original
        if old_attempts is None:
            os.environ.pop("SYNTHRAN_POS_READY_ATTEMPTS", None)
        else:
            os.environ["SYNTHRAN_POS_READY_ATTEMPTS"] = old_attempts
        if old_interval is None:
            os.environ.pop("SYNTHRAN_POS_READY_INTERVAL_SECONDS", None)
        else:
            os.environ["SYNTHRAN_POS_READY_INTERVAL_SECONDS"] = old_interval

    require(result["mode"] == "fresh", "fresh path did not remain the clean-baseline policy")
    phases = []
    for command in calls:
        if command[:3] == ["pos", "allocations", "allocate"]:
            require("--result-folder" in command, "fresh allocation lost provider ownership tag")
            phases.append("allocate")
        elif command[:3] == ["pos", "nodes", "image"]:
            phases.append("image")
        elif command[:3] == ["pos", "nodes", "bootparameter"]:
            phases.append("bootparameter")
        elif command[:3] == ["pos", "nodes", "reset"]:
            phases.append("reset")
        elif command and command[0] == "ssh":
            phases.append("ssh")
    require(
        phases == ["allocate", "image", "bootparameter", "reset", "ssh"],
        f"fresh known-clean POS sequence changed: {phases}",
    )


def main() -> int:
    # Detailed checkers retain the full classification/order/concurrency
    # assertions; this umbrella binds them to Task 12's explicit six scenarios.
    run_checker("check_sop_preflight_contract.py")
    run_checker("check_bootstrap_reconcile_contract.py")
    run_checker("check_pos_preparation_regression.py")

    preserve_regressions()       # missing kubelet + healthy preserve
    bootstrap_regressions()      # repairable missing kubelet + requires-fresh
    fresh_regression()           # known-clean fresh path
    # check_pos_preparation_regression.py above is the multi-node concurrency
    # and per-node evidence case.

    print("Issue #124 Task 12 host-preparation regression matrix OK")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CheckError, reservation.ReservationError, OSError, ValueError) as exc:
        raise SystemExit(f"issue124-regressions: {exc}") from exc
