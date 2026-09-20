#!/usr/bin/env python3
"""Regression checks for operator-facing reservation output."""

from __future__ import annotations

import contextlib
import io
import subprocess
from unittest.mock import patch

from synthran import reservation


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"reservation-output-contract: {message}")


def main() -> int:
    machine_json = '[{"id":6536,"nodes":["sopnode-f2","sopnode-f3"]}]\n'
    completed = subprocess.CompletedProcess(
        ["pos", "calendar", "list", "--json"],
        0,
        machine_json,
        "",
    )

    quiet = io.StringIO()
    with patch("synthran.reservation.subprocess.run", return_value=completed):
        with contextlib.redirect_stdout(quiet):
            result = reservation.run(["pos", "calendar", "list", "--json"])
    require(result.stdout == machine_json, "quiet command lost captured provider output")
    require(quiet.getvalue() == "", "machine-readable JSON leaked into terminal output")

    visible = io.StringIO()
    with patch("synthran.reservation.subprocess.run", return_value=completed):
        with contextlib.redirect_stdout(visible):
            reservation.run(["pos", "calendar", "list", "--json"], echo=True)
    require(machine_json.strip() in visible.getvalue(), "explicit echo no longer works")

    print("Reservation output contract checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
