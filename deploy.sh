#!/usr/bin/env bash
set -euo pipefail

CONFIG=""
CONFIG_EXPLICIT=false
INTERACTIVE=false
NO_INPUT=false
NO_RESERVATION=false
DRY_RUN=false
VERBOSE=false
UE_CATALOG_FILE=deployment/group_vars/all/ue_catalog.yaml

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      [[ $# -ge 2 ]] || { echo "--config requires a path" >&2; exit 2; }
      CONFIG="$2"
      CONFIG_EXPLICIT=true
      shift 2
      ;;
    -i|--interactive)
      INTERACTIVE=true
      shift
      ;;
    -n|--no-input)
      NO_INPUT=true
      shift
      ;;
    -r|--no-reservation)
      NO_RESERVATION=true
      shift
      ;;
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    -v|--verbose)
      VERBOSE=true
      shift
      ;;
    -h|--help)
      echo "Usage: ./deploy.sh [--config scenarios/<scenario>.yml] [--interactive] [--no-input] [--no-reservation] [--dry-run] [--verbose]"
      echo "deploy.sh provisions and verifies the 5G testbed only. Without --config it opens the interactive testbed wizard. Experiments are intentionally handled separately."
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 2
      ;;
  esac
done

if $INTERACTIVE && $NO_INPUT; then
  echo "--interactive and --no-input cannot be used together" >&2
  exit 2
fi
if $NO_INPUT && ! $CONFIG_EXPLICIT; then
  echo "--no-input requires --config because bare deploy is interactive" >&2
  exit 2
fi
if $CONFIG_EXPLICIT; then
  [[ -f "$CONFIG" ]] || { echo "Scenario not found: $CONFIG" >&2; exit 2; }
fi

RUN_ID="$(date -u +%Y%m%dT%H%M%S%NZ)"
RUN_DIR="results/$RUN_ID"
PRIVATE_RUN_DIR="$PWD/.synthran/execution/$RUN_ID"
mkdir -p "$RUN_DIR"
install -d -m 0700 "$PRIVATE_RUN_DIR"
export SYNTHRAN_PRIVATE_DIR="$PRIVATE_RUN_DIR"
git rev-parse HEAD >"$RUN_DIR/source-revision.txt" 2>/dev/null || printf 'unknown\n' >"$RUN_DIR/source-revision.txt"

ACTIVE_DEPLOYMENT_STATE="$PWD/.synthran/deployment-fingerprint.json"
ACTIVE_DEPLOYMENT_ENDPOINT="$PWD/.synthran/active-deployment.json"
mkdir -p .synthran .synthran/r2lab
R2LAB_FARADAY_KNOWN_HOSTS=${R2LAB_FARADAY_KNOWN_HOSTS:-$PWD/.synthran/r2lab/faraday_known_hosts}
export R2LAB_FARADAY_KNOWN_HOSTS

if [[ -f .r2lab_config ]]; then
  # shellcheck disable=SC1091
  source .r2lab_config
fi
R2LAB_IDENTITY_FILE=${R2LAB_IDENTITY_FILE:-}
if [[ -z "$R2LAB_IDENTITY_FILE" && -r "$HOME/.ssh/id_rsa_r2lab_duckburg" ]]; then
  R2LAB_IDENTITY_FILE="$HOME/.ssh/id_rsa_r2lab_duckburg"
fi
if [[ -n "$R2LAB_IDENTITY_FILE" ]]; then
  [[ -r "$R2LAB_IDENTITY_FILE" ]] || { echo "R2Lab SSH identity is not readable: $R2LAB_IDENTITY_FILE" >&2; exit 1; }
fi
export R2LAB_USERNAME="${R2LAB_USERNAME:-}" R2LAB_IDENTITY_FILE

command -v flock >/dev/null || { echo "flock is required to protect deployments from overlapping runs" >&2; exit 1; }
exec 9>.synthran/deploy.lock
if ! flock -n 9; then
  echo "Another SynthRAN deployment is still running. Stop it before starting a new deployment." >&2
  echo "Inspect it with: pgrep -af 'deploy.sh|ansible-playbook|prepare-demo-oai'" >&2
  exit 1
fi
printf '%s\n' "$$" 1>&9

deployment_section() {
  echo
  echo "$1"
  printf '%*s\n' "${#1}" '' | tr ' ' '-'
}

command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
SYNTHRAN_PYTHON="$(python3 -m synthran.runtime python)"

deployment_section "Preparing the local SynthRAN runtime"
"$SYNTHRAN_PYTHON" -m synthran.runtime deployment --log "$RUN_DIR/bootstrap.log"

choose_sop_node() {
  local label="$1" default_node="$2" node_choice
  echo
  echo "Which node should host the ${label}? (default: ${default_node})"
  echo "1) sopnode-f1"
  echo "2) sopnode-f2"
  echo "3) sopnode-f3"
  echo "4) sopnode-w3"
  read -r -p "Enter choice [1-4]: " node_choice
  case "${node_choice:-}" in
    "") SELECTED_NODE="$default_node" ;;
    1) SELECTED_NODE=sopnode-f1 ;;
    2) SELECTED_NODE=sopnode-f2 ;;
    3) SELECTED_NODE=sopnode-f3 ;;
    4) SELECTED_NODE=sopnode-w3 ;;
    *) echo "Invalid node choice" >&2; exit 2 ;;
  esac
}

show_r2lab_matrix() {
  cat <<'MATRIX'

R2Lab resource matrix
---------------------
5G radio units selectable by SynthRAN
  1) n300      USRP N300, SophiaNode fiber, 2x2 antenna
  2) n320      USRP N320, SophiaNode fiber, 4x4 antenna

Physical 5G UEs are discovered from the platform-independent UE catalog.
Availability and health are verified during reservation and provisioning.
MATRIX
}

discover_network_profiles() {
  local path name
  local paths=()
  shopt -s nullglob
  paths=(deployment/group_vars/all/network_profile_*.yaml)
  shopt -u nullglob
  for path in "${paths[@]}"; do
    name="$(basename -- "$path")"
    name="${name#network_profile_}"
    name="${name%.yaml}"
    printf '%s\n' "$name"
  done
}

network_profile_path() {
  if [[ "$1" == "${DEFAULT_NETWORK_PROFILE:-}" && -n "${BASE_NETWORK_PROFILE_FILE:-}" ]]; then
    printf '%s\n' "$BASE_NETWORK_PROFILE_FILE"
  else
    printf 'deployment/group_vars/all/network_profile_%s.yaml\n' "$1"
  fi
}

choose_network_profile() {
  local default_profile="$1" profile_choice default_choice=1 index
  mapfile -t AVAILABLE_NETWORK_PROFILES < <(discover_network_profiles)
  if [[ -n "${BASE_NETWORK_PROFILE_FILE:-}" && ! " ${AVAILABLE_NETWORK_PROFILES[*]} " == *" $default_profile "* ]]; then
    AVAILABLE_NETWORK_PROFILES+=("$default_profile")
  fi
  [[ ${#AVAILABLE_NETWORK_PROFILES[@]} -gt 0 ]] || {
    echo "No network profiles found under deployment/group_vars/all/network_profile_*.yaml" >&2
    exit 1
  }

  for index in "${!AVAILABLE_NETWORK_PROFILES[@]}"; do
    if [[ "${AVAILABLE_NETWORK_PROFILES[$index]}" == "$default_profile" ]]; then
      default_choice=$((index + 1))
      break
    fi
  done

  echo
  echo "Available network profiles"
  echo "--------------------------"
  for index in "${!AVAILABLE_NETWORK_PROFILES[@]}"; do
    printf '  %2d) %s\n' "$((index + 1))" "${AVAILABLE_NETWORK_PROFILES[$index]}"
  done
  read -r -p "Select network profile [$default_choice]: " profile_choice
  profile_choice=${profile_choice:-$default_choice}
  [[ "$profile_choice" =~ ^[1-9][0-9]*$ ]] || { echo "Network profile choice must be a number" >&2; exit 2; }
  (( profile_choice >= 1 && profile_choice <= ${#AVAILABLE_NETWORK_PROFILES[@]} )) || {
    echo "Network profile choice must be within 1-${#AVAILABLE_NETWORK_PROFILES[@]}" >&2
    exit 2
  }
  SELECTED_NETWORK_PROFILE="${AVAILABLE_NETWORK_PROFILES[$((profile_choice - 1))]}"
}

catalog_ues() {
  "$SYNTHRAN_PYTHON" - "$1" "$UE_CATALOG_FILE" <<'PY'
import sys, yaml
from pathlib import Path
platform = sys.argv[1]
catalog = Path(sys.argv[2])
data = yaml.safe_load(catalog.read_text()) or {}
for name, ue in (data.get('ues') or {}).items():
    if str(ue.get('platform', '')).lower() != platform:
        continue
    mode = str(ue.get('mode', 'software' if platform == 'rfsim' else 'mbim')).upper()
    interface = str(ue.get('interface', '-'))
    print(f"{name}\t{mode}\t{interface}")
PY
}

print_ue_matrix() {
  local platform="$1"
  shift
  local rows=("$@")
  local entries=()
  local index ue_name ue_mode ue_interface entry
  local max_width=0 cell_width terminal_width columns fit row_end

  for index in "${!rows[@]}"; do
    IFS=$'\t' read -r ue_name ue_mode ue_interface <<<"${rows[$index]}"
    if [[ "$platform" == r2lab ]]; then
      printf -v entry '%2d) %s [%s/%s]' "$((index + 1))" "$ue_name" "$ue_mode" "$ue_interface"
    else
      printf -v entry '%2d) %s' "$((index + 1))" "$ue_name"
    fi
    entries+=("$entry")
    (( ${#entry} > max_width )) && max_width=${#entry}
  done

  cell_width=$((max_width + 3))
  terminal_width=${COLUMNS:-}
  if [[ ! "$terminal_width" =~ ^[1-9][0-9]*$ ]]; then
    terminal_width=$(tput cols 2>/dev/null || printf '120')
  fi
  fit=$((terminal_width / cell_width))
  (( fit < 1 )) && fit=1
  columns=4
  (( fit < columns )) && columns=$fit

  for index in "${!entries[@]}"; do
    row_end=false
    if (( (index + 1) % columns == 0 || index + 1 == ${#entries[@]} )); then
      row_end=true
    fi
    if $row_end; then
      printf '  %s\n' "${entries[$index]}"
    else
      printf '  %-*s' "$cell_width" "${entries[$index]}"
    fi
  done
}

expand_ue_selection() {
  "$SYNTHRAN_PYTHON" - "$1" "$2" "$UE_CATALOG_FILE" <<'PY'
import sys, yaml
from pathlib import Path
platform, value = sys.argv[1], sys.argv[2].strip().lower()
data = yaml.safe_load(Path(sys.argv[3]).read_text()) or {}
names = [
    name for name, ue in (data.get('ues') or {}).items()
    if str(ue.get('platform', '')).lower() == platform
]
chosen = []
for part in value.replace(' ', '').split(','):
    if not part:
        continue
    if part in names:
        selected = [part]
    else:
        bounds = part.split('-', 1)
        try:
            start, stop = int(bounds[0]), int(bounds[-1])
        except ValueError:
            raise SystemExit(f'Invalid UE selection: {part}')
        if start > stop or start < 1 or stop > len(names):
            raise SystemExit(f'UE range must be ascending and within 1-{len(names)}: {part}')
        selected = names[start - 1:stop]
    for name in selected:
        if name not in chosen:
            chosen.append(name)
print(','.join(chosen))
PY
}

network_profile_slices() {
  "$SYNTHRAN_PYTHON" - "$(network_profile_path "$1")" <<'PY'
import sys, yaml
from pathlib import Path
path = Path(sys.argv[1])
data = yaml.safe_load(path.read_text()) or {}
for item in data.get('slices') or []:
    qos = item.get('qos') or {}
    bw = item.get('bandwidth') or {}
    print('\t'.join([
        str(item.get('name', '')),
        str(item.get('dnn', '')),
        str(item.get('sst', '')),
        str(item.get('sd', '')),
        str(qos.get('five_qi', '')),
        str(bw.get('uplink', '')),
        str(bw.get('downlink', '')),
    ]))
PY
}

base_slice_default() {
  local base_config="$1" profile="$2" ue="$3"
  [[ -n "$base_config" && -f "$base_config" ]] || return 0
  "$SYNTHRAN_PYTHON" - "$base_config" "$profile" "$ue" <<'PY'
import sys, yaml
from pathlib import Path
path, profile, ue = sys.argv[1:]
data = yaml.safe_load(Path(path).read_text()) or {}
d = data.get('deployment') or {}
if d.get('network_profile') == profile:
    value = (d.get('ue_slices') or {}).get(ue)
    if value:
        print(value)
PY
}

describe_ue_slice_assignments() {
  "$SYNTHRAN_PYTHON" - "$(network_profile_path "$1")" "$2" <<'PY'
import sys, yaml
from pathlib import Path
profile_path, spec = sys.argv[1:]
profile = yaml.safe_load(Path(profile_path).read_text()) or {}
by_name = {s['name']: s for s in profile.get('slices') or []}
for pair in filter(None, spec.split(',')):
    ue, slice_name = pair.split('=', 1)
    s = by_name[slice_name]
    print(f"  {ue}: {slice_name} / {s['dnn']} / SST {s['sst']} / SD {s['sd']}")
PY
}

if ! $NO_INPUT && { ! $CONFIG_EXPLICIT || $INTERACTIVE; }; then
  [[ -t 0 ]] || { echo "Interactive input requires a terminal; use --config or --no-input" >&2; exit 2; }
  BASE_CONFIG="$CONFIG"
  if [[ -n "$BASE_CONFIG" ]]; then
    "$SYNTHRAN_PYTHON" -m synthran.deployment_state resolve \
      --source "$BASE_CONFIG" --output "$PRIVATE_RUN_DIR/interactive-base.yml"
    BASE_CONFIG="$PRIVATE_RUN_DIR/interactive-base.yml"
  fi

  DEFAULT_CORE=open5gs
  DEFAULT_RAN=srsran
  DEFAULT_PLATFORM=rfsim
  DEFAULT_RU=rfsim
  DEFAULT_CORE_NODE=sopnode-f2
  DEFAULT_RAN_NODE=sopnode-f3
  DEFAULT_BROKER_NODE=sopnode-f2
  DEFAULT_NETWORK_PROFILE=default
  DEFAULT_UES=uesim01,uesim02
  DEFAULT_RESERVE=true
  DEFAULT_RESERVATION_MODE=create
  DEFAULT_HOST_PREPARATION=fresh
  DEFAULT_DURATION=120
  DEFAULT_POS_IMAGE=ubuntu-jammy
  DEFAULT_R2LAB_USERNAME=${R2LAB_USERNAME:-}
  DEFAULT_R2LAB_RESERVE=true
  DEFAULT_R2LAB_DURATION=120

  if [[ -n "$BASE_CONFIG" ]]; then
    mapfile -t SCENARIO_DEFAULTS < <("$SYNTHRAN_PYTHON" - "$BASE_CONFIG" <<'PY'
import sys, yaml
from pathlib import Path
data = yaml.safe_load(Path(sys.argv[1]).read_text()) or {}
d = data.get('deployment') or {}
n = d.get('nodes', {}); r = d.get('reservation', {}); rr = d.get('r2lab_reservation', {})
enabled = bool(r.get('enabled', True))
mode = r.get('mode', 'create' if enabled else 'disabled')
preparation = r.get('host_preparation', 'fresh' if mode != 'disabled' else 'preserve')
values = [
    d.get('core','open5gs'), d.get('ran','srsran'), d.get('platform','rfsim'), d.get('ru','rfsim'),
    n.get('core','sopnode-f2'), n.get('ran','sopnode-f3'), n.get('broker',n.get('core','sopnode-f2')),
    d.get('network_profile','default'), ','.join(d.get('ues',[])), str(enabled).lower(), mode, preparation,
    str(r.get('duration_minutes',120)), r.get('image','ubuntu-jammy'), d.get('r2lab_username',''),
    str(rr.get('enabled',True)).lower(), str(rr.get('duration_minutes',120)),
    d.get('network_profile_file',''), d.get('ue_catalog_file','deployment/group_vars/all/ue_catalog.yaml')
]
print('\n'.join(str(value) for value in values))
PY
    )
    DEFAULT_CORE=${SCENARIO_DEFAULTS[0]}
    DEFAULT_RAN=${SCENARIO_DEFAULTS[1]}
    DEFAULT_PLATFORM=${SCENARIO_DEFAULTS[2]}
    DEFAULT_RU=${SCENARIO_DEFAULTS[3]}
    DEFAULT_CORE_NODE=${SCENARIO_DEFAULTS[4]}
    DEFAULT_RAN_NODE=${SCENARIO_DEFAULTS[5]}
    DEFAULT_BROKER_NODE=${SCENARIO_DEFAULTS[6]}
    DEFAULT_NETWORK_PROFILE=${SCENARIO_DEFAULTS[7]}
    DEFAULT_UES=${SCENARIO_DEFAULTS[8]}
    DEFAULT_RESERVE=${SCENARIO_DEFAULTS[9]}
    DEFAULT_RESERVATION_MODE=${SCENARIO_DEFAULTS[10]}
    DEFAULT_HOST_PREPARATION=${SCENARIO_DEFAULTS[11]}
    DEFAULT_DURATION=${SCENARIO_DEFAULTS[12]}
    DEFAULT_POS_IMAGE=${SCENARIO_DEFAULTS[13]}
    DEFAULT_R2LAB_USERNAME=${SCENARIO_DEFAULTS[14]}
    DEFAULT_R2LAB_RESERVE=${SCENARIO_DEFAULTS[15]}
    DEFAULT_R2LAB_DURATION=${SCENARIO_DEFAULTS[16]}
    BASE_NETWORK_PROFILE_FILE=${SCENARIO_DEFAULTS[17]}
    UE_CATALOG_FILE=${SCENARIO_DEFAULTS[18]}
  fi

  echo
  printf '\033[1;36m'
  cat <<'BANNER'
  _____             _   _     _____            _   _
 / ____|           | | | |   |  __ \     /\   | \ | |
| (___  _   _ _ __ | |_| |__ | |__) |   /  \  |  \| |
 \___ \| | | | '_ \| __| '_ \|  _  /   / /\ \ | . ` |
 ____) | |_| | | | | |_| | | | | \ \  / ____ \| |\  |
|_____/ \__, |_| |_|\__|_| |_|_|  \_\/_/    \_\_| \_|
         __/ |
        |___/       5G/6G testbed deployment
BANNER
  printf '\033[0m'

  echo
  case "$DEFAULT_CORE" in oai) DEFAULT_CORE_CHOICE=1;; open5gs) DEFAULT_CORE_CHOICE=2;; free5gc) DEFAULT_CORE_CHOICE=3;; *) DEFAULT_CORE_CHOICE=2;; esac
  echo "Which CORE do you want to deploy? (default: $DEFAULT_CORE)"
  echo "1) OAI"
  echo "2) Open5GS"
  echo "3) Free5GC"
  read -r -p "Enter choice [1-3]: " CORE_CHOICE
  case "${CORE_CHOICE:-$DEFAULT_CORE_CHOICE}" in
    1) SELECTED_CORE=oai ;;
    2) SELECTED_CORE=open5gs ;;
    3) SELECTED_CORE=free5gc ;;
    *) echo "Invalid core choice" >&2; exit 2 ;;
  esac

  echo
  case "$DEFAULT_RAN" in oai) DEFAULT_RAN_CHOICE=1;; srsran) DEFAULT_RAN_CHOICE=2;; ueransim) DEFAULT_RAN_CHOICE=3;; *) DEFAULT_RAN_CHOICE=2;; esac
  echo "Which RAN do you want to deploy? (default: $DEFAULT_RAN)"
  echo "1) OAI"
  echo "2) srsRAN"
  echo "3) UERANSIM"
  read -r -p "Enter choice [1-3]: " RAN_CHOICE
  case "${RAN_CHOICE:-$DEFAULT_RAN_CHOICE}" in
    1) SELECTED_RAN=oai ;;
    2) SELECTED_RAN=srsran ;;
    3) SELECTED_RAN=ueransim ;;
    *) echo "Invalid ran choice" >&2; exit 2 ;;
  esac

  echo
  [[ "$DEFAULT_PLATFORM" == r2lab ]] && DEFAULT_PLATFORM_CHOICE=2 || DEFAULT_PLATFORM_CHOICE=1
  echo "Which platform do you want to use? (default: $DEFAULT_PLATFORM)"
  echo "1) RFSIM"
  echo "2) R2Lab physical radio"
  read -r -p "Enter choice [1-2]: " PLATFORM_CHOICE
  case "${PLATFORM_CHOICE:-$DEFAULT_PLATFORM_CHOICE}" in
    1) SELECTED_PLATFORM=rfsim; SELECTED_RU=rfsim ;;
    2) SELECTED_PLATFORM=r2lab ;;
    *) echo "Invalid platform choice" >&2; exit 2 ;;
  esac
  if [[ "$SELECTED_PLATFORM" == r2lab && "$SELECTED_RAN" == ueransim ]]; then
    echo "UERANSIM is a software RAN and cannot drive an R2Lab physical radio" >&2
    exit 2
  fi

  if [[ "$SELECTED_PLATFORM" == r2lab ]]; then
    show_r2lab_matrix
    echo
    case "$DEFAULT_RU" in
      n300) DEFAULT_RU_CHOICE=1 ;;
      n320) DEFAULT_RU_CHOICE=2 ;;
      *) DEFAULT_RU=n300; DEFAULT_RU_CHOICE=1 ;;
    esac
    echo "Which radio unit do you want to use? (default: $DEFAULT_RU)"
    echo "1) n300"
    echo "2) n320"
    read -r -p "Enter choice [1-2]: " RU_CHOICE
    case "${RU_CHOICE:-$DEFAULT_RU_CHOICE}" in
      1) SELECTED_RU=n300 ;;
      2) SELECTED_RU=n320 ;;
      *) echo "Invalid RU choice" >&2; exit 2 ;;
    esac

    if [[ -f .r2lab_config ]]; then
      SELECTED_R2LAB_USERNAME=${R2LAB_USERNAME:-$DEFAULT_R2LAB_USERNAME}
      echo "Using saved R2Lab credentials for ${SELECTED_R2LAB_USERNAME:-unknown}"
    else
      read -r -p "R2Lab username (slice name) [$DEFAULT_R2LAB_USERNAME]: " SELECTED_R2LAB_USERNAME
      SELECTED_R2LAB_USERNAME=${SELECTED_R2LAB_USERNAME:-$DEFAULT_R2LAB_USERNAME}
      read -r -p "R2Lab account email: " R2LAB_EMAIL
      read -r -s -p "R2Lab password: " R2LAB_PASSWORD
      echo
      ( umask 077
        printf 'R2LAB_USERNAME=%q\nR2LAB_EMAIL=%q\nR2LAB_PASSWORD=%q\n' \
          "$SELECTED_R2LAB_USERNAME" "$R2LAB_EMAIL" "$R2LAB_PASSWORD" > .r2lab_config
      )
      export R2LAB_USERNAME="$SELECTED_R2LAB_USERNAME" R2LAB_EMAIL R2LAB_PASSWORD
    fi
    [[ -n "$SELECTED_R2LAB_USERNAME" ]] || { echo "R2Lab username is required" >&2; exit 2; }
    export R2LAB_USERNAME="$SELECTED_R2LAB_USERNAME"

    [[ "$DEFAULT_R2LAB_RESERVE" == true ]] && R2LAB_RESERVE_PROMPT="Y/n" || R2LAB_RESERVE_PROMPT="y/N"
    read -r -p "Reserve the R2Lab testbed? [$R2LAB_RESERVE_PROMPT]: " R2LAB_RESERVE_CHOICE
    SELECTED_R2LAB_RESERVE=$DEFAULT_R2LAB_RESERVE
    SELECTED_R2LAB_DURATION=$DEFAULT_R2LAB_DURATION
    [[ "${R2LAB_RESERVE_CHOICE:-}" =~ ^[Yy]$ ]] && SELECTED_R2LAB_RESERVE=true
    [[ "${R2LAB_RESERVE_CHOICE:-}" =~ ^[Nn]$ ]] && SELECTED_R2LAB_RESERVE=false
    if $SELECTED_R2LAB_RESERVE; then
      read -r -p "R2Lab reservation duration in minutes [$DEFAULT_R2LAB_DURATION]: " SELECTED_R2LAB_DURATION
      SELECTED_R2LAB_DURATION=${SELECTED_R2LAB_DURATION:-$DEFAULT_R2LAB_DURATION}
      [[ "$SELECTED_R2LAB_DURATION" =~ ^[1-9][0-9]*$ ]] || { echo "R2Lab duration must be a positive integer" >&2; exit 2; }
    fi
  else
    SELECTED_R2LAB_USERNAME=""
    SELECTED_R2LAB_RESERVE=false
    SELECTED_R2LAB_DURATION=120
  fi

  choose_sop_node "core" "$DEFAULT_CORE_NODE"
  SELECTED_CORE_NODE="$SELECTED_NODE"
  choose_sop_node "RAN" "$DEFAULT_RAN_NODE"
  SELECTED_RAN_NODE="$SELECTED_NODE"
  SELECTED_BROKER_NODE="$SELECTED_CORE_NODE"

  mapfile -t CATALOG_UE_ROWS < <(catalog_ues "$SELECTED_PLATFORM")
  [[ ${#CATALOG_UE_ROWS[@]} -gt 0 ]] || { echo "UE catalog defines no UEs for platform $SELECTED_PLATFORM" >&2; exit 2; }
  CATALOG_UE_NAMES=()
  for index in "${!CATALOG_UE_ROWS[@]}"; do
    IFS=$'\t' read -r ue_name _ue_mode _ue_interface <<<"${CATALOG_UE_ROWS[$index]}"
    CATALOG_UE_NAMES+=("$ue_name")
  done
  echo
  if [[ "$SELECTED_PLATFORM" == r2lab ]]; then
    echo "Available R2Lab UEs"
  else
    echo "Available RFSIM UEs"
  fi
  echo "--------------------"
  print_ue_matrix "$SELECTED_PLATFORM" "${CATALOG_UE_ROWS[@]}"

  if [[ "$DEFAULT_PLATFORM" != "$SELECTED_PLATFORM" || -z "$DEFAULT_UES" ]]; then
    if [[ "$SELECTED_PLATFORM" == rfsim && ${#CATALOG_UE_NAMES[@]} -ge 2 ]]; then
      DEFAULT_UES="${CATALOG_UE_NAMES[0]},${CATALOG_UE_NAMES[1]}"
    else
      DEFAULT_UES="${CATALOG_UE_NAMES[0]}"
    fi
  fi
  read -r -p "UEs [$DEFAULT_UES]: " UE_INPUT
  if [[ -z "$UE_INPUT" ]]; then
    SELECTED_UES=$(expand_ue_selection "$SELECTED_PLATFORM" "$DEFAULT_UES")
  else
    SELECTED_UES=$(expand_ue_selection "$SELECTED_PLATFORM" "$UE_INPUT")
  fi
  [[ -n "$SELECTED_UES" ]] || { echo "At least one UE is required" >&2; exit 2; }

  choose_network_profile "$DEFAULT_NETWORK_PROFILE"

  mapfile -t NETWORK_SLICE_ROWS < <(network_profile_slices "$SELECTED_NETWORK_PROFILE")
  [[ ${#NETWORK_SLICE_ROWS[@]} -gt 0 ]] || { echo "Selected network profile has no slices" >&2; exit 2; }
  SLICE_NAMES=()
  echo
  echo "Slices in network profile $SELECTED_NETWORK_PROFILE"
  echo "-----------------------------------------------"
  for index in "${!NETWORK_SLICE_ROWS[@]}"; do
    IFS=$'\t' read -r slice_name slice_dnn slice_sst slice_sd slice_5qi slice_ul slice_dl <<<"${NETWORK_SLICE_ROWS[$index]}"
    SLICE_NAMES+=("$slice_name")
    printf '  %2d) %-8s DNN=%-10s SST=%s SD=%-6s 5QI=%s UL=%s DL=%s\n' \
      "$((index + 1))" "$slice_name" "$slice_dnn" "$slice_sst" "$slice_sd" "$slice_5qi" "$slice_ul" "$slice_dl"
  done

  IFS=',' read -r -a SELECTED_UE_ARRAY <<<"$SELECTED_UES"
  UE_SLICE_ASSIGNMENTS=()
  for ue_name in "${SELECTED_UE_ARRAY[@]}"; do
    default_slice=$(base_slice_default "$BASE_CONFIG" "$SELECTED_NETWORK_PROFILE" "$ue_name")
    default_slice_index=1
    if [[ -n "$default_slice" ]]; then
      for index in "${!SLICE_NAMES[@]}"; do
        if [[ "${SLICE_NAMES[$index]}" == "$default_slice" ]]; then
          default_slice_index=$((index + 1))
          break
        fi
      done
    fi
    read -r -p "Slice for $ue_name [$default_slice_index]: " slice_choice
    slice_choice=${slice_choice:-$default_slice_index}
    [[ "$slice_choice" =~ ^[1-9][0-9]*$ ]] || { echo "Slice choice for $ue_name must be a number" >&2; exit 2; }
    (( slice_choice >= 1 && slice_choice <= ${#SLICE_NAMES[@]} )) || {
      echo "Slice choice for $ue_name must be within 1-${#SLICE_NAMES[@]}" >&2
      exit 2
    }
    UE_SLICE_ASSIGNMENTS+=("$ue_name=${SLICE_NAMES[$((slice_choice - 1))]}")
  done
  SELECTED_UE_SLICE_SPEC=$(IFS=,; echo "${UE_SLICE_ASSIGNMENTS[*]}")

  [[ "$DEFAULT_RESERVE" == true ]] && RESERVE_PROMPT="Y/n" || RESERVE_PROMPT="y/N"
  read -r -p "Ensure selected SOP nodes are reserved? [$RESERVE_PROMPT]: " RESERVE_CHOICE
  SELECTED_RESERVE=$DEFAULT_RESERVE
  [[ "${RESERVE_CHOICE:-}" =~ ^[Yy]$ ]] && SELECTED_RESERVE=true
  [[ "${RESERVE_CHOICE:-}" =~ ^[Nn]$ ]] && SELECTED_RESERVE=false

  SELECTED_DURATION=$DEFAULT_DURATION
  SELECTED_POS_IMAGE=$DEFAULT_POS_IMAGE
  SELECTED_HOST_PREPARATION=preserve
  if $SELECTED_RESERVE; then
    if [[ "$DEFAULT_RESERVATION_MODE" == require-existing ]]; then
      SELECTED_RESERVATION_MODE=require-existing
    else
      SELECTED_RESERVATION_MODE=create
    fi
    read -r -p "Reservation duration in minutes [$DEFAULT_DURATION]: " SELECTED_DURATION
    SELECTED_DURATION=${SELECTED_DURATION:-$DEFAULT_DURATION}
    [[ "$SELECTED_DURATION" =~ ^[1-9][0-9]*$ ]] || { echo "Duration must be a positive integer" >&2; exit 2; }

    case "$DEFAULT_HOST_PREPARATION" in
      preserve) DEFAULT_PREP_CHOICE=1 ;;
      bootstrap) DEFAULT_PREP_CHOICE=2 ;;
      fresh) DEFAULT_PREP_CHOICE=3 ;;
      *) DEFAULT_PREP_CHOICE=3 ;;
    esac
    echo
    echo "How should SynthRAN prepare the selected SOP nodes?"
    echo "1) Reuse verified current node state"
    echo "   No host repair or reboot. Deployment stops if required prerequisites are missing."
    echo "2) Bootstrap/reconcile current node state"
    echo "   Keep the current OS/allocation and repair safe prerequisites in place; a bounded reboot may be used, but no reimage."
    echo "3) Fresh reset/reimage"
    echo "   Use the known-clean POS image/reset path and rebuild host/Kubernetes prerequisites."
    read -r -p "Enter choice [1-3] [$DEFAULT_PREP_CHOICE]: " PREP_CHOICE
    case "${PREP_CHOICE:-$DEFAULT_PREP_CHOICE}" in
      1) SELECTED_HOST_PREPARATION=preserve ;;
      2) SELECTED_HOST_PREPARATION=bootstrap ;;
      3) SELECTED_HOST_PREPARATION=fresh ;;
      *) echo "Invalid SOP preparation choice" >&2; exit 2 ;;
    esac

    if [[ "$SELECTED_HOST_PREPARATION" == fresh ]]; then
      read -r -p "POS image [$DEFAULT_POS_IMAGE]: " SELECTED_POS_IMAGE
      SELECTED_POS_IMAGE=${SELECTED_POS_IMAGE:-$DEFAULT_POS_IMAGE}
    fi
  else
    SELECTED_RESERVATION_MODE=disabled
  fi

  CONFIG="$RUN_DIR/interactive-scenario.yml"
  "$SYNTHRAN_PYTHON" - "$BASE_CONFIG" "$CONFIG" "$SELECTED_CORE" "$SELECTED_RAN" "$SELECTED_PLATFORM" "$SELECTED_RU" "$SELECTED_CORE_NODE" "$SELECTED_RAN_NODE" "$SELECTED_BROKER_NODE" "$SELECTED_NETWORK_PROFILE" "$SELECTED_UES" "$SELECTED_UE_SLICE_SPEC" "$SELECTED_R2LAB_USERNAME" "$SELECTED_RESERVE" "$SELECTED_RESERVATION_MODE" "$SELECTED_HOST_PREPARATION" "$SELECTED_DURATION" "$SELECTED_POS_IMAGE" "$SELECTED_R2LAB_RESERVE" "$SELECTED_R2LAB_DURATION" <<'PY'
import copy
import sys
from pathlib import Path
import yaml

(
    source, output, core, ran, platform, ru, core_node, ran_node, broker_node,
    network_profile, ue_csv, ue_slice_spec, r2lab_username, reserve,
    reservation_mode, host_preparation, duration, pos_image, r2_reserve, r2_duration,
) = sys.argv[1:]
source_data = {}
if source:
    source_data = yaml.safe_load(Path(source).read_text()) or {}
dep = copy.deepcopy(source_data.get('deployment') or {})
ues = [name.strip() for name in ue_csv.split(',') if name.strip()]
if not ues:
    raise SystemExit('At least one UE is required')
if platform == 'r2lab' and ran == 'ueransim':
    raise SystemExit('UERANSIM is a software RAN and cannot drive an R2Lab physical radio')
ue_slices = {}
for pair in filter(None, ue_slice_spec.split(',')):
    name, slice_name = pair.split('=', 1)
    ue_slices[name] = slice_name
if set(ue_slices) != set(ues):
    raise SystemExit('Interactive UE slice assignments do not match selected UEs')

host_vars = dep.get('host_vars', {})
if network_profile != dep.get('network_profile'):
    dep.pop('network_profile_file', None)
dep.update({
    'core': core,
    'ran': ran,
    'platform': platform,
    'network_profile': network_profile,
    'ru': ru,
    'nodes': {'core': core_node, 'ran': ran_node, 'broker': broker_node},
    'ues': ues,
    'ue_slices': ue_slices,
})
dep['host_vars'] = host_vars
dep['reservation'] = {
    'enabled': reserve == 'true',
    'mode': reservation_mode,
    'host_preparation': host_preparation,
    'duration_minutes': int(duration),
    'image': pos_image,
}
dep['r2lab_reservation'] = {'enabled': r2_reserve == 'true', 'duration_minutes': int(r2_duration)}
for legacy in ('profile', 'profile_file', 'ue_profiles', 'r2lab_experiment_nodes'):
    dep.pop(legacy, None)
if r2lab_username:
    dep['r2lab_username'] = r2lab_username
else:
    dep.pop('r2lab_username', None)
Path(output).write_text(yaml.safe_dump({'deployment': dep}, sort_keys=False))
PY

  echo
  echo "Deployment summary"
  echo "  Core:            $SELECTED_CORE on $SELECTED_CORE_NODE"
  echo "  RAN:             $SELECTED_RAN on $SELECTED_RAN_NODE"
  echo "  Platform:        $SELECTED_PLATFORM ($SELECTED_RU)"
  echo "  UEs:             $SELECTED_UES"
  echo "  Network profile: $SELECTED_NETWORK_PROFILE"
  echo "  UE slices:"
  describe_ue_slice_assignments "$SELECTED_NETWORK_PROFILE" "$SELECTED_UE_SLICE_SPEC"
  if $SELECTED_RESERVE; then
    case "$SELECTED_HOST_PREPARATION" in
      preserve)
        echo "  POS:             true, ${SELECTED_DURATION}m, preserve (verify/reuse only; no repair)"
        ;;
      bootstrap)
        echo "  POS:             true, ${SELECTED_DURATION}m, bootstrap (retain OS/allocation; bounded reconcile/reboot)"
        ;;
      fresh)
        echo "  POS:             true, ${SELECTED_DURATION}m, fresh reset/reimage, image $SELECTED_POS_IMAGE"
        ;;
    esac
  else
    echo "  POS:             false"
  fi
  [[ "$SELECTED_PLATFORM" == r2lab ]] && echo "  R2Lab:           $SELECTED_R2LAB_RESERVE, ${SELECTED_R2LAB_DURATION}m"
  read -r -p "Continue? [Y/n]: " CONFIRM_DEPLOY
  [[ ! "${CONFIRM_DEPLOY:-y}" =~ ^[Nn]$ ]] || exit 0
fi

SOURCE_CONFIG="$CONFIG"
[[ -n "$SOURCE_CONFIG" && -f "$SOURCE_CONFIG" ]] || { echo "No testbed scenario was produced" >&2; exit 2; }

PUBLIC_CONFIG="$RUN_DIR/resolved-scenario.yml"
CONFIG="$PRIVATE_RUN_DIR/resolved-scenario.yml"
"$SYNTHRAN_PYTHON" -m synthran.deployment_state resolve \
  --source "$SOURCE_CONFIG" --output "$CONFIG"

write_public_scenario() {
  "$SYNTHRAN_PYTHON" - "$CONFIG" "$PUBLIC_CONFIG" <<'PY'
import sys, yaml
from pathlib import Path
from synthran.scenario import redacted
source, output = map(Path, sys.argv[1:3])
data = yaml.safe_load(source.read_text()) or {}
output.write_text(yaml.safe_dump(redacted(data), sort_keys=False))
PY
}
write_public_scenario

if ! $DRY_RUN; then
  "$SYNTHRAN_PYTHON" -m synthran.deployment_state invalidate \
    --active "$ACTIVE_DEPLOYMENT_STATE" \
    --endpoint "$ACTIVE_DEPLOYMENT_ENDPOINT"
fi

if ! $NO_RESERVATION && ! $DRY_RUN; then
  deployment_section "Resolving the SOP reservation"
  command -v pos >/dev/null || { echo "POS reservation requested but the pos command is unavailable" >&2; exit 1; }
  "$SYNTHRAN_PYTHON" deployment/scripts/reserve_sop.py "$CONFIG" "$RUN_DIR"
  write_public_scenario
fi

"$SYNTHRAN_PYTHON" -m synthran.inventory "$CONFIG" "$RUN_DIR"

if $DRY_RUN; then
  echo "Prepared testbed deployment in $RUN_DIR; provisioning skipped"
  exit 0
fi

mapfile -t R2LAB_SETTINGS < <("$SYNTHRAN_PYTHON" - "$CONFIG" <<'PY'
import sys, yaml
from synthran.r2lab import access
d = yaml.safe_load(open(sys.argv[1]))['deployment']
r = d.get('r2lab_reservation', {})
a = access(d)
print(d.get('platform', 'rfsim'))
print('true' if r.get('enabled', True) else 'false')
print(int(r.get('duration_minutes', 120)))
print(a['host'])
print(a['username'])
print(a['identity_file'])
PY
)
if [[ "${R2LAB_SETTINGS[0]}" == r2lab && "${R2LAB_SETTINGS[1]}" == true && "$NO_RESERVATION" == false ]]; then
  deployment_section "Preparing the R2Lab reservation"
  R2LAB_DURATION=${R2LAB_SETTINGS[2]}
  R2LAB_HOST=${R2LAB_SETTINGS[3]}
  R2LAB_USERNAME=${R2LAB_SETTINGS[4]}
  R2LAB_IDENTITY_FILE=${R2LAB_SETTINGS[5]}
  [[ -n "${R2LAB_USERNAME:-}" ]] || { echo "R2Lab username must be set in .r2lab_config or the scenario" >&2; exit 1; }

  R2LAB_CLOCK=$(TZ=Europe/Paris date +'%H%M')
  R2LAB_START="$(TZ=Europe/Paris date +'%Y-%m-%dT')${R2LAB_CLOCK:0:2}:${R2LAB_CLOCK:2:1}0"
  R2LAB_START_EPOCH=$(TZ=Europe/Paris date -d "$R2LAB_START" +%s)
  R2LAB_END_EPOCH=$((R2LAB_START_EPOCH + R2LAB_DURATION * 60))

  if [[ -f "$RUN_DIR/pos-selection.json" ]]; then
    POS_COVERAGE_END=$("$SYNTHRAN_PYTHON" - "$RUN_DIR/pos-selection.json" <<'PY'
import json, sys
value = json.load(open(sys.argv[1]))
print(value.get('coverage_end', ''))
PY
)
    if [[ -n "$POS_COVERAGE_END" ]]; then
      POS_COVERAGE_END_EPOCH=$(date -d "$POS_COVERAGE_END" +%s)
      if (( POS_COVERAGE_END_EPOCH <= R2LAB_START_EPOCH )); then
        echo "Accepted SOP reservation ends before the R2Lab deployment window starts" >&2
        exit 1
      fi
      if (( POS_COVERAGE_END_EPOCH < R2LAB_END_EPOCH )); then
        R2LAB_END_EPOCH=$POS_COVERAGE_END_EPOCH
        echo "Capping R2Lab coverage at the accepted SOP reservation end: $POS_COVERAGE_END"
      fi
    fi
  fi

  R2LAB_END=$(TZ=Europe/Paris date -d "@$R2LAB_END_EPOCH" +'%Y-%m-%dT%H:%M')
  echo "Resolving provider-backed R2Lab coverage for $R2LAB_START to $R2LAB_END"
  "$SYNTHRAN_PYTHON" -m synthran.phase_timing start \
    --run-dir "$RUN_DIR" --phase reservation --scope r2lab
  if ! printf '%s\n' "${R2LAB_PASSWORD:-}" | \
    "$SYNTHRAN_PYTHON" deployment/scripts/reserve_r2lab.py \
      --host "$R2LAB_HOST" \
      --username "$R2LAB_USERNAME" \
      --identity-file "$R2LAB_IDENTITY_FILE" \
      --known-hosts "$R2LAB_FARADAY_KNOWN_HOSTS" \
      --email "${R2LAB_EMAIL:-}" \
      --start "$R2LAB_START" \
      --end "$R2LAB_END" \
      --output "$RUN_DIR/r2lab-lease.json" \
      --log "$RUN_DIR/r2lab-reservation.log"; then
    "$SYNTHRAN_PYTHON" -m synthran.phase_timing finish \
      --run-dir "$RUN_DIR" --phase reservation --scope r2lab --status failed || true
    echo "R2Lab reservation/coverage verification failed; the separate SOP allocation was left intact" >&2
    exit 1
  fi
  "$SYNTHRAN_PYTHON" -m synthran.phase_timing finish \
    --run-dir "$RUN_DIR" --phase reservation --scope r2lab
fi

deployment_section "Preparing Ansible dependencies"
ANSIBLE_GALAXY="$PWD/.venv/bin/ansible-galaxy"
ANSIBLE_PLAYBOOK="$PWD/.venv/bin/ansible-playbook"
[[ -x "$ANSIBLE_GALAXY" && -x "$ANSIBLE_PLAYBOOK" ]] || { echo "The isolated Ansible runtime was not installed correctly" >&2; exit 1; }
if ! "$ANSIBLE_GALAXY" collection install -r deployment/collections/requirements.yml >"$RUN_DIR/ansible-galaxy.log" 2>&1; then
  cat "$RUN_DIR/ansible-galaxy.log" >&2
  echo "Ansible dependency preparation failed; full output: $RUN_DIR/ansible-galaxy.log" >&2
  exit 1
fi

deployment_section "Provisioning nodes and deploying the selected 5G stack"
export ANSIBLE_ROLES_PATH="$PWD/deployment/roles"
export ANSIBLE_CONFIG="$PWD/deployment/ansible.cfg"
export ANSIBLE_FORCE_COLOR=0
export PYTHONUNBUFFERED=1
DEPLOYMENT_PLAYBOOK="$PRIVATE_RUN_DIR/ansible/playbooks/site.yml"
ANSIBLE_COMMAND=("$ANSIBLE_PLAYBOOK" -i "$PRIVATE_RUN_DIR/inventory.yml"
  -e "@deployment/group_vars/all/all.yml"
  -e "@$PRIVATE_RUN_DIR/deployment-vars.yml"
  "$DEPLOYMENT_PLAYBOOK")
if $VERBOSE; then
  ANSIBLE_COMMAND+=(--verbose)
fi

exec bash deployment/scripts/run_deployment.sh \
  "$RUN_DIR" "$SYNTHRAN_PYTHON" "$ACTIVE_DEPLOYMENT_STATE" \
  "${ANSIBLE_COMMAND[@]}"
