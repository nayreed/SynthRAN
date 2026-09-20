"""Authoritative SOP host-preparation policy contract.

Issue #124 introduces bootstrap as the in-place preparation policy between
strictly read-only preserve and known-clean fresh preparation.

Task 1 defines the contract only. bootstrap is intentionally declared but
not executable until the bounded reconciliation implementation lands; callers
must fail closed rather than silently escalating it to fresh.
"""

from __future__ import annotations

PRESERVE = "preserve"
BOOTSTRAP = "bootstrap"
FRESH = "fresh"

PREPARATION_MODES = frozenset({PRESERVE, BOOTSTRAP, FRESH})
EXECUTABLE_PREPARATION_MODES = frozenset({PRESERVE, FRESH})

PREPARATION_CONTRACT = {
    PRESERVE: {
        "mutates_host": False,
        "requires_pos_calendar_authority": False,
        "allows_allocation_reclaim": False,
        "allows_image_staging": False,
        "allows_package_or_service_reconcile": False,
        "allows_boot_parameter_mutation": False,
        "allows_reboot": False,
        "allows_pos_reset": False,
        "silent_escalation_to_fresh": False,
        "implementation": "active",
    },
    BOOTSTRAP: {
        "mutates_host": True,
        "requires_pos_calendar_authority": True,
        "allows_allocation_reclaim": False,
        "allows_image_staging": False,
        "allows_package_or_service_reconcile": True,
        "allows_boot_parameter_mutation": True,
        "allows_reboot": True,
        "allows_pos_reset": False,
        "silent_escalation_to_fresh": False,
        "implementation": "declared",
    },
    FRESH: {
        "mutates_host": True,
        "requires_pos_calendar_authority": True,
        "allows_allocation_reclaim": True,
        "allows_image_staging": True,
        "allows_package_or_service_reconcile": True,
        "allows_boot_parameter_mutation": True,
        "allows_reboot": True,
        "allows_pos_reset": True,
        "silent_escalation_to_fresh": False,
        "implementation": "active",
    },
}


def validate_preparation_mode(value: object) -> str:
    mode = str(value)
    if mode not in PREPARATION_MODES:
        raise ValueError(
            "deployment.reservation.host_preparation must be "
            "bootstrap, fresh, or preserve"
        )
    return mode


def requires_pos_calendar_authority(mode: str) -> bool:
    return bool(
        PREPARATION_CONTRACT[validate_preparation_mode(mode)][
            "requires_pos_calendar_authority"
        ]
    )


def is_executable(mode: str) -> bool:
    return validate_preparation_mode(mode) in EXECUTABLE_PREPARATION_MODES
