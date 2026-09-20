#!/usr/bin/env python3
"""Static contract for issue #53 host/Kubernetes bootstrap ownership."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess

import yaml

ROOT = Path(__file__).resolve().parents[1]
PIN = ROOT / "third_party/sopnode-5g-ansible/EXECUTION_REFERENCE.json"
ADAPTATIONS = ROOT / "third_party/sopnode-5g-ansible/HOST_BOOTSTRAP_ADAPTATIONS.json"


def fail(message: str) -> None:
    raise SystemExit(message)


def require(text: str, needle: str, context: str) -> None:
    if needle not in text:
        fail(f"{context}: missing required contract text: {needle!r}")


def forbid(text: str, needle: str, context: str) -> None:
    if needle in text:
        fail(f"{context}: forbidden legacy/bootstrap text remains: {needle!r}")


def _when_text(entry: dict) -> str:
    value = entry.get("when", "")
    if isinstance(value, list):
        return "\n".join(str(item) for item in value)
    return str(value)


def _named_task(tasks: list[dict], name: str) -> dict:
    for task in tasks:
        if task.get("name") == name:
            return task
    fail(f"task missing from contract: {name}")
    raise AssertionError("unreachable")


def _bootstrap_tasks(plays: list[dict]) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for play in plays:
        for section in ("pre_tasks", "tasks", "post_tasks"):
            for task in play.get(section, []) or []:
                if not isinstance(task, dict) or not task.get("name"):
                    fail(f"bootstrap_nodes.yml: unnamed or invalid task in {section}")
                name = str(task["name"])
                if name in result:
                    fail(f"bootstrap_nodes.yml: duplicate task name prevents lifecycle classification: {name}")
                result[name] = task
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", required=True, type=Path)
    args = parser.parse_args()

    pin = json.loads(PIN.read_text(encoding="utf-8"))
    adaptations = json.loads(ADAPTATIONS.read_text(encoding="utf-8"))
    expected = pin["commit"]
    if adaptations["reference_commit"] != expected:
        fail("host bootstrap adaptation manifest does not match execution reference pin")

    actual = subprocess.run(
        ["git", "-C", str(args.reference), "rev-parse", "HEAD"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()
    if actual != expected:
        fail(f"reference checkout mismatch: expected {expected}, found {actual}")

    for role in adaptations["delegated_roles"]:
        local = ROOT / "deployment/roles" / role / "tasks/main.yml"
        upstream = args.reference / "roles" / role / "tasks/main.yml"
        if not local.is_file():
            fail(f"delegated role wrapper is missing: {role}")
        if not upstream.is_file():
            fail(f"delegated role missing from pinned reference: {role}")
        wrapper = local.read_text(encoding="utf-8")
        require(wrapper, "ansible.builtin.include_tasks", f"delegated wrapper {role}")
        require(
            wrapper,
            f"{{{{ synthran_reference_root }}}}/roles/{role}/tasks/main.yml",
            f"delegated wrapper {role}",
        )
        if len([line for line in wrapper.splitlines() if line.strip()]) > 4:
            fail(f"delegated role wrapper contains lifecycle logic instead of forwarding: {role}")

    all_vars = (ROOT / "deployment/group_vars/all/all.yml").read_text(encoding="utf-8")
    require(all_vars, "synthran_host_preparation", "all.yml")
    require(all_vars, "host_preparation", "all.yml")
    require(all_vars, "synthran_reference_root", "all.yml")
    require(all_vars, expected, "all.yml")

    site = (ROOT / "deployment/playbooks/site.yml").read_text(encoding="utf-8")
    provision_at = site.index("provision_nodes.yml")
    bootstrap_at = site.index("bootstrap_nodes.yml")
    network_at = site.index("network.yml")
    if not provision_at < bootstrap_at < network_at:
        fail("site.yml must run node validation, bootstrap, then transport/workloads")

    provision = (ROOT / "deployment/playbooks/provision_nodes.yml").read_text(encoding="utf-8")
    require(provision, "synthran_configured_storage", "provision_nodes.yml")
    require(provision, "Discover containerd storage only when inventory did not select one", "provision_nodes.yml")
    require(provision, "synthran_configured_storage | length == 0", "provision_nodes.yml")
    require(provision, "Use discovered storage only when inventory did not select one", "provision_nodes.yml")
    require(provision, "refusing to format", "provision_nodes.yml")
    require(provision, "mounted:%s:%s", "provision_nodes.yml")
    require(provision, "unmounted:%s", "provision_nodes.yml")
    require(provision, "host_preparation=preserve", "provision_nodes.yml")
    forbid(provision, "Record the selected containerd storage device", "provision_nodes.yml")

    pre_k8s = (ROOT / "deployment/roles/setup/pre_k8s/tasks/main.yml").read_text(encoding="utf-8")
    require(pre_k8s, "Find an existing mountpoint for the selected storage device", "pre_k8s")
    require(pre_k8s, "Bind already-mounted storage to the containerd data directory", "pre_k8s")
    require(pre_k8s, "Mount unmounted ext4 storage at the containerd data directory", "pre_k8s")
    require(pre_k8s, "SynthRAN will not format the device", "pre_k8s")
    require(pre_k8s, "Verify containerd storage is on a real filesystem", "pre_k8s")
    require(pre_k8s, "storage binding only", "pre_k8s")
    forbid(pre_k8s, "Configure kubelet storage eviction thresholds", "pre_k8s")
    forbid(pre_k8s, "kubeadm", "pre_k8s")

    bootstrap_path = ROOT / "deployment/playbooks/bootstrap_nodes.yml"
    bootstrap = bootstrap_path.read_text(encoding="utf-8")
    bootstrap_data = yaml.safe_load(bootstrap)
    mutating_roles = {
        "setup/common",
        "setup/netplan",
        "setup/containerd",
        "setup/pre_k8s",
        "setup/k8s/k8s_setup",
        "setup/optimization/cpu",
        "setup/ovs",
        "setup/k8s/cluster_create",
        "setup/k8s/cni_dhcp",
        "setup/k8s/cluster_join",
        "setup/k8s/add_taint",
        "setup/k8s/remove_taint",
        "setup/k8s/remove_cp_taint",
        "setup/cni",
        "setup/storage",
        "setup/k8s/k8s_cpu_tuning",
    }
    rebuild_only_roles = {
        "setup/netplan",
        "setup/pre_k8s",
        "setup/k8s/k8s_setup",
        "setup/k8s/cluster_create",
        "setup/k8s/cni_dhcp",
        "setup/k8s/cluster_join",
        "setup/k8s/remove_cp_taint",
    }
    seen_roles = set()
    for play in bootstrap_data:
        for role_entry in play.get("roles", []):
            if isinstance(role_entry, str):
                role_name = role_entry
                role = {"role": role_name}
            else:
                role = role_entry
                role_name = role["role"]
            seen_roles.add(role_name)
            when_text = _when_text(role)
            if role_name in mutating_roles:
                if "synthran_host_preparation" not in when_text:
                    fail(f"bootstrap_nodes.yml: mutating role lacks preparation-mode guard: {role_name}")
                if "fresh" not in when_text and "bootstrap" not in when_text:
                    fail(f"bootstrap_nodes.yml: mutating role is not restricted to mutable modes: {role_name}")
                if "preserve" in when_text:
                    fail(f"bootstrap_nodes.yml: preserve must not execute mutating role: {role_name}")
            if role_name in rebuild_only_roles and "bootstrap" in when_text:
                if "synthran_bootstrap_cluster_action == 'rebuild'" not in when_text:
                    fail(f"bootstrap_nodes.yml: destructive bootstrap role lacks rebuild classification: {role_name}")

    for role in mutating_roles:
        if role not in seen_roles:
            fail(f"bootstrap_nodes.yml: expected lifecycle role missing: {role}")

    mutable_tasks = {
        "Resolve the supported yq architecture",
        "Require a checksum-pinned yq binary for this architecture",
        "Install the checksum-verified shared yq binary",
        "Read back the installed yq version",
        "Require the installed yq binary to match the pinned release",
        "Resolve the supported CNI plugin architecture",
        "Require a pinned CNI artifact for this architecture",
        "Stage the checksum-verified CNI plugin bundle",
    }
    bootstrap_only_tasks = {
        "Resolve bootstrap CNI repair need",
        "Resolve bootstrap CNI plugin architecture",
        "Require pinned CNI artifact for bootstrap reuse",
        "Ensure CNI plugin directory exists for bootstrap reuse",
        "Stage checksum-verified CNI plugins for bootstrap reuse",
        "Reconcile CNI plugin binaries for bootstrap reuse",
        "Reconcile CNI DHCP service for bootstrap reuse",
        "Require containerd and kubelet after bootstrap prerequisite reconciliation",
    }
    preserve_only_tasks = {
        "Verify preserved containerd is active",
        "Verify preserved kubelet is active",
        "Verify preserved CNI DHCP binary exists",
        "Require preserved CNI DHCP binary",
        "Verify preserved CNI DHCP daemon is active",
        "Verify preserved Kubernetes control plane is reachable",
        "Verify preserved RAN node is registered",
        "Verify preserved NetworkAttachmentDefinition API",
    }
    shared_tasks = {
        "Read the effective containerd mount",
        "Read CNI DHCP daemon state",
        "Read registered Kubernetes nodes",
        "Initialize bootstrap node evidence",
        "Collect per-host bootstrap evidence",
        "Write shareable bootstrap evidence",
    }

    classified = mutable_tasks | bootstrap_only_tasks | preserve_only_tasks | shared_tasks
    tasks_by_name = _bootstrap_tasks(bootstrap_data)
    actual_tasks = set(tasks_by_name)
    unclassified = actual_tasks - classified
    missing = classified - actual_tasks
    if unclassified:
        fail(
            "bootstrap_nodes.yml: task(s) added without explicit fresh/preserve/shared lifecycle classification: "
            + ", ".join(sorted(unclassified))
        )
    if missing:
        fail(
            "bootstrap_nodes.yml: lifecycle contract task(s) missing: "
            + ", ".join(sorted(missing))
        )

    preserve_guard = "synthran_host_preparation == 'preserve'"
    for name in mutable_tasks:
        when_text = _when_text(tasks_by_name[name])
        if "synthran_host_preparation" not in when_text or "fresh" not in when_text:
            fail(f"bootstrap_nodes.yml: mutable task lacks explicit fresh/bootstrap guard: {name}")
    for name in bootstrap_only_tasks:
        when_text = _when_text(tasks_by_name[name])
        if "synthran_host_preparation == 'bootstrap'" not in when_text:
            fail(f"bootstrap_nodes.yml: bootstrap-only task lacks bootstrap guard: {name}")
        if "synthran_bootstrap_cluster_action == 'reuse'" not in when_text:
            fail(f"bootstrap_nodes.yml: bootstrap reuse task lacks reuse classification: {name}")
    for name in preserve_only_tasks:
        if preserve_guard not in _when_text(tasks_by_name[name]):
            fail(f"bootstrap_nodes.yml: preserve-only task lacks preserve guard: {name}")
    for name in shared_tasks:
        if "synthran_host_preparation" in _when_text(tasks_by_name[name]):
            fail(f"bootstrap_nodes.yml: shared invariant/evidence task is mode-gated: {name}")

    yq_tasks = {
        "Resolve the supported yq architecture",
        "Require a checksum-pinned yq binary for this architecture",
        "Install the checksum-verified shared yq binary",
        "Read back the installed yq version",
        "Require the installed yq binary to match the pinned release",
    }
    for name in yq_tasks:
        tags = tasks_by_name[name].get("tags", [])
        if isinstance(tags, str):
            tags = [tags]
        if "fresh_yq" not in tags:
            fail(f"bootstrap_nodes.yml: yq mutable-path regression task lacks fresh_yq tag: {name}")

    require(bootstrap, "synthran_host_preparation == 'preserve'", "bootstrap_nodes.yml")
    require(bootstrap, "Verify preserved Kubernetes control plane is reachable", "bootstrap_nodes.yml")
    require(bootstrap, "Verify preserved CNI DHCP binary exists", "bootstrap_nodes.yml")
    require(bootstrap, "Write shareable bootstrap evidence", "bootstrap_nodes.yml")
    require(bootstrap, "bootstrap-evidence.json", "bootstrap_nodes.yml")
    require(bootstrap, "containerd_mount", "bootstrap_nodes.yml")
    require(bootstrap, "cni_dhcp", "bootstrap_nodes.yml")
    require(bootstrap, "cluster_nodes", "bootstrap_nodes.yml")
    forbid(bootstrap, "Move the kubeadm join command", "bootstrap_nodes.yml")
    forbid(bootstrap, ".kubeadm_join_command.txt", "bootstrap_nodes.yml")
    forbid(bootstrap, "setup/gre_tunnel", "bootstrap_nodes.yml")
    forbid(bootstrap, "name: 5g/", "bootstrap_nodes.yml")

    network = (ROOT / "deployment/playbooks/network.yml").read_text(encoding="utf-8")
    require(network, "setup/gre_tunnel", "network.yml")
    require(network, "5g/open5gs", "network.yml")
    forbid(network, "setup/k8s/cluster_create", "network.yml")
    forbid(network, "setup/common", "network.yml")
    forbid(network, "setup/pre_k8s", "network.yml")

    common = (ROOT / "deployment/roles/setup/common/tasks/main.yml").read_text(encoding="utf-8")
    forbid(common, "cni-dhcp.service", "common")

    cni = (ROOT / "deployment/roles/setup/k8s/cni_dhcp/tasks/main.yml").read_text(encoding="utf-8")
    binary_check = cni.index("Verify the CNI DHCP binary is installed")
    service_start = cni.index("Enable and start the CNI DHCP daemon")
    if binary_check >= service_start:
        fail("cni_dhcp must verify /opt/cni/bin/dhcp before service startup")
    require(cni, "synthran_cni_dhcp_binary.stat.executable", "cni_dhcp")

    cluster_create_path = ROOT / "deployment/roles/setup/k8s/cluster_create/tasks/main.yml"
    cluster_create = cluster_create_path.read_text(encoding="utf-8")
    cluster_create_tasks = yaml.safe_load(cluster_create)
    require(cluster_create, "Verify pre-k8s containerd storage binding", "cluster_create")
    require(cluster_create, "Configure kubelet storage eviction thresholds", "cluster_create")
    require(cluster_create, "{{ synthran_private_dir }}/kubeadm_join_command.txt", "cluster_create")
    forbid(cluster_create, "dest: .kubeadm_join_command.txt", "cluster_create")
    forbid(cluster_create, "/etc/kubernetes/config.conf", "cluster_create")
    forbid(cluster_create, "kubeproxy-config.yaml", "cluster_create")
    forbid(cluster_create, "Remount disk directly after reset", "cluster_create")
    forbid(cluster_create, "Unmount all stacked mounts on /var/lib/containerd after reset", "cluster_create")

    generate_join = _named_task(cluster_create_tasks, "Generate join command")
    save_join = _named_task(cluster_create_tasks, "Save join command in private run directory")
    if generate_join.get("no_log") is not True:
        fail("cluster_create: join-token generation must use no_log")
    if save_join.get("no_log") is not True:
        fail("cluster_create: join-token persistence must use no_log")
    copy_spec = save_join.get("ansible.builtin.copy") or {}
    if copy_spec.get("dest") != "{{ synthran_private_dir }}/kubeadm_join_command.txt":
        fail("cluster_create: join token must be born inside synthran_private_dir")
    if str(copy_spec.get("mode")) != "0600":
        fail("cluster_create: private join token must use mode 0600")

    join = (ROOT / "deployment/roles/setup/k8s/cluster_join/tasks/main.yml").read_text(encoding="utf-8")
    require(join, "{{ synthran_private_dir }}/admin.conf", "cluster_join")
    require(join, "{{ synthran_private_dir }}/kubeadm_join_command.txt", "cluster_join")
    require(join, "mode: '0600'", "cluster_join")
    forbid(join, "/var/lib/kube-proxy/kubeproxy-config.yaml", "cluster_join")
    forbid(join, "/etc/kubernetes/config.conf", "cluster_join")

    join_material = "kubeadm_join_command.txt"
    allowed_join_paths = {
        cluster_create_path,
        ROOT / "deployment/roles/setup/k8s/cluster_join/tasks/main.yml",
    }
    for path in (ROOT / "deployment").rglob("*.yml"):
        if join_material in path.read_text(encoding="utf-8") and path not in allowed_join_paths:
            fail(f"join material referenced outside private create/join roles: {path.relative_to(ROOT)}")

    reference_containerd = (
        args.reference / "roles/setup/containerd/tasks/main.yml"
    ).read_text(encoding="utf-8")
    for marker in (
        "Install containerd",
        "Switch snapshotter to overlayfs",
        "Wait for containerd socket",
    ):
        require(reference_containerd, marker, "reference containerd role")

    runtime = (ROOT / "synthran/runtime.py").read_text(encoding="utf-8")
    reference_checkout = (ROOT / "synthran/reference_checkout.py").read_text(encoding="utf-8")
    require(runtime, "ensure_execution_reference()", "runtime.py")
    require(reference_checkout, "rev-parse", "reference_checkout.py")
    require(reference_checkout, "status", "reference_checkout.py")

    print("issue #53 host/bootstrap ownership contract OK")


if __name__ == "__main__":
    main()
