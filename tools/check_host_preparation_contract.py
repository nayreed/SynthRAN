#!/usr/bin/env python3
"""Regression checks for issue #124 host-preparation policy vocabulary."""

from __future__ import annotations

from synthran import host_preparation, reservation
from synthran.scenario import _normalize_reservation_policy


class CheckError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CheckError(message)


def check_contract_matrix() -> None:
    require(
        host_preparation.PREPARATION_MODES
        == frozenset({"preserve", "bootstrap", "fresh"}),
        "canonical preparation modes changed unexpectedly",
    )
    require(
        host_preparation.EXECUTABLE_PREPARATION_MODES
        == frozenset({"preserve", "fresh"}),
        "bootstrap must remain declared-but-not-executable in task 1",
    )

    preserve = host_preparation.PREPARATION_CONTRACT["preserve"]
    bootstrap = host_preparation.PREPARATION_CONTRACT["bootstrap"]
    fresh = host_preparation.PREPARATION_CONTRACT["fresh"]

    require(not preserve["mutates_host"], "preserve must be non-mutating")
    require(not preserve["allows_image_staging"], "preserve must not stage images")
    require(not preserve["allows_allocation_reclaim"], "preserve must not reclaim allocations")

    require(bootstrap["mutates_host"], "bootstrap is the in-place mutating policy")
    require(bootstrap["requires_pos_calendar_authority"], "bootstrap requires POS authority")
    require(bootstrap["allows_package_or_service_reconcile"], "bootstrap must permit prerequisite reconciliation")
    require(not bootstrap["allows_image_staging"], "bootstrap must not stage POS images")
    require(not bootstrap["allows_allocation_reclaim"], "bootstrap must not reclaim allocation")
    require(not bootstrap["allows_pos_reset"], "bootstrap must not use the clean-image POS reset")
    require(not bootstrap["silent_escalation_to_fresh"], "bootstrap must never silently become fresh")
    require(bootstrap["implementation"] == "declared", "task 1 must not claim bootstrap is implemented")

    require(fresh["mutates_host"], "fresh must remain mutating")
    require(fresh["allows_image_staging"], "fresh must retain image staging")
    require(fresh["allows_allocation_reclaim"], "fresh must retain allocation reclaim authority")
    require(fresh["allows_pos_reset"], "fresh must retain the known-clean POS reset path")
    require(fresh["implementation"] == "active", "fresh must remain executable")


def check_scenario_normalization() -> None:
    deployment = {
        "reservation": {
            "enabled": True,
            "mode": "create",
            "host_preparation": "bootstrap",
            "duration_minutes": 30,
            "image": "ubuntu-jammy",
        }
    }
    _normalize_reservation_policy(deployment)
    require(
        deployment["reservation"]["host_preparation"] == "bootstrap",
        "scenario normalization did not retain bootstrap",
    )

    disabled = {
        "reservation": {
            "enabled": False,
            "mode": "disabled",
            "host_preparation": "bootstrap",
        }
    }
    try:
        _normalize_reservation_policy(disabled)
    except ValueError as exc:
        require(
            "bootstrap host preparation requires" in str(exc),
            f"unexpected disabled-bootstrap failure: {exc}",
        )
    else:
        raise CheckError("bootstrap without POS calendar authority was accepted")


def check_bootstrap_fails_before_host_commands() -> None:
    calls: list[list[str]] = []

    def forbidden_run(argv, *, check=True, stdin=None, echo=False):
        calls.append(list(argv))
        raise CheckError(f"bootstrap attempted a host command: {argv}")

    original_run = reservation.run
    reservation.run = forbidden_run
    try:
        try:
            reservation.prepare_hosts(
                {"host_preparation": "bootstrap", "image": "ubuntu-jammy"},
                selected=["sopnode-f2", "sopnode-f3"],
                calendar={"status": "reused"},
            )
        except reservation.ReservationError as exc:
            require(
                "declared but not executable yet" in str(exc),
                f"unexpected bootstrap guard message: {exc}",
            )
        else:
            raise CheckError("bootstrap unexpectedly executed")
    finally:
        reservation.run = original_run

    require(not calls, f"bootstrap executed host commands before Task 4: {calls}")


def main() -> int:
    check_contract_matrix()
    check_scenario_normalization()
    check_bootstrap_fails_before_host_commands()
    print("Host preparation policy contract checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
