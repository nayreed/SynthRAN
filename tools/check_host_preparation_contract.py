#!/usr/bin/env python3
"""Regression checks for issue #124 host-preparation policy vocabulary."""

from __future__ import annotations

from synthran import host_preparation, reservation
from synthran.deployment_state import build_manifest
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
        == frozenset({"preserve", "bootstrap", "fresh"}),
        "bootstrap must be executable after Tasks 4-6",
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
    require(bootstrap["implementation"] == "active", "bootstrap reconciliation must be active")

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


def check_bootstrap_reservation_retains_host() -> None:
    calls: list[list[str]] = []

    def forbidden_run(argv, *, check=True, stdin=None, echo=False):
        calls.append(list(argv))
        raise CheckError(f"bootstrap reservation unexpectedly executed a POS command: {argv}")

    original_run = reservation.run
    reservation.run = forbidden_run
    try:
        result = reservation.prepare_hosts(
            {"host_preparation": "bootstrap", "image": "ubuntu-jammy"},
            selected=["sopnode-f2", "sopnode-f3"],
            calendar={"status": "reused"},
        )
    finally:
        reservation.run = original_run

    require(result["mode"] == "bootstrap", "bootstrap reservation mode was not retained")
    require(not calls, f"bootstrap reservation executed POS commands: {calls}")
    for node in ("sopnode-f2", "sopnode-f3"):
        require(
            result["nodes"][node]["allocation"] == "retained",
            f"bootstrap did not retain allocation for {node}",
        )
        require(
            result["nodes"][node]["image"] == "retained",
            f"bootstrap did not retain OS image for {node}",
        )

    try:
        reservation.prepare_hosts(
            {"host_preparation": "bootstrap", "image": "ubuntu-jammy"},
            selected=["sopnode-f2"],
            calendar={"status": "disabled"},
        )
    except reservation.ReservationError as exc:
        require(
            "requires create or require-existing POS calendar authority" in str(exc),
            f"unexpected disabled-bootstrap failure: {exc}",
        )
    else:
        raise CheckError("bootstrap without POS calendar authority was accepted")



def check_deployment_identity_policy() -> None:
    network_profile = {
        "plmn": {"mcc": "001", "mnc": "01"},
        "slices": [],
    }
    ue_map = [{"device": "uesim01", "user_plane_target": "12.1.1.1"}]
    topology = {"namespace": "open5gs"}
    for mode in ("preserve", "bootstrap", "fresh"):
        scenario = {
            "deployment": {
                "core": "open5gs",
                "ran": "srsran",
                "platform": "rfsim",
                "nodes": {"core": "f2", "ran": "f3", "broker": "f2"},
                "network_profile": "ci",
                "ues": ["uesim01"],
                "reservation": {
                    "mode": "create",
                    "host_preparation": mode,
                    "image": "ubuntu-jammy",
                },
            }
        }
        manifest = build_manifest(scenario, network_profile, ue_map, topology)
        deployment = manifest["deployment"]
        require(
            deployment["reservation_mode"] == "create",
            f"deployment identity lost reservation mode for {mode}",
        )
        require(
            deployment["host_preparation"] == mode,
            f"deployment identity lost host preparation mode {mode}",
        )
        if mode == "fresh":
            require(
                deployment["pos_image"] == "ubuntu-jammy",
                "fresh deployment identity lost selected POS image",
            )
        else:
            require(
                deployment["pos_image"] is None,
                f"{mode} deployment identity must not claim an unused POS image",
            )


def main() -> int:
    check_contract_matrix()
    check_scenario_normalization()
    check_deployment_identity_policy()
    check_bootstrap_reservation_retains_host()
    print("Host preparation policy contract checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
