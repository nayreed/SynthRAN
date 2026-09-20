#!/usr/bin/env python3
"""Adapt the pinned Free5GC AMF N2 NAD to support OVS CNI explicitly."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: patch_amf_n2_nad.py <chart_dest>", file=sys.stderr)
        return 2

    path = (
        Path(sys.argv[1])
        / "charts/free5gc/charts/free5gc-amf/templates/amf-n2-nad.yaml"
    )
    if not path.is_file():
        print(f"ERROR: missing expected AMF N2 NAD: {path}", file=sys.stderr)
        return 1

    content = path.read_text(encoding="utf-8")
    old = '''          "type": {{ .Values.global.n2network.type | quote }},
          "capabilities": { "ips": true },
          "master": {{ .Values.global.n2network.masterIf | quote }},'''
    new = '''          "type": {{ .Values.global.n2network.type | quote }},
          "capabilities": { "ips": true },
{{- if eq .Values.global.n2network.type "ovs" }}
          "bridge": {{ .Values.global.n2network.masterIf | quote }},
{{- else }}
          "master": {{ .Values.global.n2network.masterIf | quote }},
{{- end }}'''

    if new in content:
        print(f"already patched: {path}")
        return 0
    if old not in content:
        print(f"ERROR: expected AMF N2 NAD pattern not found: {path}", file=sys.stderr)
        return 1

    path.write_text(content.replace(old, new, 1), encoding="utf-8")
    print(f"patched: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
