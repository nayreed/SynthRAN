"""Accepted-testbed teardown lifecycle state and evidence.

This module owns only the control-plane state transition.  Provider mutation
(POS/R2Lab) and Ansible execution are deliberately kept outside these helpers
so the accepted deployment is fenced before the first destructive action and
all subsequent phases can report into one retained result.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

from .acceptance import (
    ACCEPTANCE_SCHEMA_VERSION,
    ACCEPTED_ENDPOINT_SCHEMA_VERSION,
    accepted_deployment_hash,
    validate_prerequisite_evidence,
)
from .deployment_identity import validate_retained_execution_context
from .deployment_state import SCHEMA_VERSION, content_hash
from .testbed_attachment import AttachmentError, attach_active_deployment

TEARDOWN_SCHEMA = "synthran/testbed-teardown/v1"
_ACCEPTED_STATUS = "accepted-testbed"
_STOPPING_STATUS = "stopping"
_FAILED_STATUS = "cleanup-failed"
_TORN_DOWN_STATUS = "torn-down"
_SUCCESS_PHASE_STATES = {"succeeded", "skipped", "preserved", "released", "already-absent"}
_REQUIRED_PHASES = ("resources", "namespace", "pos_calendar", "r2lab")


class TeardownError(RuntimeError):
    """The selected accepted-testbed teardown contract cannot be proven."""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _atomic_json(path: str | Path, value: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _read_object(path: str | Path, label: str) -> dict[str, Any]:
    path = Path(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TeardownError(f"{label} is missing: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise TeardownError(f"{label} is unreadable: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise TeardownError(f"{label} must be a JSON object: {path}")
    return value


def _static_retry_context(endpoint: dict[str, Any]) -> dict[str, Any]:
    """Validate immutable accepted identity after teardown has already mutated liveness.

    A retry cannot require the original live UE/session evidence to still pass: a
    previous teardown attempt may already have stopped one selected resource.
    Instead, retry authority is the unchanged accepted identity plus its retained
    execution context and prerequisite evidence.
    """

    identity_path = Path(str(endpoint.get("identity_file", ""))).resolve()
    result_dir = Path(str(endpoint.get("result_dir", ""))).resolve()
    private_dir = Path(str(endpoint.get("private_execution_dir", ""))).resolve()
    identity = _read_object(identity_path, "accepted deployment identity")

    if identity.get("schema_version") != SCHEMA_VERSION:
        raise TeardownError("accepted deployment identity schema is unsupported")
    if identity.get("acceptance_schema_version") != ACCEPTANCE_SCHEMA_VERSION:
        raise TeardownError("accepted deployment acceptance schema is unsupported")
    if identity.get("status") != _ACCEPTED_STATUS:
        raise TeardownError("accepted deployment identity is no longer accepted-testbed")
    deployment = identity.get("deployment")
    implementation = identity.get("implementation")
    if not isinstance(deployment, dict) or not isinstance(implementation, dict):
        raise TeardownError("accepted deployment identity is incomplete")

    configuration_hash = content_hash(deployment)
    if identity.get("configuration_hash") != configuration_hash:
        raise TeardownError("accepted deployment configuration identity failed its integrity check")
    deployment_hash = accepted_deployment_hash(identity)
    if identity.get("deployment_hash") != deployment_hash:
        raise TeardownError("accepted deployment executable identity failed its integrity check")
    implementation_hash = content_hash(implementation)
    if identity.get("implementation_identity_sha256") != implementation_hash:
        raise TeardownError("accepted deployment implementation identity failed its integrity check")
    if endpoint.get("configuration_hash") != configuration_hash:
        raise TeardownError("teardown endpoint and accepted configuration hashes differ")
    if endpoint.get("deployment_hash") != deployment_hash:
        raise TeardownError("teardown endpoint and accepted deployment hashes differ")
    if not result_dir.is_dir():
        raise TeardownError(f"accepted deployment result directory is missing: {result_dir}")

    try:
        validate_retained_execution_context(identity, result_dir, private_dir)
        validate_prerequisite_evidence(identity, result_dir)
    except ValueError as exc:
        raise TeardownError(str(exc)) from exc

    return {
        "run_id": str(endpoint.get("run_id", "")),
        "configuration_hash": configuration_hash,
        "deployment_hash": deployment_hash,
        "identity_file": str(identity_path),
        "evidence_file": str(Path(str(endpoint.get("evidence_file", ""))).resolve()),
        "private_execution_dir": str(private_dir),
        "result_dir": str(result_dir),
        "deployment": deployment,
    }


def resolve_teardown_context(
    endpoint_path: str | Path,
    *,
    allow_retry: bool = True,
) -> dict[str, Any]:
    """Resolve the exact accepted deployment without consulting a new scenario."""

    endpoint_path = Path(endpoint_path).resolve()
    endpoint = _read_object(endpoint_path, "accepted deployment endpoint")
    if endpoint.get("schema_version") != ACCEPTED_ENDPOINT_SCHEMA_VERSION:
        raise TeardownError("accepted deployment endpoint schema is unsupported")

    status = endpoint.get("status")
    if status == _ACCEPTED_STATUS:
        try:
            attachment = attach_active_deployment(
                endpoint_path=endpoint_path,
                evidence_max_age_seconds=None,
            )
        except AttachmentError as exc:
            raise TeardownError(str(exc)) from exc
        context = {
            "run_id": str(attachment.get("deployment_run_id", "")),
            "configuration_hash": str(attachment.get("configuration_hash", "")),
            "deployment_hash": str(attachment.get("deployment_hash", "")),
            "identity_file": str(attachment["identity_file"]),
            "evidence_file": str(attachment["evidence_file"]),
            "private_execution_dir": str(attachment["private_execution_dir"]),
            "result_dir": str(attachment["result_dir"]),
            "deployment": attachment["deployment"],
        }
    elif status == _FAILED_STATUS and allow_retry:
        context = _static_retry_context(endpoint)
    else:
        raise TeardownError(
            "testbed teardown requires accepted-testbed state"
            + (" or a retained cleanup-failed retry" if allow_retry else "")
            + f"; found {status!r}"
        )

    result_dir = Path(context["result_dir"]).resolve()
    private_dir = Path(context["private_execution_dir"]).resolve()
    if result_dir.name != context["run_id"]:
        raise TeardownError("accepted endpoint run_id does not match its retained result directory")
    teardown_playbook = private_dir / "ansible/playbooks/teardown.yml"
    inventory = private_dir / "inventory.yml"
    variables = private_dir / "deployment-vars.yml"
    global_vars = private_dir / "ansible/group_vars/all/all.yml"
    ansible_config = private_dir / "ansible/ansible.cfg"
    roles = private_dir / "ansible/roles"
    missing = [
        str(path)
        for path in (teardown_playbook, inventory, variables, global_vars, ansible_config, roles)
        if not path.exists()
    ]
    if missing:
        raise TeardownError(
            "accepted retained teardown execution context is incomplete: " + ", ".join(missing)
        )

    context.update(
        {
            "endpoint_file": str(endpoint_path),
            "teardown_playbook": str(teardown_playbook),
            "inventory_file": str(inventory),
            "deployment_vars_file": str(variables),
            "global_vars_file": str(global_vars),
            "ansible_config": str(ansible_config),
            "ansible_roles_path": str(roles),
            "retry": status == _FAILED_STATUS,
        }
    )
    return context


def _new_result(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": TEARDOWN_SCHEMA,
        "status": _STOPPING_STATUS,
        "run_id": context["run_id"],
        "configuration_hash": context["configuration_hash"],
        "deployment_hash": context["deployment_hash"],
        "started_at": _now(),
        "attempts": 1,
        "phases": {},
    }


def _retry_result(path: Path, context: dict[str, Any]) -> dict[str, Any]:
    result = _read_object(path, "teardown result")
    if result.get("schema") != TEARDOWN_SCHEMA:
        raise TeardownError("teardown result schema is unsupported")
    if result.get("run_id") != context["run_id"]:
        raise TeardownError("teardown result belongs to a different deployment run")
    if result.get("configuration_hash") != context["configuration_hash"]:
        raise TeardownError("teardown result configuration hash differs from accepted deployment")
    if result.get("deployment_hash") != context["deployment_hash"]:
        raise TeardownError("teardown result deployment hash differs from accepted deployment")
    if result.get("status") != _FAILED_STATUS:
        raise TeardownError("cleanup-failed endpoint does not reference a retryable teardown result")
    phases = result.get("phases")
    if not isinstance(phases, dict):
        raise TeardownError("teardown result phase evidence is malformed")
    attempts = result.get("attempts", 1)
    if not isinstance(attempts, int) or attempts < 1:
        raise TeardownError("teardown result attempt counter is malformed")
    result["attempts"] = attempts + 1
    result["status"] = _STOPPING_STATUS
    result["retry_started_at"] = _now()
    result.pop("failure", None)
    result.pop("failed_at", None)
    return result


def begin_teardown(endpoint_path: str | Path) -> dict[str, Any]:
    """Validate authority, persist result evidence, then fence experiment attachment."""

    endpoint_path = Path(endpoint_path).resolve()
    context = resolve_teardown_context(endpoint_path)
    result_path = Path(context["result_dir"]) / "teardown-result.json"
    result = (
        _retry_result(result_path, context)
        if context["retry"]
        else _new_result(context)
    )
    _atomic_json(result_path, result)

    endpoint = _read_object(endpoint_path, "accepted deployment endpoint")
    if endpoint.get("status") not in {_ACCEPTED_STATUS, _FAILED_STATUS}:
        raise TeardownError("accepted deployment endpoint changed before teardown fencing")
    if endpoint.get("deployment_hash") != context["deployment_hash"]:
        raise TeardownError("accepted deployment endpoint changed before teardown fencing")
    endpoint["status"] = _STOPPING_STATUS
    endpoint["teardown_result_file"] = str(result_path.resolve())
    endpoint["teardown_started_at"] = _now()
    _atomic_json(endpoint_path, endpoint)

    context["result_file"] = str(result_path.resolve())
    return context


def phase_completed(result_path: str | Path, phase: str) -> bool:
    """Return True only for a previously retained successful terminal phase."""

    result = _read_object(result_path, "teardown result")
    phases = result.get("phases")
    if not isinstance(phases, dict):
        raise TeardownError("teardown result phase evidence is malformed")
    record = phases.get(phase)
    return isinstance(record, dict) and record.get("status") in _SUCCESS_PHASE_STATES


def record_phase(
    result_path: str | Path,
    phase: str,
    *,
    status: str,
    exit_code: int | None = None,
    detail: str | None = None,
    evidence_file: str | Path | None = None,
) -> None:
    """Retain one phase outcome without erasing earlier successful retry evidence."""

    if status not in _SUCCESS_PHASE_STATES | {"failed"}:
        raise TeardownError(f"unsupported teardown phase status: {status}")
    path = Path(result_path)
    result = _read_object(path, "teardown result")
    if result.get("schema") != TEARDOWN_SCHEMA or result.get("status") != _STOPPING_STATUS:
        raise TeardownError("teardown phase can only be recorded while endpoint is stopping")
    phases = result.setdefault("phases", {})
    if not isinstance(phases, dict):
        raise TeardownError("teardown result phase evidence is malformed")
    existing = phases.get(phase)
    if isinstance(existing, dict) and existing.get("status") in _SUCCESS_PHASE_STATES:
        if status not in _SUCCESS_PHASE_STATES:
            raise TeardownError(f"refusing to overwrite successful teardown phase {phase!r}")
        return

    record: dict[str, Any] = {"status": status, "recorded_at": _now()}
    if exit_code is not None:
        record["exit_code"] = int(exit_code)
    if detail:
        record["detail"] = str(detail)
    if evidence_file is not None:
        record["evidence_file"] = str(Path(evidence_file).resolve())
    phases[phase] = record
    _atomic_json(path, result)


def fail_teardown(
    endpoint_path: str | Path,
    result_path: str | Path,
    *,
    phase: str,
    exit_code: int,
    reason: str,
) -> None:
    """Fence a failed teardown so no experiment can attach until an explicit retry."""

    result_path = Path(result_path).resolve()
    result = _read_object(result_path, "teardown result")
    result["status"] = _FAILED_STATUS
    result["failed_at"] = _now()
    result["failure"] = {
        "phase": str(phase),
        "exit_code": int(exit_code),
        "reason": str(reason),
    }
    _atomic_json(result_path, result)

    endpoint_path = Path(endpoint_path).resolve()
    endpoint = _read_object(endpoint_path, "accepted deployment endpoint")
    if endpoint.get("status") != _STOPPING_STATUS:
        raise TeardownError("accepted deployment endpoint is not fenced as stopping")
    if endpoint.get("deployment_hash") != result.get("deployment_hash"):
        raise TeardownError("accepted deployment endpoint changed during teardown")
    endpoint["status"] = _FAILED_STATUS
    endpoint["teardown_result_file"] = str(result_path)
    endpoint["teardown_failed_at"] = _now()
    endpoint["teardown_failure"] = result["failure"]
    _atomic_json(endpoint_path, endpoint)


def complete_teardown(endpoint_path: str | Path, result_path: str | Path) -> None:
    """Publish torn-down only after every teardown phase has retained success evidence."""

    result_path = Path(result_path).resolve()
    result = _read_object(result_path, "teardown result")
    if result.get("status") != _STOPPING_STATUS:
        raise TeardownError("teardown result is not in stopping state")
    phases = result.get("phases")
    if not isinstance(phases, dict):
        raise TeardownError("teardown result phase evidence is malformed")
    incomplete = [
        phase
        for phase in _REQUIRED_PHASES
        if not isinstance(phases.get(phase), dict)
        or phases[phase].get("status") not in _SUCCESS_PHASE_STATES
    ]
    if incomplete:
        raise TeardownError(
            "cannot publish torn-down before successful phase evidence: " + ", ".join(incomplete)
        )

    result["status"] = _TORN_DOWN_STATUS
    result["completed_at"] = _now()
    _atomic_json(result_path, result)

    endpoint_path = Path(endpoint_path).resolve()
    endpoint = _read_object(endpoint_path, "accepted deployment endpoint")
    if endpoint.get("status") != _STOPPING_STATUS:
        raise TeardownError("accepted deployment endpoint is not fenced as stopping")
    if endpoint.get("deployment_hash") != result.get("deployment_hash"):
        raise TeardownError("accepted deployment endpoint changed during teardown")
    endpoint["status"] = _TORN_DOWN_STATUS
    endpoint["teardown_result_file"] = str(result_path)
    endpoint["torn_down_at"] = _now()
    endpoint.pop("teardown_failure", None)
    endpoint.pop("teardown_failed_at", None)
    _atomic_json(endpoint_path, endpoint)
