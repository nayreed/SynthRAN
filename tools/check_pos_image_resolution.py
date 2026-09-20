#!/usr/bin/env python3
"""Regression checks for SynthRAN POS image alias resolution."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import yaml

from synthran import reservation
from synthran.pos_images import POS_IMAGE_ALIASES, resolve_pos_image, resolve_scenario_pos_image


ALIAS = "ubuntu-jammy"
PINNED = "ubuntu-jammy-slices@2025-04-02T01:33:28+00:00"


def done(argv, rc=0, out="", err=""):
    return subprocess.CompletedProcess(list(argv), rc, out, err)


def main() -> int:
    assert POS_IMAGE_ALIASES[ALIAS] == PINNED
    assert resolve_pos_image(ALIAS) == PINNED
    assert resolve_pos_image(PINNED) == PINNED
    explicit = "custom-provider-image@2026-01-01T00:00:00+00:00"
    assert resolve_pos_image(explicit) == explicit

    with tempfile.TemporaryDirectory() as raw_tmp:
        tmp = Path(raw_tmp)
        scenario = tmp / "resolved.yml"
        scenario.write_text(
            yaml.safe_dump(
                {
                    "deployment": {
                        "reservation": {
                            "mode": "create",
                            "host_preparation": "fresh",
                            "duration_minutes": 120,
                            "image": ALIAS,
                        }
                    }
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        configured, effective = resolve_scenario_pos_image(scenario)
        assert configured == ALIAS
        assert effective == PINNED
        rewritten = yaml.safe_load(scenario.read_text(encoding="utf-8"))
        assert rewritten["deployment"]["reservation"]["image"] == PINNED

        calls: list[list[str]] = []
        original = reservation.run
        try:
            def fake(argv, *, check=True, stdin=None):
                command = list(argv)
                calls.append(command)
                return done(command)

            reservation.run = fake
            result = reservation.prepare_hosts(
                {
                    "host_preparation": "fresh",
                    "image": rewritten["deployment"]["reservation"]["image"],
                },
                selected=["sopnode-f2"],
                calendar={"status": "reused"},
            )
        finally:
            reservation.run = original

        assert result["nodes"]["sopnode-f2"]["image"] == PINNED
        image_commands = [
            command for command in calls if command[:3] == ["pos", "nodes", "image"]
        ]
        assert image_commands == [
            ["pos", "nodes", "image", "--staging", "sopnode-f2", PINNED]
        ]

    print("POS image alias resolution: passed")
    print(f"{ALIAS} -> {PINNED}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
