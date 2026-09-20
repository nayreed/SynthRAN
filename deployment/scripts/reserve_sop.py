#!/usr/bin/env python3
"""Compatibility entrypoint for SynthRAN's authoritative reservation layer."""

from pathlib import Path
import sys

import yaml

from synthran.pos_images import resolve_scenario_pos_image
from synthran.reservation import main


def _resolve_effective_image(argv: list[str]) -> None:
    if not argv:
        return
    path = Path(argv[0])
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    deployment = data.get("deployment") or {}
    reservation = deployment.get("reservation") or {}
    if reservation.get("host_preparation") != "fresh":
        return
    configured, effective = resolve_scenario_pos_image(path)
    if effective != configured:
        print(
            f"Resolved POS image alias {configured} -> {effective}",
            flush=True,
        )


if __name__ == "__main__":
    _resolve_effective_image(sys.argv[1:])
    raise SystemExit(main())
