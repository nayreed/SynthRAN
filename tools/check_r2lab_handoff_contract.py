#!/usr/bin/env python3
"""No-hardware regression checks for the R2Lab Ansible handoff."""

from __future__ import annotations

from pathlib import Path

from synthran.inventory import render_inventory
from synthran.r2lab import ssh_options


ROOT = Path(__file__).resolve().parents[1]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"r2lab-handoff-contract: {message}")


def main() -> int:
    controller_known_hosts = Path("/tmp/synthran-controller-known-hosts")
    faraday_known_hosts = Path("/tmp/synthran-faraday-known-hosts")
    identity = "/tmp/r2lab-identity"

    deployment = {
        "platform": "r2lab",
        "nodes": {
            "core": "sopnode-f2",
            "ran": "sopnode-f3",
            "broker": "sopnode-f2",
        },
        "host_vars": {
            "sopnode-f2": {"ip": "192.0.2.2"},
            "sopnode-f3": {"ip": "192.0.2.3"},
        },
        "r2lab_ssh": {
            "host": "faraday.inria.fr",
            "username": "ci-slice",
            "identity_file": identity,
        },
    }
    ue_map = [
        {"device": "qhat01", "tunnel": {"mode": "mbim"}},
        {"device": "qhat03", "tunnel": {"mode": "mbim"}},
    ]

    options = ssh_options(
        "faraday.inria.fr", faraday_known_hosts, identity
    )
    option_text = " ".join(options)
    for token in (
        "-F /dev/null",
        f"UserKnownHostsFile={faraday_known_hosts}",
        "StrictHostKeyChecking=accept-new",
        "BatchMode=yes",
        "ConnectTimeout=15",
        f"-i {identity}",
        "IdentitiesOnly=yes",
    ):
        require(token in option_text, f"canonical Faraday SSH options lost {token!r}")

    inventory = render_inventory(
        deployment,
        ue_map,
        controller_known_hosts,
        faraday_known_hosts,
    )
    children = inventory["all"]["children"]
    faraday = children["faraday"]["hosts"]["faraday.inria.fr"]
    direct = faraday["ansible_ssh_common_args"]
    require("-F /dev/null" in direct, "Ansible Faraday connection inherits local ssh config")
    require("BatchMode=yes" in direct, "Ansible Faraday connection is not non-interactive")
    require("IdentitiesOnly=yes" in direct, "Ansible Faraday connection can try unrelated identities")
    require(
        faraday.get("ansible_ssh_private_key_file") == identity,
        "Ansible Faraday identity differs from reservation identity",
    )

    for ue in ("qhat01", "qhat03"):
        host = children["qhats"]["hosts"][ue]
        proxy = host["ansible_ssh_common_args"]
        require("ProxyCommand=" in proxy, f"{ue} lost the Faraday jump path")
        require("-F /dev/null" in proxy, f"{ue} ProxyCommand inherits controller ssh config")
        require("BatchMode=yes" in proxy, f"{ue} ProxyCommand is not non-interactive")
        require("IdentitiesOnly=yes" in proxy, f"{ue} ProxyCommand can try unrelated identities")
        require(
            "ci-slice@faraday.inria.fr" in proxy,
            f"{ue} ProxyCommand lost the selected R2Lab slice identity",
        )
        require(
            host.get("ansible_ssh_private_key_file") == identity,
            f"{ue} lost the configured SSH identity",
        )

    cleanup = (ROOT / "deployment/roles/r2lab/cleanup/tasks/main.yml").read_text(
        encoding="utf-8"
    )
    stop = (ROOT / "deployment/roles/r2lab/ue/stop/tasks/main.yml").read_text(
        encoding="utf-8"
    )
    rru = (ROOT / "deployment/roles/r2lab/rru/tasks/main.yml").read_text(
        encoding="utf-8"
    )
    connect_playbook = (ROOT / "deployment/playbooks/connect_ues.yml").read_text(
        encoding="utf-8"
    )
    connect_role = (
        ROOT / "deployment/roles/r2lab/ue/connect/tasks/main.yml"
    ).read_text(encoding="utf-8")
    qmi_role = (
        ROOT / "deployment/roles/r2lab/ue/connect/tasks/qmi.yml"
    ).read_text(encoding="utf-8")
    verify_role = (
        ROOT / "deployment/roles/synthran/r2lab_ue_verify/tasks/main.yml"
    ).read_text(encoding="utf-8")

    # Sub 11 replaces the historical best-effort stop implementation. The
    # reservation handoff contract now validates the selected deployment map,
    # not the deleted ue_item/nested-ssh implementation details.
    require("all-off" not in cleanup, "cleanup still contains global all-off mutation")
    require("r2lab/ue/stop" in cleanup, "cleanup no longer invokes the selected UE stop owner")
    require(
        'r2lab_selected_ues: "{{ synthran_ue_map | map(attribute=\'device\') | list }}"' in cleanup,
        "cleanup no longer derives UE authority from the deployment contract",
    )
    require(
        "groups['qhats']" in cleanup and "groups['qfits']" in cleanup,
        "cleanup no longer verifies selected physical UEs against inventory",
    )
    require(
        'loop: "{{ synthran_ue_map }}"' in cleanup
        and 'synthran_r2lab_ue: "{{ synthran_cleanup_ue }}"' in cleanup,
        "cleanup no longer stops each selected contract UE exactly once",
    )
    require(
        "r2lab_inventory_ues | difference(r2lab_selected_ues)" not in cleanup,
        "cleanup rejects unrelated inventory UEs instead of preserving them",
    )
    require(
        'rhubarbe-pdu off "{{ rru }}"' in cleanup,
        "selected N3xx RRU cleanup no longer uses the maintained SophiaNode helper",
    )
    require(
        "selected_rru_power_off.rc == 1" in cleanup
        and "rhubarbe_status_contract=0:ON,1:OFF,255:failure" in cleanup,
        "selected N3xx RRU cleanup no longer honors Rhubarbe OFF status semantics",
    )
    require(
        'rhubarbe pdu off "{{ rru }}"' not in cleanup,
        "obsolete pinned-reference N3xx RRU power-off command returned",
    )
    require(
        "synthran_cleanup_power_settle_seconds | default(20)" in cleanup,
        "N3xx power-off settle default was removed",
    )
    require("ignore_errors:" not in cleanup, "selected cleanup can silently ignore failure")
    require(
        'rhubarbe-pdu on "{{ rru }}"' in rru,
        "selected N3xx RRU power-on no longer uses the maintained SophiaNode helper",
    )
    require(
        'rhubarbe pdu on "{{ rru }}"' not in rru,
        "obsolete pinned-reference N3xx RRU power-on command returned",
    )
    require(
        "seconds: 60" in rru,
        "N3xx cold-boot interval was removed",
    )

    require(
        "synthran_r2lab_ue is mapping" in stop
        and 'synthran_stop_device: "{{ synthran_r2lab_ue.device }}"' in stop
        and 'synthran_stop_mode: "{{ synthran_r2lab_ue.tunnel.mode }}"' in stop,
        "UE stop role no longer consumes one exact selected contract entry",
    )
    require(
        "synthran_stop_ssh_argv" in stop
        and "StrictHostKeyChecking=accept-new" in stop
        and "UserKnownHostsFile=" in stop
        and "root@{{ synthran_stop_device }}" in stop,
        "selected UE stop is no longer contract-selected Faraday-side SSH",
    )
    require(
        "/usr/local/bin/ci_ctl_qtel.py" in stop
        and "synthran_stop_mode == 'qmi'" in stop,
        "selected QMI UE detach behavior is missing",
    )
    require(
        "Retain selected UE stop evidence before enforcing phase policy" in stop
        and "Require the selected UE stop operation to satisfy phase policy" in stop,
        "selected UE stop no longer retains evidence before enforcing lifecycle policy",
    )
    require("ignore_errors:" not in stop, "selected UE stop can silently ignore lifecycle failure")
    require(
        "ignore_unreachable:" not in stop
        and "'already-unreachable'" in stop
        and "'failed-unreachable'" in stop
        and "synthran_stop_phase == 'predeploy'" in stop
        and "== 255" in stop,
        "selected UE stop no longer classifies Faraday SSH transport failure by lifecycle phase",
    )
    require(
        "synthran_stop_helper_unreachable" in stop
        and "synthran_stop_helper_succeeded" in stop,
        "selected UE stop phase policy no longer distinguishes connection failure from helper failure",
    )
    require(
        'ue: "{{ ue | default(ue_item) }}"' not in stop
        and "r2lab_stop_target" not in stop,
        "historical sticky include-loop UE state returned",
    )

    # Sub 09 makes the selected deployment contract the sole attachment input.
    # The handoff must fail closed at the mutating owner; the later verifier is
    # deliberately read-only and may not become a repair path.
    require(
        "any_errors_fatal: true" in connect_playbook,
        "R2Lab UE attachment orchestration no longer fails closed",
    )
    require(
        'loop: "{{ synthran_ue_map }}"' in connect_playbook
        and 'synthran_r2lab_ue: "{{ item }}"' in connect_playbook
        and 'ue_item: "{{ item.device }}"' in connect_playbook,
        "R2Lab UE attachment no longer iterates exact selected contract entries",
    )
    require(
        "ignore_task_errors" not in connect_playbook,
        "R2Lab UE handoff reintroduced a best-effort compatibility knob",
    )
    require(
        "network_profile_file" not in connect_role and "fiveg." not in connect_role,
        "R2Lab connect owner reintroduced independent profile reconstruction",
    )
    require(
        "Attach the selected MBIM context with the installed R2Lab helpers" in connect_role
        and "stop.sh; start.sh -F {{ mbim_access_string }}" in connect_role
        and "Require the selected MBIM helper to complete" in connect_role,
        "selected MBIM attachment is no longer explicit and fail closed",
    )
    require(
        "Wait for the selected UE IPv4 session on its modem interface" in connect_role
        and "Wait for the selected modem interface to be operational" in connect_role
        and "Route the selected UPF endpoint through the modem interface" in connect_role,
        "required selected UE address/link/route handoff tasks disappeared",
    )
    require(
        "ignore_errors" not in connect_role,
        "selected UE attachment can silently ignore a main-path failure",
    )
    require(
        "synthran_qmi_manager_acceptable" in qmi_role
        and "Require the selected QMI attachment procedure to complete" in qmi_role
        and "not wwan0_up or synthran_qmi_existing.rc == 0" in qmi_role,
        "selected QMI handoff no longer rejects ambiguous or stale live manager identity",
    )
    require(
        "ignore_errors" not in qmi_role,
        "selected QMI attachment can silently ignore a lifecycle failure",
    )
    require(
        "that: synthran_r2lab_probe.rc == 0" in verify_role
        and "verification does not repair or reattach modem state" in verify_role,
        "read-only R2Lab UE acceptance gate was weakened or made mutating",
    )

    reserve = (ROOT / "deployment/scripts/reserve_r2lab.py").read_text(encoding="utf-8")
    require(
        'if args.host == "faraday.inria.fr"' in reserve
        and '["-F", "/dev/null"]' in reserve,
        "provider lease verifier no longer bypasses local ssh config",
    )
    require(
        '"IdentitiesOnly=yes"' in reserve,
        "provider lease verifier no longer pins the selected identity",
    )

    print("R2Lab Ansible handoff contract checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
