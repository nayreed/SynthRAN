#!/usr/bin/env python3
"""Verify provider/POS/R2Lab failures retain known ownership evidence."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import subprocess
import tempfile
from pathlib import Path

import yaml

from synthran import reservation

ROOT = Path(__file__).resolve().parents[1]


class CheckError(RuntimeError):
    pass


def done(argv, rc=0, out="", err=""):
    return subprocess.CompletedProcess(list(argv), rc, out, err)


def load_r2lab():
    path = ROOT / "deployment/scripts/reserve_r2lab.py"
    spec = importlib.util.spec_from_file_location("failure_evidence_r2lab", path)
    if spec is None or spec.loader is None:
        raise CheckError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pos_failure(tmp: Path) -> None:
    original_run = reservation.run
    original_state = reservation.STATE_PATH
    original_user = os.environ.get("USER")
    now = dt.datetime.now().astimezone()
    nodes = ["sopnode-f2", "sopnode-f3"]
    lists = 0

    def fake(argv, *, check=True, stdin=None):
        nonlocal lists
        command = list(argv)
        if command == ["pos", "calendar", "list", "--json"]:
            lists += 1
            rows = []
            if lists > 1:
                rows = [
                    {
                        "id": "42",
                        "owner": "ci-user",
                        "nodes": nodes,
                        "start_date": (now - dt.timedelta(minutes=1)).isoformat(),
                        "end_date": (now + dt.timedelta(minutes=180)).isoformat(),
                    }
                ]
            result = done(command, out=json.dumps(rows))
        elif command[:3] == ["pos", "calendar", "create"]:
            result = done(command, out="42\n")
        elif command[:3] == ["pos", "allocations", "allocate"]:
            result = done(command)
        elif command[:3] == ["pos", "nodes", "image"]:
            result = done(command, rc=9, err="image provider rejected selected node")
        else:
            raise CheckError(f"unexpected POS failure-evidence command: {command}")
        if check and result.returncode:
            raise reservation.ReservationError(
                f"command failed: {' '.join(command)}\n{reservation._output(result)}"
            )
        return result

    try:
        os.environ["USER"] = "ci-user"
        reservation.run = fake
        reservation.STATE_PATH = tmp / "pos-state.json"
        config = tmp / "resolved.yml"
        result_dir = tmp / "pos-result"
        config.write_text(
            yaml.safe_dump(
                {
                    "deployment": {
                        "nodes": {
                            "core": "sopnode-f2",
                            "ran": "sopnode-f3",
                            "broker": "sopnode-f2",
                        },
                        "provider": {"mode": "disabled"},
                        "reservation": {
                            "mode": "create",
                            "host_preparation": "fresh",
                            "duration_minutes": 120,
                            "image": "scenario-image",
                        },
                        "r2lab_reservation": {"mode": "disabled"},
                    }
                },
                sort_keys=False,
            )
        )
        try:
            reservation.execute(config, result_dir)
        except reservation.ReservationError as exc:
            if "image provider rejected selected node" not in str(exc):
                raise CheckError(f"original POS provider error was lost: {exc}") from exc
        else:
            raise CheckError("expected POS image failure")

        evidence = json.loads((result_dir / "reservation-authority.json").read_text())
        if evidence["status"] != "failed":
            raise CheckError("POS failure did not mark authority evidence failed")
        if evidence.get("pos_calendar", {}).get("id") != "42":
            raise CheckError("known POS calendar id was lost after later mutation failure")
        if "image provider rejected selected node" not in evidence["failure"]["message"]:
            raise CheckError("POS failure evidence lost the original provider error")
        if evidence["selected_resources"] != nodes:
            raise CheckError("POS failure evidence lost selected allocation identities")
        preparation = evidence.get("host_preparation", {})
        if preparation.get("mode") != "fresh" or preparation.get("status") != "failed":
            raise CheckError("parallel fresh failure did not retain host-preparation failure evidence")
        records = preparation.get("nodes", {})
        if set(records) != set(nodes):
            raise CheckError(f"parallel fresh failure lost per-node evidence: {records}")
        failed_nodes = [
            node for node, record in records.items()
            if record.get("status") == "failed"
        ]
        if not failed_nodes:
            raise CheckError("parallel fresh failure evidence contains no failed node record")
        if not all(
            record.get("completed_phases") is not None
            for record in records.values()
        ):
            raise CheckError("parallel fresh failure evidence lost phase history")
        state = json.loads((tmp / "pos-state.json").read_text())
        if state.get("event_id") != "42" or state.get("nodes") != nodes:
            raise CheckError("persistent POS state lost known reservation identity")
    finally:
        reservation.run = original_run
        reservation.STATE_PATH = original_state
        if original_user is None:
            os.environ.pop("USER", None)
        else:
            os.environ["USER"] = original_user


def r2lab_failure(tmp: Path) -> None:
    module = load_r2lab()
    result_dir = tmp / "r2lab-result"
    result_dir.mkdir()
    authority_path = result_dir / "reservation-authority.json"
    authority_path.write_text(
        json.dumps(
            {
                "schema": "synthran/reservation-authority/v1",
                "status": "ready",
                "selected_resources": ["sopnode-f2", "sopnode-f3"],
                "pos_calendar": {"id": "42", "status": "created"},
                "policies": {"r2lab": "book"},
            }
        )
    )

    query = json.dumps(
        {"requested_start_epoch": 100, "requested_end_epoch": 200, "leases": []}
    )

    def remote(_args, argv, *, stdin=None):
        if argv == ["true"]:
            return done(argv)
        if argv and module.BOOK_CODE in argv:
            if stdin != "top-secret\n":
                raise CheckError("R2Lab password did not stay on stdin")
            return done(argv, rc=3, out="provider denied booking")
        return done(argv, out=query)

    module._remote = remote
    module._read_password = lambda: "top-secret"
    argv = [
        "--host", "faraday.inria.fr",
        "--username", "ci-slice",
        "--known-hosts", str(tmp / "known_hosts"),
        "--email", "ci@example.invalid",
        "--start", "2026-09-16T00:00",
        "--end", "2026-09-16T02:00",
        "--output", str(result_dir / "r2lab-lease.json"),
        "--log", str(result_dir / "r2lab-reservation.log"),
    ]
    try:
        module.main(argv)
    except SystemExit as exc:
        if "provider denied booking" not in str(exc):
            raise CheckError(f"original R2Lab provider error was lost: {exc}") from exc
    else:
        raise CheckError("expected R2Lab booking failure")

    lease = json.loads((result_dir / "r2lab-lease.json").read_text())
    authority = json.loads(authority_path.read_text())
    if lease["status"] != "failed" or "provider denied booking" not in lease["failure"]["message"]:
        raise CheckError("R2Lab failure file did not preserve original provider error")
    if authority["status"] != "failed":
        raise CheckError("R2Lab failure did not fail the canonical authority evidence")
    if authority.get("pos_calendar", {}).get("id") != "42":
        raise CheckError("R2Lab failure erased already-known POS reservation identity")
    if authority.get("r2lab", {}).get("status") != "failed":
        raise CheckError("canonical authority evidence omitted R2Lab failure record")
    if "provider denied booking" not in authority["failure"]["message"]:
        raise CheckError("canonical authority evidence lost R2Lab provider error")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="synthran-failure-evidence-") as value:
        tmp = Path(value)
        pos_failure(tmp)
        r2lab_failure(tmp)
    print(
        json.dumps(
            {
                "schema": "synthran/reservation-failure-evidence-check/v1",
                "pos_failure": "passed",
                "r2lab_failure": "passed",
                "result": "pass",
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CheckError, OSError, ValueError, yaml.YAMLError) as exc:
        raise SystemExit(f"reservation-failure-evidence-check: {exc}") from exc
