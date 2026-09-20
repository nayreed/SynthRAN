#!/usr/bin/env python3
"""Static contract checks for the simple interactive reservation UX."""

from pathlib import Path


class CheckError(RuntimeError):
    pass


def require(text: str, needle: str) -> None:
    if needle not in text:
        raise CheckError(f"deploy.sh is missing reservation wizard contract marker: {needle}")


def main() -> int:
    text = Path("deploy.sh").read_text(encoding="utf-8")
    markers = [
        "Ensure selected SOP nodes are reserved?",
        "How should SynthRAN prepare the selected SOP nodes?",
        "Reuse verified current node state",
        "Bootstrap/reconcile current node state",
        "Fresh reset/reimage",
        "No host repair or reboot.",
        "Keep the current OS/allocation and repair safe prerequisites in place",
        "known-clean POS image/reset path",
        "SELECTED_RESERVATION_MODE=create",
        "SELECTED_RESERVATION_MODE=require-existing",
        "SELECTED_RESERVATION_MODE=disabled",
        "SELECTED_HOST_PREPARATION=fresh",
        "SELECTED_HOST_PREPARATION=bootstrap",
        "SELECTED_HOST_PREPARATION=preserve",
        "Enter choice [1-3]",
        "bootstrap (retain OS/allocation; bounded reconcile/reboot)",
        "preserve (verify/reuse only; no repair)",
        "fresh reset/reimage, image",
        "'mode': reservation_mode",
        "'host_preparation': host_preparation",
    ]
    for marker in markers:
        require(text, marker)

    for leaked_internal_label in (
        "SOP reservation policy (default:",
        "Create or reuse the exact selected-node reservation",
        "Require an existing exact selected-node reservation",
        "Disable SOP reservation management",
        "SOP host preparation policy (default:",
    ):
        if leaked_internal_label in text:
            raise CheckError(
                "internal reservation policy leaked back into the ordinary interactive UX: "
                + leaked_internal_label
            )

    preserve_choice = text.index("1) SELECTED_HOST_PREPARATION=preserve")
    bootstrap_choice = text.index("2) SELECTED_HOST_PREPARATION=bootstrap")
    fresh_choice = text.index("3) SELECTED_HOST_PREPARATION=fresh")
    if not preserve_choice < bootstrap_choice < fresh_choice:
        raise CheckError("interactive preparation choice mapping is not preserve -> bootstrap -> fresh")

    if text.index("'mode': reservation_mode") > text.index("Path(output).write_text"):
        raise CheckError("reservation mode is not materialized before scenario write")
    if text.index("'host_preparation': host_preparation") > text.index("Path(output).write_text"):
        raise CheckError("host preparation is not materialized before scenario write")

    print("Reservation wizard contract checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
