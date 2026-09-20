#!/usr/bin/env python3
"""Behavioral regression checks for issue #115 software-UE diagnostics."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]


class CheckError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CheckError(message)


def done(argv: list[str], rc: int = 0, out: str = "", err: str = ""):
    return subprocess.CompletedProcess(argv, rc, out, err)


def load_probe():
    path = ROOT / "deployment/scripts/probe_software_ues.py"
    spec = importlib.util.spec_from_file_location("probe_software_ues_issue115", path)
    require(spec is not None and spec.loader is not None, "cannot load software UE probe")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def manifest() -> dict[str, Any]:
    return {
        "deployment": {
            "platform": "rfsim",
            "ran": "oai",
            "topology": {"namespace": "oai"},
            "ues": [
                {
                    "device": "uesim01",
                    "index": 1,
                    "imsi": "001010000000001",
                    "slice": "slice1",
                    "sst": "1",
                    "sd": "EMPTY",
                    "dnn": "internet",
                    "address_cidr": "12.1.1.0/24",
                    "user_plane_target": "12.1.1.1",
                    "tunnel": {
                        "namespace": "oai",
                        "interface": "oaitun_ue1",
                        "pod_name_prefix": "oai-nr-ue-",
                    },
                }
            ],
        }
    }


def pod(*, phase: str = "Running", ready: bool = True) -> dict[str, Any]:
    return {
        "metadata": {
            "name": "oai-nr-ue-abc",
            "labels": {"app.kubernetes.io/instance": "oai-nr-ue"},
        },
        "spec": {"containers": [{"name": "nr-ue"}]},
        "status": {
            "phase": phase,
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
            "containerStatuses": [
                {
                    "name": "nr-ue",
                    "ready": ready,
                    "restartCount": 0,
                    "state": {"running": {"startedAt": "fixture"}} if phase == "Running" else {},
                }
            ],
        },
    }


def router(case: str) -> Callable[[list[str]], subprocess.CompletedProcess[str]]:
    pods: list[dict[str, Any]]
    if case == "pod-absent":
        pods = []
    elif case == "pod-not-running":
        pods = [pod(phase="Pending", ready=False)]
    elif case == "pod-not-ready":
        pods = [pod(phase="Running", ready=False)]
    else:
        pods = [pod()]

    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        command = list(argv)

        if command[:4] == ["kubectl", "get", "pods", "-n"]:
            return done(command, out=json.dumps({"items": pods}))

        if command[:2] == ["helm", "status"]:
            if case == "helm-release-absent":
                return done(command, rc=1, err="release: not found")
            return done(command, out=json.dumps({"info": {"status": "deployed"}}))

        if command[:2] == ["kubectl", "exec"]:
            if command[-4:] == ["ip", "-o", "link", "show"]:
                if case == "exec-failed":
                    return done(command, rc=126, err="container exec unavailable")
                if case == "tunnel-absent":
                    return done(command, out="1: lo: <LOOPBACK> mtu 65536\n")
                return done(
                    command,
                    out="1: lo: <LOOPBACK> mtu 65536\n7: oaitun_ue1: <POINTOPOINT,UP> mtu 1500\n",
                )

            if "addr" in command and "dev" in command:
                if case == "address-query-failed":
                    return done(command, rc=2, err="ip addr failed")
                if case == "tunnel-no-ipv4":
                    return done(command, out="")
                address = "14.1.1.2" if case == "slice-address-mismatch" else "12.1.1.2"
                return done(
                    command,
                    out=f"7: oaitun_ue1    inet {address}/24 scope global oaitun_ue1\n",
                )

            if command[-5:-1] == ["ip", "-4", "-o", "addr"]:
                return done(
                    command,
                    out="7: oaitun_ue1    inet 12.1.1.2/24 scope global oaitun_ue1\n",
                )

            if "ping" in command:
                if case == "user-plane-failed":
                    return done(command, rc=1, err="100% packet loss")
                return done(command, out="1 packets transmitted, 1 received")

        if command[:2] == ["kubectl", "logs"]:
            return done(command, out="token=super-secret-value\nnormal UE log line\n")

        raise CheckError(f"unhandled command for {case}: {command}")

    return run


def stage_checks() -> None:
    probe = load_probe()
    cases = [
        "helm-release-absent",
        "pod-absent",
        "pod-not-running",
        "pod-not-ready",
        "exec-failed",
        "tunnel-absent",
        "address-query-failed",
        "tunnel-no-ipv4",
        "slice-address-mismatch",
        "user-plane-failed",
    ]

    for case in cases:
        probe._run = router(case)
        with tempfile.TemporaryDirectory(prefix="synthran-issue115-") as value:
            evidence = Path(value) / "failure.json"
            try:
                probe.probe_selected_bindings(
                    manifest(),
                    evidence_path=str(evidence),
                )
            except probe.ProbeFailure as exc:
                require(exc.stage == case, f"{case}: got stage {exc.stage!r}")
            else:
                raise CheckError(f"{case}: probe unexpectedly succeeded")

            require(evidence.exists(), f"{case}: no retained evidence")
            document = json.loads(evidence.read_text(encoding="utf-8"))
            require(document["stage"] == case, f"{case}: evidence stage mismatch")
            require(document["device"] == "uesim01", f"{case}: evidence device mismatch")
            serialized = evidence.read_text(encoding="utf-8")
            require("super-secret-value" not in serialized, f"{case}: evidence leaked token")
            require("<redacted>" in serialized or not document.get("runtime"), f"{case}: token was not redacted")

    probe._run = router("success")
    bindings = probe.probe_selected_bindings(manifest())
    require(len(bindings) == 1, "success case did not return one binding")
    require(bindings[0]["device"] == "uesim01", "success case returned wrong device")
    require(bindings[0]["address"] == "12.1.1.2", "success case returned wrong address")
    require(bindings[0]["user_plane"]["verified"] is True, "success user plane not verified")


def playbook_checks() -> None:
    text = (ROOT / "deployment/playbooks/verify_live_testbed.yml").read_text(encoding="utf-8")
    for marker in (
        "--evidence",
        "/tmp/synthran-software-ue-acceptance-failure.json",
        "failed_when: false",
        "Retain structured software UE acceptance failure evidence",
        "software-ue-acceptance-failure.json",
        "Require the complete read-only software UE acceptance probe",
    ):
        require(marker in text, f"final acceptance lost diagnostic marker: {marker}")

    probe_pos = text.index("Probe every selected software UE without changing testbed state")
    evidence_pos = text.index("Retain structured software UE acceptance failure evidence")
    gate_pos = text.index("Require the complete read-only software UE acceptance probe")
    require(probe_pos < evidence_pos < gate_pos, "failure evidence is not retained before the fatal gate")

    forbidden = (
        "kubectl delete",
        "helm uninstall",
        "helm upgrade",
        "pos allocations",
        "rhubarbe",
    )
    acceptance_section = text[probe_pos:gate_pos]
    for marker in forbidden:
        require(marker not in acceptance_section, f"read-only acceptance contains mutation marker: {marker}")


def main() -> int:
    stage_checks()
    playbook_checks()
    print("Software UE diagnostic acceptance checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
