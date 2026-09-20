"""Read-only attachment to an accepted SynthRAN testbed deployment.

Historical acceptance and current experiment eligibility are deliberately
separate. Attachment proves that a deployment reached the accepted-testbed
state with internally consistent identity/evidence. Experiment eligibility
requires a fresh read-only observation of the same accepted deployment.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from synthran.acceptance import (
    ACCEPTED_ENDPOINT_SCHEMA_VERSION,
    validate_live_evidence,
    validate_prerequisite_evidence,
)
from synthran.deployment_identity import (
    validate_current_cluster_runtime,
    validate_retained_execution_context,
)
from synthran.deployment_state import SCHEMA_VERSION

ROOT = Path(__file__).resolve().parents[1]
ACTIVE_DEPLOYMENT_ENDPOINT = ROOT / ".synthran/active-deployment.json"


class AttachmentError(ValueError):
    """The saved accepted deployment cannot satisfy an experiment contract."""


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AttachmentError(f"{label} is missing: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise AttachmentError(f"{label} is unreadable: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AttachmentError(f"{label} must be a JSON object: {path}")
    return value


def _expected(actual: Any, requirement: Any, label: str) -> None:
    if isinstance(requirement, (list, tuple, set, frozenset)):
        if actual not in requirement:
            raise AttachmentError(
                f"accepted deployment {label}={actual!r} is not one of {list(requirement)!r}"
            )
        return
    if actual != requirement:
        raise AttachmentError(
            f"accepted deployment {label}={actual!r} does not match required {requirement!r}"
        )


def _transport_value(binding: dict[str, Any], key: str) -> Any:
    if key in binding:
        return binding.get(key)
    tunnel = binding.get("tunnel", {})
    return tunnel.get(key) if isinstance(tunnel, dict) else None


def _ue_matches(binding: dict[str, Any], requirement: dict[str, Any]) -> bool:
    direct = {
        "device",
        "index",
        "imsi",
        "slice",
        "sst",
        "sd",
        "dnn",
        "address_cidr",
        "user_plane_target",
    }
    transport = {"host", "namespace", "interface", "mode", "mbim_session"}
    for key, expected in requirement.items():
        if key in direct:
            actual = binding.get(key)
        elif key in transport:
            actual = _transport_value(binding, key)
        else:
            raise AttachmentError(f"unsupported UE attachment requirement: {key}")
        if isinstance(expected, (list, tuple, set, frozenset)):
            if actual not in expected:
                return False
        elif actual != expected:
            return False
    return True


def _slice_matches(candidate: dict[str, Any], requirement: dict[str, Any]) -> bool:
    for key, expected in requirement.items():
        if key not in {"name", "sst", "sd", "dnn", "ip_prefix"}:
            raise AttachmentError(f"unsupported slice attachment requirement: {key}")
        actual = candidate.get(key)
        if isinstance(expected, (list, tuple, set, frozenset)):
            if actual not in expected:
                return False
        elif actual != expected:
            return False
    return True


def validate_requirements(
    deployment: dict[str, Any],
    requirements: dict[str, Any] | None,
    *,
    deployment_hash: str,
) -> None:
    requirements = requirements or {}
    allowed = {
        "deployment_hash",
        "core",
        "ran",
        "platform",
        "radio_unit",
        "network_profile",
        "bridge_enabled",
        "reservation_mode",
        "host_preparation",
        "nodes",
        "minimum_ues",
        "ue_devices",
        "exact_ue_devices",
        "ues",
        "slices",
    }
    unknown = sorted(set(requirements) - allowed)
    if unknown:
        raise AttachmentError(
            "unsupported accepted-testbed requirement(s): " + ", ".join(unknown)
        )

    if "deployment_hash" in requirements:
        _expected(deployment_hash, requirements["deployment_hash"], "deployment_hash")
    for key in (
        "core",
        "ran",
        "platform",
        "radio_unit",
        "network_profile",
        "bridge_enabled",
        "reservation_mode",
        "host_preparation",
    ):
        if key in requirements:
            _expected(deployment.get(key), requirements[key], key)

    if "nodes" in requirements:
        required_nodes = requirements["nodes"]
        if not isinstance(required_nodes, dict):
            raise AttachmentError("accepted-testbed nodes requirement must be a mapping")
        actual_nodes = deployment.get("nodes", {})
        if not isinstance(actual_nodes, dict):
            raise AttachmentError("accepted deployment node identity is malformed")
        for role, expected in required_nodes.items():
            if role not in actual_nodes:
                raise AttachmentError(f"accepted deployment has no node role {role!r}")
            _expected(actual_nodes[role], expected, f"nodes.{role}")

    bindings = deployment.get("ues", [])
    if not isinstance(bindings, list) or not all(isinstance(item, dict) for item in bindings):
        raise AttachmentError("accepted deployment UE identity is malformed")
    if "minimum_ues" in requirements:
        minimum = requirements["minimum_ues"]
        if not isinstance(minimum, int) or minimum < 0:
            raise AttachmentError("minimum_ues must be a non-negative integer")
        if len(bindings) < minimum:
            raise AttachmentError(
                f"accepted deployment has {len(bindings)} UE(s); at least {minimum} required"
            )

    if "ue_devices" in requirements:
        required_devices = requirements["ue_devices"]
        if not isinstance(required_devices, list) or not all(
            isinstance(value, str) and value for value in required_devices
        ):
            raise AttachmentError("ue_devices must be a non-empty-name list")
        actual_devices = [str(item.get("device")) for item in bindings]
        missing = sorted(set(required_devices) - set(actual_devices))
        if missing:
            raise AttachmentError(
                "accepted deployment is missing required UE device(s): " + ", ".join(missing)
            )
        if requirements.get("exact_ue_devices") is True and set(actual_devices) != set(required_devices):
            raise AttachmentError(
                "accepted deployment UE set differs from the experiment's exact UE set"
            )

    ue_requirements = requirements.get("ues", [])
    if not isinstance(ue_requirements, list):
        raise AttachmentError("accepted-testbed ues requirement must be a list")
    for requirement in ue_requirements:
        if not isinstance(requirement, dict) or not requirement:
            raise AttachmentError("each accepted-testbed UE requirement must be a mapping")
        matches = [item for item in bindings if _ue_matches(item, requirement)]
        if len(matches) != 1:
            raise AttachmentError(
                f"UE requirement {requirement!r} matched {len(matches)} accepted binding(s); expected exactly one"
            )

    slices = deployment.get("slices", [])
    if not isinstance(slices, list) or not all(isinstance(item, dict) for item in slices):
        raise AttachmentError("accepted deployment slice identity is malformed")
    slice_requirements = requirements.get("slices", [])
    if not isinstance(slice_requirements, list):
        raise AttachmentError("accepted-testbed slices requirement must be a list")
    for requirement in slice_requirements:
        if not isinstance(requirement, dict) or not requirement:
            raise AttachmentError("each accepted-testbed slice requirement must be a mapping")
        matches = [item for item in slices if _slice_matches(item, requirement)]
        if len(matches) != 1:
            raise AttachmentError(
                f"slice requirement {requirement!r} matched {len(matches)} accepted slice(s); expected exactly one"
            )


def requirements_from_deployment(deployment: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(deployment, dict):
        raise AttachmentError("deployment requirement source must be a mapping")
    return {
        "core": deployment.get("core"),
        "ran": str(deployment.get("ran", "")).lower(),
        "platform": deployment.get("platform"),
        "radio_unit": (
            "rfsim"
            if deployment.get("platform") == "rfsim"
            else deployment.get("ru", deployment.get("platform"))
        ),
        "network_profile": deployment.get("network_profile"),
        "nodes": copy.deepcopy(deployment.get("nodes", {})),
        "ue_devices": list(deployment.get("ues", [])),
        "exact_ue_devices": True,
    }


def attach_active_deployment(
    requirements: dict[str, Any] | None = None,
    *,
    endpoint_path: str | Path = ACTIVE_DEPLOYMENT_ENDPOINT,
    evidence_max_age_seconds: int | None = None,
) -> dict[str, Any]:
    """Attach read-only to historical accepted-testbed state."""

    endpoint = _read_object(Path(endpoint_path).resolve(), "accepted deployment endpoint")
    if endpoint.get("schema_version") != ACCEPTED_ENDPOINT_SCHEMA_VERSION:
        raise AttachmentError("accepted deployment endpoint schema is unsupported")
    if endpoint.get("status") != "accepted-testbed":
        raise AttachmentError("saved deployment endpoint is not accepted-testbed")

    identity_path = Path(str(endpoint.get("identity_file", ""))).resolve()
    evidence_path = Path(str(endpoint.get("evidence_file", ""))).resolve()
    private_dir = Path(str(endpoint.get("private_execution_dir", ""))).resolve()
    result_dir = Path(str(endpoint.get("result_dir", ""))).resolve()
    identity = _read_object(identity_path, "accepted deployment identity")
    if identity.get("schema_version") != SCHEMA_VERSION:
        raise AttachmentError("accepted deployment identity schema is unsupported")
    if identity.get("status") != "accepted-testbed":
        raise AttachmentError("accepted deployment identity is not accepted-testbed")
    if not result_dir.is_dir():
        raise AttachmentError(f"accepted deployment result directory is missing: {result_dir}")

    try:
        evidence = validate_live_evidence(
            identity_path,
            evidence_path,
            max_age_seconds=evidence_max_age_seconds,
        )
        validate_retained_execution_context(identity, result_dir, private_dir)
        validate_prerequisite_evidence(identity, result_dir)
    except ValueError as exc:
        raise AttachmentError(str(exc)) from exc

    deployment = identity["deployment"]
    configuration_hash = identity["configuration_hash"]
    deployment_hash = identity["deployment_hash"]
    if endpoint.get("deployment_hash") != deployment_hash:
        raise AttachmentError("accepted deployment endpoint and identity hashes differ")
    if endpoint.get("configuration_hash") != configuration_hash:
        raise AttachmentError("accepted deployment endpoint and configuration hashes differ")
    validate_requirements(deployment, requirements, deployment_hash=deployment_hash)

    return {
        "schema_version": 2,
        "status": "accepted-testbed-attached",
        "acceptance_state": "accepted-testbed",
        "experiment_eligible": False,
        "mode": "read_only",
        "configuration_hash": configuration_hash,
        "deployment_hash": deployment_hash,
        "deployment_run_id": endpoint.get("run_id"),
        "endpoint_published_at": endpoint.get("published_at"),
        "deployment_accepted_at": identity.get("accepted_at"),
        "acceptance_evidence_observed_at": evidence.get("observed_at"),
        "identity_file": str(identity_path),
        "evidence_file": str(evidence_path),
        "private_execution_dir": str(private_dir),
        "result_dir": str(result_dir),
        "requirements": copy.deepcopy(requirements or {}),
        "deployment": {
            key: copy.deepcopy(deployment.get(key))
            for key in (
                "core",
                "ran",
                "platform",
                "radio_unit",
                "reservation_mode",
                "host_preparation",
                "pos_image",
                "network_profile",
                "bridge_enabled",
                "nodes",
                "slices",
                "ues",
            )
        },
        "claim_boundary": (
            "Historical attachment proves a previously accepted-testbed identity, "
            "an intact retained execution context, and intact prerequisite evidence; "
            "it does not prove current RF, UE, session, or user-plane liveness."
        ),
    }


def prove_experiment_eligible(
    eligibility_evidence_path: str | Path,
    requirements: dict[str, Any] | None = None,
    *,
    endpoint_path: str | Path = ACTIVE_DEPLOYMENT_ENDPOINT,
    max_age_seconds: int = 120,
    attachment: dict[str, Any] | None = None,
    cluster_snapshot_path: str | Path | None = None,
) -> dict[str, Any]:
    """Require one fresh read-only observation of an already accepted deployment."""

    if attachment is None:
        attachment = attach_active_deployment(requirements, endpoint_path=endpoint_path)
    else:
        if attachment.get("status") != "accepted-testbed-attached":
            raise AttachmentError("provided deployment attachment is not an accepted-testbed attachment")
        deployment = attachment.get("deployment")
        if not isinstance(deployment, dict):
            raise AttachmentError("provided deployment attachment has no deployment mapping")
        validate_requirements(
            deployment,
            requirements,
            deployment_hash=str(attachment.get("deployment_hash", "")),
        )

    evidence_path = Path(eligibility_evidence_path).resolve()
    if cluster_snapshot_path is None:
        cluster_path = evidence_path.with_name("experiment-eligibility-cluster.json")
    else:
        cluster_path = Path(cluster_snapshot_path).resolve()
    identity = _read_object(Path(attachment["identity_file"]), "accepted deployment identity")
    try:
        validate_retained_execution_context(
            identity,
            attachment["result_dir"],
            attachment["private_execution_dir"],
        )
        validate_prerequisite_evidence(identity, attachment["result_dir"])
        validate_current_cluster_runtime(identity, cluster_path)
        evidence = validate_live_evidence(
            attachment["identity_file"],
            evidence_path,
            max_age_seconds=max_age_seconds,
        )
    except ValueError as exc:
        raise AttachmentError(str(exc)) from exc

    result = copy.deepcopy(attachment)
    result["status"] = "experiment-eligible"
    result["experiment_eligible"] = True
    result["eligibility_evidence_file"] = str(evidence_path)
    result["eligibility_cluster_snapshot_file"] = str(cluster_path)
    result["eligibility_observed_at"] = evidence.get("observed_at")
    result["claim_boundary"] = (
        "Experiment eligibility proves one fresh selected workload/image/Helm identity, "
        "UE/session identity, and source-bound user-plane observation for this accepted "
        "deployment only."
    )
    return result
