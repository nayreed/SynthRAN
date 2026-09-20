#!/usr/bin/env python3
"""Issue #58 contract checks for selected-resource teardown and owned release."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import yaml

from synthran import reservation_release as release
from synthran import teardown as teardown_state

ROOT = Path(__file__).resolve().parents[1]
TEARDOWN_PLAYBOOK = ROOT / "deployment/playbooks/teardown.yml"
ROLES = ROOT / "deployment/roles"
ANSIBLE_CONFIG = ROOT / "deployment/ansible.cfg"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def run(
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: Path = ROOT,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def ansible_env(fake_bin: Path, **extra: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "PATH": str(fake_bin) + os.pathsep + env.get("PATH", ""),
            "ANSIBLE_CONFIG": str(ANSIBLE_CONFIG),
            "ANSIBLE_ROLES_PATH": str(ROLES),
            "ANSIBLE_FORCE_COLOR": "0",
            "ANSIBLE_HOST_KEY_CHECKING": "False",
            **extra,
        }
    )
    return env


def inventory(path: Path, *, ssh_failure: bool = False) -> None:
    qhat03: dict[str, object]
    if ssh_failure:
        qhat03 = {
            "ansible_connection": "ssh",
            "ansible_host": "127.0.0.1",
            "ansible_port": 1,
            "ansible_user": "nobody",
            "ansible_ssh_common_args": (
                "-o ConnectTimeout=1 -o BatchMode=yes -o StrictHostKeyChecking=no"
            ),
        }
    else:
        qhat03 = {"ansible_connection": "local"}
    data = {
        "all": {
            "children": {
                "faraday": {
                    "hosts": {"faraday-ci": {"ansible_connection": "local"}}
                },
                "qhats": {
                    "hosts": {
                        "qhat01": {"ansible_connection": "local"},
                        "qhat03": qhat03,
                        "qhat99": {"ansible_connection": "local"},
                    }
                },
                "qfits": {"hosts": {}},
                "physical_ues": {"children": {"qhats": {}, "qfits": {}}},
                "core_node": {
                    "hosts": {"core-ci": {"ansible_connection": "local"}}
                },
            }
        }
    }
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def variables(path: Path, run_dir: Path, kubectl: Path, namespace: str = "synthran-ci") -> None:
    data = {
        "platform": "r2lab",
        "rru": "n320",
        "run_dir": str(run_dir.resolve()),
        "kubectl_bin": str(kubectl.resolve()),
        "synthran_cleanup_power_settle_seconds": 0,
        "synthran_delete_namespace": False,
        "synthran_ue_map": [
            {
                "device": "qhat01",
                "tunnel": {
                    "host": "qhat01",
                    "interface": "wwan0",
                    "mode": "mbim",
                    "mbim_session": 0,
                },
            },
            {
                "device": "qhat03",
                "tunnel": {
                    "host": "qhat03",
                    "interface": "wwan0",
                    "mode": "mbim",
                    "mbim_session": 0,
                },
            },
        ],
        "synthran_deployment_contract": {
            "deployment": {"topology": {"namespace": namespace}}
        },
    }
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def ansible_command(ansible: str, inv: Path, vars_file: Path, tag: str, *extra: str) -> list[str]:
    return [
        ansible,
        "-i",
        str(inv),
        "-e",
        "@" + str(vars_file),
        str(TEARDOWN_PLAYBOOK),
        "--tags",
        tag,
        *extra,
    ]


def predeploy_command(
    ansible: str,
    inv: Path,
    vars_file: Path,
    playbook: Path,
) -> list[str]:
    playbook.write_text(
        """---
- name: Exercise selected predeploy cleanup
  hosts: faraday
  gather_facts: false
  roles:
    - role: r2lab/cleanup
      vars:
        synthran_cleanup_phase: predeploy
""",
        encoding="utf-8",
    )
    return [
        ansible,
        "-i",
        str(inv),
        "-e",
        "@" + str(vars_file),
        str(playbook),
    ]


def check_static_boundaries() -> None:
    cleanup = (ROOT / "deployment/roles/r2lab/cleanup/tasks/main.yml").read_text()
    stop = (ROOT / "deployment/roles/r2lab/ue/stop/tasks/main.yml").read_text()
    provision = (ROOT / "deployment/playbooks/provision_r2lab.yml").read_text()
    experiment_cleanup = (
        ROOT / "synthran/experiment_runtime/ansible/playbooks/cleanup.yml"
    ).read_text()
    controller = (ROOT / "synthran/teardown_controller.py").read_text()

    for label, text in (("cleanup role", cleanup), ("UE stop role", stop), ("provisioning", provision)):
        require("all-off" not in text, f"{label} still contains global R2Lab all-off")
    require("ignore_errors:" not in cleanup, "selected cleanup still ignores failures")
    require("ignore_errors:" not in stop, "selected UE stop still ignores failures")
    require(
        "ignore_unreachable:" not in stop,
        "selected UE stop still depends on Ansible unreachable suppression",
    )
    require(
        "synthran_stop_ssh_argv" in stop
        and "StrictHostKeyChecking=accept-new" in stop
        and "UserKnownHostsFile=" in stop
        and "ConnectTimeout=5" in stop,
        "selected UE stop no longer uses bounded Faraday-side SSH with explicit trust",
    )
    require(
        "'already-unreachable'" in stop
        and "'failed-unreachable'" in stop
        and "synthran_stop_phase == 'predeploy'" in stop
        and "== 255" in stop,
        "selected UE stop no longer distinguishes predeploy SSH transport failure from teardown failure",
    )
    require(
        "r2lab_inventory_ues | difference(r2lab_selected_ues)" not in cleanup,
        "cleanup rejects unrelated inventory UEs instead of preserving them",
    )
    require("loop: \"{{ synthran_ue_map }}\"" in cleanup, "cleanup is not contract-selected")
    require(
        "selected_rru_power_off.rc == 1" in cleanup
        and "rhubarbe_status_contract=0:ON,1:OFF,255:failure" in cleanup,
        "selected RRU cleanup no longer honors Rhubarbe OFF success status",
    )
    require("synthran_delete_namespace" in TEARDOWN_PLAYBOOK.read_text(), "namespace opt-in is missing")
    for forbidden in ("rhubarbe-pdu", "r2lab/cleanup", "teardown.yml", "pos calendar"):
        require(
            forbidden not in experiment_cleanup,
            f"experiment cleanup crosses the testbed boundary via {forbidden!r}",
        )
    require(
        controller.index("begin_teardown(endpoint)")
        < controller.index("rc = _run_resources(context, verbose=verbose)"),
        "controller does not fence accepted endpoint before resource mutation",
    )
    require(
        "book.delete(start, end)" in release.R2LAB_RELEASE_CODE,
        "R2Lab release no longer follows upstream Book.delete(start,end) semantics",
    )


def check_state_machine(tmp: Path) -> None:
    tmp.mkdir(parents=True, exist_ok=True)
    result_dir = tmp / "run-ci"
    private = tmp / "private"
    (private / "ansible/playbooks").mkdir(parents=True)
    (private / "ansible/group_vars/all").mkdir(parents=True)
    (private / "ansible/roles").mkdir(parents=True)
    result_dir.mkdir()
    for path in (
        private / "ansible/playbooks/teardown.yml",
        private / "inventory.yml",
        private / "deployment-vars.yml",
        private / "ansible/group_vars/all/all.yml",
        private / "ansible/ansible.cfg",
    ):
        path.write_text("---\n", encoding="utf-8")

    endpoint = tmp / "active-deployment.json"
    endpoint.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "status": "accepted-testbed",
                "configuration_hash": "config-ci",
                "deployment_hash": "deploy-ci",
                "run_id": result_dir.name,
                "identity_file": str(tmp / "identity.json"),
                "evidence_file": str(tmp / "evidence.json"),
                "private_execution_dir": str(private),
                "result_dir": str(result_dir),
            }
        ),
        encoding="utf-8",
    )

    original_attach = teardown_state.attach_active_deployment
    teardown_state.attach_active_deployment = lambda **_: {
        "deployment_run_id": result_dir.name,
        "configuration_hash": "config-ci",
        "deployment_hash": "deploy-ci",
        "identity_file": str(tmp / "identity.json"),
        "evidence_file": str(tmp / "evidence.json"),
        "private_execution_dir": str(private),
        "result_dir": str(result_dir),
        "deployment": {"platform": "r2lab"},
    }
    try:
        context = teardown_state.begin_teardown(endpoint)
    finally:
        teardown_state.attach_active_deployment = original_attach

    fenced = json.loads(endpoint.read_text(encoding="utf-8"))
    require(fenced["status"] == "stopping", "accepted endpoint was not fenced")
    result = Path(context["result_file"])
    teardown_state.record_phase(result, "resources", status="succeeded")
    teardown_state.record_phase(result, "namespace", status="skipped")
    teardown_state.record_phase(result, "pos_calendar", status="preserved")
    teardown_state.record_phase(result, "r2lab", status="preserved")
    teardown_state.complete_teardown(endpoint, result)
    require(
        json.loads(endpoint.read_text(encoding="utf-8"))["status"] == "torn-down",
        "successful teardown did not publish torn-down terminal state",
    )


def check_ansible_selected_cleanup(tmp: Path) -> None:
    tmp.mkdir(parents=True, exist_ok=True)
    ansible = shutil.which("ansible-playbook")
    require(ansible is not None, "ansible-playbook is required for the selected cleanup contract")
    fake = tmp / "fake-bin"
    fake.mkdir()
    stop_log = tmp / "stop.log"
    pdu_log = tmp / "pdu.log"
    kubectl_log = tmp / "kubectl.log"

    executable(
        fake / "stop.sh",
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "${SYNTHRAN_STOP_DEVICE:-missing}" >>"$SYNTHRAN_STOP_LOG"
if [[ "${SYNTHRAN_FAIL_UE:-}" == "${SYNTHRAN_STOP_DEVICE:-}" ]]; then exit 31; fi
""",
    )
    executable(fake / "qhat-check", "#!/usr/bin/env bash\nexit 0\n")
    executable(
        fake / "ssh",
        """#!/usr/bin/env bash
set -euo pipefail
target=""
command=()
for arg in "$@"; do
  if [[ -z "$target" && "$arg" == root@* ]]; then
    target="${arg#root@}"
    continue
  fi
  if [[ -n "$target" ]]; then
    command+=("$arg")
  fi
done
[[ -n "$target" ]] || exit 64
if [[ "${SYNTHRAN_UNREACHABLE_UE:-}" == "$target" ]]; then
  printf 'ssh: connect to host %s port 22: Connection refused\\n' "$target" >&2
  exit 255
fi
case "${command[0]:-}" in
  stop.sh)
    SYNTHRAN_STOP_DEVICE="$target" stop.sh
    ;;
  qhat-check)
    qhat-check
    ;;
  python3)
    exit 0
    ;;
  *)
    exit 64
    ;;
esac
""",
    )
    executable(
        fake / "rhubarbe-pdu",
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >>"$SYNTHRAN_PDU_LOG"
if [[ "${SYNTHRAN_FAIL_RRU:-0}" == 1 ]]; then exit 255; fi
if [[ "$1" == "off" ]]; then exit 1; fi
if [[ "$1" == "on" ]]; then exit 0; fi
exit 255
""",
    )
    executable(
        fake / "kubectl",
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >>"$SYNTHRAN_KUBECTL_LOG"
printf 'namespace/%s deleted\\n' "${3:-unknown}"
""",
    )

    inv = tmp / "inventory.yml"
    vars_file = tmp / "vars.yml"
    run_dir = tmp / "run-selected"
    run_dir.mkdir()
    inventory(inv)
    variables(vars_file, run_dir, fake / "kubectl")
    env = ansible_env(
        fake,
        SYNTHRAN_STOP_LOG=str(stop_log),
        SYNTHRAN_PDU_LOG=str(pdu_log),
        SYNTHRAN_KUBECTL_LOG=str(kubectl_log),
    )

    selected = run(ansible_command(ansible, inv, vars_file, "resources"), env=env)
    require(selected.returncode == 0, "selected two-UE cleanup failed:\n" + selected.stdout + selected.stderr)
    stopped = stop_log.read_text(encoding="utf-8").splitlines()
    require(stopped.count("qhat01") == 1, "qhat01 was not stopped exactly once")
    require(stopped.count("qhat03") == 1, "qhat03 was not stopped exactly once")
    require("qhat99" not in stopped, "unselected sentinel qhat99 was touched")
    require(
        pdu_log.read_text(encoding="utf-8").splitlines() == ["off n320"],
        "cleanup did not power off only the selected n320 once",
    )
    for device in ("qhat01", "qhat03"):
        require(
            (run_dir / f"r2lab-ue-{device}-teardown-stop.log").is_file(),
            f"missing retained stop evidence for {device}",
        )

    fail_dir = tmp / "run-rru-fail"
    fail_dir.mkdir()
    fail_vars = tmp / "vars-rru-fail.yml"
    variables(fail_vars, fail_dir, fake / "kubectl")
    fail_env = env | {"SYNTHRAN_FAIL_RRU": "1"}
    failed = run(ansible_command(ansible, inv, fail_vars, "resources"), env=fail_env)
    require(failed.returncode != 0, "RRU power-off failure was masked")
    rru_evidence = fail_dir / "r2lab-rru-n320-teardown-stop.log"
    require(rru_evidence.is_file(), "RRU failure evidence was not retained")
    require("rc=255" in rru_evidence.read_text(encoding="utf-8"), "RRU failure rc was not retained")

    ue_fail_dir = tmp / "run-ue-helper-fail"
    ue_fail_dir.mkdir()
    ue_fail_vars = tmp / "vars-ue-helper-fail.yml"
    variables(ue_fail_vars, ue_fail_dir, fake / "kubectl")
    ue_fail_env = env | {"SYNTHRAN_FAIL_UE": "qhat03"}
    ue_failed = run(ansible_command(ansible, inv, ue_fail_vars, "resources"), env=ue_fail_env)
    require(ue_failed.returncode != 0, "reachable selected UE helper failure was masked")
    ue_fail_evidence = ue_fail_dir / "r2lab-ue-qhat03-teardown-stop.log"
    require(ue_fail_evidence.is_file(), "reachable UE helper failure evidence was not retained")
    ue_fail_text = ue_fail_evidence.read_text(encoding="utf-8")
    require("outcome=failed-helper" in ue_fail_text, "reachable UE helper failure was misclassified")
    require("helper_rc=31" in ue_fail_text, "reachable UE helper failure rc was not retained")

    ssh_inv = tmp / "inventory-ssh-fail.yml"
    inventory(ssh_inv)

    if stop_log.exists():
        stop_log.unlink()
    if pdu_log.exists():
        pdu_log.unlink()

    predeploy_dir = tmp / "run-predeploy-ssh-unreachable"
    predeploy_dir.mkdir()
    predeploy_vars = tmp / "vars-predeploy-ssh-unreachable.yml"
    variables(predeploy_vars, predeploy_dir, fake / "kubectl")
    predeploy_playbook = tmp / "predeploy-cleanup.yml"
    unreachable_env = env | {"SYNTHRAN_UNREACHABLE_UE": "qhat03"}
    predeploy = run(
        predeploy_command(ansible, ssh_inv, predeploy_vars, predeploy_playbook),
        env=unreachable_env,
    )
    require(
        predeploy.returncode == 0,
        "predeploy rejected an initially unreachable selected UE:\n"
        + predeploy.stdout
        + predeploy.stderr,
    )
    predeploy_stopped = stop_log.read_text(encoding="utf-8").splitlines()
    require(predeploy_stopped.count("qhat01") == 1, "reachable predeploy UE was not stopped once")
    require("qhat03" not in predeploy_stopped, "unreachable predeploy UE unexpectedly executed stop.sh")
    require("qhat99" not in predeploy_stopped, "unselected sentinel was touched during predeploy")
    predeploy_evidence = predeploy_dir / "r2lab-ue-qhat03-predeploy-stop.log"
    require(predeploy_evidence.is_file(), "predeploy unreachable UE evidence was not retained")
    predeploy_text = predeploy_evidence.read_text(encoding="utf-8")
    require(
        "outcome=already-unreachable" in predeploy_text,
        "predeploy unreachable UE was not classified as already-unreachable",
    )
    require(
        "helper_unreachable=True" in predeploy_text
        or "helper_unreachable=true" in predeploy_text,
        "predeploy unreachable UE did not retain the unreachable result",
    )
    require(
        pdu_log.read_text(encoding="utf-8").splitlines() == ["off n320"],
        "predeploy unreachable UE changed selected-RRU cleanup semantics",
    )

    ssh_dir = tmp / "run-teardown-ssh-unreachable"
    ssh_dir.mkdir()
    ssh_vars = tmp / "vars-teardown-ssh-unreachable.yml"
    variables(ssh_vars, ssh_dir, fake / "kubectl")
    ssh_failed = run(
        ansible_command(ansible, ssh_inv, ssh_vars, "resources"),
        env=unreachable_env,
    )
    ssh_output = ssh_failed.stdout + ssh_failed.stderr
    require(ssh_failed.returncode != 0, "teardown selected UE SSH failure was masked")
    ssh_evidence = ssh_dir / "r2lab-ue-qhat03-teardown-stop.log"
    require(
        ssh_evidence.is_file(),
        "teardown unreachable UE evidence was not retained:\n" + ssh_output,
    )
    require(
        "outcome=failed-unreachable" in ssh_evidence.read_text(encoding="utf-8"),
        "teardown unreachable UE was not classified as a terminal failure",
    )
    require(
        "Selected R2Lab UE stop failed" in ssh_output,
        "teardown unreachable UE failure was not visible at the phase-policy assertion",
    )

    if kubectl_log.exists():
        kubectl_log.unlink()
    namespace_false = run(ansible_command(ansible, inv, vars_file, "namespace"), env=env)
    require(namespace_false.returncode == 0, "namespace-preserve path failed")
    require(not kubectl_log.exists(), "namespace was deleted without explicit opt-in")

    namespace_true = run(
        ansible_command(
            ansible,
            inv,
            vars_file,
            "namespace",
            "-e",
            "synthran_delete_namespace=true",
        ),
        env=env,
    )
    require(namespace_true.returncode == 0, "explicit namespace deletion failed")
    calls = kubectl_log.read_text(encoding="utf-8").splitlines()
    require(
        calls == ["delete namespace synthran-ci --ignore-not-found=true"],
        "namespace teardown did not target exactly the accepted namespace",
    )

    system_vars = tmp / "vars-system-namespace.yml"
    variables(system_vars, tmp / "run-system", fake / "kubectl", namespace="kube-system")
    (tmp / "run-system").mkdir()
    before = list(calls)
    system = run(
        ansible_command(
            ansible,
            inv,
            system_vars,
            "namespace",
            "-e",
            "synthran_delete_namespace=true",
        ),
        env=env,
    )
    require(system.returncode != 0, "system namespace deletion was not rejected")
    require(
        kubectl_log.read_text(encoding="utf-8").splitlines() == before,
        "kubectl was invoked for a protected system namespace",
    )


def check_reservation_release(tmp: Path) -> None:
    tmp.mkdir(parents=True, exist_ok=True)
    fake = tmp / "provider-bin"
    fake.mkdir()
    pos_state = tmp / "pos-state.json"
    pos_log = tmp / "pos.log"
    ssh_log = tmp / "ssh.log"
    executable(
        fake / "pos",
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == calendar && "$2" == list ]]; then cat "$POS_STATE"; exit 0; fi
if [[ "$1" == calendar && "$2" == delete ]]; then
  printf '%s\\n' "$*" >>"$POS_LOG"
  if [[ "${POS_FAIL_DELETE:-0}" == 1 ]]; then exit 17; fi
  printf '[]\\n' >"$POS_STATE"
  exit 0
fi
exit 64
""",
    )
    executable(
        fake / "ssh",
        """#!/usr/bin/env bash
set -euo pipefail
cat >/dev/null
printf '%s\\n' "$*" >>"$R2LAB_SSH_LOG"
if [[ "${R2LAB_SSH_FAIL:-0}" == 1 ]]; then exit 19; fi
printf '%s\\n' '{"status":"released","lease_id":77,"slice_name":"slice-ci","remaining":[]}'
""",
    )

    old_env = os.environ.copy()
    old_state_path = release.STATE_PATH
    try:
        os.environ.update(
            {
                "PATH": str(fake) + os.pathsep + old_env.get("PATH", ""),
                "USER": "ci-owner",
                "POS_STATE": str(pos_state),
                "POS_LOG": str(pos_log),
                "R2LAB_SSH_LOG": str(ssh_log),
                "R2LAB_USERNAME": "slice-ci",
                "R2LAB_EMAIL": "ci@example.invalid",
                "R2LAB_PASSWORD": "not-on-argv",
                "R2LAB_FARADAY_KNOWN_HOSTS": str(tmp / "known-hosts"),
            }
        )
        release.STATE_PATH = tmp / "compat-pos-state.json"
        now = dt.datetime.now().astimezone()
        event = {
            "id": "42",
            "owner": "ci-owner",
            "nodes": ["sopnode-f2", "sopnode-f3"],
            "start_date": (now - dt.timedelta(minutes=5)).isoformat(),
            "end_date": (now + dt.timedelta(minutes=60)).isoformat(),
        }
        pos_state.write_text(json.dumps([event]), encoding="utf-8")
        authority = tmp / "reservation-authority.json"
        calendar = {
            "status": "created",
            "id": "42",
            "owner": "ci-owner",
            "nodes": ["sopnode-f2", "sopnode-f3"],
            "start": event["start_date"],
            "end": event["end_date"],
            "managed_by": "synthran",
        }
        authority.write_text(json.dumps({"pos_calendar": calendar}), encoding="utf-8")
        outcome = release.release_pos_calendar(authority)
        require(outcome["status"] == "released", "created POS calendar was not released")
        require(
            pos_log.read_text(encoding="utf-8").splitlines()
            == ["calendar delete --id 42 sopnode-f2 sopnode-f3"],
            "POS release did not use exact event id and selected nodes",
        )

        calendar["status"] = "reused"
        authority.write_text(json.dumps({"pos_calendar": calendar}), encoding="utf-8")
        before = pos_log.read_text(encoding="utf-8")
        outcome = release.release_pos_calendar(authority)
        require(outcome["status"] == "preserved", "reused POS calendar was not preserved")
        require(pos_log.read_text(encoding="utf-8") == before, "reused POS calendar was mutated")

        calendar["status"] = "created"
        authority.write_text(json.dumps({"pos_calendar": calendar}), encoding="utf-8")
        pos_state.write_text(json.dumps([event]), encoding="utf-8")
        os.environ["POS_FAIL_DELETE"] = "1"
        try:
            release.release_pos_calendar(authority)
        except release.ReservationReleaseError:
            pass
        else:
            raise AssertionError("POS provider delete failure was masked")
        os.environ.pop("POS_FAIL_DELETE", None)

        run_dir = tmp / "accepted-run"
        private = tmp / "accepted-private"
        run_dir.mkdir()
        private.mkdir()
        (private / "resolved-scenario.yml").write_text(
            yaml.safe_dump(
                {
                    "deployment": {
                        "platform": "r2lab",
                        "r2lab_username": "slice-ci",
                        "r2lab_ssh": {
                            "host": "faraday.inria.fr",
                            "username": "slice-ci",
                        },
                    }
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        lease = {
            "status": "booked",
            "policy_mode": "book",
            "requested": {"start": "2026-09-17T20:00", "end": "2026-09-17T22:00"},
            "provider_lease": {
                "id": 77,
                "slice_name": "slice-ci",
                "t_from": "2026-09-17T18:00:00.000Z",
                "t_until": "2026-09-17T20:00:00.000Z",
            },
        }
        (run_dir / "r2lab-lease.json").write_text(json.dumps(lease), encoding="utf-8")
        outcome = release.release_r2lab_lease(run_dir, private)
        require(outcome["status"] == "released", "booked R2Lab lease was not released")
        ssh_call = ssh_log.read_text(encoding="utf-8")
        for coordinate in ("77", "slice-ci", "2026-09-17T20:00", "2026-09-17T22:00"):
            require(coordinate in ssh_call, f"R2Lab release omitted recorded coordinate {coordinate}")
        require("not-on-argv" not in ssh_call, "R2Lab password leaked into SSH argv")

        lease["status"] = "extended"
        (run_dir / "r2lab-lease.json").write_text(json.dumps(lease), encoding="utf-8")
        before = ssh_log.read_text(encoding="utf-8")
        outcome = release.release_r2lab_lease(run_dir, private)
        require(outcome["status"] == "preserved", "extended R2Lab lease was not preserved")
        require(ssh_log.read_text(encoding="utf-8") == before, "extended R2Lab lease was mutated")

        lease["status"] = "booked"
        (run_dir / "r2lab-lease.json").write_text(json.dumps(lease), encoding="utf-8")
        os.environ["R2LAB_SSH_FAIL"] = "1"
        try:
            release.release_r2lab_lease(run_dir, private)
        except release.ReservationReleaseError:
            pass
        else:
            raise AssertionError("R2Lab SSH/provider failure was masked")
    finally:
        release.STATE_PATH = old_state_path
        os.environ.clear()
        os.environ.update(old_env)


def main() -> int:
    check_static_boundaries()
    with tempfile.TemporaryDirectory(prefix="synthran-sub11-") as directory:
        tmp = Path(directory)
        check_state_machine(tmp / "state")
        check_ansible_selected_cleanup(tmp / "ansible")
        check_reservation_release(tmp / "reservation")
    print("Sub 11 selected-resource teardown contract: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
