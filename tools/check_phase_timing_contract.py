#!/usr/bin/env python3
"""Issue #124 Task 11 deployment phase timing evidence contract."""

from __future__ import annotations

import json
import tempfile
import threading
from pathlib import Path

import yaml

from synthran import phase_timing

ROOT = Path(__file__).resolve().parents[1]


class CheckError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CheckError(message)


def require_text(path: Path, needle: str) -> None:
    text = path.read_text(encoding="utf-8")
    require(needle in text, f"{path}: missing timing marker {needle!r}")


def behavioral_contract() -> None:
    with tempfile.TemporaryDirectory(prefix="synthran-phase-timing-") as value:
        run_dir = Path(value)

        phase_timing.start(run_dir, "kubernetes_bootstrap")
        finished = phase_timing.finish(run_dir, "kubernetes_bootstrap")
        require(finished["status"] == "success", "finished phase lost success status")
        require(
            isinstance(finished.get("elapsed_seconds"), (int, float))
            and finished["elapsed_seconds"] >= 0,
            "finished phase has no bounded elapsed duration",
        )

        marker = phase_timing.capture_start()
        failed = phase_timing.record_interval(
            run_dir,
            "pos_image_staging",
            scope="sopnode-f2",
            started=marker,
            status="failed",
        )
        require(failed["status"] == "failed", "failed interval status was lost")

        # Concurrent POS workers must not overwrite each other's evidence.
        barrier = threading.Barrier(4, timeout=10)
        failures: list[BaseException] = []

        def writer(index: int) -> None:
            try:
                started = phase_timing.capture_start()
                barrier.wait()
                phase_timing.record_interval(
                    run_dir,
                    "ssh_readiness",
                    scope=f"sopnode-{index}",
                    started=started,
                )
            except BaseException as exc:  # surfaced below in the main thread
                failures.append(exc)

        workers = [threading.Thread(target=writer, args=(index,)) for index in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)
        require(not any(worker.is_alive() for worker in workers), "timing writer thread did not terminate")
        require(not failures, f"concurrent timing write failed: {failures}")

        phase_timing.start(run_dir, "stack_deployment")
        phase_timing.start(run_dir, "verification")
        closed = phase_timing.close_open(
            run_dir,
            status="failed",
            reason="synthetic controller failure",
        )
        require(len(closed) == 2, f"close-open did not close both phases: {closed}")
        require(
            all(record["status"] == "failed" for record in closed),
            "close-open lost failed status",
        )

        artifact = json.loads((run_dir / phase_timing.ARTIFACT).read_text(encoding="utf-8"))
        require(artifact["schema"] == phase_timing.SCHEMA, "timing schema changed")
        records = artifact["records"]
        require(len(records) == 8, f"unexpected timing record count: {len(records)}")
        require(
            all(record.get("status") != "running" for record in records),
            "final timing artifact retained an open phase",
        )
        for record in records:
            require("started_at" in record, f"record lost start timestamp: {record}")
            require("completed_at" in record, f"record lost completion timestamp: {record}")
            require(
                isinstance(record.get("elapsed_seconds"), (int, float))
                and record["elapsed_seconds"] >= 0,
                f"record lost elapsed duration: {record}",
            )
            require(
                "started_monotonic_ns" not in record,
                f"final timing record leaked internal open marker: {record}",
            )


def instrumentation_contract() -> None:
    reservation = ROOT / "synthran/reservation.py"
    for marker in (
        '"reservation"',
        '"pos_image_staging"',
        '"boot_parameter_mutation"',
        '"pos_reset"',
        '"ssh_readiness"',
        "phase_timing.capture_start()",
        "phase_timing.record_interval(",
    ):
        require_text(reservation, marker)

    deploy = ROOT / "deploy.sh"
    require_text(deploy, "--phase reservation --scope r2lab")

    bootstrap_classify = ROOT / "deployment/playbooks/bootstrap_classify.yml"
    yaml.safe_load(bootstrap_classify.read_text(encoding="utf-8"))
    require_text(bootstrap_classify, "Start bootstrap boot-parameter mutation timing")
    require_text(bootstrap_classify, "- boot_parameter_mutation")
    require_text(bootstrap_classify, "Start bootstrap reboot timing")
    require_text(bootstrap_classify, "- bootstrap_reboot")

    bootstrap = ROOT / "deployment/playbooks/bootstrap_nodes.yml"
    yaml.safe_load(bootstrap.read_text(encoding="utf-8"))
    require_text(bootstrap, "- kubernetes_bootstrap")

    r2lab = ROOT / "deployment/playbooks/provision_r2lab.yml"
    yaml.safe_load(r2lab.read_text(encoding="utf-8"))
    require_text(r2lab, "- r2lab_rru_setup")
    require_text(r2lab, "- ue_setup")

    network = ROOT / "deployment/playbooks/network.yml"
    yaml.safe_load(network.read_text(encoding="utf-8"))
    require_text(network, "- stack_deployment")

    controller = ROOT / "deployment/scripts/run_deployment.sh"
    require_text(controller, "--phase verification")
    require_text(controller, "synthran.phase_timing close-open")
    require_text(controller, "timing_closure_status=failed")
    require_text(controller, "timing_closure_status=incomplete")

    inventory = ROOT / "synthran/inventory.py"
    require_text(inventory, '"synthran_controller_python": sys.executable')


def main() -> int:
    behavioral_contract()
    instrumentation_contract()
    print("Issue #124 Task 11 phase timing evidence contract OK")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CheckError, phase_timing.PhaseTimingError, OSError, ValueError) as exc:
        raise SystemExit(f"phase-timing-contract: {exc}") from exc
