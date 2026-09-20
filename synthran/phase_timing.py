"""Run-scoped deployment phase timing evidence.

The timing artifact is intentionally small and controller-owned. Wall-clock UTC
timestamps make records human-readable while monotonic nanoseconds provide
elapsed durations that are not distorted by wall-clock corrections.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import time
from pathlib import Path
from typing import Any, Callable

SCHEMA = "synthran/phase-timings/v1"
ARTIFACT = "phase-timings.json"
_LOCK = ".phase-timings.lock"
_FINAL_STATUSES = {"success", "failed", "incomplete", "skipped"}


class PhaseTimingError(ValueError):
    pass


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def capture_start() -> dict[str, Any]:
    return {
        "started_at": _utc_now(),
        "started_monotonic_ns": time.monotonic_ns(),
    }


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"schema": SCHEMA, "records": []}
    except (OSError, json.JSONDecodeError) as exc:
        raise PhaseTimingError(f"phase timing artifact is unreadable: {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise PhaseTimingError(f"phase timing artifact schema is unsupported: {path}")
    records = value.get("records")
    if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
        raise PhaseTimingError(f"phase timing records are malformed: {path}")
    return value


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _mutate(run_dir: str | Path, action: Callable[[dict[str, Any]], Any]) -> Any:
    root = Path(run_dir)
    root.mkdir(parents=True, exist_ok=True)
    artifact = root / ARTIFACT
    lock_path = root / _LOCK
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        value = _read(artifact)
        result = action(value)
        value["updated_at"] = _utc_now()
        _write(artifact, value)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return result


def _key(record: dict[str, Any]) -> tuple[str, str]:
    return str(record.get("phase", "")), str(record.get("scope", ""))


def start(
    run_dir: str | Path,
    phase: str,
    *,
    scope: str = "",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    phase = str(phase).strip()
    scope = str(scope).strip()
    if not phase:
        raise PhaseTimingError("phase name cannot be empty")
    marker = capture_start()

    def action(value: dict[str, Any]) -> dict[str, Any]:
        target = (phase, scope)
        if any(
            _key(record) == target and record.get("status") == "running"
            for record in value["records"]
        ):
            raise PhaseTimingError(f"phase already running: {phase} scope={scope!r}")
        record: dict[str, Any] = {
            "phase": phase,
            "scope": scope,
            "status": "running",
            **marker,
        }
        if details:
            record["details"] = dict(details)
        value["records"].append(record)
        return dict(record)

    return _mutate(run_dir, action)


def finish(
    run_dir: str | Path,
    phase: str,
    *,
    scope: str = "",
    status: str = "success",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    phase = str(phase).strip()
    scope = str(scope).strip()
    if status not in _FINAL_STATUSES:
        raise PhaseTimingError(f"unsupported final timing status: {status}")
    end_ns = time.monotonic_ns()
    ended_at = _utc_now()

    def action(value: dict[str, Any]) -> dict[str, Any]:
        target = (phase, scope)
        for record in reversed(value["records"]):
            if _key(record) != target or record.get("status") != "running":
                continue
            started_ns = record.get("started_monotonic_ns")
            if not isinstance(started_ns, int):
                raise PhaseTimingError(f"phase has no monotonic start: {phase} scope={scope!r}")
            record["status"] = status
            record["completed_at"] = ended_at
            record["elapsed_seconds"] = round(max(0, end_ns - started_ns) / 1_000_000_000, 6)
            record.pop("started_monotonic_ns", None)
            if details:
                merged = dict(record.get("details") or {})
                merged.update(details)
                record["details"] = merged
            return dict(record)
        raise PhaseTimingError(f"no running phase: {phase} scope={scope!r}")

    return _mutate(run_dir, action)


def record_interval(
    run_dir: str | Path,
    phase: str,
    *,
    started: dict[str, Any],
    scope: str = "",
    status: str = "success",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if status not in _FINAL_STATUSES:
        raise PhaseTimingError(f"unsupported final timing status: {status}")
    started_at = started.get("started_at")
    started_ns = started.get("started_monotonic_ns")
    if not isinstance(started_at, str) or not isinstance(started_ns, int):
        raise PhaseTimingError("interval start marker is malformed")
    end_ns = time.monotonic_ns()
    record: dict[str, Any] = {
        "phase": str(phase).strip(),
        "scope": str(scope).strip(),
        "status": status,
        "started_at": started_at,
        "completed_at": _utc_now(),
        "elapsed_seconds": round(max(0, end_ns - started_ns) / 1_000_000_000, 6),
    }
    if not record["phase"]:
        raise PhaseTimingError("phase name cannot be empty")
    if details:
        record["details"] = dict(details)

    def action(value: dict[str, Any]) -> dict[str, Any]:
        value["records"].append(record)
        return dict(record)

    return _mutate(run_dir, action)


def close_open(
    run_dir: str | Path,
    *,
    status: str,
    reason: str = "",
) -> list[dict[str, Any]]:
    if status not in {"failed", "incomplete"}:
        raise PhaseTimingError("close-open status must be failed or incomplete")
    end_ns = time.monotonic_ns()
    ended_at = _utc_now()

    def action(value: dict[str, Any]) -> list[dict[str, Any]]:
        closed: list[dict[str, Any]] = []
        for record in value["records"]:
            if record.get("status") != "running":
                continue
            started_ns = record.get("started_monotonic_ns")
            if not isinstance(started_ns, int):
                continue
            record["status"] = status
            record["completed_at"] = ended_at
            record["elapsed_seconds"] = round(max(0, end_ns - started_ns) / 1_000_000_000, 6)
            record.pop("started_monotonic_ns", None)
            if reason:
                details = dict(record.get("details") or {})
                details["closure_reason"] = reason
                record["details"] = details
            closed.append(dict(record))
        return closed

    return _mutate(run_dir, action)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m synthran.phase_timing")
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("start", "finish"):
        command = sub.add_parser(name)
        command.add_argument("--run-dir", required=True)
        command.add_argument("--phase", required=True)
        command.add_argument("--scope", default="")
        if name == "finish":
            command.add_argument(
                "--status",
                choices=sorted(_FINAL_STATUSES),
                default="success",
            )

    close = sub.add_parser("close-open")
    close.add_argument("--run-dir", required=True)
    close.add_argument("--status", choices=["failed", "incomplete"], required=True)
    close.add_argument("--reason", default="")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        if args.command == "start":
            start(args.run_dir, args.phase, scope=args.scope)
        elif args.command == "finish":
            finish(
                args.run_dir,
                args.phase,
                scope=args.scope,
                status=args.status,
            )
        else:
            close_open(args.run_dir, status=args.status, reason=args.reason)
    except PhaseTimingError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
