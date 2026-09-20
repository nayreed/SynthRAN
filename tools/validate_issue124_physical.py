#!/usr/bin/env python3
"""Validate issue #124 against two physical R2Lab deployment runs.

Task 13 requires two distinct post-refactor proofs:

1. a preserve-mode run that fails SOP preflight before any RRU/UE mutation;
2. a bootstrap-mode OAI/OAI R2Lab N320 run that reaches accepted-testbed.

The checker is intentionally artifact-only: it never contacts the testbed and it
never treats static CI as a substitute for physical evidence.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

DEFAULT_UES = ("qhat01", "qhat03")
MUTATION_MARKERS = (
    "Power OFF only the selected RRU",
    'Power ON "n320"',
    "Power OFF selected QHAT UEs",
    "Power ON selected QHAT UEs",
    "Initialize the selected UE DNN and S-NSSAI",
    "Detach the prepared MBIM UE before RAN attachment",
)
PHYSICAL_PHASES = {"r2lab_rru_setup", "ue_setup"}
FRESH_ONLY_PHASES = {"pos_image_staging", "pos_reset"}


class ValidationError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValidationError(f"required artifact is missing: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"artifact is unreadable: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"artifact must contain a JSON object: {path}")
    return value


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ValidationError(f"artifact is unreadable: {path}: {exc}") from exc


def controller_revision(run: Path) -> str:
    provenance = read_json(run / "provenance" / "controller.json")
    source = provenance.get("source")
    require(isinstance(source, dict), f"{run}: controller provenance has no source mapping")
    revision = str(source.get("revision", "")).strip()
    require(revision, f"{run}: controller provenance has no source revision")
    require(
        source.get("dirty_worktree") is False,
        f"{run}: physical validation requires a clean source worktree",
    )
    source_file = run / "source-revision.txt"
    if source_file.is_file():
        require(
            read_text(source_file).strip() == revision,
            f"{run}: source-revision.txt disagrees with controller provenance",
        )
    return revision


def ancestor_or_equal(expected: str, observed: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", expected, observed],
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def require_revision(run: Path, expected: str) -> str:
    observed = controller_revision(run)
    require(
        ancestor_or_equal(expected, observed),
        f"{run}: source revision {observed} predates required baseline {expected}",
    )
    return observed


def exit_code(run: Path) -> int:
    raw = read_text(run / "controller-exit-code").strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise ValidationError(f"{run}: invalid controller-exit-code {raw!r}") from exc


def timing_records(run: Path) -> list[dict[str, Any]]:
    artifact = read_json(run / "phase-timings.json")
    require(
        artifact.get("schema") == "synthran/phase-timings/v1",
        f"{run}: unsupported phase timing schema",
    )
    records = artifact.get("records")
    require(isinstance(records, list), f"{run}: phase timing records are missing")
    result = [item for item in records if isinstance(item, dict)]
    require(len(result) == len(records), f"{run}: malformed phase timing record")
    return result


def timing_status(
    records: list[dict[str, Any]],
    phase: str,
    *,
    scope: str | None = None,
) -> list[str]:
    return [
        str(item.get("status", ""))
        for item in records
        if item.get("phase") == phase
        and (scope is None or item.get("scope") == scope)
    ]


def host_preflight(run: Path, host: str) -> dict[str, Any]:
    return read_json(run / f"sop-preflight-{host}.json")


def validate_preserve_failure(run: Path, expected_revision: str) -> dict[str, Any]:
    revision = require_revision(run, expected_revision)
    require(exit_code(run) != 0, f"{run}: negative preserve proof unexpectedly exited 0")

    authority = read_json(run / "reservation-authority.json")
    policies = authority.get("policies")
    require(isinstance(policies, dict), f"{run}: reservation policies are missing")
    require(
        policies.get("host_preparation") == "preserve",
        f"{run}: negative proof is not a preserve-mode deployment",
    )
    selected = authority.get("selected_nodes")
    require(isinstance(selected, dict), f"{run}: selected SOP role mapping is missing")
    require(
        selected.get("core") == "sopnode-f2" and selected.get("ran") == "sopnode-f3",
        f"{run}: negative proof must use split sopnode-f2/sopnode-f3",
    )

    preflights = {
        host: host_preflight(run, host)
        for host in ("sopnode-f2", "sopnode-f3")
    }
    unhealthy: dict[str, list[str]] = {}
    for host, evidence in preflights.items():
        services = evidence.get("services") if isinstance(evidence.get("services"), dict) else {}
        cni = evidence.get("cni") if isinstance(evidence.get("cni"), dict) else {}
        network = evidence.get("network") if isinstance(evidence.get("network"), dict) else {}
        reasons: list[str] = []
        if int(services.get("containerd_rc", 1)) != 0:
            reasons.append("containerd")
        if int(services.get("kubelet_rc", 1)) != 0:
            reasons.append("kubelet")
        if int(services.get("cni_dhcp_rc", 1)) != 0:
            reasons.append("cni-dhcp-service")
        if cni.get("dhcp_binary_exists") is not True or cni.get("dhcp_binary_executable") is not True:
            reasons.append("cni-dhcp-binary")
        if int(services.get("ovsdb_server_rc", 1)) != 0 or int(services.get("ovs_vswitchd_rc", 1)) != 0:
            reasons.append("ovs")
        if str(network.get("ip_forward", "")) != "1":
            reasons.append("ip-forward")
        if reasons:
            unhealthy[host] = reasons
    require(
        unhealthy,
        f"{run}: preserve failure does not demonstrate an incomplete SOP prerequisite state",
    )

    log = read_text(run / "ansible.log")
    require(
        "Preflight selected SOP hosts before physical testbed mutation" in log,
        f"{run}: SOP preflight evidence is not visible in ansible.log",
    )
    for marker in MUTATION_MARKERS:
        require(
            marker not in log,
            f"{run}: preserve failure reached forbidden physical mutation task {marker!r}",
        )

    timings = timing_records(run)
    physical = {
        item.get("phase")
        for item in timings
        if item.get("phase") in PHYSICAL_PHASES
    }
    require(
        not physical,
        f"{run}: preserve failure started physical R2Lab phase(s): {sorted(physical)}",
    )
    require(
        all(item.get("phase") not in FRESH_ONLY_PHASES for item in timings),
        f"{run}: preserve failure contains fresh-only POS timing evidence",
    )

    fingerprint_path = run / "deployment-fingerprint.json"
    if fingerprint_path.is_file():
        fingerprint = read_json(fingerprint_path)
        require(
            fingerprint.get("status") != "accepted-testbed",
            f"{run}: negative preserve run must not be accepted",
        )

    return {
        "run": run.name,
        "revision": revision,
        "host_preparation": "preserve",
        "controller_exit_code": exit_code(run),
        "preflight_failure_reasons": unhealthy,
        "physical_mutation_started": False,
        "status": "passed",
    }


def validate_bootstrap_success(
    run: Path,
    expected_revision: str,
    expected_ues: list[str],
) -> dict[str, Any]:
    revision = require_revision(run, expected_revision)
    require(exit_code(run) == 0, f"{run}: bootstrap proof did not exit successfully")

    authority = read_json(run / "reservation-authority.json")
    policies = authority.get("policies")
    require(isinstance(policies, dict), f"{run}: reservation policies are missing")
    require(
        policies.get("host_preparation") == "bootstrap",
        f"{run}: positive proof is not bootstrap mode",
    )
    selected = authority.get("selected_nodes")
    require(isinstance(selected, dict), f"{run}: selected SOP role mapping is missing")
    require(
        selected.get("core") == "sopnode-f2" and selected.get("ran") == "sopnode-f3",
        f"{run}: positive proof must use split sopnode-f2/sopnode-f3",
    )
    host_prep = authority.get("host_preparation")
    require(isinstance(host_prep, dict), f"{run}: host-preparation evidence is missing")
    require(host_prep.get("mode") == "bootstrap", f"{run}: reservation result lost bootstrap mode")
    require(
        host_prep.get("mutations") == [],
        f"{run}: reservation layer claims fresh/destructive host mutations under bootstrap",
    )

    classifications: dict[str, str] = {}
    for host in ("sopnode-f2", "sopnode-f3"):
        classification = read_json(run / f"sop-bootstrap-classification-{host}.json")
        value = str(classification.get("classification", ""))
        require(
            value in {"healthy", "repairable-in-place", "reboot-required"},
            f"{run}: {host} bootstrap classification is not physically acceptable: {value!r}",
        )
        classifications[host] = value
        host_preflight(run, host)

    bootstrap = read_json(run / "bootstrap-evidence.json")
    require(
        bootstrap.get("host_preparation") == "bootstrap",
        f"{run}: bootstrap evidence does not describe bootstrap mode",
    )

    identity = read_json(run / "deployment-fingerprint.json")
    require(
        identity.get("status") == "accepted-testbed",
        f"{run}: deployment did not reach accepted-testbed state",
    )
    deployment = identity.get("deployment")
    require(isinstance(deployment, dict), f"{run}: accepted deployment mapping is missing")
    require(deployment.get("core") == "oai", f"{run}: Task 13 requires OAI core")
    require(deployment.get("ran") == "oai", f"{run}: Task 13 requires OAI RAN")
    require(deployment.get("platform") == "r2lab", f"{run}: Task 13 requires R2Lab")
    require(deployment.get("radio_unit") == "n320", f"{run}: Task 13 requires N320")
    require(
        deployment.get("host_preparation") == "bootstrap",
        f"{run}: accepted identity lost bootstrap preparation policy",
    )
    nodes = deployment.get("nodes")
    require(isinstance(nodes, dict), f"{run}: accepted node mapping is missing")
    require(
        nodes.get("core") == "sopnode-f2" and nodes.get("ran") == "sopnode-f3",
        f"{run}: accepted identity does not preserve split SOP roles",
    )

    contracts = deployment.get("ues")
    require(isinstance(contracts, list), f"{run}: accepted UE contracts are missing")
    contract_devices = {str(item.get("device")) for item in contracts if isinstance(item, dict)}
    require(
        set(expected_ues).issubset(contract_devices),
        f"{run}: accepted deployment is missing expected UE(s) {sorted(set(expected_ues)-contract_devices)}",
    )

    evidence = read_json(run / "live-deployment-evidence.json")
    require(
        evidence.get("cluster_identity_verified") is True,
        f"{run}: live evidence does not verify cluster identity",
    )
    bindings = evidence.get("bindings")
    require(isinstance(bindings, list), f"{run}: live UE bindings are missing")
    by_device = {
        str(item.get("device")): item
        for item in bindings
        if isinstance(item, dict) and item.get("device") is not None
    }
    for device in expected_ues:
        binding = by_device.get(device)
        require(binding is not None, f"{run}: live binding is missing for {device}")
        require(
            binding.get("modem_verified") is True,
            f"{run}: physical modem identity is not verified for {device}",
        )
        user_plane = binding.get("user_plane")
        require(isinstance(user_plane, dict), f"{run}: user-plane evidence is missing for {device}")
        require(
            user_plane.get("verified") is True,
            f"{run}: source-bound user plane is not verified for {device}",
        )

    timings = timing_records(run)
    for phase in (
        "kubernetes_bootstrap",
        "r2lab_rru_setup",
        "ue_setup",
        "stack_deployment",
        "verification",
    ):
        statuses = timing_status(timings, phase)
        require(statuses, f"{run}: timing evidence is missing phase {phase}")
        require(
            statuses[-1] == "success",
            f"{run}: timing phase {phase} did not complete successfully: {statuses}",
        )
    for item in timings:
        require(
            item.get("phase") not in FRESH_ONLY_PHASES,
            f"{run}: bootstrap proof contains fresh-only phase {item.get('phase')}",
        )

    return {
        "run": run.name,
        "revision": revision,
        "host_preparation": "bootstrap",
        "classification": classifications,
        "deployment": {
            "core": "oai",
            "ran": "oai",
            "platform": "r2lab",
            "radio_unit": "n320",
            "core_node": "sopnode-f2",
            "ran_node": "sopnode-f3",
            "ues": expected_ues,
        },
        "accepted_testbed": True,
        "status": "passed",
    }


def git_head() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise ValidationError("cannot resolve current Git revision")
    return result.stdout.strip()


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--preserve-failure", type=Path, required=True)
    p.add_argument("--bootstrap-success", type=Path, required=True)
    p.add_argument("--expected-revision", default=git_head())
    p.add_argument(
        "--expected-ue",
        action="append",
        default=[],
        help="Expected physical UE device; repeat as needed. Defaults to qhat01 and qhat03.",
    )
    p.add_argument(
        "--output",
        type=Path,
        help="Output report path (default: <bootstrap-success>/issue124-physical-validation.json)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    preserve = args.preserve_failure.resolve()
    bootstrap = args.bootstrap_success.resolve()
    require(preserve != bootstrap, "Task 13 requires distinct negative and positive physical runs")
    expected_ues = args.expected_ue or list(DEFAULT_UES)

    negative = validate_preserve_failure(preserve, args.expected_revision)
    positive = validate_bootstrap_success(
        bootstrap,
        args.expected_revision,
        expected_ues,
    )
    report = {
        "schema": "synthran/issue124-physical-validation/v1",
        "required_revision": args.expected_revision,
        "preserve_failure": negative,
        "bootstrap_success": positive,
        "task13": "passed",
    }
    output = args.output or (bootstrap / "issue124-physical-validation.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValidationError as exc:
        raise SystemExit(f"issue124-physical-validation: {exc}") from exc
