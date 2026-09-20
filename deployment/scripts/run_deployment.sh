#!/usr/bin/env bash
set -euo pipefail
set +m

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${1:-}" != --worker ]]; then
  RUN_DIR=$1
  SYNTHRAN_PYTHON=$2
  for required in nohup setsid tail; do
    command -v "$required" >/dev/null || { echo "$required is required for deployment process control" >&2; exit 1; }
  done
  : >"$RUN_DIR/ansible.log"
  echo "Deployment continues on this host if the terminal disconnects. Ctrl+C cancels it."
  echo "Full Ansible output: $RUN_DIR/ansible.log"
  echo "Controller output: $RUN_DIR/deployment.log"
  nohup setsid --wait bash "$SCRIPT_DIR/run_deployment.sh" --worker "$@" \
    </dev/null >"$RUN_DIR/deployment.log" 2>&1 &
  WORKER_PID=$!

  cancel_deployment() {
    trap '' INT TERM
    if [[ -n "$WORKER_PID" ]]; then
      kill -TERM "$WORKER_PID" 2>/dev/null || true
      wait "$WORKER_PID" 2>/dev/null || true
    fi
    exit "$1"
  }
  trap 'cancel_deployment 130' INT
  trap 'cancel_deployment 143' TERM
  trap 'exit 129' HUP

  tail --pid="$WORKER_PID" -n +1 -f -- "$RUN_DIR/ansible.log" 9>&- \
    | "$SYNTHRAN_PYTHON" -u "$SCRIPT_DIR/filter_ansible_output.py" 9>&- &
  VIEWER_PID=$!
  WORKER_RC=0
  wait "$WORKER_PID" || WORKER_RC=$?
  WORKER_PID=""
  wait "$VIEWER_PID" || true
  cat "$RUN_DIR/deployment.log"
  exit "$WORKER_RC"
fi

shift
RUN_DIR=$1
SYNTHRAN_PYTHON=$2
ACTIVE_DEPLOYMENT_STATE=$3
shift 3
DEPLOYMENT_COMMAND=("$@")
CHILD_PID=""
printf '%s\n' "$$" >"$RUN_DIR/controller.pid"

if [[ -n "${SYNTHRAN_PRIVATE_DIR:-}" ]]; then
  export ANSIBLE_ROLES_PATH="$SYNTHRAN_PRIVATE_DIR/ansible/roles"
  export ANSIBLE_CONFIG="$SYNTHRAN_PRIVATE_DIR/ansible/ansible.cfg"
  for ((i = 0; i < ${#DEPLOYMENT_COMMAND[@]}; i++)); do
    if [[ "${DEPLOYMENT_COMMAND[$i]}" == "@deployment/group_vars/all/all.yml" ]]; then
      DEPLOYMENT_COMMAND[$i]="@$SYNTHRAN_PRIVATE_DIR/ansible/group_vars/all/all.yml"
    fi
  done
fi

record_exit() {
  local original_status=$?
  local timing_status=0
  local safety_status=0
  local status=$original_status
  local timing_closure_status=incomplete
  local timing_reason="controller exited with an unfinished deployment phase"
  trap - EXIT

  if (( original_status != 0 )); then
    timing_closure_status=failed
    timing_reason="controller exited with status $original_status"
  fi
  if [[ -d "$RUN_DIR" ]]; then
    "$SYNTHRAN_PYTHON" -m synthran.phase_timing close-open \
      --run-dir "$RUN_DIR" \
      --status "$timing_closure_status" \
      --reason "$timing_reason" || timing_status=$?
  fi

  if [[ -n "${SYNTHRAN_PRIVATE_DIR:-}" && -d "$RUN_DIR" ]]; then
    "$SYNTHRAN_PYTHON" -m synthran.result_safety \
      --run-dir "$RUN_DIR" --private-dir "$SYNTHRAN_PRIVATE_DIR" || safety_status=$?
  fi
  if (( status == 0 && timing_status != 0 )); then
    status=$timing_status
  fi
  if (( status == 0 && safety_status != 0 )); then
    status=$safety_status
  fi

  printf '%s\n' "$status" >"$RUN_DIR/controller-exit-code"
  echo "SynthRAN deployment controller exited with status $status; shareable artifacts retained in $RUN_DIR"
  exit "$status"
}

cancel_worker() {
  local status=$1
  trap '' INT TERM
  if [[ -n "$CHILD_PID" ]]; then
    kill -TERM -- "-$CHILD_PID" 2>/dev/null || true
    wait "$CHILD_PID" 2>/dev/null || true
    CHILD_PID=""
  fi
  exit "$status"
}
trap record_exit EXIT
trap 'cancel_worker 130' INT
trap 'cancel_worker 143' TERM

run_step() {
  local status=0
  setsid --wait "$@" <&0 9>&- &
  CHILD_PID=$!
  wait "$CHILD_PID" || status=$?
  CHILD_PID=""
  return "$status"
}

mark_failed() {
  local phase=$1 status=$2 reason=$3
  "$SYNTHRAN_PYTHON" -m synthran.acceptance fail \
    --candidate "$RUN_DIR/deployment-fingerprint.json" \
    --phase "$phase" \
    --exit-code "$status" \
    --reason "$reason" >/dev/null 2>&1 || true
}

collect_failure_diagnostics() {
  local reason=$1
  local diagnostics_rc=0
  local replaced=false
  local skip_next=false
  local i arg
  local diagnostics_playbook="${SYNTHRAN_PRIVATE_DIR:-}/ansible/playbooks/diagnostics.yml"
  local diagnostics_command=("${DEPLOYMENT_COMMAND[@]}")
  local filtered_command=()

  if [[ -z "${SYNTHRAN_PRIVATE_DIR:-}" || ! -f "$diagnostics_playbook" ]]; then
    echo "Failure diagnostics unavailable: staged diagnostics playbook is missing." >&2
    return 0
  fi

  for arg in "${diagnostics_command[@]}"; do
    if [[ "$skip_next" == true ]]; then
      skip_next=false
      continue
    fi
    case "$arg" in
      --start-at-task)
        skip_next=true
        continue
        ;;
      --start-at-task=*)
        continue
        ;;
    esac
    filtered_command+=("$arg")
  done
  diagnostics_command=("${filtered_command[@]}")

  for ((i = 0; i < ${#diagnostics_command[@]}; i++)); do
    case "${diagnostics_command[$i]}" in
      "$SYNTHRAN_PRIVATE_DIR"/ansible/playbooks/*.yml)
        diagnostics_command[$i]="$diagnostics_playbook"
        replaced=true
        break
        ;;
    esac
  done
  if [[ "$replaced" != true ]]; then
    echo "Failure diagnostics unavailable: deployment playbook argument was not found." >&2
    return 0
  fi

  echo "Collecting bounded deployment diagnostics after $reason."
  run_step "${diagnostics_command[@]}" </dev/null >>"$RUN_DIR/ansible.log" 2>&1 || diagnostics_rc=$?
  if (( diagnostics_rc != 0 )); then
    echo "Diagnostics collection was incomplete (status $diagnostics_rc); the original failure is preserved." >&2
  fi
  echo "Diagnostic artifacts: $RUN_DIR/diagnostics"
  return 0
}

if [[ -f "$RUN_DIR/source-revision.txt" ]]; then
  echo "Source revision: $(cat "$RUN_DIR/source-revision.txt")"
fi

CONTROLLER_PROVENANCE_RC=0
run_step "$SYNTHRAN_PYTHON" -m synthran.provenance --run-dir "$RUN_DIR" || CONTROLLER_PROVENANCE_RC=$?
if (( CONTROLLER_PROVENANCE_RC != 0 )); then
  echo "Controller dependency provenance failed with status $CONTROLLER_PROVENANCE_RC; provisioning was not started." >&2
  mark_failed "controller-provenance" "$CONTROLLER_PROVENANCE_RC" "controller dependency provenance failed"
  exit "$CONTROLLER_PROVENANCE_RC"
fi
echo "Controller dependency provenance recorded."

ANSIBLE_RC=0
run_step "${DEPLOYMENT_COMMAND[@]}" </dev/null >"$RUN_DIR/ansible.log" 2>&1 || ANSIBLE_RC=$?
if (( ANSIBLE_RC != 0 )); then
  echo "Deployment provisioning failed with status $ANSIBLE_RC; complete Ansible output: $RUN_DIR/ansible.log" >&2
  mark_failed "provisioning" "$ANSIBLE_RC" "Ansible provisioning or path-specific verification failed"
  collect_failure_diagnostics "provisioning failure"
  exit "$ANSIBLE_RC"
fi

echo "Provisioning owners completed; sealing executable deployment identity."
PROVISIONED_RC=0
run_step "$SYNTHRAN_PYTHON" -m synthran.acceptance provisioning-complete \
  --candidate "$RUN_DIR/deployment-fingerprint.json" \
  --evidence "$RUN_DIR/live-deployment-evidence.json" \
  --run-dir "$RUN_DIR" \
  --private-dir "$SYNTHRAN_PRIVATE_DIR" || PROVISIONED_RC=$?
if (( PROVISIONED_RC != 0 )); then
  echo "Provisioning-complete identity sealing failed with status $PROVISIONED_RC; deployment was not accepted." >&2
  mark_failed "provisioning-complete" "$PROVISIONED_RC" "executable deployment identity could not be sealed"
  collect_failure_diagnostics "provisioning-complete rejection"
  exit "$PROVISIONED_RC"
fi
echo "State: provisioning-complete."

ACCEPTANCE_PLAYBOOK="$SYNTHRAN_PRIVATE_DIR/ansible/playbooks/acceptance.yml"
if [[ ! -f "$ACCEPTANCE_PLAYBOOK" ]]; then
  echo "Staged accepted-testbed verification playbook is missing: $ACCEPTANCE_PLAYBOOK" >&2
  mark_failed "accepted-testbed-probe" 2 "staged acceptance playbook is missing"
  exit 2
fi

ACCEPTANCE_COMMAND=()
ACCEPTANCE_SKIP_NEXT=false
for arg in "${DEPLOYMENT_COMMAND[@]}"; do
  if [[ "$ACCEPTANCE_SKIP_NEXT" == true ]]; then
    ACCEPTANCE_SKIP_NEXT=false
    continue
  fi
  case "$arg" in
    --start-at-task)
      ACCEPTANCE_SKIP_NEXT=true
      continue
      ;;
    --start-at-task=*)
      continue
      ;;
  esac
  ACCEPTANCE_COMMAND+=("$arg")
done

ACCEPTANCE_REPLACED=false
for ((i = 0; i < ${#ACCEPTANCE_COMMAND[@]}; i++)); do
  case "${ACCEPTANCE_COMMAND[$i]}" in
    "$SYNTHRAN_PRIVATE_DIR"/ansible/playbooks/*.yml)
      ACCEPTANCE_COMMAND[$i]="$ACCEPTANCE_PLAYBOOK"
      ACCEPTANCE_REPLACED=true
      break
      ;;
  esac
done
if [[ "$ACCEPTANCE_REPLACED" != true ]]; then
  echo "Accepted-testbed verification cannot locate the staged deployment playbook argument." >&2
  mark_failed "accepted-testbed-probe" 2 "deployment playbook argument could not be replaced"
  exit 2
fi

echo "Running path-specific accepted-testbed UE/session/user-plane verification."
"$SYNTHRAN_PYTHON" -m synthran.phase_timing start \
  --run-dir "$RUN_DIR" --phase verification
ACCEPTANCE_ANSIBLE_RC=0
run_step "${ACCEPTANCE_COMMAND[@]}" </dev/null >>"$RUN_DIR/ansible.log" 2>&1 || ACCEPTANCE_ANSIBLE_RC=$?
if (( ACCEPTANCE_ANSIBLE_RC != 0 )); then
  "$SYNTHRAN_PYTHON" -m synthran.phase_timing finish \
    --run-dir "$RUN_DIR" --phase verification --status failed || true
  echo "Accepted-testbed live verification failed with status $ACCEPTANCE_ANSIBLE_RC; deployment was not published for reuse." >&2
  mark_failed "accepted-testbed-probe" "$ACCEPTANCE_ANSIBLE_RC" "path-specific UE/session/user-plane verification failed"
  collect_failure_diagnostics "accepted-testbed live verification failure"
  exit "$ACCEPTANCE_ANSIBLE_RC"
fi

ACTIVE_DEPLOYMENT_ENDPOINT="$PWD/.synthran/active-deployment.json"
ACCEPT_RC=0
run_step "$SYNTHRAN_PYTHON" -m synthran.acceptance accept \
  --candidate "$RUN_DIR/deployment-fingerprint.json" \
  --active "$ACTIVE_DEPLOYMENT_STATE" \
  --evidence "$RUN_DIR/live-deployment-evidence.json" \
  --cluster-snapshot "$RUN_DIR/acceptance-cluster.json" \
  --endpoint "$ACTIVE_DEPLOYMENT_ENDPOINT" \
  --private-dir "$SYNTHRAN_PRIVATE_DIR" || ACCEPT_RC=$?
if (( ACCEPT_RC != 0 )); then
  "$SYNTHRAN_PYTHON" -m synthran.phase_timing finish \
    --run-dir "$RUN_DIR" --phase verification --status failed || true
  echo "Accepted-testbed validation failed with status $ACCEPT_RC; deployment was not published for reuse." >&2
  mark_failed "accepted-testbed" "$ACCEPT_RC" "fresh selected workload/UE/user-plane acceptance failed"
  collect_failure_diagnostics "accepted-testbed rejection"
  exit "$ACCEPT_RC"
fi

"$SYNTHRAN_PYTHON" -m synthran.phase_timing finish \
  --run-dir "$RUN_DIR" --phase verification

echo "State: accepted-testbed."
echo "Accepted deployment endpoint: $ACTIVE_DEPLOYMENT_ENDPOINT"
echo "Experiments must still pass a fresh read-only experiment-eligibility probe before workload execution."
