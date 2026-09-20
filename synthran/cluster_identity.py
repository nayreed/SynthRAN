from __future__ import annotations

import re
from typing import Any

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_OPEN5GS_NFS = {
    "nrf", "scp", "amf", "udr", "bsf", "ausf", "nssf", "pcf", "udm", "smf", "upf", "webui"
}
_OAI_RAN_PREFIXES = (
    "oai-gnb",
    "oai-nr-ue",
    "oai-cu-cp",
    "oai-cu-up",
    "oai-cu",
    "oai-du",
)


def _namespace(deployment: dict[str, Any]) -> str:
    topology = deployment.get("topology")
    if not isinstance(topology, dict):
        raise ValueError("deployment identity has no topology mapping")
    namespace = str(topology.get("namespace", ""))
    if not namespace:
        raise ValueError("deployment identity has no selected Kubernetes namespace")
    return namespace


def _is_oai_ran_name(name: str) -> bool:
    return name.startswith(_OAI_RAN_PREFIXES)


def _release_selected(deployment: dict[str, Any], name: str) -> bool:
    core = str(deployment.get("core", "")).lower()
    ran = str(deployment.get("ran", "")).lower()
    if core == "free5gc" and name == "free5gc":
        return True
    if core == "oai" and name.startswith("oai-") and not _is_oai_ran_name(name):
        return True
    if ran == "oai" and _is_oai_ran_name(name):
        return True
    if ran == "srsran" and name in {"srsran-gnb", "srsran-ue"}:
        return True
    if ran == "ueransim" and name.startswith("ueransim-"):
        return True
    return False


def _pod_owner(deployment: dict[str, Any], pod: dict[str, Any]) -> str | None:
    labels = pod.get("labels")
    if not isinstance(labels, dict):
        labels = {}
    labels = {str(key): str(value) for key, value in labels.items()}
    name = str(pod.get("name", ""))
    core = str(deployment.get("core", "")).lower()
    ran = str(deployment.get("ran", "")).lower()

    instance = labels.get("app.kubernetes.io/instance", "")
    if instance and _release_selected(deployment, instance):
        return f"helm:{instance}"

    if core == "open5gs":
        nf = labels.get("nf", "")
        if nf in _OPEN5GS_NFS:
            return f"open5gs:nf:{nf}"
        if labels.get("app.kubernetes.io/name") == "mongodb":
            return "open5gs:mongodb"

    if core == "free5gc":
        app_name = labels.get("app.kubernetes.io/name", "")
        if app_name.startswith("free5gc-") or name.startswith("free5gc-"):
            return f"free5gc:{app_name or name}"

    if core == "oai":
        app_name = labels.get("app.kubernetes.io/name", "")
        candidate = app_name or name
        if candidate.startswith("oai-") and not _is_oai_ran_name(candidate):
            return f"oai-core:{candidate}"

    if ran == "srsran":
        if labels.get("app") == "srsran" and labels.get("component") in {"gnb", "ue"}:
            return f"srsran:{labels['component']}"

    if ran == "ueransim":
        if labels.get("app") == "ueransim" and labels.get("component") in {"gnb", "ue"}:
            return f"ueransim:{labels['component']}"
        if name.startswith("ueransim-"):
            return f"ueransim:{name}"

    if ran == "oai":
        app_name = labels.get("app.kubernetes.io/name", "")
        candidate = app_name or name
        if _is_oai_ran_name(candidate):
            return f"oai-ran:{candidate}"

    return None


def selected_cluster_runtime(
    deployment: dict[str, Any], snapshot: dict[str, Any]
) -> dict[str, Any]:
    """Return runtime identity for only workloads owned by the selected deployment."""

    namespace = _namespace(deployment)
    if str(snapshot.get("namespace", "")) != namespace:
        raise ValueError(
            f"cluster snapshot namespace {snapshot.get('namespace')!r} does not match {namespace!r}"
        )
    pods = snapshot.get("pods")
    if not isinstance(pods, list):
        raise ValueError("cluster snapshot pods must be a list")

    workloads: list[dict[str, Any]] = []
    selected_pods = 0
    for pod in pods:
        if not isinstance(pod, dict):
            continue
        owner = _pod_owner(deployment, pod)
        if owner is None:
            continue
        selected_pods += 1
        if str(pod.get("phase", "")) != "Running" or pod.get("ready") is not True:
            raise ValueError(f"selected workload {owner} pod is not Running and Ready")
        containers = pod.get("containers")
        if not isinstance(containers, list) or not containers:
            raise ValueError(f"selected workload {owner} exposes no container identity")
        for container in containers:
            if not isinstance(container, dict):
                raise ValueError(f"selected workload {owner} has malformed container identity")
            kind = str(container.get("kind", "container"))
            if kind == "ephemeral":
                continue
            name = str(container.get("name", ""))
            configured = str(container.get("configured_image", ""))
            runtime_image_id = str(container.get("runtime_image_id", ""))
            if not name or not configured or _DIGEST_RE.search(runtime_image_id) is None:
                raise ValueError(
                    f"selected workload {owner}/{name or '<unnamed>'} has incomplete image identity"
                )
            if kind == "container" and container.get("ready") is not True:
                raise ValueError(f"selected workload {owner}/{name} is not Ready")
            workloads.append({
                "owner": owner,
                "kind": kind,
                "container": name,
                "configured_image": configured,
                "runtime_image_id": runtime_image_id,
            })

    if selected_pods == 0 or not workloads:
        raise ValueError("cluster snapshot contains no workloads owned by the selected deployment")

    releases = snapshot.get("helm_releases")
    if not isinstance(releases, list):
        raise ValueError("cluster snapshot Helm releases must be a list")
    selected_releases: list[dict[str, Any]] = []
    for release in releases:
        if not isinstance(release, dict):
            continue
        name = str(release.get("name", ""))
        if not _release_selected(deployment, name):
            continue
        if str(release.get("status", "")).lower() != "deployed":
            raise ValueError(f"selected Helm release {name} is not deployed")
        values_sha256 = str(release.get("values_sha256", ""))
        if not re.fullmatch(r"[0-9a-f]{64}", values_sha256):
            raise ValueError(f"selected Helm release {name} has no values digest")
        selected_releases.append({
            "name": name,
            "chart": release.get("chart"),
            "app_version": release.get("app_version"),
            "values_sha256": values_sha256,
        })

    workloads.sort(key=lambda item: (
        str(item["owner"]), str(item["kind"]), str(item["container"]),
        str(item["configured_image"]), str(item["runtime_image_id"]),
    ))
    selected_releases.sort(key=lambda item: str(item["name"]))
    return {
        "schema_version": 1,
        "namespace": namespace,
        "workloads": workloads,
        "helm_releases": selected_releases,
    }


def validate_current_cluster(
    identity: dict[str, Any], snapshot: dict[str, Any]
) -> dict[str, Any]:
    """Require a fresh cluster snapshot to match one historical accepted identity."""

    deployment = identity.get("deployment")
    implementation = identity.get("implementation")
    if not isinstance(deployment, dict) or not isinstance(implementation, dict):
        raise ValueError("accepted deployment identity is missing deployment implementation data")
    cluster_attestation = snapshot.get("cluster_attestation")
    if not isinstance(cluster_attestation, dict):
        raise ValueError("fresh cluster snapshot has no deployment ConfigMap attestation")
    if cluster_attestation.get("configuration_hash") != str(identity.get("configuration_hash", "")):
        raise ValueError("live deployment ConfigMap does not match the accepted configuration identity")

    current = selected_cluster_runtime(deployment, snapshot)
    if current != implementation.get("cluster_runtime"):
        raise ValueError("selected live workload/image/Helm identity differs from accepted-testbed state")
    return current
