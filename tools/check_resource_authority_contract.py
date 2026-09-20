#!/usr/bin/env python3
"""Prove original upstream cannot reacquire SynthRAN-owned resources."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "third_party/sopnode-5g-ansible/EXECUTION_REFERENCE.json"
EXPECTED_REPOSITORY = "https://github.com/sopnode/5g_ansible"
FORBIDDEN_ENTRYPOINTS = {
    "playbooks/deploy_r2lab.yml",
    "playbooks/deploy.yml",
    "playbooks/run_pos.yml",
}


class ContractError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(value, dict), f"expected JSON object: {path}")
    return value


def text(path: Path) -> str:
    require(path.is_file(), f"missing required file: {path}")
    return path.read_text(encoding="utf-8")


def git_head(reference: Path) -> str:
    return subprocess.check_output(
        ["git", "-C", str(reference), "rev-parse", "HEAD"], text=True
    ).strip()


def verify_upstream_pos_boundary(reference: Path) -> dict[str, Any]:
    pos = text(reference / "roles/pos/tasks/main.yml")
    free_at = pos.find("pos allocations free")
    allocate_at = pos.find("pos allocations allocate")
    boot_guard_at = pos.find("when: should_boot")
    require(free_at >= 0, "original upstream POS no longer frees allocations; re-audit")
    require(allocate_at >= 0, "original upstream POS no longer allocates; re-audit")
    require(boot_guard_at >= 0, "original upstream POS no_boot/should_boot shape changed; re-audit")
    require(free_at < boot_guard_at and allocate_at < boot_guard_at, "upstream allocation unexpectedly moved behind boot guard; re-audit")
    require("pos_manage_allocation" not in pos, "upstream gained an allocation suppression surface; re-audit")

    deploy = text(reference / "playbooks/deploy.yml")
    require("playbooks/run_pos.yml" in deploy, "original upstream deploy no longer invokes POS; re-audit")
    r2lab = text(reference / "playbooks/deploy_r2lab.yml")
    for role in ("r2lab/cleanup", "r2lab/rru", "r2lab/ue/setup"):
        require(role in r2lab, f"original upstream R2Lab ownership changed: missing {role}")

    return {
        "allocation_free_before_boot_guard": True,
        "allocation_allocate_before_boot_guard": True,
        "allocation_suppression_surface": False,
        "full_deploy_invokes_pos": True,
        "deploy_r2lab_owns_external_state": True,
    }


def verify_local_delegation(contract: dict[str, Any], reference: Path) -> dict[str, Any]:
    authority = contract.get("external_resource_authority", {})
    require(authority.get("owner") == "synthran", "execution contract no longer names SynthRAN as resource authority")
    configured_forbidden = set(authority.get("forbidden_full_upstream_entrypoints", []))
    require(configured_forbidden == FORBIDDEN_ENTRYPOINTS, "forbidden upstream entrypoint set changed")

    policy = contract.get("delegation_policy", {})
    require(policy.get("mode") == "explicit-reviewed-task-files-only", "selective delegation policy changed")
    require(policy.get("full_upstream_playbooks_allowed") is False, "full upstream playbooks were re-enabled")

    allowed = set(policy.get("allowed_reference_task_files", []))
    require(bool(allowed), "no reviewed upstream task files are declared")
    for relative in allowed:
        require((reference / relative).is_file(), f"declared delegated task file missing upstream: {relative}")

    # Runtime code may mention the reference checkout and individual role task
    # files, but it must never launch the full original-upstream entrypoints.
    offenders: list[str] = []
    roots = (ROOT / "deployment", ROOT / "synthran")
    extensions = {".yml", ".yaml", ".py", ".sh"}
    for root in roots:
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in extensions:
                continue
            source = path.read_text(encoding="utf-8", errors="replace")
            for entrypoint in FORBIDDEN_ENTRYPOINTS:
                # Local playbooks with the same basename are allowed; what is
                # forbidden is resolving/calling these paths from the pinned
                # upstream checkout.
                if entrypoint in source and (
                    "synthran_reference_root" in source
                    or "reference_root" in source
                    or ".synthran/reference/sopnode-5g-ansible" in source
                ):
                    offenders.append(f"{path.relative_to(ROOT)} -> {entrypoint}")
    require(not offenders, "runtime can invoke forbidden full upstream entrypoints: " + "; ".join(sorted(offenders)))

    checkout = text(ROOT / "synthran/reference_checkout.py")
    require("EXECUTION_REFERENCE.json" in checkout, "shared checkout bypasses execution reference")
    require("git" in checkout and "fetch" in checkout, "shared checkout no longer materializes immutable Git reference")

    return {
        "mode": policy["mode"],
        "full_upstream_playbooks_allowed": False,
        "allowed_task_files": sorted(allowed),
        "runtime_forbidden_entrypoint_offenders": [],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", required=True, type=Path)
    args = parser.parse_args()
    reference = args.reference.expanduser().resolve()
    contract = load_json(CONTRACT)

    require(contract.get("repository") == EXPECTED_REPOSITORY, "execution authority is not original sopnode/5g_ansible")
    expected = str(contract.get("commit", ""))
    actual = git_head(reference)
    require(actual == expected, f"reference commit mismatch: expected {expected}, got {actual}")

    result = {
        "schema": "synthran/resource-authority-contract-check/v2",
        "reference_repository": contract["repository"],
        "reference_commit": actual,
        "owner": "synthran",
        "upstream": verify_upstream_pos_boundary(reference),
        "delegation": verify_local_delegation(contract, reference),
        "result": "pass",
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ContractError, OSError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"resource-authority-contract: {exc}") from exc
