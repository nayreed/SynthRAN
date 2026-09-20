#!/usr/bin/env python3
"""No-hardware behavioral checks for SynthRAN's authoritative reservation layer."""

from __future__ import annotations

import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

import yaml

from synthran import reservation
from synthran.scenario import _normalize_reservation_policy

ROOT = Path(__file__).resolve().parents[1]


class CheckError(RuntimeError):
    pass


def done(argv, rc=0, out="", err=""):
    return subprocess.CompletedProcess(list(argv), rc, out, err)


class Fake:
    def __init__(self, handler: Callable[[list[str], str | None, int], subprocess.CompletedProcess[str]]):
        self.handler = handler
        self.calls: list[dict[str, Any]] = []

    def __call__(self, argv, *, check=True, stdin=None):
        command = list(argv)
        self.calls.append({"argv": command, "stdin": stdin})
        result = self.handler(command, stdin, len(self.calls))
        if check and result.returncode:
            detail = reservation._output(result) or str(result.returncode)
            raise reservation.ReservationError(
                f"command failed: {' '.join(command)}\n{detail}"
            )
        return result


def expect_error(call: Callable[[], Any], needle: str) -> str:
    try:
        call()
    except (Exception, SystemExit) as exc:
        text = str(exc)
        if needle not in text:
            raise CheckError(f"expected {needle!r}, got {text!r}") from exc
        return text
    raise CheckError(f"expected failure containing {needle!r}")


def policy_checks() -> dict[str, str]:
    legacy = {
        "platform": "r2lab",
        "reservation": {
            "enabled": True,
            "duration_minutes": 120,
            "image": "keep-me",
            "node_pool": ["sopnode-f1"],
        },
        "r2lab_reservation": {"enabled": True, "duration_minutes": 90},
    }
    _normalize_reservation_policy(legacy)
    assert legacy["reservation"]["mode"] == "create"
    assert legacy["reservation"]["host_preparation"] == "fresh"
    assert legacy["reservation"]["image"] == "keep-me"
    assert "node_pool" not in legacy["reservation"]
    assert legacy["r2lab_reservation"]["mode"] == "book"

    explicit = {
        "platform": "r2lab",
        "provider": {
            "mode": "require-existing",
            "project": "post5g-beta",
            "experiment": "synthran-ci",
        },
        "reservation": {
            "mode": "require-existing",
            "host_preparation": "preserve",
            "duration_minutes": 120,
            "image": "keep-me",
        },
        "r2lab_reservation": {"mode": "require-existing", "duration_minutes": 120},
    }
    _normalize_reservation_policy(explicit)
    assert explicit["reservation"]["mode"] == "require-existing"
    assert explicit["reservation"]["host_preparation"] == "preserve"
    assert explicit["r2lab_reservation"]["mode"] == "require-existing"

    bootstrap = {
        "platform": "r2lab",
        "reservation": {
            "mode": "require-existing",
            "host_preparation": "bootstrap",
            "duration_minutes": 120,
            "image": "unused-for-bootstrap",
        },
        "r2lab_reservation": {"mode": "require-existing", "duration_minutes": 120},
    }
    _normalize_reservation_policy(bootstrap)
    assert bootstrap["reservation"]["mode"] == "require-existing"
    assert bootstrap["reservation"]["host_preparation"] == "bootstrap"

    invalid = {
        "platform": "r2lab",
        "reservation": {"mode": "disabled", "host_preparation": "fresh"},
    }
    expect_error(lambda: _normalize_reservation_policy(invalid), "fresh host preparation requires")
    return {
        "legacy_canonicalization": "passed",
        "explicit_independent_policy": "passed",
        "bootstrap_policy": "passed",
        "invalid_policy_rejected": "passed",
    }


def provider_checks() -> dict[str, str]:
    original = reservation.run
    old_interval = os.environ.get("SYNTHRAN_PROVIDER_PREFIX_INTERVAL_SECONDS")
    os.environ["SYNTHRAN_PROVIDER_PREFIX_INTERVAL_SECONDS"] = "0"
    try:
        network = json.dumps(
            {
                "subnet": "192.0.2.0/24",
                "lb": "192.0.2.10",
                "expiration_time": "2026-09-16T04:00:00Z",
            }
        )

        def existing(argv, _stdin, _n):
            if argv[:3] == ["slices", "project", "use"]:
                return done(argv)
            if argv[:3] == ["slices", "experiment", "show"]:
                return done(argv, out="exists\n")
            if argv[:3] == ["post5g", "experiment", "prefix"]:
                return done(argv, out=network)
            raise CheckError(f"unexpected provider command {argv}")

        fake = Fake(existing)
        reservation.run = fake
        result = reservation.provider_context(
            {"mode": "require-existing", "project": "post5g-beta", "experiment": "ci"}
        )
        assert result["experiment_created"] is False
        assert not any(c["argv"][:3] == ["slices", "experiment", "create"] for c in fake.calls)

        prefixes = 0

        def creating(argv, _stdin, _n):
            nonlocal prefixes
            if argv[:3] == ["slices", "project", "use"]:
                return done(argv)
            if argv[:3] == ["slices", "experiment", "show"]:
                return done(argv, rc=1, err="missing")
            if argv[:3] == ["slices", "experiment", "create"]:
                return done(argv, out="created\n")
            if argv[:3] == ["post5g", "experiment", "prefix"]:
                prefixes += 1
                return done(argv, out="{}" if prefixes == 1 else network)
            raise CheckError(f"unexpected provider command {argv}")

        reservation.run = Fake(creating)
        result = reservation.provider_context(
            {
                "mode": "create",
                "project": "post5g-beta",
                "experiment": "ci-new",
                "experiment_duration": "4h",
            }
        )
        assert result["experiment_created"] is True and prefixes == 2

        def failing(argv, _stdin, _n):
            if argv[:3] == ["slices", "project", "use"]:
                return done(argv)
            if argv[:3] == ["slices", "experiment", "show"]:
                return done(argv, out="exists\n")
            if argv[:3] == ["post5g", "experiment", "prefix"]:
                return done(argv, rc=7, err="provider unavailable")
            raise CheckError(f"unexpected provider command {argv}")

        failed = Fake(failing)
        reservation.run = failed
        expect_error(
            lambda: reservation.provider_context(
                {"mode": "require-existing", "project": "post5g-beta", "experiment": "ci"}
            ),
            "provider unavailable",
        )
        attempts = sum(
            c["argv"][:3] == ["post5g", "experiment", "prefix"] for c in failed.calls
        )
        assert attempts == reservation.PROVIDER_PREFIX_ATTEMPTS_EXISTING
        return {
            "existing": "passed",
            "create_retry": "passed",
            "bounded_failure": "passed",
        }
    finally:
        reservation.run = original
        if old_interval is None:
            os.environ.pop("SYNTHRAN_PROVIDER_PREFIX_INTERVAL_SECONDS", None)
        else:
            os.environ["SYNTHRAN_PROVIDER_PREFIX_INTERVAL_SECONDS"] = old_interval


def pos_event(event_id: str, nodes: list[str], now: dt.datetime) -> dict[str, Any]:
    return {
        "id": event_id,
        "owner": "ci-user",
        "nodes": nodes,
        "start_date": (now - dt.timedelta(minutes=5)).isoformat(),
        "end_date": (now + dt.timedelta(minutes=180)).isoformat(),
    }


def pos_checks() -> dict[str, str]:
    original = reservation.run
    now = dt.datetime(2026, 9, 16, 0, 0, tzinfo=dt.timezone.utc)
    selected = ["sopnode-f2", "sopnode-f3"]
    try:
        lists = 0

        def creating(argv, _stdin, _n):
            nonlocal lists
            if argv == ["pos", "calendar", "list", "--json"]:
                lists += 1
                rows = [] if lists == 1 else [pos_event("42", selected, now)]
                return done(argv, out=json.dumps(rows))
            if argv[:3] == ["pos", "calendar", "create"]:
                assert argv[-2:] == selected
                return done(argv, out="42\n")
            raise CheckError(f"unexpected POS command {argv}")

        reservation.run = Fake(creating)
        record = reservation.acquire_calendar(
            {"mode": "create", "duration_minutes": 120},
            selected=selected,
            owner="ci-user",
            now=now,
        )
        assert record["id"] == "42" and record["nodes"] == selected

        def existing(argv, _stdin, _n):
            if argv == ["pos", "calendar", "list", "--json"]:
                return done(argv, out=json.dumps([pos_event("77", selected, now)]))
            raise CheckError(f"require-existing mutated POS: {argv}")

        fake = Fake(existing)
        reservation.run = fake
        record = reservation.acquire_calendar(
            {"mode": "require-existing", "duration_minutes": 120},
            selected=selected,
            owner="ci-user",
            now=now,
        )
        assert record["status"] == "required-existing"

        def unavailable(argv, _stdin, _n):
            if argv == ["pos", "calendar", "list", "--json"]:
                return done(argv, out="[]")
            if argv[:3] == ["pos", "calendar", "create"]:
                assert argv[-2:] == selected
                return done(argv, rc=1, err="sopnode-f3 is busy")
            raise CheckError(f"unexpected unavailable command {argv}")

        fake = Fake(unavailable)
        reservation.run = fake
        expect_error(
            lambda: reservation.acquire_calendar(
                {"mode": "create", "duration_minutes": 120},
                selected=selected,
                owner="ci-user",
                now=now,
            ),
            "sopnode-f3 is busy",
        )
        assert sum(c["argv"][:3] == ["pos", "calendar", "create"] for c in fake.calls) == 1

        def ambiguous(argv, _stdin, _n):
            if argv == ["pos", "calendar", "list", "--json"]:
                rows = [pos_event("80", selected, now), pos_event("81", selected, now)]
                return done(argv, out=json.dumps(rows))
            raise CheckError(f"ambiguous coverage mutated POS: {argv}")

        reservation.run = Fake(ambiguous)
        expect_error(
            lambda: reservation.acquire_calendar(
                {"mode": "require-existing", "duration_minutes": 120},
                selected=selected,
                owner="ci-user",
                now=now,
            ),
            "ambiguous authority",
        )
        return {
            "exact_create": "passed",
            "require_existing": "passed",
            "unavailable_no_remap": "passed",
            "ambiguous_fail_closed": "passed",
        }
    finally:
        reservation.run = original


def allocation_probe_output_checks() -> dict[str, str]:
    original = reservation.run
    try:
        def already_allocated(argv, _stdin, _n):
            if argv[:3] == ["pos", "allocations", "allocate"]:
                return done(
                    argv,
                    rc=1,
                    err="ERROR pos Unable to POST /allocations/allocate\nNodes are already allocated: sopnode-f3",
                )
            raise CheckError(f"unexpected POS command {argv}")

        reservation.run = Fake(already_allocated)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            state = reservation._probe_allocation_for_fresh(
                "sopnode-f3", result_folder="ci-probe"
            )
        captured = out.getvalue()
        assert state == "already-active"
        assert "existing allocation detected" in captured
        assert "Unable to POST" not in captured
        assert "Nodes are already allocated" not in captured

        def real_failure(argv, _stdin, _n):
            if argv[:3] == ["pos", "allocations", "allocate"]:
                return done(argv, rc=7, err="provider unavailable")
            raise CheckError(f"unexpected POS command {argv}")

        reservation.run = Fake(real_failure)
        expect_error(
            lambda: reservation._probe_allocation_for_fresh(
                "sopnode-f3", result_folder="ci-probe"
            ),
            "provider unavailable",
        )
        return {
            "known_existing_quiet": "passed",
            "unexpected_failure_visible": "passed",
        }
    finally:
        reservation.run = original


def preparation_checks() -> dict[str, str]:
    original = reservation.run
    old_attempts = os.environ.get("SYNTHRAN_POS_READY_ATTEMPTS")
    old_interval = os.environ.get("SYNTHRAN_POS_READY_INTERVAL_SECONDS")
    os.environ["SYNTHRAN_POS_READY_ATTEMPTS"] = "1"
    os.environ["SYNTHRAN_POS_READY_INTERVAL_SECONDS"] = "0"
    node = "sopnode-f3"
    try:
        def success(argv, _stdin, _n):
            if argv[:3] in (
                ["pos", "allocations", "allocate"],
                ["pos", "nodes", "image"],
                ["pos", "nodes", "bootparameter"],
                ["pos", "nodes", "reset"],
            ) or argv[0] == "ssh":
                return done(argv)
            raise CheckError(f"unexpected preparation command {argv}")

        fresh = Fake(success)
        reservation.run = fresh
        result = reservation.prepare_hosts(
            {"host_preparation": "fresh", "image": "configured-image"},
            selected=[node],
            calendar={"status": "created"},
        )
        assert result["nodes"][node]["image"] == "configured-image"
        commands = [c["argv"] for c in fresh.calls]
        assert [c[:3] for c in commands[:4]] == [
            ["pos", "allocations", "allocate"],
            ["pos", "nodes", "image"],
            ["pos", "nodes", "bootparameter"],
            ["pos", "nodes", "reset"],
        ]
        assert commands[4][0] == "ssh"
        assert "isolcpus=managed_irq,16-63" in commands[2][-1]

        allocations = 0

        def conflict(argv, _stdin, _n):
            nonlocal allocations
            if argv[:3] == ["pos", "allocations", "allocate"]:
                allocations += 1
                return done(argv, rc=1, out="already allocated") if allocations == 1 else done(argv)
            if argv[:4] == ["pos", "allocations", "free", "-k"]:
                return done(argv)
            if argv[:3] in (
                ["pos", "nodes", "image"],
                ["pos", "nodes", "bootparameter"],
                ["pos", "nodes", "reset"],
            ) or argv[0] == "ssh":
                return done(argv)
            raise CheckError(f"unexpected conflict command {argv}")

        fake = Fake(conflict)
        reservation.run = fake
        reservation.prepare_hosts(
            {"host_preparation": "fresh", "image": "configured-image"},
            selected=[node],
            calendar={"status": "required-existing"},
        )
        assert sum(c["argv"][:4] == ["pos", "allocations", "free", "-k"] for c in fake.calls) == 1

        preserve = Fake(lambda argv, _stdin, _n: (_ for _ in ()).throw(CheckError(f"preserve mutated {argv}")))
        reservation.run = preserve
        result = reservation.prepare_hosts(
            {"host_preparation": "preserve", "image": "configured-image"},
            selected=[node],
            calendar={"status": "required-existing"},
        )
        assert result["mutations"] == [] and not preserve.calls
        expect_error(
            lambda: reservation.prepare_hosts(
                {"host_preparation": "fresh", "image": "configured-image"},
                selected=[node],
                calendar={"status": "disabled"},
            ),
            "requires create or require-existing POS calendar authority",
        )
        return {
            "fresh_order": "passed",
            "allocation_conflict_guarded": "passed",
            "configured_image_preserved": "passed",
            "preserve_zero_mutation": "passed",
        }
    finally:
        reservation.run = original
        reservation.STATE_PATH = original_state
        state_tmp.cleanup()
        if old_attempts is None:
            os.environ.pop("SYNTHRAN_POS_READY_ATTEMPTS", None)
        else:
            os.environ["SYNTHRAN_POS_READY_ATTEMPTS"] = old_attempts
        if old_interval is None:
            os.environ.pop("SYNTHRAN_POS_READY_INTERVAL_SECONDS", None)
        else:
            os.environ["SYNTHRAN_POS_READY_INTERVAL_SECONDS"] = old_interval



def allocation_reuse_checks() -> dict[str, str]:
    original = reservation.run
    original_state = reservation.STATE_PATH
    state_tmp = tempfile.TemporaryDirectory(prefix="synthran-allocation-reuse-")
    reservation.STATE_PATH = Path(state_tmp.name) / "pos-reservation.json"
    old_attempts = os.environ.get("SYNTHRAN_POS_READY_ATTEMPTS")
    old_interval = os.environ.get("SYNTHRAN_POS_READY_INTERVAL_SECONDS")
    os.environ["SYNTHRAN_POS_READY_ATTEMPTS"] = "1"
    os.environ["SYNTHRAN_POS_READY_INTERVAL_SECONDS"] = "0"
    node = "sopnode-f3"
    authority = {
        "managed_by": "synthran",
        "event_id": "42",
        "owner": "ci-user",
        "nodes": [node],
        "allocation_result_folder": "synthran-ci-token",
        "allocations": {
            node: {
                "id": "alloc-42",
                "result_folder": "synthran-ci-token",
            }
        },
    }
    reservation._write_json(reservation.STATE_PATH, authority)
    try:
        def matching(argv, _stdin, _n):
            if argv[:3] == ["pos", "allocations", "allocate"]:
                assert argv[3:5] == ["--result-folder", "synthran-ci-token"]
                assert argv[-1] == node
                return done(argv, rc=1, err=f"Nodes are already allocated: {node}")
            if argv == ["pos", "allocations", "show", node]:
                return done(argv, out=json.dumps({"id": "alloc-42"}))
            if argv == ["pos", "allocations", "show", "alloc-42"]:
                return done(
                    argv,
                    out=json.dumps({"result_folder": "synthran-ci-token"}),
                )
            if argv[:3] in (
                ["pos", "nodes", "image"],
                ["pos", "nodes", "bootparameter"],
                ["pos", "nodes", "reset"],
            ) or argv[0] == "ssh":
                return done(argv)
            raise CheckError(f"unexpected managed-reuse command {argv}")

        reuse = Fake(matching)
        reservation.run = reuse
        result = reservation.prepare_hosts(
            {"host_preparation": "fresh", "image": "configured-image"},
            selected=[node],
            calendar={
                "status": "reused",
                "id": "42",
                "owner": "ci-user",
                "nodes": [node],
            },
            allocation_authority=json.loads(json.dumps(authority)),
        )
        record = result["nodes"][node]
        assert record["allocation"] == "managed-existing"
        assert record["allocation_id"] == "alloc-42"
        assert not any(
            call["argv"][:4] == ["pos", "allocations", "free", "-k"]
            for call in reuse.calls
        )

        reclaimed = False

        def mismatch(argv, _stdin, _n):
            nonlocal reclaimed
            if argv[:3] == ["pos", "allocations", "allocate"]:
                assert argv[3:5] == ["--result-folder", "synthran-ci-token"]
                if not reclaimed:
                    return done(argv, rc=1, err=f"Nodes are already allocated: {node}")
                return done(argv, out="allocated\n")
            if argv[:4] == ["pos", "allocations", "free", "-k"]:
                reclaimed = True
                return done(argv)
            if argv == ["pos", "allocations", "show", node]:
                return done(
                    argv,
                    out=json.dumps({"id": "alloc-new" if reclaimed else "alloc-other"}),
                )
            if argv == ["pos", "allocations", "show", "alloc-other"]:
                return done(
                    argv,
                    out=json.dumps({"result_folder": "someone-else"}),
                )
            if argv == ["pos", "allocations", "show", "alloc-new"]:
                return done(
                    argv,
                    out=json.dumps({"result_folder": "synthran-ci-token"}),
                )
            if argv[:3] in (
                ["pos", "nodes", "image"],
                ["pos", "nodes", "bootparameter"],
                ["pos", "nodes", "reset"],
            ) or argv[0] == "ssh":
                return done(argv)
            raise CheckError(f"unexpected mismatched-allocation command {argv}")

        stale = Fake(mismatch)
        reservation.run = stale
        stale_authority = json.loads(json.dumps(authority))
        reservation._write_json(reservation.STATE_PATH, stale_authority)
        result = reservation.prepare_hosts(
            {"host_preparation": "fresh", "image": "configured-image"},
            selected=[node],
            calendar={
                "status": "reused",
                "id": "42",
                "owner": "ci-user",
                "nodes": [node],
            },
            allocation_authority=stale_authority,
        )
        record = result["nodes"][node]
        assert record["allocation"] == "reclaimed"
        assert record["allocation_id"] == "alloc-new"
        assert sum(
            call["argv"][:4] == ["pos", "allocations", "free", "-k"]
            for call in stale.calls
        ) == 1
        assert stale_authority["allocations"][node]["id"] == "alloc-new"

        # Direct callers have no retained calendar identity and must therefore
        # never reuse an already-active allocation from metadata alone.
        direct_allocations = 0

        def direct(argv, _stdin, _n):
            nonlocal direct_allocations
            if argv[:3] == ["pos", "allocations", "allocate"]:
                direct_allocations += 1
                if direct_allocations == 1:
                    return done(argv, rc=1, err=f"Nodes are already allocated: {node}")
                return done(argv)
            if argv[:4] == ["pos", "allocations", "free", "-k"]:
                return done(argv)
            if argv[:3] in (
                ["pos", "nodes", "image"],
                ["pos", "nodes", "bootparameter"],
                ["pos", "nodes", "reset"],
            ) or argv[0] == "ssh":
                return done(argv)
            if argv[:3] == ["pos", "allocations", "show"]:
                raise CheckError("direct caller attempted managed allocation reuse")
            raise CheckError(f"unexpected direct-allocation command {argv}")

        direct_fake = Fake(direct)
        reservation.run = direct_fake
        direct_result = reservation.prepare_hosts(
            {"host_preparation": "fresh", "image": "configured-image"},
            selected=[node],
            calendar={"status": "required-existing"},
        )
        assert direct_result["nodes"][node]["allocation"] == "reclaimed"
        assert sum(
            call["argv"][:4] == ["pos", "allocations", "free", "-k"]
            for call in direct_fake.calls
        ) == 1

        return {
            "provider_identity_reuse": "passed",
            "mismatch_reclaims": "passed",
            "direct_call_fail_closed": "passed",
        }
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


def managed_state_checks() -> dict[str, str]:
    original_state = reservation.STATE_PATH
    try:
        with tempfile.TemporaryDirectory(prefix="synthran-pos-state-") as value:
            reservation.STATE_PATH = Path(value) / "pos-reservation.json"
            roles = {"core": "sopnode-f2", "ran": "sopnode-f3", "broker": "sopnode-f2"}
            calendar = {
                "status": "created",
                "id": "42",
                "owner": "ci-user",
                "nodes": ["sopnode-f2", "sopnode-f3"],
                "start": "2026-09-20T08:00:00+00:00",
                "end": "2026-09-20T10:00:00+00:00",
            }
            first = reservation._save_state(calendar, roles)
            token = first["allocation_result_folder"]
            first["allocations"]["sopnode-f2"] = {
                "id": "alloc-42",
                "result_folder": token,
            }
            reservation._write_json(reservation.STATE_PATH, first)

            same = reservation._save_state(
                dict(calendar, status="reused"),
                roles,
            )
            assert same["allocation_result_folder"] == token
            assert same["allocations"]["sopnode-f2"]["id"] == "alloc-42"

            changed = reservation._save_state(
                dict(calendar, id="43", status="created"),
                roles,
            )
            assert changed["allocation_result_folder"] != token
            assert changed["allocations"] == {}

        return {
            "same_calendar_retains_allocation_identity": "passed",
            "new_calendar_rotates_allocation_identity": "passed",
        }
    finally:
        reservation.STATE_PATH = original_state


def load_r2lab():
    path = ROOT / "deployment/scripts/reserve_r2lab.py"
    spec = importlib.util.spec_from_file_location("reservation_check_r2lab", path)
    if spec is None or spec.loader is None:
        raise CheckError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def r2_args(tmp: Path, mode: str) -> list[str]:
    return [
        "--host", "faraday.inria.fr",
        "--username", "ci-slice",
        "--known-hosts", str(tmp / "known_hosts"),
        "--email", "ci@example.invalid",
        "--start", "2026-09-16T00:00",
        "--end", "2026-09-16T02:00",
        "--output", str(tmp / f"{mode}.json"),
        "--log", str(tmp / f"{mode}.log"),
        "--mode", mode,
    ]


def r2lab_checks() -> dict[str, str]:
    module = load_r2lab()
    start, end = 100, 200

    def payload(leases):
        return json.dumps(
            {"requested_start_epoch": start, "requested_end_epoch": end, "leases": leases}
        )

    covering = {
        "id": 10,
        "slice_name": "ci-slice",
        "t_from": "2026-09-16T00:00:00+00:00",
        "t_until": "2026-09-16T02:00:00+00:00",
        "start_epoch": 90,
        "end_epoch": 210,
    }
    with tempfile.TemporaryDirectory(prefix="synthran-r2lab-") as value:
        tmp = Path(value)
        calls: list[dict[str, Any]] = []

        def existing(_args, argv, *, stdin=None):
            calls.append({"argv": list(argv), "stdin": stdin})
            return done(argv) if argv == ["true"] else done(argv, out=payload([covering]))

        module._remote = existing
        module._read_password = lambda: (_ for _ in ()).throw(CheckError("password read"))
        assert module.main(r2_args(tmp, "require-existing")) == 0
        record = json.loads((tmp / "require-existing.json").read_text())
        assert record["status"] == "reused"

        short = dict(covering)
        short.update({"id": 11, "end_epoch": 150})
        queries = 0
        calls.clear()

        def extending(_args, argv, *, stdin=None):
            nonlocal queries
            calls.append({"argv": list(argv), "stdin": stdin})
            if argv == ["true"]:
                return done(argv)
            if argv and module.EXTEND_CODE in argv:
                return done(argv)
            queries += 1
            leases = [short] if queries == 1 else [covering | {"id": 11}]
            return done(argv, out=payload(leases))

        module._remote = extending
        module._read_password = lambda: "top-secret"
        assert module.main(r2_args(tmp, "book")) == 0
        mutations = [c for c in calls if c["argv"] and module.EXTEND_CODE in c["argv"]]
        assert len(mutations) == 1 and mutations[0]["stdin"] == "top-secret\n"
        assert all("top-secret" not in " ".join(map(str, c["argv"])) for c in calls)

        calls.clear()

        def denied(_args, argv, *, stdin=None):
            calls.append({"argv": list(argv), "stdin": stdin})
            if argv == ["true"]:
                return done(argv)
            if argv and module.BOOK_CODE in argv:
                return done(argv, rc=3, out="booking denied by provider")
            return done(argv, out=payload([]))

        module._remote = denied
        module._read_password = lambda: "top-secret"
        expect_error(lambda: module.main(r2_args(tmp, "book")), "booking denied by provider")
        bookings = [c for c in calls if c["argv"] and module.BOOK_CODE in c["argv"]]
        assert len(bookings) == 1 and bookings[0]["stdin"] == "top-secret\n"

        def ambiguous(_args, argv, *, stdin=None):
            if argv == ["true"]:
                return done(argv)
            return done(argv, out=payload([covering, covering | {"id": 12}]))

        module._remote = ambiguous
        module._read_password = lambda: (_ for _ in ()).throw(CheckError("password read"))
        expect_error(lambda: module.main(r2_args(tmp, "book")), "multiple owned R2Lab leases cover")

        touched = False

        def forbidden(*_args, **_kwargs):
            nonlocal touched
            touched = True
            raise CheckError("disabled mode touched provider")

        module._remote = forbidden
        assert module.main(r2_args(tmp, "disabled")) == 0 and not touched

    return {
        "require_existing": "passed",
        "extension": "passed",
        "booking_denial": "passed",
        "ambiguous_fail_closed": "passed",
        "stdin_only_secret": "passed",
        "disabled_zero_provider_action": "passed",
    }


def no_input_checks() -> dict[str, str]:
    original_run, original_stdin = reservation.run, sys.stdin
    original_user = os.environ.get("USER")

    class NoRead(io.StringIO):
        def read(self, *args, **kwargs):
            raise CheckError("stdin read")

        def readline(self, *args, **kwargs):
            raise CheckError("stdin read")

    try:
        os.environ["USER"] = "ci-user"
        sys.stdin = NoRead("")
        reservation.run = Fake(
            lambda argv, _stdin, _n: (_ for _ in ()).throw(
                CheckError(f"disabled/preserve ran command {argv}")
            )
        )
        with tempfile.TemporaryDirectory(prefix="synthran-no-input-") as value:
            root = Path(value)
            config, result_dir = root / "config.yml", root / "result"
            nodes = {"core": "sopnode-f2", "ran": "sopnode-f3", "broker": "sopnode-f2"}
            config.write_text(
                yaml.safe_dump(
                    {
                        "deployment": {
                            "nodes": nodes,
                            "provider": {"mode": "disabled"},
                            "reservation": {
                                "mode": "disabled",
                                "host_preparation": "preserve",
                                "duration_minutes": 120,
                                "image": "unchanged",
                            },
                            "r2lab_reservation": {"mode": "disabled"},
                        }
                    },
                    sort_keys=False,
                )
            )
            evidence = reservation.execute(config, result_dir)
            selection = json.loads((result_dir / "pos-selection.json").read_text())
            assert evidence["selected_nodes"] == nodes == selection["nodes"]
            assert evidence["status"] == "ready"
        assert "input(" not in (ROOT / "synthran/reservation.py").read_text()
        assert "input(" not in (ROOT / "deployment/scripts/reserve_sop.py").read_text()
        return {
            "stdin_eof_safe": "passed",
            "selected_identity_immutable": "passed",
            "no_input_calls": "passed",
        }
    finally:
        reservation.run = original_run
        sys.stdin = original_stdin
        if original_user is None:
            os.environ.pop("USER", None)
        else:
            os.environ["USER"] = original_user


def main() -> int:
    result = {
        "schema": "synthran/reservation-authority-check/v1",
        "policy": policy_checks(),
        "provider": provider_checks(),
        "pos_calendar": pos_checks(),
        "allocation_probe_output": allocation_probe_output_checks(),
        "allocation_reuse": allocation_reuse_checks(),
        "managed_state": managed_state_checks(),
        "host_preparation": preparation_checks(),
        "r2lab": r2lab_checks(),
        "noninteractive": no_input_checks(),
        "result": "pass",
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CheckError, reservation.ReservationError, AssertionError, OSError, ValueError, yaml.YAMLError) as exc:
        raise SystemExit(f"reservation-authority-check: {exc}") from exc
