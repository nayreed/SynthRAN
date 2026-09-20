from __future__ import annotations

import datetime as dt
import json
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
import os
import re
import secrets
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from . import phase_timing
from .host_preparation import BOOTSTRAP, validate_preparation_mode


STATE_PATH = Path(".synthran/pos-reservation.json")
PROVIDER_PREFIX_ATTEMPTS_AFTER_CREATE = 12
PROVIDER_PREFIX_ATTEMPTS_EXISTING = 3
PROVIDER_PREFIX_INTERVAL_SECONDS = 5.0
POS_READY_ATTEMPTS = 60
POS_READY_INTERVAL_SECONDS = 5.0

ACQUISITION_MODES = {"create", "require-existing", "disabled"}
PROVIDER_MODES = {"create", "require-existing", "disabled"}
R2LAB_MODES = {"book", "require-existing", "disabled"}
_SAFE_CONTEXT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

# Pinned from sopnode/5g_ansible@b73fccf87f55060484b3759e9cb347222253534b
# roles/pos/defaults/main.yml. SynthRAN deliberately keeps its configured image,
# but adopts the reference boot-parameter mechanics for the supported SOP/N3xx path.
REFERENCE_BOOT_PARAMETERS = (
    "biosdevname=0 net.ifnames=0 mitigations=off intel_iommu=on iommu=pt "
    "selinux=0 enforcing=0 nosoftlockup intel_pstate=disable idle=poll nosmt"
)
REFERENCE_BOOT_RAN_PARAMETERS = (
    "biosdevname=0 net.ifnames=0 mitigations=off intel_iommu=on iommu=pt "
    "selinux=0 enforcing=0 isolcpus=managed_irq,16-63 "
    "nohz_full=16-63 nohz=on rcu_nocbs=16-63 "
    "kthread_cpus=0-4 irqaffinity=0-4 rcu_nocb_poll "
    "nosoftlockup intel_pstate=disable idle=poll skew_tick=1 "
    "tsc=nowatchdog nmi_watchdog=0 softlockup_panic=0 audit=0 nosmt"
)


class ReservationError(RuntimeError):
    pass


class FreshNodePreparationError(ReservationError):
    def __init__(
        self,
        node: str,
        phase: str,
        record: Mapping[str, Any],
        cause: Exception,
    ) -> None:
        self.node = node
        self.phase = phase
        self.record = dict(record)
        self.cause = cause
        super().__init__(f"{node} fresh preparation failed during {phase}: {cause}")


class FreshNodePreparationCancelled(ReservationError):
    def __init__(self, node: str, record: Mapping[str, Any]) -> None:
        self.node = node
        self.record = dict(record)
        super().__init__(f"{node} fresh preparation stopped after peer failure")


class FreshPreparationError(ReservationError):
    def __init__(
        self,
        nodes: Mapping[str, Mapping[str, Any]],
        failures: Mapping[str, str],
    ) -> None:
        self.nodes = {name: dict(record) for name, record in nodes.items()}
        self.failures = dict(failures)
        detail = "; ".join(f"{node}: {message}" for node, message in self.failures.items())
        super().__init__("fresh host preparation failed: " + detail)


def _output(result: subprocess.CompletedProcess[str]) -> str:
    return "\n".join(
        part.strip() for part in (result.stdout or "", result.stderr or "") if part.strip()
    )


def run(
    argv: Sequence[str],
    *,
    check: bool = True,
    stdin: str | None = None,
    echo: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a reservation command while keeping machine responses quiet by default."""

    options: dict[str, Any] = {
        "text": True,
        "capture_output": True,
        "check": False,
    }
    if stdin is None:
        options["stdin"] = subprocess.DEVNULL
    else:
        options["input"] = stdin
    result = subprocess.run(list(argv), **options)
    if echo and result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n", flush=True)
    if echo and result.stderr:
        print(result.stderr, end="" if result.stderr.endswith("\n") else "\n", flush=True)
    if check and result.returncode:
        detail = _output(result) or f"exit status {result.returncode}"
        raise ReservationError(f"command failed: {' '.join(argv)}\n{detail}")
    return result


def run_visible(
    argv: Sequence[str],
    *,
    check: bool = True,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run an operator-facing mutation without changing the historical run() call contract."""

    result = run(argv, check=check, stdin=stdin)
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n", flush=True)
    if result.stderr:
        print(result.stderr, end="" if result.stderr.endswith("\n") else "\n", flush=True)
    return result


def stamp(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.datetime.now().astimezone().tzinfo)
    return parsed


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _safe_mapping(value: Any, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ReservationError(f"{label} must be a mapping")
    return dict(value)


def _selected_nodes(deployment: Mapping[str, Any]) -> tuple[dict[str, str], list[str]]:
    roles = _safe_mapping(deployment.get("nodes"), "deployment.nodes")
    required = ("core", "ran", "broker")
    missing = [role for role in required if not isinstance(roles.get(role), str) or not roles[role].strip()]
    if missing:
        raise ReservationError("deployment.nodes is missing: " + ", ".join(missing))
    selected = list(dict.fromkeys(str(roles[role]) for role in required))
    return {role: str(roles[role]) for role in required}, selected


def _owner() -> str:
    value = os.environ.get("USER", "").strip()
    if value:
        return value
    result = run(["id", "-un"])
    value = result.stdout.strip()
    if not value:
        raise ReservationError("could not determine POS reservation owner")
    return value


def _provider_network(experiment: str, *, attempts: int) -> dict[str, str]:
    if attempts < 1:
        raise ReservationError("provider prefix attempts must be positive")
    interval = float(
        os.environ.get(
            "SYNTHRAN_PROVIDER_PREFIX_INTERVAL_SECONDS",
            str(PROVIDER_PREFIX_INTERVAL_SECONDS),
        )
    )
    last_detail = "no response"
    for attempt in range(1, attempts + 1):
        result = run(["post5g", "experiment", "prefix", experiment], check=False)
        text = result.stdout.strip()
        if result.returncode == 0 and text:
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                last_detail = "provider response was not JSON"
            else:
                if isinstance(value, dict):
                    missing = [
                        key
                        for key in ("subnet", "lb", "expiration_time")
                        if not isinstance(value.get(key), str) or not value[key].strip()
                    ]
                    if not missing:
                        return {key: str(value[key]) for key in ("subnet", "lb", "expiration_time")}
                    last_detail = "provider response was missing " + ", ".join(missing)
                else:
                    last_detail = "provider response was not one JSON object"
        else:
            last_detail = _output(result) or f"provider command exit={result.returncode}"
        if attempt < attempts and interval > 0:
            time.sleep(interval)
    raise ReservationError(
        f"Post5G provider network acquisition failed after {attempts} attempt(s): {last_detail}"
    )


def provider_context(provider: Mapping[str, Any]) -> dict[str, Any]:
    mode = str(provider.get("mode", "disabled"))
    if mode not in PROVIDER_MODES:
        raise ReservationError(
            "deployment.provider.mode must be create, require-existing, or disabled"
        )
    if mode == "disabled":
        return {"mode": "disabled", "managed_by": "synthran"}

    project = str(provider.get("project", ""))
    experiment = str(provider.get("experiment", ""))
    if _SAFE_CONTEXT.fullmatch(project) is None:
        raise ReservationError("deployment.provider.project is required and contains unsafe characters")
    if _SAFE_CONTEXT.fullmatch(experiment) is None:
        raise ReservationError("deployment.provider.experiment is required and contains unsafe characters")
    duration = str(provider.get("experiment_duration", "4h"))
    if re.fullmatch(r"[1-9][0-9]*(?:m|h)", duration) is None:
        raise ReservationError("deployment.provider.experiment_duration must look like 30m or 4h")

    selected = run(["slices", "project", "use", project], check=False)
    if selected.returncode:
        raise ReservationError(
            "SLICES project selection failed: " + (_output(selected) or f"project={project}")
        )

    shown = run(["slices", "experiment", "show", experiment], check=False)
    created = False
    if shown.returncode:
        if mode == "require-existing":
            raise ReservationError(
                "SLICES experiment is required to exist: "
                + (_output(shown) or f"experiment={experiment}")
            )
        result = run_visible(
            ["slices", "experiment", "create", experiment, "--duration", duration],
            check=False,
        )
        if result.returncode:
            raise ReservationError(
                "SLICES experiment creation failed: "
                + (_output(result) or f"experiment={experiment}")
            )
        created = True

    attempts = (
        PROVIDER_PREFIX_ATTEMPTS_AFTER_CREATE if created else PROVIDER_PREFIX_ATTEMPTS_EXISTING
    )
    network = _provider_network(experiment, attempts=attempts)
    return {
        "mode": mode,
        "managed_by": "synthran",
        "project": project,
        "experiment": experiment,
        "experiment_created": created,
        "network": network,
    }


def _calendars() -> list[dict[str, Any]]:
    result = run(["pos", "calendar", "list", "--json"])
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ReservationError("POS calendar list did not return JSON") from exc
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ReservationError("POS calendar list returned an unexpected shape")
    return value


def _active_exact_events(
    events: Sequence[Mapping[str, Any]],
    *,
    owner: str,
    selected: Sequence[str],
    now: dt.datetime,
) -> list[dict[str, Any]]:
    wanted = set(selected)
    matches: list[dict[str, Any]] = []
    for raw in events:
        nodes = raw.get("nodes")
        if not isinstance(nodes, list) or set(map(str, nodes)) != wanted:
            continue
        if str(raw.get("owner", "")) != owner:
            continue
        try:
            start = stamp(str(raw["start_date"]))
            stop = stamp(str(raw["end_date"]))
        except (KeyError, TypeError, ValueError):
            continue
        if start <= now < stop:
            matches.append(dict(raw))
    return matches


def _covering_exact_events(
    events: Sequence[Mapping[str, Any]],
    *,
    owner: str,
    selected: Sequence[str],
    now: dt.datetime,
    end: dt.datetime,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for raw in _active_exact_events(
        events, owner=owner, selected=selected, now=now
    ):
        try:
            stop = stamp(str(raw["end_date"]))
        except (KeyError, TypeError, ValueError):
            continue
        if stop >= end:
            matches.append(dict(raw))
    return matches


def _booked_duration(event: Mapping[str, Any]) -> dt.timedelta:
    try:
        start = stamp(str(event["start_date"]))
        stop = stamp(str(event["end_date"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ReservationError("POS calendar event has invalid start/end timestamps") from exc
    return stop - start


def _calendar_record(event: Mapping[str, Any], *, status: str) -> dict[str, Any]:
    return {
        "status": status,
        "id": str(event.get("id", "")),
        "owner": str(event.get("owner", "")),
        "nodes": [str(value) for value in event.get("nodes", [])],
        "start": stamp(str(event["start_date"])).isoformat(),
        "end": stamp(str(event["end_date"])).isoformat(),
        "managed_by": "synthran",
    }


def acquire_calendar(
    reservation: Mapping[str, Any],
    *,
    selected: Sequence[str],
    owner: str,
    now: dt.datetime,
) -> dict[str, Any]:
    mode = str(reservation.get("mode", ""))
    if mode not in ACQUISITION_MODES:
        raise ReservationError(
            "deployment.reservation.mode must be create, require-existing, or disabled"
        )
    duration = reservation.get("duration_minutes", 120)
    if not isinstance(duration, int) or duration < 1:
        raise ReservationError("deployment.reservation.duration_minutes must be a positive integer")
    if mode == "disabled":
        return {
            "status": "disabled",
            "nodes": list(selected),
            "managed_by": "synthran",
        }

    required_end = now + dt.timedelta(minutes=duration)
    events = _calendars()

    if mode == "require-existing":
        covering = _covering_exact_events(
            events, owner=owner, selected=selected, now=now, end=required_end
        )
        if len(covering) > 1:
            raise ReservationError(
                "multiple owned POS calendar events exactly cover the selected nodes; refusing ambiguous authority"
            )
        if covering:
            return _calendar_record(covering[0], status="required-existing")
        raise ReservationError(
            "no owned active POS calendar event exactly covers the selected nodes for the requested duration"
        )

    active = _active_exact_events(
        events, owner=owner, selected=selected, now=now
    )
    if len(active) > 1:
        raise ReservationError(
            "multiple owned active POS calendar events exactly cover the selected nodes; refusing ambiguous authority"
        )
    if active:
        booked = _booked_duration(active[0])
        requested = dt.timedelta(minutes=duration)
        if booked < requested:
            booked_minutes = max(0, int(booked.total_seconds() // 60))
            raise ReservationError(
                "owned active POS calendar event exactly covers the selected nodes but was booked "
                f"for {booked_minutes} minute(s), shorter than requested {duration}; "
                "refusing an overlapping calendar create"
            )
        record = _calendar_record(active[0], status="reused")
        print(
            f"Reusing active SOP reservation {record['id']} through {record['end']}",
            flush=True,
        )
        return record

    result = run(
        [
            "pos",
            "calendar",
            "create",
            "--start",
            "now",
            "--duration",
            str(duration),
            *selected,
        ],
        check=False,
    )
    reservation_id = result.stdout.strip()
    if result.returncode or not reservation_id or reservation_id == "-1":
        raise ReservationError(
            "POS calendar creation failed for the exact selected nodes: "
            + (_output(result) or f"exit status {result.returncode}")
        )

    matches = [
        event
        for event in _active_exact_events(
            _calendars(), owner=owner, selected=selected, now=now
        )
        if str(event.get("id")) == reservation_id
        and _booked_duration(event) >= dt.timedelta(minutes=duration)
    ]
    if len(matches) != 1:
        raise ReservationError(
            "POS provider evidence did not prove the newly created reservation exactly covers the selected nodes for the requested booked duration"
        )
    print(
        f"Created SOP reservation {reservation_id} for {', '.join(selected)}",
        flush=True,
    )
    return _calendar_record(matches[0], status="created")


def _allocation_state(node: str, result: subprocess.CompletedProcess[str]) -> str:
    text = _output(result)
    lower = text.lower()
    if "a command for allocation" in lower:
        raise ReservationError(
            f"POS is still processing an allocation command for {node}: {text}"
        )
    if "already allocated" in lower:
        return "already-active"
    if result.returncode == 0:
        return "new"
    raise ReservationError(f"POS allocation failed for {node}: {text or result.returncode}")


def _allocation_command(node: str, result_folder: str) -> list[str]:
    return [
        "pos",
        "allocations",
        "allocate",
        "--result-folder",
        result_folder,
        node,
    ]


def _live_allocation_record(node: str) -> dict[str, str] | None:
    """Return provider-backed allocation identity when POS exposes it.

    SLICES documents pos allocations show <node> for resolving the live
    allocation id and pos allocations show <id> for the result folder.
    This metadata is an optimization proof only: inability to read it never
    weakens the normal reclaim/reacquire safety path.
    """

    shown = run(["pos", "allocations", "show", node], check=False)
    if shown.returncode or not shown.stdout.strip():
        return None
    try:
        node_record = json.loads(shown.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(node_record, dict):
        return None
    allocation_id = str(node_record.get("id", "")).strip()
    if not allocation_id:
        return None

    detail = run(["pos", "allocations", "show", allocation_id], check=False)
    if detail.returncode or not detail.stdout.strip():
        return None
    try:
        allocation_record = json.loads(detail.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(allocation_record, dict):
        return None
    result_folder = str(allocation_record.get("result_folder", "")).strip()
    if not result_folder:
        return None
    return {
        "id": allocation_id,
        "result_folder": result_folder,
    }


def _managed_allocation_matches(
    node: str,
    *,
    expected: Mapping[str, Any] | None,
    result_folder: str,
) -> dict[str, str] | None:
    if not isinstance(expected, Mapping):
        return None
    expected_id = str(expected.get("id", "")).strip()
    expected_folder = str(expected.get("result_folder", "")).strip()
    if not expected_id or expected_folder != result_folder:
        return None
    live = _live_allocation_record(node)
    if live is None:
        return None
    if live["id"] != expected_id or live["result_folder"] != expected_folder:
        return None
    return live


def _probe_allocation_for_fresh(node: str, *, result_folder: str) -> str:
    print(f"[POS allocation] Probing {node}", flush=True)
    # POS logs a provider-level ERROR before returning its expected
    # "already allocated" state. Capture the probe so that known existing
    # allocation state is classified here instead of emitted as a false-red
    # operator error. Real failures are still surfaced by _allocation_state().
    result = run(_allocation_command(node, result_folder), check=False)
    state = _allocation_state(node, result)
    if state == "new":
        print(f"[POS allocation] {node}: fresh allocation acquired", flush=True)
    else:
        print(
            f"[POS allocation] {node}: existing allocation detected; ownership "
            "will be checked against retained provider-backed SynthRAN evidence",
            flush=True,
        )
    return state


def _reclaim_allocation_for_fresh(node: str, *, result_folder: str) -> str:
    print(f"[POS allocation] {node}: reclaiming unproven existing allocation", flush=True)
    released = run_visible(["pos", "allocations", "free", "-k", node], check=False)
    print(f"[POS allocation] {node}: requesting fresh allocation after reclaim", flush=True)
    retry = run_visible(_allocation_command(node, result_folder), check=False)
    retry_state = _allocation_state(node, retry)
    if retry_state != "new":
        detail = _output(retry) or _output(released)
        raise ReservationError(
            f"unable to prove fresh allocation ownership for {node} after explicit reclaim"
            + (f": {detail}" if detail else "")
        )
    print(f"[POS allocation] {node}: fresh allocation ownership proven", flush=True)
    return "reclaimed"


def _allocate_for_fresh(node: str, *, result_folder: str) -> str:
    """Compatibility helper for callers that prepare a single node."""
    state = _probe_allocation_for_fresh(node, result_folder=result_folder)
    if state == "new":
        return state
    return _reclaim_allocation_for_fresh(node, result_folder=result_folder)


def _boot_parameters(node: str) -> tuple[str, str]:
    if node in {"sopnode-f1", "sopnode-f2", "sopnode-f3"}:
        return "reference-n3xx", REFERENCE_BOOT_RAN_PARAMETERS
    return "reference-generic", REFERENCE_BOOT_PARAMETERS


def _wait_for_ssh(node: str) -> int:
    attempts = int(os.environ.get("SYNTHRAN_POS_READY_ATTEMPTS", str(POS_READY_ATTEMPTS)))
    interval = float(
        os.environ.get("SYNTHRAN_POS_READY_INTERVAL_SECONDS", str(POS_READY_INTERVAL_SECONDS))
    )
    if attempts < 1:
        raise ReservationError("SYNTHRAN_POS_READY_ATTEMPTS must be positive")
    print(
        f"[POS readiness] {node}: waiting for SSH after reset "
        f"(up to {attempts} probes)",
        flush=True,
    )
    last = "no response"
    for attempt in range(1, attempts + 1):
        result = run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=5",
                "-o",
                "StrictHostKeyChecking=accept-new",
                f"root@{node}",
                "true",
            ],
            check=False,
        )
        if result.returncode == 0:
            print(
                f"[POS readiness] {node}: SSH ready on probe {attempt}/{attempts}",
                flush=True,
            )
            return attempt
        last = _output(result) or f"exit status {result.returncode}"
        if attempt % 5 == 0 and attempt < attempts:
            print(
                f"[POS readiness] {node}: still waiting for SSH "
                f"({attempt}/{attempts})",
                flush=True,
            )
        if attempt < attempts and interval > 0:
            time.sleep(interval)
    raise ReservationError(f"{node} did not become SSH-ready after POS reset: {last}")


def _prepare_fresh_node(
    node: str,
    *,
    allocation: str,
    allocation_id: str | None,
    image: str,
    stop_event: threading.Event,
    timing_run_dir: Path | None,
) -> dict[str, Any]:
    boot_profile, boot_parameters = _boot_parameters(node)
    record: dict[str, Any] = {
        "allocation": allocation,
        "allocation_id": allocation_id,
        "image": image,
        "boot_profile": boot_profile,
        "status": "preparing",
        "completed_phases": [],
    }
    phase = "image-staging"

    def stop_if_peer_failed(next_phase: str) -> None:
        if stop_event.is_set():
            record["status"] = "cancelled-after-peer-failure"
            record["cancelled_before_phase"] = next_phase
            raise FreshNodePreparationCancelled(node, record)

    try:
        stop_if_peer_failed("image-staging")
        print(
            f"[POS prepare] {node}: selecting image {image}; provider staging may "
            "take several minutes",
            flush=True,
        )
        timing = phase_timing.capture_start()
        try:
            run_visible(["pos", "nodes", "image", "--staging", node, image])
        except Exception:
            if timing_run_dir is not None:
                phase_timing.record_interval(
                    timing_run_dir,
                    "pos_image_staging",
                    scope=node,
                    started=timing,
                    status="failed",
                )
            raise
        else:
            if timing_run_dir is not None:
                phase_timing.record_interval(
                    timing_run_dir,
                    "pos_image_staging",
                    scope=node,
                    started=timing,
                )
        record["completed_phases"].append("image-staging")
        print(f"[POS prepare] {node}: image staging completed", flush=True)

        phase = "boot-parameters"
        stop_if_peer_failed("boot-parameters")
        print(
            f"[POS prepare] {node}: applying boot parameters ({boot_profile})",
            flush=True,
        )
        timing = phase_timing.capture_start()
        try:
            run_visible(["pos", "nodes", "bootparameter", node, "--raw", boot_parameters])
        except Exception:
            if timing_run_dir is not None:
                phase_timing.record_interval(
                    timing_run_dir,
                    "boot_parameter_mutation",
                    scope=node,
                    started=timing,
                    status="failed",
                )
            raise
        else:
            if timing_run_dir is not None:
                phase_timing.record_interval(
                    timing_run_dir,
                    "boot_parameter_mutation",
                    scope=node,
                    started=timing,
                )
        record["completed_phases"].append("boot-parameters")
        print(f"[POS prepare] {node}: boot parameters applied", flush=True)

        phase = "reset"
        stop_if_peer_failed("reset")
        print(
            f"[POS prepare] {node}: resetting node with POS --blocking; this command "
            "returns only after POS reports reset completion",
            flush=True,
        )
        timing = phase_timing.capture_start()
        try:
            run_visible(["pos", "nodes", "reset", "--blocking", "--verbose", node])
        except Exception:
            if timing_run_dir is not None:
                phase_timing.record_interval(
                    timing_run_dir,
                    "pos_reset",
                    scope=node,
                    started=timing,
                    status="failed",
                )
            raise
        else:
            if timing_run_dir is not None:
                phase_timing.record_interval(
                    timing_run_dir,
                    "pos_reset",
                    scope=node,
                    started=timing,
                )
        record["completed_phases"].append("reset")
        record["reset"] = "blocking"
        print(f"[POS prepare] {node}: POS reset completed", flush=True)

        phase = "ssh-readiness"
        stop_if_peer_failed("ssh-readiness")
        timing = phase_timing.capture_start()
        try:
            ready_attempt = _wait_for_ssh(node)
        except Exception:
            if timing_run_dir is not None:
                phase_timing.record_interval(
                    timing_run_dir,
                    "ssh_readiness",
                    scope=node,
                    started=timing,
                    status="failed",
                )
            raise
        else:
            if timing_run_dir is not None:
                phase_timing.record_interval(
                    timing_run_dir,
                    "ssh_readiness",
                    scope=node,
                    started=timing,
                )
        record["completed_phases"].append("ssh-readiness")
        record["ssh_ready_attempt"] = ready_attempt
        record["status"] = "ready"
        return record
    except FreshNodePreparationCancelled:
        raise
    except Exception as exc:
        stop_event.set()
        record["status"] = "failed"
        record["failed_phase"] = phase
        record["failure"] = {
            "type": type(exc).__name__,
            "message": str(exc),
        }
        raise FreshNodePreparationError(node, phase, record, exc) from exc


def prepare_hosts(
    reservation: Mapping[str, Any],
    *,
    selected: Sequence[str],
    calendar: Mapping[str, Any],
    allocation_authority: dict[str, Any] | None = None,
    timing_run_dir: Path | None = None,
) -> dict[str, Any]:
    try:
        mode = validate_preparation_mode(reservation.get("host_preparation", ""))
    except ValueError as exc:
        raise ReservationError(str(exc)) from exc
    if mode == "preserve":
        print(
            "Reusing existing SOP host state; no allocation, image, boot-parameter, or reset mutation will be performed",
            flush=True,
        )
        return {
            "mode": "preserve",
            "nodes": list(selected),
            "mutations": [],
            "managed_by": "synthran",
        }
    if calendar.get("status") == "disabled" and mode in {BOOTSTRAP, "fresh"}:
        raise ReservationError(
            f"{mode} host preparation requires create or require-existing POS calendar authority"
        )
    if mode == BOOTSTRAP:
        print(
            "Retaining the current SOP allocation and OS for in-place bootstrap; "
            "image staging, allocation reclaim, and POS reset are forbidden",
            flush=True,
        )
        return {
            "mode": "bootstrap",
            "nodes": {
                node: {
                    "allocation": "retained",
                    "image": "retained",
                    "boot_profile": _boot_parameters(node)[0],
                    "reconcile": "ansible-preflight",
                }
                for node in selected
            },
            "mutations": [],
            "managed_by": "synthran",
        }

    image = reservation.get("image", "ubuntu-jammy")
    if not isinstance(image, str) or not image.strip():
        raise ReservationError("deployment.reservation.image must be a non-empty string")

    print(
        "Preparing selected SOP nodes: first proving allocation authority for every node before image/reset mutation",
        flush=True,
    )
    allocation_authority = allocation_authority or {}
    persisted_allocation_authority = _read_managed_state()
    persistent_allocation_authority = (
        bool(str(allocation_authority.get("event_id", "")).strip())
        and _same_calendar_authority(allocation_authority, calendar)
        and persisted_allocation_authority == allocation_authority
    )
    result_folder = str(
        allocation_authority.get("allocation_result_folder", "")
    ).strip()
    if not result_folder:
        # Direct/library callers that bypass execute() still receive a unique
        # provider result folder, but cannot claim cross-run reuse authority.
        result_folder = "synthran-" + secrets.token_hex(12)

    retained_allocations = allocation_authority.get("allocations")
    if not isinstance(retained_allocations, Mapping):
        retained_allocations = {}

    allocation_states: dict[str, str] = {}
    allocation_records: dict[str, dict[str, str]] = {}
    for node in selected:
        state = _probe_allocation_for_fresh(node, result_folder=result_folder)
        allocation_states[node] = state
        if state == "new" and persistent_allocation_authority:
            live = _live_allocation_record(node)
            if live is not None and live.get("result_folder") == result_folder:
                allocation_records[node] = live
                _record_managed_allocation(allocation_authority, node, live)

    for node in selected:
        if allocation_states[node] != "already-active":
            continue
        live = (
            _managed_allocation_matches(
                node,
                expected=retained_allocations.get(node),
                result_folder=result_folder,
            )
            if persistent_allocation_authority
            else None
        )
        if live is not None:
            allocation_states[node] = "managed-existing"
            allocation_records[node] = live
            print(
                f"[POS allocation] {node}: provider allocation {live['id']} matches "
                "retained SynthRAN authority; skipping release/reacquire",
                flush=True,
            )
            continue

        allocation_states[node] = _reclaim_allocation_for_fresh(
            node,
            result_folder=result_folder,
        )
        live = (
            _live_allocation_record(node)
            if persistent_allocation_authority
            else None
        )
        if live is not None and live.get("result_folder") == result_folder:
            allocation_records[node] = live
            _record_managed_allocation(allocation_authority, node, live)

    print(
        "Allocation authority proven for all selected SOP nodes; preparing independent nodes concurrently",
        flush=True,
    )

    for node in selected:
        if allocation_states[node] not in {"new", "reclaimed", "managed-existing"}:
            raise ReservationError(
                f"internal allocation state for {node} is not safe to prepare: "
                f"{allocation_states[node]}"
            )

    node_records: dict[str, dict[str, Any]] = {}
    failures: dict[str, str] = {}
    max_workers = max(1, len(selected))
    stop_event = threading.Event()
    with ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix="synthran-pos-fresh",
    ) as executor:
        futures = {
            executor.submit(
                _prepare_fresh_node,
                node,
                allocation=allocation_states[node],
                allocation_id=allocation_records.get(node, {}).get("id"),
                image=image,
                stop_event=stop_event,
                timing_run_dir=timing_run_dir,
            ): node
            for node in selected
        }
        done, pending = wait(futures, return_when=FIRST_EXCEPTION)
        if any(
            not future.cancelled() and future.exception() is not None
            for future in done
        ):
            for future in pending:
                future.cancel()

    for future, node in futures.items():
        if future.cancelled():
            node_records[node] = {
                "allocation": allocation_states[node],
                "allocation_id": allocation_records.get(node, {}).get("id"),
                "image": image,
                "boot_profile": _boot_parameters(node)[0],
                "status": "cancelled-after-peer-failure",
                "completed_phases": [],
            }
            failures[node] = "cancelled after another selected node failed"
            continue
        try:
            node_records[node] = future.result()
        except FreshNodePreparationCancelled as exc:
            node_records[node] = dict(exc.record)
        except FreshNodePreparationError as exc:
            node_records[node] = dict(exc.record)
            failures[node] = str(exc)

    ordered_records = {node: node_records[node] for node in selected}
    if failures:
        raise FreshPreparationError(ordered_records, failures)

    return {
        "mode": "fresh",
        "nodes": ordered_records,
        "parallel_preparation": len(selected) > 1,
        "managed_by": "synthran",
    }


def _read_managed_state() -> dict[str, Any]:
    try:
        value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _same_calendar_authority(
    state: Mapping[str, Any],
    calendar: Mapping[str, Any],
) -> bool:
    if not state or calendar.get("status") == "disabled":
        return False
    state_nodes = state.get("nodes")
    calendar_nodes = calendar.get("nodes")
    return (
        state.get("managed_by") == "synthran"
        and str(state.get("event_id", "")) == str(calendar.get("id", ""))
        and str(state.get("owner", "")) == str(calendar.get("owner", ""))
        and isinstance(state_nodes, list)
        and isinstance(calendar_nodes, list)
        and set(map(str, state_nodes)) == set(map(str, calendar_nodes))
    )


def _save_state(
    calendar: Mapping[str, Any],
    role_nodes: Mapping[str, str],
) -> dict[str, Any]:
    if calendar.get("status") == "disabled":
        return {}

    previous = _read_managed_state()
    if _same_calendar_authority(previous, calendar):
        result_folder = str(previous.get("allocation_result_folder", "")).strip()
        allocations = previous.get("allocations")
        if not isinstance(allocations, dict):
            allocations = {}
    else:
        result_folder = ""
        allocations = {}

    if not result_folder:
        result_folder = "synthran-" + secrets.token_hex(12)

    state: dict[str, Any] = {
        "managed_by": "synthran",
        "event_id": str(calendar.get("id", "")),
        "owner": str(calendar.get("owner", "")),
        "nodes": list(calendar.get("nodes", [])),
        "roles": dict(role_nodes),
        "start": str(calendar.get("start", "")),
        "end": str(calendar.get("end", "")),
        "allocation_result_folder": result_folder,
        "allocations": allocations,
    }
    _write_json(STATE_PATH, state)
    try:
        STATE_PATH.chmod(0o600)
    except OSError:
        pass
    return state


def _record_managed_allocation(
    authority: dict[str, Any],
    node: str,
    live: Mapping[str, Any] | None,
) -> None:
    if not authority or live is None:
        return
    allocations = authority.setdefault("allocations", {})
    if not isinstance(allocations, dict):
        allocations = {}
        authority["allocations"] = allocations
    allocations[node] = {
        "id": str(live.get("id", "")),
        "result_folder": str(live.get("result_folder", "")),
    }
    _write_json(STATE_PATH, authority)
    try:
        STATE_PATH.chmod(0o600)
    except OSError:
        pass


def execute(config_path: Path, run_dir: Path) -> dict[str, Any]:
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("deployment"), dict):
        raise ReservationError("resolved scenario requires deployment mapping")
    deployment = raw["deployment"]
    reservation = _safe_mapping(deployment.get("reservation"), "deployment.reservation")
    provider = _safe_mapping(deployment.get("provider"), "deployment.provider")
    r2lab = _safe_mapping(deployment.get("r2lab_reservation"), "deployment.r2lab_reservation")
    role_nodes, selected = _selected_nodes(deployment)

    evidence_path = run_dir / "reservation-authority.json"
    evidence: dict[str, Any] = {
        "schema": "synthran/reservation-authority/v1",
        "managed_by": "synthran",
        "status": "preparing",
        "selected_nodes": role_nodes,
        "selected_resources": selected,
        "policies": {
            "provider": str(provider.get("mode", "disabled")),
            "pos_calendar": str(reservation.get("mode", "")),
            "host_preparation": str(reservation.get("host_preparation", "")),
            "r2lab": str(r2lab.get("mode", "disabled")),
        },
        "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    _write_json(evidence_path, evidence)

    try:
        reservation_timing = phase_timing.capture_start()
        try:
            evidence["provider"] = provider_context(provider)
            _write_json(evidence_path, evidence)

            now = dt.datetime.now().astimezone()
            owner = _owner()
            calendar = acquire_calendar(
                reservation, selected=selected, owner=owner, now=now
            )
        except Exception:
            phase_timing.record_interval(
                run_dir,
                "reservation",
                scope="sop",
                started=reservation_timing,
                status="failed",
                details={"mode": str(reservation.get("mode", ""))},
            )
            raise
        else:
            phase_timing.record_interval(
                run_dir,
                "reservation",
                scope="sop",
                started=reservation_timing,
                details={"mode": str(reservation.get("mode", ""))},
            )
        evidence["pos_calendar"] = calendar
        allocation_authority = _save_state(calendar, role_nodes)
        _write_json(evidence_path, evidence)

        evidence["host_preparation"] = prepare_hosts(
            reservation,
            selected=selected,
            calendar=calendar,
            allocation_authority=allocation_authority,
            timing_run_dir=run_dir,
        )
        evidence["status"] = "ready"
        evidence["completed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        _write_json(evidence_path, evidence)

        # Compatibility/evidence surface consumed by deploy.sh when constraining
        # the R2Lab lease interval. It intentionally preserves role identities.
        pos_selection: dict[str, Any] = {
            "managed_by": "synthran",
            "nodes": role_nodes,
            "selected_resources": selected,
            "reservation_mode": reservation.get("mode"),
            "host_preparation": reservation.get("host_preparation"),
            "provider": evidence.get("provider", {}),
            "r2lab_mode": r2lab.get("mode", "disabled"),
        }
        if calendar.get("status") != "disabled":
            pos_selection.update(
                {
                    "event_id": calendar.get("id"),
                    "coverage_start": calendar.get("start"),
                    "coverage_end": calendar.get("end"),
                }
            )
        _write_json(run_dir / "pos-selection.json", pos_selection)
        return evidence
    except Exception as exc:
        if isinstance(exc, FreshPreparationError):
            evidence["host_preparation"] = {
                "mode": "fresh",
                "status": "failed",
                "nodes": exc.nodes,
                "parallel_preparation": len(selected) > 1,
                "managed_by": "synthran",
            }
        evidence["status"] = "failed"
        evidence["failure"] = {
            "type": type(exc).__name__,
            "message": str(exc),
        }
        evidence["failed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        _write_json(evidence_path, evidence)
        raise


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Apply SynthRAN's explicit SLICES/POS reservation and host-preparation policy."
    )
    parser.add_argument("config", type=Path)
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args(argv)
    try:
        execute(args.config, args.run_dir)
    except (ReservationError, OSError, ValueError, yaml.YAMLError) as exc:
        raise SystemExit(str(exc)) from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
