"""Connection settings shared by inventory rendering and reservation commands."""

from __future__ import annotations

import os
from pathlib import Path


def access(deployment: dict) -> dict[str, str]:
    settings = deployment.get("r2lab_ssh", {})
    identity = os.environ.get("R2LAB_IDENTITY_FILE") or settings.get(
        "identity_file", ""
    )
    return {
        "host": settings.get("host", "faraday.inria.fr"),
        "username": os.environ.get("R2LAB_USERNAME")
        or settings.get("username")
        or deployment.get("r2lab_username", ""),
        "identity_file": str(Path(identity).expanduser()) if identity else "",
    }


def ssh_options(
    host: str,
    known_hosts: str | Path,
    identity_file: str = "",
    *,
    connect_timeout: int = 15,
) -> list[str]:
    """Return the canonical non-interactive SSH options for the R2Lab gateway.

    Faraday must not inherit a controller-local ~/.ssh/config. The reservation
    command already required this to avoid accidental ProxyJump/config rules;
    inventory rendering uses the same policy so Ansible and nested UE proxy
    connections cannot drift from the provider-verification path.
    """

    options: list[str] = []
    if host == "faraday.inria.fr":
        options += ["-F", "/dev/null"]
    options += [
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={connect_timeout}",
    ]
    if identity_file:
        options += ["-i", identity_file, "-o", "IdentitiesOnly=yes"]
    return options
