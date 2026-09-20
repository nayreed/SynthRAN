#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
export PATH="$HOME/.local/bin:$PATH"

if [[ -f .r2lab_config ]]; then
  # shellcheck disable=SC1091
  source .r2lab_config
fi

# Connection identity may be inherited by provider helpers, but the password
# must not be exported into the controller/Ansible child-process environment.
export R2LAB_USERNAME R2LAB_EMAIL R2LAB_IDENTITY_FILE R2LAB_FARADAY_KNOWN_HOSTS
export -n R2LAB_PASSWORD 2>/dev/null || true

command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
SYNTHRAN_PYTHON="$(python3 -m synthran.runtime python)"
"$SYNTHRAN_PYTHON" -m synthran.runtime deployment --log /tmp/synthran-teardown-bootstrap.log

# Open the secret channel only after runtime bootstrap. The controller receives
# only the descriptor number; reservation_release consumes and closes it before
# spawning ssh, so neither Ansible nor the provider child inherits the password.
if [[ -n "${R2LAB_PASSWORD:-}" ]]; then
  exec 8<<<"$R2LAB_PASSWORD"
  export SYNTHRAN_R2LAB_PASSWORD_FD=8
  unset R2LAB_PASSWORD
fi

exec "$SYNTHRAN_PYTHON" -m synthran.teardown_controller "$@"
