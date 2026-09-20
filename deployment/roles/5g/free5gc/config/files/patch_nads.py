#!/usr/bin/env python3
"""Patch Free5GC NAD templates so an empty gateway does not create a default route."""

from __future__ import annotations

import glob
import os
import re
import sys


def patch_nad_file(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        content = handle.read()

    match = re.search(r"\.Values\.global\.(\w+network)\.gatewayIP", content)
    if not match:
        return "not-applicable"

    netvar = match.group(1)
    old = (
        '            "type": "static",\n'
        '            "routes": [\n'
        '              {\n'
        '                "dst": "0.0.0.0/0",\n'
        f'                "gw": "{{{{ .Values.global.{netvar}.gatewayIP }}}}"\n'
        '              }\n'
        '            ]'
    )
    new = (
        f'{{{{- if and .Values.global.{netvar}.gatewayIP '
        f'(ne .Values.global.{netvar}.gatewayIP "") }}}}\n'
        '            "type": "static",\n'
        '            "routes": [\n'
        '              {\n'
        '                "dst": "0.0.0.0/0",\n'
        f'                "gw": "{{{{ .Values.global.{netvar}.gatewayIP }}}}"\n'
        '              }\n'
        '            ]\n'
        '{{- else }}\n'
        '            "type": "static"\n'
        '{{- end }}'
    )

    if new in content:
        print(f"already patched: {path}")
        return "already"
    if old not in content:
        raise RuntimeError(f"expected gateway route pattern not found: {path}")

    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content.replace(old, new, 1))
    print(f"patched: {path}")
    return "patched"


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: patch_nads.py <chart_dest>", file=sys.stderr)
        return 2

    chart_dest = sys.argv[1]
    nad_files = sorted(
        glob.glob(os.path.join(chart_dest, "charts/**/*-nad.yaml"), recursive=True)
    )
    if not nad_files:
        print(f"ERROR: no NAD files found under {chart_dest}", file=sys.stderr)
        return 1

    applicable = 0
    patched = 0
    try:
        for path in nad_files:
            result = patch_nad_file(path)
            if result != "not-applicable":
                applicable += 1
            if result == "patched":
                patched += 1
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if applicable == 0:
        print("ERROR: no gateway-bearing NAD template matched the pinned source", file=sys.stderr)
        return 1

    print(
        f"Done: {patched} patched, {applicable} applicable, "
        f"{len(nad_files)} NAD files inspected."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
