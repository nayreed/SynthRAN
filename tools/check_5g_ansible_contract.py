#!/usr/bin/env python3
"""Verify the pinned original sopnode/5g_ansible source/task contract."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "third_party/sopnode-5g-ansible/EXECUTION_REFERENCE.json"
EXPECTED_REPOSITORY = "https://github.com/sopnode/5g_ansible"


class ContractError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def text(path: Path) -> str:
    require(path.is_file(), f"missing required upstream path: {path}")
    return path.read_text(encoding="utf-8")


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(value, dict), f"expected JSON object: {path}")
    return value


def git_head(reference: Path) -> str:
    return subprocess.check_output(
        ["git", "-C", str(reference), "rev-parse", "HEAD"], text=True
    ).strip()


def verify_profiles(reference: Path, contract: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for profile in contract["required_capabilities"]["profiles"]:
        path = reference / "group_vars/all" / f"5g_profile_{profile}.yaml"
        document = yaml.safe_load(text(path))
        require(isinstance(document, dict), f"invalid upstream profile: {profile}")
        require(isinstance(document.get("plmn"), dict), f"{profile}: PLMN missing")
        require(isinstance(document.get("slices"), list) and document["slices"], f"{profile}: slices missing")
        require(isinstance(document.get("ues"), dict) and document["ues"], f"{profile}: UEs missing")
        summary[profile] = {
            "slice_names": [item.get("name") for item in document["slices"]],
            "ue_count": len(document["ues"]),
        }

    default = yaml.safe_load(text(reference / "group_vars/all/5g_profile_default.yaml"))
    for ue in contract["required_capabilities"]["r2lab_probe_ues"]["qhats"]:
        require(ue in default["ues"], f"default upstream profile lost probe QHAT {ue}")
    for ue in contract["required_capabilities"]["r2lab_probe_ues"]["qfits"]:
        require(ue in default["ues"], f"default upstream profile lost probe QFIT {ue}")
    return summary


def verify_upstream_call_graph(reference: Path) -> dict[str, Any]:
    deploy_r2lab = text(reference / "playbooks/deploy_r2lab.yml")
    for role in ("r2lab/cleanup", "r2lab/rru", "r2lab/ue/setup"):
        require(role in deploy_r2lab, f"upstream R2Lab preparation lost role {role}")

    deploy = text(reference / "playbooks/deploy.yml")
    require("playbooks/run_pos.yml" in deploy, "upstream deploy no longer invokes POS preparation")
    for marker in (
        "5g/open5gs/config",
        "5g/open5gs/deploy",
        "5g/free5gc/config",
        "5g/free5gc/deploy",
        "5g/oai/core",
        "5g/oai/ran",
        "5g/srsRAN/config",
        "5g/srsRAN/deploy",
        "5g/ueransim/config",
        "5g/ueransim/deploy",
    ):
        require(marker in deploy, f"upstream deploy lost downstream role marker {marker}")

    connect = text(reference / "playbooks/test-ue-connect.yml")
    require("r2lab/ue/connect" in connect, "upstream UE-connect playbook lost connect role")
    for group in ("groups['qhats']", "groups['qfits']", "groups['phones']"):
        require(group in connect, f"upstream UE-connect playbook lost {group}")
    require("ignore_errors: true" in connect, "upstream UE-connect failure policy changed; re-audit")

    return {
        "deploy_r2lab_owns": ["cleanup", "rru", "ue_setup"],
        "deploy_invokes_pos": True,
        "ue_connect_separate": True,
        "ue_connect_ignores_individual_errors": True,
    }


def verify_resource_boundary(reference: Path, contract: dict[str, Any]) -> dict[str, Any]:
    policy = contract.get("delegation_policy", {})
    require(policy.get("mode") == "explicit-reviewed-task-files-only", "delegation mode changed")
    require(policy.get("full_upstream_playbooks_allowed") is False, "full upstream playbooks were re-enabled")

    pos = text(reference / "roles/pos/tasks/main.yml")
    require("pos allocations free" in pos, "upstream POS free behavior changed; re-audit")
    require("pos allocations allocate" in pos, "upstream POS allocation behavior changed; re-audit")
    require('should_boot: "{{ not (no_boot | default(false) | bool) }}"' in pos, "upstream no_boot contract changed")
    require("pos_manage_allocation" not in pos, "upstream gained an allocation suppression surface; re-audit boundary")

    for relative in policy.get("allowed_reference_task_files", []):
        require((reference / relative).is_file(), f"allowed delegated upstream task missing: {relative}")

    forbidden = contract.get("external_resource_authority", {}).get("forbidden_full_upstream_entrypoints", [])
    require(set(forbidden) == {"playbooks/deploy_r2lab.yml", "playbooks/deploy.yml", "playbooks/run_pos.yml"}, "forbidden entrypoint set changed")

    return {
        "owner": "synthran",
        "upstream_pos_reacquires": True,
        "full_playbooks_allowed": False,
        "delegated_task_count": len(policy.get("allowed_reference_task_files", [])),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", required=True, type=Path)
    args = parser.parse_args()
    reference = args.reference.expanduser().resolve()

    contract = load_json(CONTRACT)
    require(contract.get("schema") == "synthran/5g-ansible-execution-reference/v2", "unexpected execution-reference schema")
    require(contract.get("repository") == EXPECTED_REPOSITORY, "execution reference is not original sopnode/5g_ansible")

    expected = str(contract.get("commit", ""))
    require(len(expected) == 40 and all(ch in "0123456789abcdef" for ch in expected.lower()), "invalid execution-reference commit")
    actual = git_head(reference)
    require(actual == expected, f"reference checkout mismatch: expected {expected}, found {actual}")

    for relative in contract.get("required_paths", []):
        require((reference / relative).is_file(), f"required original-upstream path missing: {relative}")

    # These were fork-only surfaces that caused the original Sub 02 contract to
    # point at the wrong authority. Their absence is intentional in pure upstream.
    require(not (reference / "bin/fiveg").exists(), "pinned upstream unexpectedly contains fork-style bin/fiveg; re-audit")
    require(not (reference / "tools/fiveg_machine.py").exists(), "pinned upstream unexpectedly contains fork-style fiveg_machine.py; re-audit")

    result = {
        "schema": "synthran/5g-ansible-contract-check/v2",
        "repository": contract["repository"],
        "reference_commit": actual,
        "execution_model": contract.get("execution_model"),
        "machine_interface": "absent-by-design",
        "profiles": verify_profiles(reference, contract),
        "call_graph": verify_upstream_call_graph(reference),
        "resource_boundary": verify_resource_boundary(reference, contract),
        "result": "pass",
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ContractError, OSError, json.JSONDecodeError, subprocess.CalledProcessError, yaml.YAMLError) as exc:
        raise SystemExit(f"5g-ansible-contract: {exc}") from exc
