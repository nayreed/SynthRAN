#!/usr/bin/env python3
"""Regression checks for safe multi-node POS fresh preparation."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import Any

from synthran import reservation


class CheckError(RuntimeError):
    pass


def done(argv: list[str], rc: int = 0, out: str = "", err: str = ""):
    return subprocess.CompletedProcess(list(argv), rc, out, err)


class Fake:
    def __init__(self, handler):
        self.handler = handler
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, check=True, stdin=None):
        command = list(argv)
        self.calls.append(command)
        result = self.handler(command, len(self.calls))
        if check and result.returncode:
            detail = reservation._output(result) or str(result.returncode)
            raise reservation.ReservationError(
                f"command failed: {' '.join(command)}\n{detail}"
            )
        return result


def is_allocate(command: list[str], node: str) -> bool:
    return (
        command[:3] == ["pos", "allocations", "allocate"]
        and len(command) == 6
        and command[3] == "--result-folder"
        and command[4].startswith("synthran-")
        and command[5] == node
    )


def assert_no_destructive_host_mutation(calls: list[list[str]]) -> None:
    forbidden = (
        ["pos", "allocations", "free", "-k"],
        ["pos", "nodes", "image"],
        ["pos", "nodes", "bootparameter"],
        ["pos", "nodes", "reset"],
    )
    for command in calls:
        if any(command[: len(prefix)] == prefix for prefix in forbidden):
            raise CheckError(f"destructive command ran before all allocation probes succeeded: {command}")


def failed_second_probe_is_non_destructive() -> None:
    selected = ["sopnode-f2", "sopnode-f3"]

    def handler(argv: list[str], _n: int):
        if is_allocate(argv, "sopnode-f2"):
            return done(argv, rc=1, out="Nodes are already allocated: sopnode-f2")
        if is_allocate(argv, "sopnode-f3"):
            return done(argv, rc=7, err="provider allocation failure")
        raise CheckError(f"unexpected command after failed probe: {argv}")

    fake = Fake(handler)
    original = reservation.run
    reservation.run = fake
    try:
        try:
            reservation.prepare_hosts(
                {"host_preparation": "fresh", "image": "configured-image"},
                selected=selected,
                calendar={"status": "reused"},
            )
        except reservation.ReservationError as exc:
            if "provider allocation failure" not in str(exc):
                raise CheckError(f"unexpected failure: {exc}") from exc
        else:
            raise CheckError("expected second allocation probe to fail")
    finally:
        reservation.run = original

    if len(fake.calls) != 2 or not is_allocate(fake.calls[0], "sopnode-f2") or not is_allocate(fake.calls[1], "sopnode-f3"):
        raise CheckError(f"unexpected probe sequence: {fake.calls}")
    assert_no_destructive_host_mutation(fake.calls)


def all_nodes_are_probed_before_reclaim_or_image() -> None:
    selected = ["sopnode-f2", "sopnode-f3"]
    first_f2 = True

    def handler(argv: list[str], _n: int):
        nonlocal first_f2
        if is_allocate(argv, "sopnode-f2") and first_f2:
            first_f2 = False
            return done(argv, rc=1, out="Nodes are already allocated: sopnode-f2")
        if argv[:3] == ["pos", "allocations", "allocate"]:
            return done(argv, out=f"Allocation ID: ci-{argv[-1]}")
        if argv[:4] == ["pos", "allocations", "free", "-k"]:
            return done(argv)
        if argv[:3] in (
            ["pos", "nodes", "image"],
            ["pos", "nodes", "bootparameter"],
            ["pos", "nodes", "reset"],
        ):
            return done(argv)
        if argv and argv[0] == "ssh":
            return done(argv)
        raise CheckError(f"unexpected command: {argv}")

    fake = Fake(handler)
    original = reservation.run
    old_attempts = os.environ.get("SYNTHRAN_POS_READY_ATTEMPTS")
    old_interval = os.environ.get("SYNTHRAN_POS_READY_INTERVAL_SECONDS")
    os.environ["SYNTHRAN_POS_READY_ATTEMPTS"] = "1"
    os.environ["SYNTHRAN_POS_READY_INTERVAL_SECONDS"] = "0"
    reservation.run = fake
    try:
        result = reservation.prepare_hosts(
            {"host_preparation": "fresh", "image": "configured-image"},
            selected=selected,
            calendar={"status": "reused"},
        )
    finally:
        reservation.run = original
        if old_attempts is None:
            os.environ.pop("SYNTHRAN_POS_READY_ATTEMPTS", None)
        else:
            os.environ["SYNTHRAN_POS_READY_ATTEMPTS"] = old_attempts
        if old_interval is None:
            os.environ.pop("SYNTHRAN_POS_READY_INTERVAL_SECONDS", None)
        else:
            os.environ["SYNTHRAN_POS_READY_INTERVAL_SECONDS"] = old_interval

    if result["nodes"]["sopnode-f2"]["allocation"] != "reclaimed":
        raise CheckError("sopnode-f2 should be recorded as reclaimed")
    if result["nodes"]["sopnode-f3"]["allocation"] != "new":
        raise CheckError("sopnode-f3 should be recorded as newly allocated")

    probe_f2 = next(index for index, command in enumerate(fake.calls) if is_allocate(command, "sopnode-f2"))
    probe_f3 = next(index for index, command in enumerate(fake.calls) if is_allocate(command, "sopnode-f3"))
    reclaim_f2 = fake.calls.index(["pos", "allocations", "free", "-k", "sopnode-f2"])
    first_image = next(
        index for index, command in enumerate(fake.calls)
        if command[:3] == ["pos", "nodes", "image"]
    )
    if not (probe_f2 < probe_f3 < reclaim_f2 < first_image):
        raise CheckError(f"two-phase ordering regressed: {fake.calls}")



def independent_fresh_preparation_overlaps() -> None:
    selected = ["sopnode-f2", "sopnode-f3"]
    image_barrier = threading.Barrier(2, timeout=10)

    def handler(argv: list[str], _n: int):
        if argv[:3] == ["pos", "allocations", "allocate"]:
            return done(argv, out=f"Allocation ID: ci-{argv[-1]}")
        if argv[:3] == ["pos", "nodes", "image"]:
            try:
                image_barrier.wait()
            except threading.BrokenBarrierError as exc:
                raise CheckError(
                    "fresh per-node preparation did not overlap after the authority barrier"
                ) from exc
            return done(argv)
        if argv[:3] in (
            ["pos", "nodes", "bootparameter"],
            ["pos", "nodes", "reset"],
        ):
            return done(argv)
        if argv and argv[0] == "ssh":
            return done(argv)
        raise CheckError(f"unexpected parallel preparation command: {argv}")

    fake = Fake(handler)
    original = reservation.run
    old_attempts = os.environ.get("SYNTHRAN_POS_READY_ATTEMPTS")
    old_interval = os.environ.get("SYNTHRAN_POS_READY_INTERVAL_SECONDS")
    os.environ["SYNTHRAN_POS_READY_ATTEMPTS"] = "1"
    os.environ["SYNTHRAN_POS_READY_INTERVAL_SECONDS"] = "0"
    reservation.run = fake
    try:
        result = reservation.prepare_hosts(
            {"host_preparation": "fresh", "image": "configured-image"},
            selected=selected,
            calendar={"status": "created"},
        )
    finally:
        reservation.run = original
        if old_attempts is None:
            os.environ.pop("SYNTHRAN_POS_READY_ATTEMPTS", None)
        else:
            os.environ["SYNTHRAN_POS_READY_ATTEMPTS"] = old_attempts
        if old_interval is None:
            os.environ.pop("SYNTHRAN_POS_READY_INTERVAL_SECONDS", None)
        else:
            os.environ["SYNTHRAN_POS_READY_INTERVAL_SECONDS"] = old_interval

    first_image = next(
        index for index, command in enumerate(fake.calls)
        if command[:3] == ["pos", "nodes", "image"]
    )
    last_probe = max(
        next(index for index, command in enumerate(fake.calls) if is_allocate(command, node))
        for node in selected
    )
    if last_probe >= first_image:
        raise CheckError(f"parallel phase crossed the allocation-authority barrier: {fake.calls}")
    if result.get("parallel_preparation") is not True:
        raise CheckError("multi-node fresh preparation did not record parallel execution")
    for node in selected:
        record = result["nodes"][node]
        if record.get("status") != "ready":
            raise CheckError(f"{node} did not retain ready per-node evidence: {record}")
        if record.get("completed_phases") != [
            "image-staging",
            "boot-parameters",
            "reset",
            "ssh-readiness",
        ]:
            raise CheckError(f"{node} phase evidence is incomplete: {record}")


def parallel_failure_retains_per_node_evidence() -> None:
    selected = ["sopnode-f2", "sopnode-f3"]
    image_barrier = threading.Barrier(2, timeout=10)
    failed_image_returned = threading.Event()

    def handler(argv: list[str], _n: int):
        if argv[:3] == ["pos", "allocations", "allocate"]:
            return done(argv)
        if argv[:3] == ["pos", "nodes", "image"]:
            try:
                image_barrier.wait()
            except threading.BrokenBarrierError as exc:
                raise CheckError("parallel failure fixture never reached both image workers") from exc
            node = argv[-2]
            if node == "sopnode-f2":
                failed_image_returned.set()
                return done(argv, rc=9, err="synthetic image failure")
            if not failed_image_returned.wait(timeout=5):
                raise CheckError("peer image failure was not released in the fixture")
            # Let the failing worker propagate its error and set the shared
            # stop signal before this worker crosses the next phase boundary.
            time.sleep(0.05)
            return done(argv)
        if argv[:3] in (
            ["pos", "nodes", "bootparameter"],
            ["pos", "nodes", "reset"],
        ):
            return done(argv)
        if argv and argv[0] == "ssh":
            return done(argv)
        raise CheckError(f"unexpected parallel failure command: {argv}")

    fake = Fake(handler)
    original = reservation.run
    old_attempts = os.environ.get("SYNTHRAN_POS_READY_ATTEMPTS")
    old_interval = os.environ.get("SYNTHRAN_POS_READY_INTERVAL_SECONDS")
    os.environ["SYNTHRAN_POS_READY_ATTEMPTS"] = "1"
    os.environ["SYNTHRAN_POS_READY_INTERVAL_SECONDS"] = "0"
    reservation.run = fake
    try:
        try:
            reservation.prepare_hosts(
                {"host_preparation": "fresh", "image": "configured-image"},
                selected=selected,
                calendar={"status": "created"},
            )
        except reservation.FreshPreparationError as exc:
            failed = exc.nodes["sopnode-f2"]
            sibling = exc.nodes["sopnode-f3"]
            if failed.get("status") != "failed" or failed.get("failed_phase") != "image-staging":
                raise CheckError(f"failed node evidence is incomplete: {failed}")
            if "synthetic image failure" not in failed.get("failure", {}).get("message", ""):
                raise CheckError(f"failed node provider error was lost: {failed}")
            if sibling.get("status") != "cancelled-after-peer-failure":
                raise CheckError(f"peer did not stop after selected-node failure: {sibling}")
            if sibling.get("completed_phases") != ["image-staging"]:
                raise CheckError(f"peer crossed too many phases after failure: {sibling}")
            if sibling.get("cancelled_before_phase") != "boot-parameters":
                raise CheckError(f"peer cancellation boundary is wrong: {sibling}")
        else:
            raise CheckError("expected parallel fresh preparation failure")
    finally:
        reservation.run = original
        if old_attempts is None:
            os.environ.pop("SYNTHRAN_POS_READY_ATTEMPTS", None)
        else:
            os.environ["SYNTHRAN_POS_READY_ATTEMPTS"] = old_attempts
        if old_interval is None:
            os.environ.pop("SYNTHRAN_POS_READY_INTERVAL_SECONDS", None)
        else:
            os.environ["SYNTHRAN_POS_READY_INTERVAL_SECONDS"] = old_interval


def main() -> int:
    failed_second_probe_is_non_destructive()
    all_nodes_are_probed_before_reclaim_or_image()
    independent_fresh_preparation_overlaps()
    parallel_failure_retains_per_node_evidence()
    print("POS multi-node preparation ordering/concurrency checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
