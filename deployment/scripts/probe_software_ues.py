#!/usr/bin/env python3
"""Prove selected software UE identity, tunnel and source-bound user plane.

The final accepted-testbed path is read-only.  Failures are classified by stage
and can be retained as bounded JSON evidence with --evidence.
"""

from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import json
import re
import subprocess
from pathlib import Path
from typing import Any


TUNNEL_PATTERN = re.compile(r"^(tun_srsue\d+|uesimtun\d*|oaitun_[A-Za-z0-9_.-]+)$")
_SECRET_PATTERN = re.compile(
    r"(?i)\b(password|passwd|token|secret|fullkey|full_key|opc)\b\s*[:=]\s*([^\s,;]+)"
)
MAX_TEXT = 12000
LOG_TAIL_LINES = 120


class ProbeFailure(ValueError):
    def __init__(
        self,
        *,
        stage: str,
        device: str,
        detail: str,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.stage = stage
        self.device = device
        self.detail = detail
        self.evidence = evidence or {}


def _bounded(value: str, limit: int = MAX_TEXT) -> str:
    text = _SECRET_PATTERN.sub(lambda m: f"{m.group(1)}=<redacted>", value or "")
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n<truncated {len(text) - limit} chars>\n"


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=False)


def _run_json(command: list[str], *, stage: str, device: str) -> dict[str, Any]:
    result = _run(command)
    if result.returncode:
        raise ProbeFailure(
            stage=stage,
            device=device,
            detail=_bounded(result.stderr or result.stdout or f"exit status {result.returncode}"),
            evidence={
                "command": command,
                "returncode": result.returncode,
                "stdout": _bounded(result.stdout),
                "stderr": _bounded(result.stderr),
            },
        )
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeFailure(
            stage=stage,
            device=device,
            detail=f"command returned invalid JSON: {exc}",
            evidence={
                "command": command,
                "returncode": result.returncode,
                "stdout": _bounded(result.stdout),
                "stderr": _bounded(result.stderr),
            },
        ) from exc
    if not isinstance(value, dict):
        raise ProbeFailure(
            stage=stage,
            device=device,
            detail="command JSON result is not an object",
            evidence={"command": command, "stdout": _bounded(result.stdout)},
        )
    return value


def _ready(pod: dict[str, Any]) -> bool:
    for condition in pod.get("status", {}).get("conditions", []) or []:
        if condition.get("type") == "Ready":
            return str(condition.get("status", "")).lower() == "true"
    return False


def _pod_summary(pod: dict[str, Any]) -> dict[str, Any]:
    status = pod.get("status", {}) or {}
    spec = pod.get("spec", {}) or {}
    container_statuses = status.get("containerStatuses", []) or []
    return {
        "name": str((pod.get("metadata", {}) or {}).get("name", "")),
        "phase": str(status.get("phase", "")),
        "ready": _ready(pod),
        "containers": [str(item.get("name", "")) for item in spec.get("containers", []) or []],
        "container_statuses": [
            {
                "name": str(item.get("name", "")),
                "ready": bool(item.get("ready", False)),
                "restart_count": int(item.get("restartCount", 0) or 0),
                "state": item.get("state", {}),
            }
            for item in container_statuses
        ],
    }


def _selector_matches_pod(pod: dict[str, Any], selector: dict[str, Any]) -> bool:
    metadata = pod.get("metadata", {}) or {}
    name = str(metadata.get("name", ""))
    prefix = selector.get("pod_name_prefix")
    if prefix and not name.startswith(str(prefix)):
        return False
    labels = metadata.get("labels", {}) or {}
    expected_labels = selector.get("pod_labels", {}) or {}
    return all(labels.get(key) == value for key, value in expected_labels.items())


def _release_name(deployment: dict[str, Any], ue: dict[str, Any]) -> str | None:
    ran = str(deployment.get("ran", "")).lower()
    if ran == "oai":
        prefix = str(ue.get("tunnel", {}).get("pod_name_prefix", ""))
        return prefix[:-1] if prefix.endswith("-") else prefix or None
    if ran == "srsran":
        return "srsran-ue"
    if ran == "ueransim":
        return f"ueransim-{str(ue.get('device', '')).lower()}"
    return None


def _helm_snapshot(namespace: str, release: str | None) -> dict[str, Any]:
    if not release:
        return {"release": None, "checked": False}
    result = _run(["helm", "status", release, "--namespace", namespace, "--output", "json"])
    snapshot: dict[str, Any] = {
        "release": release,
        "checked": True,
        "returncode": result.returncode,
        "stdout": _bounded(result.stdout),
        "stderr": _bounded(result.stderr),
    }
    if result.returncode == 0:
        try:
            parsed = json.loads(result.stdout)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            snapshot["status"] = str(parsed.get("info", {}).get("status", ""))
    return snapshot


def _pod_runtime_evidence(namespace: str, pod: dict[str, Any]) -> dict[str, Any]:
    summary = _pod_summary(pod)
    name = summary["name"]
    details: dict[str, Any] = {"pod": summary, "containers": {}}
    for container in summary["containers"]:
        links = _run(
            ["kubectl", "exec", "-n", namespace, name, "-c", container, "--", "ip", "-o", "link", "show"]
        )
        addresses = _run(
            ["kubectl", "exec", "-n", namespace, name, "-c", container, "--", "ip", "-4", "-o", "addr", "show"]
        )
        logs = _run(
            [
                "kubectl",
                "logs",
                "-n",
                namespace,
                name,
                "-c",
                container,
                "--tail",
                str(LOG_TAIL_LINES),
            ]
        )
        details["containers"][container] = {
            "links": {
                "returncode": links.returncode,
                "stdout": _bounded(links.stdout),
                "stderr": _bounded(links.stderr),
            },
            "addresses": {
                "returncode": addresses.returncode,
                "stdout": _bounded(addresses.stdout),
                "stderr": _bounded(addresses.stderr),
            },
            "logs": {
                "returncode": logs.returncode,
                "stdout": _bounded(logs.stdout),
                "stderr": _bounded(logs.stderr),
            },
        }
    return details


def _failure_evidence(
    *,
    deployment: dict[str, Any],
    namespace: str,
    ue: dict[str, Any],
    pods: list[dict[str, Any]],
    failure: ProbeFailure,
) -> dict[str, Any]:
    selector = ue.get("tunnel", {}) or {}
    selected = [pod for pod in pods if _selector_matches_pod(pod, selector)]
    return {
        "schema_version": 1,
        "device": ue.get("device"),
        "stage": failure.stage,
        "detail": failure.detail,
        "selector": selector,
        "release": _helm_snapshot(namespace, _release_name(deployment, ue)),
        "matching_pods": [_pod_summary(pod) for pod in selected],
        "namespace_pods": [_pod_summary(pod) for pod in pods],
        "runtime": [_pod_runtime_evidence(namespace, pod) for pod in selected[:2]],
        "failure_context": failure.evidence,
        "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }


def _write_failure(path: str | None, evidence: dict[str, Any]) -> None:
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def discover(namespace: str) -> list[dict]:
    """Compatibility discovery used by existing source-level contract tests."""
    pods = json.loads(
        subprocess.check_output(
            [
                "kubectl",
                "get",
                "pods",
                "-n",
                namespace,
                "--field-selector=status.phase=Running",
                "-o",
                "json",
            ],
            text=True,
        )
    )
    found: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for pod in pods.get("items", []):
        metadata = pod.get("metadata", {})
        name = str(metadata.get("name", ""))
        labels = metadata.get("labels", {}) or {}
        for container in pod.get("spec", {}).get("containers", []) or []:
            cname = str(container.get("name", ""))
            result = _run(
                [
                    "kubectl",
                    "exec",
                    "-n",
                    namespace,
                    name,
                    "-c",
                    cname,
                    "--",
                    "ip",
                    "-o",
                    "link",
                    "show",
                ]
            )
            if result.returncode:
                continue
            for line in result.stdout.splitlines():
                parts = line.split(":", 2)
                if len(parts) < 2:
                    continue
                interface = parts[1].strip().split("@", 1)[0]
                identity = (namespace, name, interface)
                if not TUNNEL_PATTERN.fullmatch(interface) or identity in seen:
                    continue
                address_result = _run(
                    [
                        "kubectl",
                        "exec",
                        "-n",
                        namespace,
                        name,
                        "-c",
                        cname,
                        "--",
                        "ip",
                        "-4",
                        "-o",
                        "addr",
                        "show",
                        "dev",
                        interface,
                    ]
                )
                addresses = re.findall(r"\binet\s+([0-9.]+)/", address_result.stdout)
                if len(addresses) == 1:
                    seen.add(identity)
                    found.append(
                        {
                            "namespace": namespace,
                            "pod": name,
                            "container": cname,
                            "interface": interface,
                            "address": addresses[0],
                            "pod_labels": labels,
                        }
                    )
    return found


def _matches(candidate: dict, selector: dict) -> bool:
    if candidate["namespace"] != selector.get("namespace"):
        return False
    if candidate["interface"] != selector.get("interface"):
        return False
    prefix = selector.get("pod_name_prefix")
    if prefix and not candidate["pod"].startswith(prefix):
        return False
    labels = selector.get("pod_labels", {})
    return all(candidate.get("pod_labels", {}).get(key) == value for key, value in labels.items())


def _read_remote_config(candidate: dict, path: str) -> str:
    result = _run(
        [
            "kubectl",
            "exec",
            "-n",
            candidate["namespace"],
            candidate["pod"],
            "-c",
            candidate["container"],
            "--",
            "cat",
            path,
        ]
    )
    if result.returncode:
        raise ValueError(
            f"cannot read {path} from {candidate['namespace']}/{candidate['pod']}: "
            f"{result.stderr.strip()}"
        )
    return result.stdout


def _check_config(ue: dict, candidate: dict) -> None:
    path = ue["tunnel"].get("identity_file")
    if not path:
        return
    content = _read_remote_config(candidate, path)
    checks = {
        "IMSI": rf"(?m)^\s*imsi\s*=\s*{re.escape(ue['imsi'])}\s*$",
        "DNN": rf"(?m)^\s*apn\s*=\s*{re.escape(ue['dnn'])}\s*$",
        "interface": rf"(?m)^\s*ip_devname\s*=\s*{re.escape(ue['tunnel']['interface'])}\s*$",
    }
    missing = [label for label, pattern in checks.items() if not re.search(pattern, content)]
    if missing:
        raise ValueError(
            f"{ue['device']} tunnel exists in {candidate['pod']}, but {path} "
            f"does not match its expected {', '.join(missing)}"
        )


def _verify_oai_upf_probe(namespace: str, probe: dict, device: str) -> dict:
    target = str(probe.get("address", ""))
    interface = str(probe.get("interface", "tun0"))
    pods = _run_json(
        [
            "kubectl",
            "get",
            "pods",
            "-n",
            namespace,
            "-l",
            "app.kubernetes.io/name=oai-upf",
            "-o",
            "json",
        ],
        stage="user-plane-target-absent",
        device=device,
    ).get("items", [])
    if len(pods) != 1:
        raise ProbeFailure(
            stage="user-plane-target-ambiguous",
            device=device,
            detail=f"expected exactly one OAI UPF pod, found {len(pods)}",
            evidence={"matching_pods": [_pod_summary(pod) for pod in pods]},
        )
    pod = pods[0]
    summary = _pod_summary(pod)
    if summary["phase"] != "Running" or not summary["ready"]:
        raise ProbeFailure(
            stage="user-plane-target-not-ready",
            device=device,
            detail=f"OAI UPF pod {summary['name']} is not Running/Ready",
            evidence={"pod": summary},
        )
    container = "upf" if "upf" in summary["containers"] else (summary["containers"][0] if summary["containers"] else "")
    if not container:
        raise ProbeFailure(
            stage="user-plane-target-absent",
            device=device,
            detail=f"OAI UPF pod {summary['name']} has no executable container",
            evidence={"pod": summary},
        )
    address_result = _run(
        [
            "kubectl",
            "exec",
            "-n",
            namespace,
            summary["name"],
            "-c",
            container,
            "--",
            "ip",
            "-4",
            "-o",
            "addr",
            "show",
            "dev",
            interface,
        ]
    )
    addresses = re.findall(r"\binet\s+([0-9.]+)/", address_result.stdout)
    if address_result.returncode or target not in addresses:
        raise ProbeFailure(
            stage="user-plane-target-mismatch",
            device=device,
            detail=(
                f"sealed OAI UPF endpoint {target} is not owned by "
                f"{summary['name']}:{interface}"
            ),
            evidence={
                "pod": summary,
                "interface": interface,
                "returncode": address_result.returncode,
                "stdout": _bounded(address_result.stdout),
                "stderr": _bounded(address_result.stderr),
                "addresses": addresses,
            },
        )
    return {
        "pod": summary["name"],
        "container": container,
        "interface": interface,
        "address": target,
    }


def _probe_user_plane(candidate: dict, ue: dict) -> dict:
    probe = ue.get("user_plane_probe", {})
    target = (
        str(probe.get("address", ""))
        if isinstance(probe, dict)
        else ""
    ) or str(ue.get("user_plane_target", ""))
    try:
        ipaddress.ip_address(target)
    except ValueError as exc:
        raise ValueError(
            f"invalid user-plane target in deployment contract for {ue['device']}: {target!r}"
        ) from exc
    target_kind = (
        str(probe.get("kind", "legacy-user-plane-target"))
        if isinstance(probe, dict)
        else "legacy-user-plane-target"
    )
    target_interface = probe.get("interface") if isinstance(probe, dict) else None
    target_runtime = None
    if target_kind == "oai-upf-tun0":
        target_runtime = _verify_oai_upf_probe(
            candidate["namespace"],
            probe,
            str(ue["device"]),
        )
    command = [
        "kubectl",
        "exec",
        "-n",
        candidate["namespace"],
        candidate["pod"],
        "-c",
        candidate["container"],
        "--",
        "ping",
        "-I",
        candidate["interface"],
        "-c",
        "1",
        "-W",
        "3",
        target,
    ]
    result = _run(command)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise ValueError(
            f"{ue['device']} cannot reach selected user-plane endpoint {target} from "
            f"{candidate['interface']}: {detail}"
        )
    return {
        "verified": True,
        "method": "icmp_echo",
        "source_interface": candidate["interface"],
        "source_address": candidate["address"],
        "target_address": target,
        "target_kind": target_kind,
        "target_interface": target_interface,
        "target_runtime": target_runtime,
        "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }


def _validate_candidate(
    ue: dict[str, Any],
    candidate: dict[str, Any],
    used: set[tuple[str, str, str]],
) -> dict[str, Any]:
    identity = (candidate["namespace"], candidate["pod"], candidate["interface"])
    if identity in used:
        raise ValueError(f"multiple devices resolve to the same live tunnel: {identity}")
    try:
        network = ipaddress.ip_network(ue["address_cidr"], strict=False)
        address = ipaddress.ip_address(candidate["address"])
    except ValueError as exc:
        raise ValueError(f"invalid tunnel address contract for {ue['device']}: {exc}") from exc
    if address not in network:
        raise ValueError(
            f"{ue['device']} has {address} on {candidate['interface']}, "
            f"outside its expected slice network {network}"
        )
    _check_config(ue, candidate)
    user_plane = _probe_user_plane(candidate, ue)
    used.add(identity)
    return {
        "device": ue["device"],
        "index": ue["index"],
        "imsi": ue["imsi"],
        "slice": ue["slice"],
        "sst": str(ue["sst"]),
        "sd": str(ue["sd"]),
        "dnn": ue["dnn"],
        "namespace": candidate["namespace"],
        "pod": candidate["pod"],
        "container": candidate["container"],
        "interface": candidate["interface"],
        "address": candidate["address"],
        "software_verified": True,
        "user_plane": user_plane,
    }


def resolve_bindings(
    manifest: dict,
    discovered: list[dict],
    device: str | None = None,
) -> list[dict]:
    """Compatibility resolver for pre-discovered live tunnels."""
    deployment = manifest.get("deployment", {})
    if deployment.get("platform") != "rfsim":
        raise ValueError("software UE validation requires an rfsim deployment")
    expected = deployment.get("ues", [])
    if not expected:
        raise ValueError("deployment identity contains no UEs")
    if device is not None:
        expected = [ue for ue in expected if str(ue.get("device")) == device]
        if len(expected) != 1:
            raise ValueError(
                f"deployment identity must contain exactly one selected UE named {device!r}"
            )
    bindings: list[dict] = []
    used: set[tuple[str, str, str]] = set()

    for ue in expected:
        matches = [candidate for candidate in discovered if _matches(candidate, ue["tunnel"])]
        if len(matches) != 1:
            locations = [
                f"{item['namespace']}/{item['pod']}:{item['interface']}" for item in matches
            ]
            raise ValueError(
                f"expected exactly one live tunnel for {ue['device']} "
                f"({ue['tunnel']}), found {len(matches)}: {locations}"
            )
        bindings.append(_validate_candidate(ue, matches[0], used))
    return bindings


def _select_expected(manifest: dict[str, Any], device: str | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    deployment = manifest.get("deployment", {})
    if deployment.get("platform") != "rfsim":
        raise ProbeFailure(
            stage="contract",
            device=device or "<all>",
            detail="software UE validation requires an rfsim deployment",
        )
    expected = deployment.get("ues", [])
    if not expected:
        raise ProbeFailure(stage="contract", device=device or "<all>", detail="deployment identity contains no UEs")
    if device is not None:
        expected = [ue for ue in expected if str(ue.get("device")) == device]
        if len(expected) != 1:
            raise ProbeFailure(
                stage="contract",
                device=device,
                detail=f"deployment identity must contain exactly one selected UE named {device!r}",
            )
    return deployment, expected


def _candidate_from_runtime(
    *,
    deployment: dict[str, Any],
    namespace: str,
    ue: dict[str, Any],
    pods: list[dict[str, Any]],
) -> dict[str, Any]:
    device = str(ue["device"])
    selector = ue["tunnel"]
    release = _release_name(deployment, ue)
    helm = _helm_snapshot(namespace, release)
    if helm.get("checked") and helm.get("returncode") != 0:
        raise ProbeFailure(
            stage="helm-release-absent",
            device=device,
            detail=f"expected Helm release {release!r} is not readable in namespace {namespace}",
            evidence={"release": helm},
        )
    if helm.get("checked") and helm.get("status") != "deployed":
        raise ProbeFailure(
            stage="helm-release-not-deployed",
            device=device,
            detail=f"Helm release {release!r} status is {helm.get('status')!r}, expected 'deployed'",
            evidence={"release": helm},
        )

    matching = [pod for pod in pods if _selector_matches_pod(pod, selector)]
    if not matching:
        raise ProbeFailure(
            stage="pod-absent",
            device=device,
            detail=f"no pod matches selector {selector}",
        )
    if len(matching) != 1:
        raise ProbeFailure(
            stage="pod-ambiguous",
            device=device,
            detail=f"{len(matching)} pods match selector {selector}",
            evidence={"matching_pods": [_pod_summary(pod) for pod in matching]},
        )

    pod = matching[0]
    summary = _pod_summary(pod)
    if summary["phase"] != "Running":
        raise ProbeFailure(
            stage="pod-not-running",
            device=device,
            detail=f"pod {summary['name']} phase is {summary['phase']!r}",
            evidence={"pod": summary},
        )
    if not summary["ready"]:
        raise ProbeFailure(
            stage="pod-not-ready",
            device=device,
            detail=f"pod {summary['name']} is Running but Ready is false",
            evidence={"pod": summary},
        )
    if not summary["containers"]:
        raise ProbeFailure(
            stage="container-absent",
            device=device,
            detail=f"pod {summary['name']} contains no containers",
            evidence={"pod": summary},
        )

    interface = str(selector["interface"])
    successful_exec = 0
    interface_matches: list[tuple[str, subprocess.CompletedProcess[str]]] = []
    exec_errors: dict[str, Any] = {}
    observed_links: dict[str, str] = {}
    for container in summary["containers"]:
        result = _run(
            [
                "kubectl",
                "exec",
                "-n",
                namespace,
                summary["name"],
                "-c",
                container,
                "--",
                "ip",
                "-o",
                "link",
                "show",
            ]
        )
        if result.returncode == 0:
            successful_exec += 1
            observed_links[container] = _bounded(result.stdout)
            interfaces = []
            for line in result.stdout.splitlines():
                parts = line.split(":", 2)
                if len(parts) >= 2:
                    interfaces.append(parts[1].strip().split("@", 1)[0])
            if interface in interfaces:
                interface_matches.append((container, result))
        else:
            exec_errors[container] = {
                "returncode": result.returncode,
                "stdout": _bounded(result.stdout),
                "stderr": _bounded(result.stderr),
            }

    if not interface_matches:
        stage = "exec-failed" if successful_exec == 0 else "tunnel-absent"
        detail = (
            f"cannot execute interface probe in any container of pod {summary['name']}"
            if stage == "exec-failed"
            else f"expected interface {interface!r} is absent from pod {summary['name']}"
        )
        raise ProbeFailure(
            stage=stage,
            device=device,
            detail=detail,
            evidence={
                "pod": summary,
                "exec_errors": exec_errors,
                "observed_links": observed_links,
            },
        )
    if len(interface_matches) != 1:
        raise ProbeFailure(
            stage="tunnel-ambiguous",
            device=device,
            detail=f"interface {interface!r} exists in {len(interface_matches)} containers",
            evidence={"containers": [name for name, _ in interface_matches]},
        )

    container = interface_matches[0][0]
    address_result = _run(
        [
            "kubectl",
            "exec",
            "-n",
            namespace,
            summary["name"],
            "-c",
            container,
            "--",
            "ip",
            "-4",
            "-o",
            "addr",
            "show",
            "dev",
            interface,
        ]
    )
    if address_result.returncode:
        raise ProbeFailure(
            stage="address-query-failed",
            device=device,
            detail=f"cannot query IPv4 address for {interface!r} in {summary['name']}/{container}",
            evidence={
                "returncode": address_result.returncode,
                "stdout": _bounded(address_result.stdout),
                "stderr": _bounded(address_result.stderr),
            },
        )
    addresses = re.findall(r"\binet\s+([0-9.]+)/", address_result.stdout)
    if not addresses:
        raise ProbeFailure(
            stage="tunnel-no-ipv4",
            device=device,
            detail=f"interface {interface!r} has no IPv4 address",
            evidence={"address_output": _bounded(address_result.stdout)},
        )
    if len(addresses) != 1:
        raise ProbeFailure(
            stage="tunnel-multiple-ipv4",
            device=device,
            detail=f"interface {interface!r} has {len(addresses)} IPv4 addresses, expected exactly one",
            evidence={"addresses": addresses},
        )

    return {
        "namespace": namespace,
        "pod": summary["name"],
        "container": container,
        "interface": interface,
        "address": addresses[0],
        "pod_labels": (pod.get("metadata", {}) or {}).get("labels", {}) or {},
    }


def probe_selected_bindings(
    manifest: dict[str, Any],
    *,
    device: str | None = None,
    evidence_path: str | None = None,
) -> list[dict[str, Any]]:
    deployment, expected = _select_expected(manifest, device)
    namespace = str(manifest["deployment"]["topology"]["namespace"])
    pods_doc = _run_json(
        ["kubectl", "get", "pods", "-n", namespace, "-o", "json"],
        stage="pod-list-failed",
        device=device or "<all>",
    )
    pods = pods_doc.get("items", [])
    if not isinstance(pods, list):
        raise ProbeFailure(
            stage="pod-list-failed",
            device=device or "<all>",
            detail="kubectl pod list JSON does not contain an items list",
        )

    bindings: list[dict[str, Any]] = []
    used: set[tuple[str, str, str]] = set()
    for ue in expected:
        try:
            candidate = _candidate_from_runtime(
                deployment=deployment,
                namespace=namespace,
                ue=ue,
                pods=pods,
            )
            try:
                binding = _validate_candidate(ue, candidate, used)
            except ValueError as exc:
                detail = str(exc)
                if "outside its expected slice network" in detail:
                    stage = "slice-address-mismatch"
                elif "does not match its expected" in detail or "cannot read" in detail:
                    stage = "identity-mismatch"
                elif (
                    "cannot reach selected UPF" in detail
                    or "cannot reach selected user-plane endpoint" in detail
                ):
                    stage = "user-plane-failed"
                else:
                    stage = "binding-validation-failed"
                raise ProbeFailure(
                    stage=stage,
                    device=str(ue["device"]),
                    detail=detail,
                ) from exc
            bindings.append(binding)
        except ProbeFailure as failure:
            evidence = _failure_evidence(
                deployment=deployment,
                namespace=namespace,
                ue=ue,
                pods=pods,
                failure=failure,
            )
            _write_failure(evidence_path, evidence)
            raise ProbeFailure(
                stage=failure.stage,
                device=failure.device,
                detail=failure.detail,
                evidence=evidence,
            ) from failure
    return bindings


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected", required=True)
    parser.add_argument(
        "--device",
        help="validate only one selected logical UE while preserving the full deployment identity",
    )
    parser.add_argument(
        "--evidence",
        help="write bounded structured failure evidence to this path before exiting nonzero",
    )
    args = parser.parse_args(argv)
    manifest = json.loads(Path(args.expected).read_text(encoding="utf-8"))
    try:
        bindings = probe_selected_bindings(
            manifest,
            device=args.device,
            evidence_path=args.evidence,
        )
    except (KeyError, ProbeFailure, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        if isinstance(exc, ProbeFailure):
            raise SystemExit(
                f"live software UE acceptance check failed at stage={exc.stage} "
                f"device={exc.device}: {exc.detail}"
                + (f"; evidence={args.evidence}" if args.evidence else "")
            ) from exc
        raise SystemExit(f"live software UE acceptance check failed: {exc}") from exc
    print(json.dumps(bindings, sort_keys=True))


if __name__ == "__main__":
    main()
