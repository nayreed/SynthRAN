"""Authoritative SOP host-preparation policy contract.

Issue #124 introduces bootstrap as the in-place preparation policy between
strictly read-only preserve and known-clean fresh preparation.

Issue #124 Tasks 4-6 activate bootstrap as a bounded in-place reconciler.
It may repair declared host prerequisites and perform a minimum authorized
reboot, but it never stages an image, reclaims allocation, uses the fresh POS
reset path, or silently escalates to fresh.
"""

from __future__ import annotations

PRESERVE = "preserve"
BOOTSTRAP = "bootstrap"
FRESH = "fresh"

PREPARATION_MODES = frozenset({PRESERVE, BOOTSTRAP, FRESH})
EXECUTABLE_PREPARATION_MODES = frozenset({PRESERVE, BOOTSTRAP, FRESH})

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
        "implementation": "active",
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
