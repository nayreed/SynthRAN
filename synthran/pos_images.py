from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


# Provider artifact pinned from sopnode/5g_ansible@
# b73fccf87f55060484b3759e9cb347222253534b roles/pos/defaults/main.yml.
# Keep the short SynthRAN alias stable for users, but never pass it directly to POS.
POS_IMAGE_ALIASES = {
    "ubuntu-jammy": "ubuntu-jammy-slices@2025-04-02T01:33:28+00:00",
}


def resolve_pos_image(value: str) -> str:
    image = str(value).strip()
    if not image:
        raise ValueError("deployment.reservation.image must be a non-empty string")
    return POS_IMAGE_ALIASES.get(image, image)


def resolve_scenario_pos_image(path: str | Path) -> tuple[str, str]:
    """Resolve a friendly POS image alias in the private deployment snapshot.

    Returns ``(configured, effective)``. Exact provider image identifiers are left
    unchanged. When an alias is used, the private resolved scenario is rewritten
    atomically before reservation/POS mutation so inventory, evidence and the
    post-reservation public snapshot all see the effective image.
    """

    source = Path(path)
    raw: Any = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("deployment"), dict):
        raise ValueError("resolved scenario requires deployment mapping")
    reservation = raw["deployment"].get("reservation")
    if not isinstance(reservation, dict):
        raise ValueError("deployment.reservation must be a mapping")

    configured = str(reservation.get("image", "ubuntu-jammy")).strip()
    effective = resolve_pos_image(configured)
    if effective == configured:
        return configured, effective

    reservation["image"] = effective
    temporary = source.with_name(source.name + ".pos-image.tmp")
    temporary.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    temporary.replace(source)
    return configured, effective
