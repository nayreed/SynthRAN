#!/usr/bin/env python3
"""No-hardware contract for the issue #124 physical validation artifact gate."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "tools" / "validate_issue124_physical.py"


class CheckError(RuntimeError):
    pass


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CheckError(message)


def load_validator():
    spec = importlib.util.spec_from_file_location("issue124_physical_validator", VALIDATOR)
    if spec is None or spec.loader is None:
        raise CheckError("cannot load physical validation module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def head() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def base_run(path: Path, revision: str, exit_code: int) -> None:
    write_json(
        path / "provenance" / "controller.json",
        {
            "schema_version": 1,
            "source": {
                "revision": revision,
                "dirty_worktree": False,
                "worktree_status_sha256": "fixture",
            },
        },
    )
    (path / "source-revision.txt").write_text(revision + "\n", encoding="utf-8")
    (path / "controller-exit-code").write_text(str(exit_code) + "\n", encoding="utf-8")


def preflight(host: str, *, kubelet_rc: int) -> dict:
    return {
        "schema": "synthran/sop-preflight-host/v1",
        "host": host,
        "host_preparation": "preserve",
        "services": {
            "containerd_rc": 0,
            "kubelet_rc": kubelet_rc,
            "cni_dhcp_rc": 0,
            "ovsdb_server_rc": 0,
            "ovs_vswitchd_rc": 0,
        },
        "cni": {
            "dhcp_binary_exists": True,
            "dhcp_binary_executable": True,
        },
        "network": {"ip_forward": "1"},
    }


def timing(*records: dict) -> dict:
    return {
        "schema": "synthran/phase-timings/v1",
        "records": list(records),
    }


def success_phase(name: str) -> dict:
    return {
        "phase": name,
        "scope": "",
        "status": "success",
        "started_at": "2026-09-20T09:00:00+00:00",
        "completed_at": "2026-09-20T09:00:01+00:00",
        "elapsed_seconds": 1.0,
    }


def build_negative(path: Path, revision: str) -> None:
    base_run(path, revision, 2)
    write_json(
        path / "reservation-authority.json",
        {
            "policies": {"host_preparation": "preserve"},
            "selected_nodes": {
                "core": "sopnode-f2",
                "ran": "sopnode-f3",
                "broker": "sopnode-f2",
            },
        },
    )
    write_json(path / "sop-preflight-sopnode-f2.json", preflight("sopnode-f2", kubelet_rc=3))
    write_json(path / "sop-preflight-sopnode-f3.json", preflight("sopnode-f3", kubelet_rc=3))
    write_json(
        path / "phase-timings.json",
        timing(success_phase("reservation")),
    )
    (path / "ansible.log").write_text(
        "Preflight selected SOP hosts before physical testbed mutation\n"
        "Require preserved kubelet\n"
        "FAILED\n",
        encoding="utf-8",
    )
    write_json(path / "deployment-fingerprint.json", {"status": "failed"})



def build_reachability_negative(path: Path, revision: str) -> None:
    base_run(path, revision, 2)
    write_json(
        path / "reservation-authority.json",
        {
            "policies": {"host_preparation": "preserve"},
            "selected_nodes": {
                "core": "sopnode-f2",
                "ran": "sopnode-f3",
                "broker": "sopnode-f2",
            },
        },
    )
    write_json(
        path / "phase-timings.json",
        timing(success_phase("reservation")),
    )
    (path / "ansible.log").write_text(
        "PLAY [Validate selected SOP nodes]\\n"
        "TASK [Wait for the selected SOP node to become reachable]\\n"
        "fatal: [sopnode-f2]: FAILED! => timed out waiting for ping module test: "
        "Failed to connect to the host via ssh: root@sopnode-f2: Permission denied (publickey).\\n"
        "fatal: [sopnode-f3]: FAILED! => timed out waiting for ping module test: "
        "Failed to connect to the host via ssh: root@sopnode-f3: Permission denied (publickey).\\n",
        encoding="utf-8",
    )


def build_positive(path: Path, revision: str) -> None:
    base_run(path, revision, 0)
    write_json(
        path / "reservation-authority.json",
        {
            "policies": {"host_preparation": "bootstrap"},
            "selected_nodes": {
                "core": "sopnode-f2",
                "ran": "sopnode-f3",
                "broker": "sopnode-f2",
            },
            "host_preparation": {
                "mode": "bootstrap",
                "mutations": [],
            },
        },
    )
    for host in ("sopnode-f2", "sopnode-f3"):
        evidence = preflight(host, kubelet_rc=3)
        evidence["host_preparation"] = "bootstrap"
        write_json(path / f"sop-preflight-{host}.json", evidence)
        write_json(
            path / f"sop-bootstrap-classification-{host}.json",
            {
                "classification": "repairable-in-place",
                "host_preparation": "bootstrap",
            },
        )
    write_json(
        path / "bootstrap-evidence.json",
        {"host_preparation": "bootstrap"},
    )
    write_json(
        path / "deployment-fingerprint.json",
        {
            "status": "accepted-testbed",
            "deployment": {
                "core": "oai",
                "ran": "oai",
                "platform": "r2lab",
                "radio_unit": "n320",
                "host_preparation": "bootstrap",
                "nodes": {
                    "core": "sopnode-f2",
                    "ran": "sopnode-f3",
                    "broker": "sopnode-f2",
                },
                "ues": [
                    {"device": "qhat01"},
                    {"device": "qhat03"},
                ],
            },
        },
    )
    write_json(
        path / "live-deployment-evidence.json",
        {
            "cluster_identity_verified": True,
            "bindings": [
                {
                    "device": "qhat01",
                    "modem_verified": True,
                    "user_plane": {"verified": True},
                },
                {
                    "device": "qhat03",
                    "modem_verified": True,
                    "user_plane": {"verified": True},
                },
            ],
        },
    )
    write_json(
        path / "phase-timings.json",
        timing(
            success_phase("reservation"),
            success_phase("kubernetes_bootstrap"),
            success_phase("r2lab_rru_setup"),
            success_phase("ue_setup"),
            success_phase("stack_deployment"),
            success_phase("verification"),
        ),
    )


def expect_failure(callable_, label: str) -> None:
    try:
        callable_()
    except Exception as exc:
        if exc.__class__.__name__ != "ValidationError":
            raise
        return
    raise CheckError(f"validator accepted invalid fixture: {label}")


def main() -> int:
    validator = load_validator()
    revision = head()

    with tempfile.TemporaryDirectory(prefix="synthran-issue124-physical-") as value:
        root = Path(value)
        negative = root / "negative"
        positive = root / "positive"
        build_negative(negative, revision)
        build_positive(positive, revision)

        neg = validator.validate_preserve_failure(negative, revision)
        require(
            neg["preflight_failure_stage"] == "detailed-preflight",
            "detailed negative fixture did not report detailed-preflight stage",
        )

        reachability_negative = root / "reachability-negative"
        build_reachability_negative(reachability_negative, revision)
        reachability = validator.validate_preserve_failure(
            reachability_negative,
            revision,
        )
        require(
            reachability["preflight_failure_stage"] == "reachability-gate",
            "reachability fixture did not report reachability-gate stage",
        )

        pos = validator.validate_bootstrap_success(
            positive,
            revision,
            ["qhat01", "qhat03"],
        )
        require(neg["physical_mutation_started"] is False, "negative proof lost no-mutation result")
        require(pos["accepted_testbed"] is True, "positive proof lost accepted-testbed result")

        (negative / "ansible.log").write_text(
            (negative / "ansible.log").read_text(encoding="utf-8")
            + "Power OFF selected QHAT UEs\n",
            encoding="utf-8",
        )
        expect_failure(
            lambda: validator.validate_preserve_failure(negative, revision),
            "preserve physical mutation",
        )

        build_negative(negative, revision)
        timings = json.loads((positive / "phase-timings.json").read_text(encoding="utf-8"))
        timings["records"].append(success_phase("pos_reset"))
        write_json(positive / "phase-timings.json", timings)
        expect_failure(
            lambda: validator.validate_bootstrap_success(
                positive,
                revision,
                ["qhat01", "qhat03"],
            ),
            "bootstrap fresh-only POS reset",
        )

    print("issue #124 physical validation artifact gate OK")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CheckError as exc:
        raise SystemExit(f"issue124-physical-validator-contract: {exc}") from exc
