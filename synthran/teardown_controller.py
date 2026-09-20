"""Public selected-resource teardown controller for one accepted SynthRAN testbed."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import IO, Any

from .reservation_release import (
    ReservationReleaseError,
    release_pos_calendar,
    release_r2lab_lease,
)
from .teardown import (
    TeardownError,
    begin_teardown,
    complete_teardown,
    fail_teardown,
    phase_completed,
    record_phase,
)

ROOT = Path(__file__).resolve().parents[1]
ACTIVE_ENDPOINT = ROOT / ".synthran/active-deployment.json"


class TeardownControllerError(RuntimeError):
    """The teardown controller could not safely continue."""


def _section(title: str) -> None:
    print()
    print(title)
    print("-" * len(title))


def _acquire_lock(path: Path, label: str) -> IO[str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        stream.close()
        raise TeardownControllerError(label) from exc
    stream.seek(0)
    stream.truncate()
    stream.write(f"{os.getpid()}\n")
    stream.flush()
    return stream


def _ansible_playbook() -> str:
    local = ROOT / ".venv/bin/ansible-playbook"
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    resolved = shutil.which("ansible-playbook")
    if resolved:
        return resolved
    raise TeardownControllerError(
        "ansible-playbook is unavailable; run teardown.sh so the deployment runtime is prepared"
    )


def _run_ansible_phase(
    context: dict[str, Any],
    *,
    phase: str,
    tags: str,
    extra_vars: dict[str, Any],
    verbose: bool,
) -> tuple[int, Path]:
    log_path = Path(context["result_dir"]) / f"teardown-{phase}.log"
    command = [
        _ansible_playbook(),
        "-i",
        context["inventory_file"],
        "-e",
        "@" + context["global_vars_file"],
        "-e",
        "@" + context["deployment_vars_file"],
    ]
    for key, value in extra_vars.items():
        command += ["-e", f"{key}={json.dumps(value) if isinstance(value, bool) else value}"]
    command += [context["teardown_playbook"], "--tags", tags]
    if verbose:
        command.append("--verbose")

    env = os.environ.copy()
    env.update(
        {
            "ANSIBLE_CONFIG": context["ansible_config"],
            "ANSIBLE_ROLES_PATH": context["ansible_roles_path"],
            "ANSIBLE_FORCE_COLOR": "0",
            "PYTHONUNBUFFERED": "1",
        }
    )
    with log_path.open("w", encoding="utf-8") as log:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            check=False,
        )
    return result.returncode, log_path


def _record_failure(
    context: dict[str, Any],
    *,
    phase: str,
    exit_code: int,
    reason: str,
    evidence_file: str | Path | None = None,
) -> int:
    result_path = context["result_file"]
    try:
        if not phase_completed(result_path, phase):
            record_phase(
                result_path,
                phase,
                status="failed",
                exit_code=exit_code,
                detail=reason,
                evidence_file=evidence_file,
            )
    finally:
        fail_teardown(
            context["endpoint_file"],
            result_path,
            phase=phase,
            exit_code=exit_code,
            reason=reason,
        )
    return exit_code


def _run_resources(context: dict[str, Any], *, verbose: bool) -> int:
    result_path = context["result_file"]
    if phase_completed(result_path, "resources"):
        print("Resources: retained success from an earlier teardown attempt")
        return 0
    deployment = context.get("deployment")
    if not isinstance(deployment, dict):
        raise TeardownControllerError("accepted deployment mapping is missing")
    if deployment.get("platform") != "r2lab":
        record_phase(
            result_path,
            "resources",
            status="skipped",
            detail="selected radio/UE teardown is only required for platform=r2lab",
        )
        print("Resources: skipped (accepted platform is not R2Lab)")
        return 0

    rc, log = _run_ansible_phase(
        context,
        phase="resources",
        tags="resources",
        extra_vars={"synthran_delete_namespace": False},
        verbose=verbose,
    )
    if rc:
        print(f"Resources: FAILED (status {rc}); evidence: {log}", file=sys.stderr)
        return _record_failure(
            context,
            phase="resources",
            exit_code=rc,
            reason="selected R2Lab UE/RRU teardown failed",
            evidence_file=log,
        )
    record_phase(
        result_path,
        "resources",
        status="succeeded",
        exit_code=0,
        detail="selected accepted-deployment UE/RRU resources stopped",
        evidence_file=log,
    )
    print(f"Resources: selected UE/RRU teardown succeeded; evidence: {log}")
    return 0


def _run_namespace(
    context: dict[str, Any],
    *,
    delete_namespace: bool,
    verbose: bool,
) -> int:
    result_path = context["result_file"]
    if phase_completed(result_path, "namespace"):
        print("Namespace: retained success from an earlier teardown attempt")
        return 0
    if not delete_namespace:
        record_phase(
            result_path,
            "namespace",
            status="skipped",
            detail="namespace deletion was not explicitly requested",
        )
        print("Namespace: preserved (use --delete-namespace to delete the accepted namespace)")
        return 0

    rc, log = _run_ansible_phase(
        context,
        phase="namespace",
        tags="namespace",
        extra_vars={"synthran_delete_namespace": True},
        verbose=verbose,
    )
    if rc:
        print(f"Namespace: FAILED (status {rc}); evidence: {log}", file=sys.stderr)
        return _record_failure(
            context,
            phase="namespace",
            exit_code=rc,
            reason="explicit accepted namespace deletion failed",
            evidence_file=log,
        )
    record_phase(
        result_path,
        "namespace",
        status="succeeded",
        exit_code=0,
        detail="explicit accepted deployment namespace deletion completed",
        evidence_file=log,
    )
    print(f"Namespace: explicit deletion completed; evidence: {log}")
    return 0


def _release_pos(context: dict[str, Any], *, keep_reservations: bool) -> int:
    result_path = context["result_file"]
    if phase_completed(result_path, "pos_calendar"):
        print("POS calendar: retained success from an earlier teardown attempt")
        return 0
    if keep_reservations:
        record_phase(
            result_path,
            "pos_calendar",
            status="preserved",
            detail="operator requested --keep-reservations",
        )
        print("POS calendar: preserved by operator request")
        return 0

    authority = Path(context["result_dir"]) / "reservation-authority.json"
    try:
        outcome = release_pos_calendar(authority)
    except ReservationReleaseError as exc:
        print(f"POS calendar: FAILED: {exc}", file=sys.stderr)
        return _record_failure(
            context,
            phase="pos_calendar",
            exit_code=1,
            reason=str(exc),
            evidence_file=authority if authority.exists() else None,
        )
    status = str(outcome.get("status"))
    record_phase(
        result_path,
        "pos_calendar",
        status=status,
        detail=json.dumps(outcome, sort_keys=True),
        evidence_file=authority if authority.exists() else None,
    )
    print(f"POS calendar: {status}")
    return 0


def _release_r2lab(context: dict[str, Any], *, keep_reservations: bool) -> int:
    result_path = context["result_file"]
    if phase_completed(result_path, "r2lab"):
        print("R2Lab lease: retained success from an earlier teardown attempt")
        return 0
    if keep_reservations:
        record_phase(
            result_path,
            "r2lab",
            status="preserved",
            detail="operator requested --keep-reservations",
        )
        print("R2Lab lease: preserved by operator request")
        return 0

    evidence = Path(context["result_dir"]) / "r2lab-lease.json"
    try:
        outcome = release_r2lab_lease(
            context["result_dir"],
            context["private_execution_dir"],
        )
    except ReservationReleaseError as exc:
        print(f"R2Lab lease: FAILED: {exc}", file=sys.stderr)
        return _record_failure(
            context,
            phase="r2lab",
            exit_code=1,
            reason=str(exc),
            evidence_file=evidence if evidence.exists() else None,
        )
    status = str(outcome.get("status"))
    record_phase(
        result_path,
        "r2lab",
        status=status,
        detail=json.dumps(outcome, sort_keys=True),
        evidence_file=evidence if evidence.exists() else None,
    )
    print(f"R2Lab lease: {status}")
    return 0


def execute(
    *,
    endpoint: Path,
    delete_namespace: bool,
    keep_reservations: bool,
    verbose: bool,
) -> int:
    deploy_lock: IO[str] | None = None
    experiment_lock: IO[str] | None = None
    context: dict[str, Any] | None = None
    try:
        deploy_lock = _acquire_lock(
            ROOT / ".synthran/deploy.lock",
            "another SynthRAN deployment or teardown controller is running",
        )
        experiment_lock = _acquire_lock(
            ROOT / ".synthran/experiment.lock",
            "a SynthRAN experiment controller is running; stop/finish it before testbed teardown",
        )

        _section("Resolving accepted testbed teardown authority")
        context = begin_teardown(endpoint)
        print(f"Accepted run: {context['run_id']}")
        print(f"Deployment hash: {context['deployment_hash']}")
        print(f"Retained execution context: {context['private_execution_dir']}")
        print("Endpoint state: stopping (new experiment attachment is fenced)")

        _section("Stopping selected testbed resources")
        rc = _run_resources(context, verbose=verbose)
        if rc:
            return rc

        _section("Handling deployment namespace")
        rc = _run_namespace(
            context,
            delete_namespace=delete_namespace,
            verbose=verbose,
        )
        if rc:
            return rc

        _section("Releasing owned reservations")
        rc = _release_pos(context, keep_reservations=keep_reservations)
        if rc:
            return rc
        rc = _release_r2lab(context, keep_reservations=keep_reservations)
        if rc:
            return rc

        complete_teardown(context["endpoint_file"], context["result_file"])
        _section("Teardown complete")
        print("Endpoint state: torn-down")
        print(f"Evidence: {context['result_file']}")
        return 0
    except (TeardownError, TeardownControllerError, OSError) as exc:
        if context is not None:
            try:
                return _record_failure(
                    context,
                    phase="controller",
                    exit_code=1,
                    reason=str(exc),
                )
            except Exception as evidence_error:  # preserve the primary controller error
                print(
                    f"Teardown controller failed: {exc}; additionally could not retain failure state: {evidence_error}",
                    file=sys.stderr,
                )
                return 1
        print(f"Teardown refused: {exc}", file=sys.stderr)
        return 1
    finally:
        # Closing the streams releases both non-blocking flock locks.
        if experiment_lock is not None:
            experiment_lock.close()
        if deploy_lock is not None:
            deploy_lock.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="./teardown.sh",
        description=(
            "Stop only resources owned by the currently accepted SynthRAN testbed. "
            "The accepted namespace is preserved unless explicitly requested, and "
            "only reservations proven created/booked by that accepted run are released."
        ),
    )
    parser.add_argument(
        "--delete-namespace",
        action="store_true",
        help="delete the exact non-system namespace recorded by the accepted deployment",
    )
    parser.add_argument(
        "--keep-reservations",
        action="store_true",
        help="preserve even reservations whose run evidence proves SynthRAN creation ownership",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "--endpoint",
        type=Path,
        default=ACTIVE_ENDPOINT,
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return execute(
        endpoint=args.endpoint.resolve(),
        delete_namespace=args.delete_namespace,
        keep_reservations=args.keep_reservations,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    raise SystemExit(main())
