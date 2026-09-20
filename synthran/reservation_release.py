"""Release only reservations whose per-run evidence proves SynthRAN ownership."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
from typing import Any, Mapping

import yaml

from .r2lab import access, ssh_options
from .reservation import (
    ReservationError,
    STATE_PATH,
    _calendars,
    _owner,
    run_visible,
    stamp,
)


class ReservationReleaseError(RuntimeError):
    """A recorded reservation could not be released without widening authority."""


R2LAB_RELEASE_CODE = r'''
import json, sys, time
from rhubarbe.book import Book
from rhubarbe.r2labapiproxy import iso_to_epoch

(
    email,
    expected_id_text,
    expected_slice,
    start_text,
    end_text,
    expected_from,
    expected_until,
) = sys.argv[1:8]
password = sys.stdin.readline().rstrip("\r\n")
if not email or not password:
    raise SystemExit("R2Lab email/password are required to release a booked lease")

try:
    expected_id = int(expected_id_text)
except ValueError as error:
    raise SystemExit("recorded R2Lab lease id is invalid") from error

book = Book(email=email, password=password, verbose=False)
start = book.canonical_date(start_text)
end = book.canonical_date(end_text)
leases = book.leases(start, end)
if len(leases) != 1:
    raise SystemExit(
        f"expected exactly one provider lease in recorded interval, found {len(leases)}"
    )
lease = leases[0]
if int(lease.get("id", -1)) != expected_id:
    raise SystemExit("provider lease id differs from recorded SynthRAN booking")
if str(lease.get("slice_name", "")) != expected_slice:
    raise SystemExit("provider lease slice differs from recorded SynthRAN booking")
if str(lease.get("t_from", "")) != expected_from:
    raise SystemExit("provider lease start differs from recorded SynthRAN booking")
if str(lease.get("t_until", "")) != expected_until:
    raise SystemExit("provider lease end differs from recorded SynthRAN booking")

now = int(time.time())
if iso_to_epoch(lease["t_until"]) <= now:
    print(json.dumps({
        "status": "already-ended",
        "lease_id": expected_id,
        "slice_name": expected_slice,
        "t_until": lease["t_until"],
    }, separators=(",", ":")))
    raise SystemExit(0)

if not book.delete(start, end):
    raise SystemExit("R2Lab provider refused the recorded lease release")

after = [
    item for item in book.leases(start, end)
    if int(item.get("id", -1)) == expected_id
    and str(item.get("slice_name", "")) == expected_slice
]
now_after = int(time.time())
if any(iso_to_epoch(item["t_until"]) > now_after + 2 for item in after):
    raise SystemExit("R2Lab lease still extends into the future after release")
print(json.dumps({
    "status": "released",
    "lease_id": expected_id,
    "slice_name": expected_slice,
    "remaining": [
        {"t_from": item.get("t_from"), "t_until": item.get("t_until")}
        for item in after
    ],
}, separators=(",", ":")))
'''.strip()


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReservationReleaseError(f"{label} is missing: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ReservationReleaseError(f"{label} is unreadable: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReservationReleaseError(f"{label} must be a JSON object: {path}")
    return value


def _read_r2lab_password() -> str:
    """Consume the teardown password without leaving it in child-process environment."""

    descriptor = os.environ.pop("SYNTHRAN_R2LAB_PASSWORD_FD", "").strip()
    if descriptor:
        try:
            fd = int(descriptor)
            with os.fdopen(fd, "r", encoding="utf-8", closefd=True) as stream:
                password = stream.readline().rstrip("\r\n")
        except (OSError, ValueError) as exc:
            raise ReservationReleaseError(
                "R2Lab teardown password descriptor is invalid or unreadable"
            ) from exc
        if not password:
            raise ReservationReleaseError("R2Lab teardown password descriptor was empty")
        return password

    # Compatibility for direct/library callers. Pop before spawning ssh so even
    # this fallback is not inherited by the provider child process.
    password = os.environ.pop("R2LAB_PASSWORD", "")
    if password:
        return password
    raise ReservationReleaseError(
        "R2Lab password is required to release a booked lease; use teardown.sh or provide R2LAB_PASSWORD to a direct library call"
    )


def _clear_matching_global_pos_state(record: Mapping[str, Any]) -> None:
    """Remove only the compatibility state file that names this exact event."""

    try:
        value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return
    if not isinstance(value, dict):
        return
    if str(value.get("event_id", "")) != str(record.get("id", "")):
        return
    if str(value.get("owner", "")) != str(record.get("owner", "")):
        return
    state_nodes = value.get("nodes")
    record_nodes = record.get("nodes")
    if not isinstance(state_nodes, list) or not isinstance(record_nodes, list):
        return
    if set(map(str, state_nodes)) != set(map(str, record_nodes)):
        return
    try:
        STATE_PATH.unlink()
    except FileNotFoundError:
        pass


def release_pos_calendar(authority_path: str | Path) -> dict[str, Any]:
    """Release a POS calendar only when this run created the exact event."""

    path = Path(authority_path)
    if not path.is_file():
        return {
            "status": "skipped",
            "reason": "accepted run has no reservation-authority evidence",
        }
    authority = _read_object(path, "reservation authority")
    calendar = authority.get("pos_calendar")
    if not isinstance(calendar, dict):
        return {
            "status": "skipped",
            "reason": "accepted run has no POS calendar evidence",
        }

    acquired = str(calendar.get("status", ""))
    if acquired in {"disabled", "reused", "required-existing"}:
        return {
            "status": "preserved",
            "authority_status": acquired,
            "reason": "run evidence does not prove creation ownership",
        }
    if acquired != "created":
        raise ReservationReleaseError(
            f"unsupported POS calendar authority status for teardown: {acquired!r}"
        )
    if calendar.get("managed_by") != "synthran":
        raise ReservationReleaseError("POS calendar evidence is not managed by SynthRAN")

    event_id = str(calendar.get("id", "")).strip()
    owner = str(calendar.get("owner", "")).strip()
    nodes = calendar.get("nodes")
    if not event_id or not owner or not isinstance(nodes, list) or not nodes:
        raise ReservationReleaseError("created POS calendar evidence is incomplete")
    selected = [str(node) for node in nodes]
    if len(selected) != len(set(selected)):
        raise ReservationReleaseError("created POS calendar evidence contains duplicate nodes")
    try:
        current_owner = _owner()
    except ReservationError as exc:
        raise ReservationReleaseError(str(exc)) from exc
    if current_owner != owner:
        raise ReservationReleaseError(
            f"current POS owner {current_owner!r} differs from recorded creator {owner!r}"
        )

    try:
        events = _calendars()
    except ReservationError as exc:
        raise ReservationReleaseError(str(exc)) from exc
    matches = [event for event in events if str(event.get("id", "")) == event_id]
    if not matches:
        _clear_matching_global_pos_state(calendar)
        return {
            "status": "already-absent",
            "event_id": event_id,
            "nodes": selected,
        }
    if len(matches) != 1:
        raise ReservationReleaseError(
            f"POS provider returned {len(matches)} events with recorded id {event_id}"
        )
    live = matches[0]
    live_nodes = live.get("nodes")
    if str(live.get("owner", "")) != owner:
        raise ReservationReleaseError("POS provider event owner differs from recorded creator")
    if not isinstance(live_nodes, list) or set(map(str, live_nodes)) != set(selected):
        raise ReservationReleaseError("POS provider event nodes differ from recorded created set")
    try:
        ended = stamp(str(live["end_date"])) <= __import__("datetime").datetime.now().astimezone()
    except (KeyError, TypeError, ValueError) as exc:
        raise ReservationReleaseError("POS provider event has invalid end timestamp") from exc
    if ended:
        _clear_matching_global_pos_state(calendar)
        return {
            "status": "already-absent",
            "event_id": event_id,
            "nodes": selected,
            "reason": "recorded created calendar has already ended",
        }

    result = run_visible(
        ["pos", "calendar", "delete", "--id", event_id, *selected],
        check=False,
    )
    if result.returncode:
        detail = "\n".join(
            part.strip()
            for part in (result.stdout or "", result.stderr or "")
            if part.strip()
        )
        raise ReservationReleaseError(
            "POS calendar release failed for the exact recorded event"
            + (f": {detail}" if detail else f" (exit {result.returncode})")
        )
    try:
        remaining = _calendars()
    except ReservationError as exc:
        raise ReservationReleaseError(str(exc)) from exc
    if any(str(event.get("id", "")) == event_id for event in remaining):
        raise ReservationReleaseError("POS calendar event is still present after provider delete")
    _clear_matching_global_pos_state(calendar)
    return {
        "status": "released",
        "event_id": event_id,
        "owner": owner,
        "nodes": selected,
    }


def _remote(
    *,
    host: str,
    username: str,
    identity_file: str,
    known_hosts: Path,
    argv: list[str],
    stdin: str,
) -> subprocess.CompletedProcess[str]:
    target = f"{username}@{host}"
    command = [
        "ssh",
        *ssh_options(host, known_hosts, identity_file),
        target,
        shlex.join(argv),
    ]
    return subprocess.run(
        command,
        text=True,
        input=stdin,
        capture_output=True,
        check=False,
    )


def release_r2lab_lease(
    run_dir: str | Path,
    private_dir: str | Path,
) -> dict[str, Any]:
    """Release only an R2Lab lease that this accepted run newly booked."""

    run_dir = Path(run_dir).resolve()
    private_dir = Path(private_dir).resolve()
    record_path = run_dir / "r2lab-lease.json"
    if not record_path.is_file():
        return {
            "status": "skipped",
            "reason": "accepted run has no R2Lab lease evidence",
        }
    record = _read_object(record_path, "R2Lab lease evidence")
    acquired = str(record.get("status", ""))
    if acquired in {"disabled", "reused", "extended"}:
        return {
            "status": "preserved",
            "authority_status": acquired,
            "reason": "run evidence does not prove independent booking ownership",
        }
    if acquired != "booked":
        raise ReservationReleaseError(
            f"unsupported R2Lab lease authority status for teardown: {acquired!r}"
        )

    requested = record.get("requested")
    provider = record.get("provider_lease")
    if not isinstance(requested, dict) or not isinstance(provider, dict):
        raise ReservationReleaseError("booked R2Lab lease evidence is incomplete")
    start = str(requested.get("start", "")).strip()
    end = str(requested.get("end", "")).strip()
    lease_id = str(provider.get("id", "")).strip()
    slice_name = str(provider.get("slice_name", "")).strip()
    t_from = str(provider.get("t_from", "")).strip()
    t_until = str(provider.get("t_until", "")).strip()
    if not all((start, end, lease_id, slice_name, t_from, t_until)):
        raise ReservationReleaseError("booked R2Lab provider coordinates are incomplete")

    scenario_path = private_dir / "resolved-scenario.yml"
    if not scenario_path.is_file():
        raise ReservationReleaseError(
            f"accepted private scenario is missing: {scenario_path}"
        )
    try:
        scenario = yaml.safe_load(scenario_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ReservationReleaseError(f"accepted private scenario is unreadable: {exc}") from exc
    deployment = scenario.get("deployment")
    if not isinstance(deployment, dict):
        raise ReservationReleaseError("accepted private scenario has no deployment mapping")
    settings = access(deployment)
    host = str(settings.get("host", "")).strip()
    username = str(settings.get("username", "")).strip()
    identity_file = str(settings.get("identity_file", "")).strip()
    if not host or not username:
        raise ReservationReleaseError("accepted R2Lab connection identity is incomplete")
    if username != slice_name:
        raise ReservationReleaseError(
            "current R2Lab username differs from the recorded booked provider slice"
        )
    if identity_file and not Path(identity_file).expanduser().is_file():
        raise ReservationReleaseError(f"R2Lab identity file is missing: {identity_file}")

    email = os.environ.get("R2LAB_EMAIL", "").strip()
    if not email:
        raise ReservationReleaseError("R2LAB_EMAIL is required to release a booked R2Lab lease")
    password = _read_r2lab_password()
    known_hosts = Path(
        os.environ.get(
            "R2LAB_FARADAY_KNOWN_HOSTS",
            ".synthran/r2lab/faraday_known_hosts",
        )
    ).expanduser().resolve()
    known_hosts.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    known_hosts.touch(exist_ok=True)
    known_hosts.chmod(0o600)

    result = _remote(
        host=host,
        username=username,
        identity_file=identity_file,
        known_hosts=known_hosts,
        argv=[
            "python3",
            "-c",
            R2LAB_RELEASE_CODE,
            email,
            lease_id,
            slice_name,
            start,
            end,
            t_from,
            t_until,
        ],
        stdin=password + "\n",
    )
    password = ""
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise ReservationReleaseError(
            "R2Lab booked lease release failed"
            + (f": {detail}" if detail else f" (exit {result.returncode})")
        )
    try:
        provider_result = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ReservationReleaseError(
            "R2Lab provider returned unreadable release evidence"
        ) from exc
    if not isinstance(provider_result, dict):
        raise ReservationReleaseError("R2Lab provider release evidence is not an object")
    provider_status = provider_result.get("status")
    if provider_status == "already-ended":
        status = "already-absent"
    elif provider_status == "released":
        status = "released"
    else:
        raise ReservationReleaseError(
            f"R2Lab provider returned unexpected release status: {provider_status!r}"
        )
    return {
        "status": status,
        "lease_id": lease_id,
        "slice_name": slice_name,
        "requested": {"start": start, "end": end},
        "provider": provider_result,
    }
