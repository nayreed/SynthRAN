#!/usr/bin/env python3
"""Regression checks for remaining coverage of an active exact POS reservation.

Physical validation on 2026-09-20 proved that reusing an active event based on
its original booked duration is unsafe: a nearly expired reservation was
accepted for a new 120-minute deployment request and both SOP nodes disappeared
when the reservation ended. Reuse must therefore be based on coverage remaining
from the current time, while still refusing overlapping calendar creation.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from typing import Any

from synthran import reservation


class CheckError(RuntimeError):
    pass


def done(argv: list[str], rc: int = 0, out: str = "", err: str = ""):
    return subprocess.CompletedProcess(argv, rc, out, err)


class CalendarOnlyFake:
    def __init__(self, events: list[dict[str, Any]]):
        self.events = events
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, check=True, stdin=None):
        command = list(argv)
        self.calls.append(command)
        if command == ["pos", "calendar", "list", "--json"]:
            return done(command, out=json.dumps(self.events))
        if command[:3] == ["pos", "calendar", "create"]:
            raise CheckError(f"active exact reservation triggered overlapping create: {command}")
        raise CheckError(f"unexpected command: {command}")


def event(
    event_id: str,
    *,
    start: dt.datetime,
    duration_minutes: int,
    nodes: list[str],
) -> dict[str, Any]:
    return {
        "id": event_id,
        "owner": "ci-user",
        "nodes": nodes,
        "start_date": start.isoformat(),
        "end_date": (start + dt.timedelta(minutes=duration_minutes)).isoformat(),
    }


def expect_error(call, needle: str) -> None:
    try:
        call()
    except reservation.ReservationError as exc:
        if needle not in str(exc):
            raise CheckError(f"expected {needle!r}, got {str(exc)!r}") from exc
        return
    raise CheckError(f"expected ReservationError containing {needle!r}")


def main() -> int:
    original = reservation.run
    tz = dt.timezone(dt.timedelta(hours=2))
    # Mirrors physical event 6536: booked for 120 minutes, rerun 7 minutes later.
    start = dt.datetime(2026, 9, 16, 10, 10, tzinfo=tz)
    now = start + dt.timedelta(minutes=7)
    selected = ["sopnode-f3", "sopnode-f2"]

    try:
        active_120 = event("6536", start=start, duration_minutes=120, nodes=selected)

        # Seven elapsed minutes leave only 113 minutes of authority. A new
        # 120-minute request must fail closed and must not attempt an overlapping
        # POS calendar create.
        insufficient = CalendarOnlyFake([active_120])
        reservation.run = insufficient
        expect_error(
            lambda: reservation.acquire_calendar(
                {"mode": "create", "duration_minutes": 120},
                selected=selected,
                owner="ci-user",
                now=now,
            ),
            "remaining coverage",
        )
        if any(call[:3] == ["pos", "calendar", "create"] for call in insufficient.calls):
            raise CheckError("insufficient active reservation triggered overlapping create")

        # The same active event may still be reused when its remaining window
        # really covers the requested duration.
        reusable = CalendarOnlyFake([active_120])
        reservation.run = reusable
        record = reservation.acquire_calendar(
            {"mode": "create", "duration_minutes": 110},
            selected=selected,
            owner="ci-user",
            now=now,
        )
        if record["status"] != "reused" or record["id"] != "6536":
            raise CheckError(f"unexpected reuse record: {record}")
        if any(call[:3] == ["pos", "calendar", "create"] for call in reusable.calls):
            raise CheckError("covering active reservation triggered overlapping create")

        # require-existing uses the same remaining-coverage promise.
        strict = CalendarOnlyFake([active_120])
        reservation.run = strict
        expect_error(
            lambda: reservation.acquire_calendar(
                {"mode": "require-existing", "duration_minutes": 120},
                selected=selected,
                owner="ci-user",
                now=now,
            ),
            "requested duration",
        )
        if any(call[:3] == ["pos", "calendar", "create"] for call in strict.calls):
            raise CheckError("require-existing attempted a mutation")

        # A still shorter active event must likewise fail closed rather than
        # being silently accepted or overlapped.
        short = CalendarOnlyFake(
            [event("short", start=start, duration_minutes=60, nodes=selected)]
        )
        reservation.run = short
        expect_error(
            lambda: reservation.acquire_calendar(
                {"mode": "create", "duration_minutes": 120},
                selected=selected,
                owner="ci-user",
                now=now,
            ),
            "remaining coverage",
        )
        if any(call[:3] == ["pos", "calendar", "create"] for call in short.calls):
            raise CheckError("short active reservation triggered overlapping create")
    finally:
        reservation.run = original

    print("POS calendar reuse regression: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
