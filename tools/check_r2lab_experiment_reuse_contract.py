#!/usr/bin/env python3
"""Guard Sub 09's accepted-testbed reuse boundary against modem mutation."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT_RUNTIME = ROOT / "synthran/experiment_runtime"
EX2 = ROOT / "Experiments/Ex2_Flagship_Causal_5G_Transport"

# These are lifecycle mutation owners during deployment. None may be invoked by
# an experiment after deploy.sh has accepted the physical UE binding.
FORBIDDEN = {
    "qhat-init": "modem initialization",
    "start.sh": "MBIM attachment",
    "stop.sh": "MBIM detach",
    "ci_ctl_qtel.py": "QMI modem control",
    "quectel-CM": "QMI connection-manager mutation",
    "rhubarbe pdu on": "QHAT power-on",
    "rhubarbe pdu off": "QHAT power-off",
    "qfit on": "QFIT power-on",
    "qfit off": "QFIT power-off",
}


def execution_surfaces() -> list[Path]:
    files = [ROOT / "experiment.sh", ROOT / "synthran/experiments.py"]
    for directory in (EXPERIMENT_RUNTIME, EX2):
        for path in directory.rglob("*"):
            if path.is_file() and path.suffix in {".py", ".yml", ".yaml", ".sh"}:
                files.append(path)
    return sorted(set(files))


def main() -> None:
    surfaces = execution_surfaces()
    if not surfaces:
        raise SystemExit("no experiment execution surfaces found")

    violations: list[str] = []
    for path in surfaces:
        content = path.read_text(encoding="utf-8")
        for needle, meaning in FORBIDDEN.items():
            if needle in content:
                violations.append(
                    f"{path.relative_to(ROOT)} contains {needle!r} ({meaning})"
                )

    if violations:
        raise SystemExit(
            "Sub 09 read-only reuse boundary violated:\n- " + "\n- ".join(violations)
        )

    runner = (EXPERIMENT_RUNTIME / "runner.py").read_text(encoding="utf-8")
    replay = (
        EXPERIMENT_RUNTIME / "ansible/roles/publisher/tasks/replay.yml"
    ).read_text(encoding="utf-8")
    if "attach_active_deployment" not in runner:
        raise SystemExit("experiment runtime no longer attaches through accepted-testbed state")
    if "--interface wwan0" not in replay:
        raise SystemExit("R2Lab replay no longer binds workload traffic to the accepted UE interface")

    print(
        f"Sub 09 experiment reuse contract OK: {len(surfaces)} execution surfaces "
        "contain no UE reset/reattach owner"
    )


if __name__ == "__main__":
    main()
