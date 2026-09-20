#!/usr/bin/env python3
"""Adapt the pinned Free5GC UPF N3 NAD to select OVS or master correctly."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: patch_iupf_n3_nad.py <chart_dest>", file=sys.stderr)
        return 2

    path = (
        Path(sys.argv[1])
        / "charts/free5gc/charts/free5gc-upf/templates/upf-n3-nad.yaml"
    )
    if not path.is_file():
        print(f"ERROR: missing expected UPF N3 NAD: {path}", file=sys.stderr)
        return 1

    content = path.read_text(encoding="utf-8")
    old = '''          "type": {{ .Values.global.n3network.type | quote }},
          "capabilities": { "ips": true },
          "master": {{ .Values.global.n3network.masterIf | quote }},'''
    new = '''          "type": {{ .Values.global.n3network.type | quote }},
          "capabilities": { "ips": true },
{{- if eq .Values.global.n3network.type "ovs" }}
          "bridge": {{ .Values.global.n3network.masterIf | quote }},
          "mtu": 1400,
{{- else }}
          "master": {{ .Values.global.n3network.masterIf | quote }},
{{- end }}'''

    if new in content:
        print(f"already patched: {path}")
        return 0
    if old not in content:
        print(f"ERROR: expected UPF N3 NAD pattern not found: {path}", file=sys.stderr)
        return 1

    path.write_text(content.replace(old, new, 1), encoding="utf-8")
    print(f"patched: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
