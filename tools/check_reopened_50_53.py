#!/usr/bin/env python3
"""Regression checks for the reopened #50 and #53 findings."""

from __future__ import annotations

import copy
from pathlib import Path
import shutil
import sys
import tempfile

import yaml


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from synthran.deployment_state import resolve_scenario
from synthran.scenario import load_scenario


def fail(message: str) -> None:
    raise SystemExit(message)


def expect_value_error(call, expected: str) -> None:
    try:
        call()
    except ValueError as error:
        if expected not in str(error):
            fail(f"expected ValueError containing {expected!r}, got: {error}")
    else:
        fail(f"expected ValueError containing {expected!r}")


def named_task(tasks: list[dict], name: str) -> dict:
    for task in tasks:
        if task.get("name") == name:
            return task
    fail(f"missing task: {name}")
    raise AssertionError("unreachable")


def when_text(task: dict) -> str:
    value = task.get("when", "")
    if isinstance(value, list):
        return "\n".join(str(item) for item in value)
    return str(value)


def check_issue_50() -> None:
    deploy = (ROOT / "deploy.sh").read_text(encoding="utf-8")
    authoritative_resolve = '--source "$SOURCE_CONFIG" --output "$CONFIG"'
    if authoritative_resolve not in deploy:
        fail("#50: deploy.sh no longer resolves the original source into the private snapshot")
    if deploy.index(authoritative_resolve) >= deploy.index("deployment/scripts/reserve_sop.py"):
        fail("#50: authoritative source resolution must complete before SOP reservation mutation")

    with tempfile.TemporaryDirectory() as temporary:
        fixture = Path(temporary)
        profile = fixture / "profile.yml"
        catalog = fixture / "catalog.yml"
        topology = fixture / "topology.yml"
        shutil.copyfile(
            ROOT / "deployment/group_vars/all/network_profile_default.yaml", profile
        )
        shutil.copyfile(ROOT / "deployment/group_vars/all/ue_catalog.yaml", catalog)
        shutil.copyfile(ROOT / "deployment/topology.yml", topology)

        source = fixture / "scenario.yml"
        resolved = fixture / "resolved.yml"
        scenario = {
            "deployment": {
                "core": "oai",
                "ran": "srsran",
                "platform": "rfsim",
                "network_profile": "default",
                "network_profile_file": "profile.yml",
                "ue_catalog_file": "catalog.yml",
                "topology_file": "topology.yml",
                "ues": ["uesim01"],
                "ue_slices": {"uesim01": "slice1"},
                "nodes": {"core": "sopnode-f2", "ran": "sopnode-f3"},
                "reservation": {"mode": "disabled", "host_preparation": "preserve"},
                "provider": {"mode": "disabled"},
                "r2lab_reservation": {"mode": "disabled"},
            },
            "model": {"private_science": True},
            "mqtt": {"password": "must-not-leak"},
            "devices": {"private": True},
        }
        source.write_text(yaml.safe_dump(scenario, sort_keys=False), encoding="utf-8")

        first = resolve_scenario(source, resolved)
        if set(first) != {"deployment"}:
            fail("#50: resolved deployment snapshot leaked non-deployment sections")
        deployment = first["deployment"]
        for key, expected in (
            ("network_profile_file", profile),
            ("ue_catalog_file", catalog),
            ("topology_file", topology),
        ):
            if Path(deployment[key]) != expected.resolve():
                fail(f"#50: {key} was not normalized relative to the original source")

        reloaded = load_scenario(resolved, deployment_only=True)
        reloaded.pop("_source_directory", None)
        if reloaded != first:
            fail("#50: relocated resolved deployment cannot be loaded equivalently")

        absolute = copy.deepcopy(scenario)
        absolute["deployment"]["network_profile_file"] = str(profile.resolve())
        absolute["deployment"]["ue_catalog_file"] = str(catalog.resolve())
        absolute["deployment"]["topology_file"] = str(topology.resolve())
        absolute_source = fixture / "absolute.yml"
        absolute_source.write_text(
            yaml.safe_dump(absolute, sort_keys=False), encoding="utf-8"
        )
        load_scenario(absolute_source, deployment_only=True)

        topology.unlink()
        expect_value_error(
            lambda: load_scenario(source, deployment_only=True), "topology not found"
        )

        topology.write_text("[]\n", encoding="utf-8")
        expect_value_error(
            lambda: load_scenario(source, deployment_only=True),
            "topology document must be a mapping",
        )

        topology.write_text(
            yaml.safe_dump(
                {
                    "schema_version": 0,
                    "rans": {"srsran": {"oai": {"network": {"n2": {}}}}},
                }
            ),
            encoding="utf-8",
        )
        expect_value_error(
            lambda: load_scenario(source, deployment_only=True),
            "topology schema_version must be a positive integer",
        )

        topology.write_text(
            yaml.safe_dump({"schema_version": 1, "rans": {"srsran": {}}}),
            encoding="utf-8",
        )
        expect_value_error(
            lambda: load_scenario(source, deployment_only=True),
            "no asserted topology contract for srsran + oai",
        )

        topology.write_text(
            yaml.safe_dump(
                {
                    "schema_version": 1,
                    "rans": {"srsran": {"oai": {"namespace": "oai"}}},
                }
            ),
            encoding="utf-8",
        )
        expect_value_error(
            lambda: load_scenario(source, deployment_only=True),
            "requires a non-empty network mapping",
        )


def check_issue_53() -> None:
    provision_path = ROOT / "deployment/playbooks/provision_nodes.yml"
    provision_text = provision_path.read_text(encoding="utf-8")
    if "findmnt --nofsroot -n -o SOURCE /var/lib/containerd" not in provision_text:
        fail("#53: safe discovery must normalize bind-mount SOURCE with --nofsroot")

    pre_path = ROOT / "deployment/roles/setup/pre_k8s/tasks/main.yml"
    pre_text = pre_path.read_text(encoding="utf-8")
    tasks = yaml.safe_load(pre_text)
    if not isinstance(tasks, list):
        fail("#53: pre_k8s task file must be a list")

    detect_name = "Detect whether selected storage already backs containerd"
    detect = named_task(tasks, detect_name)
    detect_shell = str(detect.get("ansible.builtin.shell", ""))
    for marker in (
        "findmnt --nofsroot -n -o SOURCE /var/lib/containerd",
        "readlink -f /dev/{{ storage }}",
        "echo selected",
    ):
        if marker not in detect_shell:
            fail(f"#53: retained-binding detection is missing {marker!r}")

    names = [str(task.get("name", "")) for task in tasks]
    if names.index(detect_name) >= names.index("Clear stale containerd mounts"):
        fail("#53: selected-device binding must be detected before any unmount")

    retain_guard = "containerd_existing_binding.stdout | trim != 'selected'"
    guarded_tasks = (
        "Stop containerd before changing its storage binding",
        "Remove stale /var/lib/containerd fstab entries",
        "Clear stale containerd mounts",
        "Create the containerd directory on already-mounted storage",
        "Bind already-mounted storage to the containerd data directory",
        "Mount unmounted ext4 storage at the containerd data directory",
        "Restart containerd after changing its storage binding",
    )
    for name in guarded_tasks:
        task = named_task(tasks, name)
        if retain_guard not in when_text(task):
            fail(f"#53: {name!r} can still mutate an already-correct binding")

    filesystem = named_task(tasks, "Require a supported selected storage filesystem")
    filesystem_assert = str((filesystem.get("ansible.builtin.assert") or {}).get("that", ""))
    for marker in (
        "containerd_existing_binding.stdout | trim == 'selected'",
        "['ext4', 'xfs', 'btrfs']",
    ):
        if marker not in filesystem_assert:
            fail(f"#53: retained mounted-filesystem validation is missing {marker!r}")

    keep = named_task(tasks, "Keep containerd running on the retained storage binding")
    if "containerd_existing_binding.stdout | trim == 'selected'" not in when_text(keep):
        fail("#53: retained binding path must leave containerd running without rebinding")

    verify = named_task(tasks, "Verify containerd storage is on a real filesystem")
    verify_shell = str(verify.get("ansible.builtin.shell", ""))
    if "ext4|xfs|btrfs" not in verify_shell:
        fail("#53: retained mounted XFS/Btrfs storage must remain supported")


def main() -> None:
    check_issue_50()
    check_issue_53()
    print("reopened #50/#53 regression contracts OK")


if __name__ == "__main__":
    main()
