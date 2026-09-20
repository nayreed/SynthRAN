#!/usr/bin/env python3
"""Collect a sanitized read-only Kubernetes/Helm runtime snapshot."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import subprocess
from typing import Any


def _output(argv: list[str]) -> str:
    return subprocess.check_output(argv, text=True)


def _status_by_name(pod: dict[str, Any], key: str) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("name", "")): item
        for item in (pod.get("status", {}).get(key, []) or [])
        if isinstance(item, dict)
    }


def _pod_ready(pod: dict[str, Any]) -> bool:
    return any(
        isinstance(item, dict)
        and item.get("type") == "Ready"
        and item.get("status") == "True"
        for item in (pod.get("status", {}).get("conditions", []) or [])
    )


def _containers(pod: dict[str, Any]) -> list[dict[str, Any]]:
    spec = pod.get("spec", {}) or {}
    records: list[dict[str, Any]] = []
    for spec_key, status_key, kind in (
        ("initContainers", "initContainerStatuses", "init"),
        ("containers", "containerStatuses", "container"),
        ("ephemeralContainers", "ephemeralContainerStatuses", "ephemeral"),
    ):
        statuses = _status_by_name(pod, status_key)
        for container in spec.get(spec_key, []) or []:
            if not isinstance(container, dict):
                continue
            name = str(container.get("name", ""))
            status = statuses.get(name, {})
            records.append(
                {
                    "kind": kind,
                    "name": name,
                    "configured_image": str(container.get("image", "")),
                    "runtime_image_id": str(status.get("imageID", "")),
                    "ready": status.get("ready") is True,
                }
            )
    return records


def _cluster_attestation(namespace: str) -> dict[str, Any]:
    raw = json.loads(
        _output(
            [
                "kubectl",
                "get",
                "configmap",
                "synthran-deployment-identity",
                "-n",
                namespace,
                "-o",
                "json",
            ]
        )
    )
    manifest = json.loads(raw.get("data", {}).get("manifest.json", ""))
    configuration_hash = manifest.get("configuration_hash", manifest.get("deployment_hash"))
    if not isinstance(configuration_hash, str) or not configuration_hash:
        raise RuntimeError("live deployment ConfigMap has no configuration identity")
    return {
        "configuration_hash": configuration_hash,
        "resource_version": raw.get("metadata", {}).get("resourceVersion"),
        "uid": raw.get("metadata", {}).get("uid"),
    }


def collect(namespace: str) -> dict[str, Any]:
    pods_raw = json.loads(
        _output(["kubectl", "get", "pods", "-n", namespace, "-o", "json"])
    )
    pods = []
    for pod in pods_raw.get("items", []):
        if not isinstance(pod, dict):
            continue
        metadata = pod.get("metadata", {}) or {}
        pods.append(
            {
                "name": str(metadata.get("name", "")),
                "labels": {
                    str(key): str(value)
                    for key, value in (metadata.get("labels", {}) or {}).items()
                },
                "phase": pod.get("status", {}).get("phase"),
                "ready": _pod_ready(pod),
                "containers": _containers(pod),
            }
        )

    releases = json.loads(_output(["helm", "list", "-n", namespace, "-o", "json"]))
    helm_releases = []
    for release in releases:
        if not isinstance(release, dict):
            continue
        name = str(release.get("name", ""))
        rendered = _output(
            ["helm", "get", "values", name, "-n", namespace, "-a", "-o", "yaml"]
        )
        helm_releases.append(
            {
                "name": name,
                "namespace": namespace,
                "chart": release.get("chart", ""),
                "app_version": release.get("app_version", ""),
                "status": release.get("status", ""),
                "values_sha256": hashlib.sha256(rendered.encode()).hexdigest(),
            }
        )

    nodes_raw = json.loads(_output(["kubectl", "get", "nodes", "-o", "json"]))
    nodes = []
    for node in nodes_raw.get("items", []):
        if not isinstance(node, dict):
            continue
        info = node.get("status", {}).get("nodeInfo", {}) or {}
        nodes.append(
            {
                "name": node.get("metadata", {}).get("name", ""),
                "architecture": info.get("architecture", ""),
                "osImage": info.get("osImage", ""),
                "kernelVersion": info.get("kernelVersion", ""),
                "containerRuntimeVersion": info.get("containerRuntimeVersion", ""),
                "kubeletVersion": info.get("kubeletVersion", ""),
            }
        )

    return {
        "schema_version": 3,
        "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "namespace": namespace,
        "cluster_attestation": _cluster_attestation(namespace),
        "kubernetes": json.loads(_output(["kubectl", "version", "-o", "json"])),
        "nodes": sorted(nodes, key=lambda item: str(item["name"])),
        "helm_releases": sorted(helm_releases, key=lambda item: str(item["name"])),
        "pods": sorted(pods, key=lambda item: str(item["name"])),
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True)
    args = parser.parse_args(argv)
    print(json.dumps(collect(args.namespace), sort_keys=True))


if __name__ == "__main__":
    main()
