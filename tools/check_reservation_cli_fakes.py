#!/usr/bin/env python3
"""End-to-end no-hardware check using temporary fake slices/post5g/pos/ssh executables."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap

import yaml

ROOT = Path(__file__).resolve().parents[1]


class CheckError(RuntimeError):
    pass


FAKE = r'''#!/usr/bin/env python3
import datetime as dt
import json
import os
from pathlib import Path
import sys

name = Path(sys.argv[0]).name
args = sys.argv[1:]
state_path = Path(os.environ["SYNTHRAN_FAKE_STATE"])
log_path = Path(os.environ["SYNTHRAN_FAKE_LOG"])
state = json.loads(state_path.read_text()) if state_path.exists() else {}
stdin = sys.stdin.read()
with log_path.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({"command": name, "argv": args, "stdin": stdin}) + "\n")

def save():
    state_path.write_text(json.dumps(state), encoding="utf-8")

def lease_payload():
    return {
        "requested_start_epoch": 100,
        "requested_end_epoch": 200,
        "leases": state.get("r2_leases", []),
    }

if name == "slices":
    if args[:2] == ["project", "use"]:
        raise SystemExit(0)
    if args[:2] == ["experiment", "show"]:
        if state.get("experiment_exists"):
            print("exists")
            raise SystemExit(0)
        print("experiment missing", file=sys.stderr)
        raise SystemExit(1)
    if args[:2] == ["experiment", "create"]:
        state["experiment_exists"] = True
        save()
        print("created")
        raise SystemExit(0)

if name == "post5g" and args[:2] == ["experiment", "prefix"]:
    attempt = int(state.get("prefix_attempt", 0)) + 1
    state["prefix_attempt"] = attempt
    save()
    if state.get("prefix_fail_first") and attempt == 1:
        print("{}")
    else:
        print(json.dumps({
            "subnet": "192.0.2.0/24",
            "lb": "192.0.2.10",
            "expiration_time": "2026-09-16T04:00:00Z",
        }))
    raise SystemExit(0)

if name == "pos":
    if args[:3] == ["calendar", "list", "--json"]:
        rows = []
        if state.get("calendar_exists"):
            now = dt.datetime.now().astimezone()
            rows = [{
                "id": "42",
                "owner": os.environ.get("USER", "ci-user"),
                "nodes": state.get("calendar_nodes", []),
                "start_date": (now - dt.timedelta(minutes=5)).isoformat(),
                "end_date": (now + dt.timedelta(minutes=180)).isoformat(),
            }]
        print(json.dumps(rows))
        raise SystemExit(0)
    if args[:2] == ["calendar", "create"]:
        nodes = [value for value in args if value.startswith("sopnode-")]
        state["calendar_exists"] = True
        state["calendar_nodes"] = nodes
        save()
        print("42")
        raise SystemExit(0)
    if args[:2] == ["allocations", "allocate"]:
        raise SystemExit(0)
    if args[:2] == ["allocations", "free"]:
        raise SystemExit(0)
    if args[:2] == ["nodes", "image"]:
        raise SystemExit(0)
    if args[:2] == ["nodes", "bootparameter"]:
        raise SystemExit(0)
    if args[:2] == ["nodes", "reset"]:
        raise SystemExit(0)

if name == "ssh":
    remote = args[-1] if args else ""
    if remote == "true" or "root@sopnode-" in " ".join(args):
        raise SystemExit(0)
    if "b.leases" in remote:
        print(json.dumps(lease_payload(), separators=(",", ":")))
        raise SystemExit(0)
    if "update_lease" in remote:
        if state.get("r2_extend_denied"):
            print("extension denied", file=sys.stderr)
            raise SystemExit(4)
        leases = state.get("r2_leases", [])
        if leases:
            leases[0]["end_epoch"] = 210
            leases[0]["t_until"] = "2026-09-16T02:00:00+00:00"
            state["r2_leases"] = leases
            save()
        raise SystemExit(0)
    if "b.book" in remote:
        if state.get("r2_book_denied"):
            print("booking denied by fake provider")
            raise SystemExit(3)
        state["r2_leases"] = [{
            "id": 99,
            "slice_name": "ci-slice",
            "t_from": "2026-09-16T00:00:00+00:00",
            "t_until": "2026-09-16T02:00:00+00:00",
            "start_epoch": 90,
            "end_epoch": 210,
        }]
        save()
        raise SystemExit(0)

print(f"unexpected fake command: {name} {args}", file=sys.stderr)
raise SystemExit(97)
'''


def write_state(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def read_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def make_fake_bin(root: Path) -> Path:
    bindir = root / "bin"
    bindir.mkdir()
    dispatcher = bindir / "fake"
    dispatcher.write_text(FAKE, encoding="utf-8")
    dispatcher.chmod(0o755)
    for name in ("slices", "post5g", "pos", "ssh"):
        (bindir / name).symlink_to(dispatcher.name)
    return bindir


def environment(bindir: Path, state: Path, log: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "PATH": str(bindir) + os.pathsep + env.get("PATH", ""),
            "USER": "ci-user",
            "SYNTHRAN_FAKE_STATE": str(state),
            "SYNTHRAN_FAKE_LOG": str(log),
            "SYNTHRAN_PROVIDER_PREFIX_INTERVAL_SECONDS": "0",
            "SYNTHRAN_POS_READY_ATTEMPTS": "1",
            "SYNTHRAN_POS_READY_INTERVAL_SECONDS": "0",
        }
    )
    return env


def run(command: list[str], *, env: dict[str, str], stdin=None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        stdin=subprocess.DEVNULL if stdin is None else None,
        input=stdin,
        text=True,
        capture_output=True,
        check=False,
    )


def sop_scenario(path: Path, *, provider: str, pos: str, preparation: str) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "deployment": {
                    "nodes": {
                        "core": "sopnode-f2",
                        "ran": "sopnode-f3",
                        "broker": "sopnode-f2",
                    },
                    "provider": {
                        "mode": provider,
                        "project": "post5g-beta",
                        "experiment": "synthran-cli-fake",
                        "experiment_duration": "4h",
                    },
                    "reservation": {
                        "mode": pos,
                        "host_preparation": preparation,
                        "duration_minutes": 120,
                        "image": "scenario-image",
                    },
                    "r2lab_reservation": {"mode": "disabled"},
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def check_sop(root: Path, env: dict[str, str], state: Path, log: Path) -> dict[str, str]:
    config = root / "fresh.yml"
    result_dir = root / "fresh-result"
    sop_scenario(config, provider="create", pos="create", preparation="fresh")
    write_state(state, {"prefix_fail_first": True})
    log.write_text("", encoding="utf-8")
    result = run(
        [sys.executable, "deployment/scripts/reserve_sop.py", str(config), str(result_dir)],
        env=env,
    )
    if result.returncode:
        raise CheckError(f"fresh fake-CLI path failed:\n{result.stdout}\n{result.stderr}")
    calls = read_log(log)
    names = [entry["command"] for entry in calls]
    if names[:6] != ["slices", "slices", "slices", "post5g", "post5g", "pos"]:
        raise CheckError(f"provider/POS ordering changed: {names}")
    create = next(entry for entry in calls if entry["command"] == "pos" and entry["argv"][:2] == ["calendar", "create"])
    selected = [value for value in create["argv"] if value.startswith("sopnode-")]
    if selected != ["sopnode-f2", "sopnode-f3"]:
        raise CheckError(f"fake-CLI path remapped resources: {selected}")
    pos_calls = [entry for entry in calls if entry["command"] == "pos"]
    allocation_indices = [
        index
        for index, entry in enumerate(pos_calls)
        if entry["argv"][:2] == ["allocations", "allocate"]
    ]
    mutation_indices = [
        index
        for index, entry in enumerate(pos_calls)
        if entry["argv"][:2]
        in (
            ["nodes", "image"],
            ["nodes", "bootparameter"],
            ["nodes", "reset"],
        )
    ]
    if len(allocation_indices) != 2 or not mutation_indices:
        raise CheckError(f"fresh authority/preparation calls are incomplete: {pos_calls}")
    if max(allocation_indices) >= min(mutation_indices):
        raise CheckError(
            "fresh per-node preparation started before all allocation probes completed"
        )

    for node in selected:
        phases = []
        for entry in pos_calls:
            argv = entry["argv"]
            if argv[:2] == ["nodes", "image"] and argv[-2] == node:
                phases.append("image")
            elif argv[:2] == ["nodes", "bootparameter"] and argv[2] == node:
                phases.append("bootparameter")
            elif argv[:2] == ["nodes", "reset"] and argv[-1] == node:
                phases.append("reset")
        if phases != ["image", "bootparameter", "reset"]:
            raise CheckError(f"fresh per-node phase ordering changed for {node}: {phases}")
    if any(entry["stdin"] for entry in calls):
        raise CheckError("SOP/provider authority read or forwarded stdin despite EOF")
    evidence = json.loads((result_dir / "reservation-authority.json").read_text())
    if evidence["pos_calendar"]["id"] != "42" or evidence["selected_resources"] != ["sopnode-f2", "sopnode-f3"]:
        raise CheckError("fresh fake-CLI evidence lost exact provider identity")
    if evidence.get("host_preparation", {}).get("parallel_preparation") is not True:
        raise CheckError("fresh fake-CLI evidence did not record multi-node parallel preparation")
    for node in selected:
        if evidence["host_preparation"]["nodes"][node].get("status") != "ready":
            raise CheckError(f"fresh fake-CLI evidence lost ready state for {node}")

    before = len(read_log(log))
    config = root / "existing.yml"
    result_dir = root / "existing-result"
    sop_scenario(config, provider="require-existing", pos="require-existing", preparation="preserve")
    result = run(
        [sys.executable, "deployment/scripts/reserve_sop.py", str(config), str(result_dir)],
        env=env,
    )
    if result.returncode:
        raise CheckError(f"require-existing fake-CLI path failed:\n{result.stdout}\n{result.stderr}")
    later = read_log(log)[before:]
    forbidden = [
        entry
        for entry in later
        if entry["command"] == "pos"
        and entry["argv"][:2]
        in (
            ["calendar", "create"],
            ["allocations", "allocate"],
            ["allocations", "free"],
            ["nodes", "image"],
            ["nodes", "bootparameter"],
            ["nodes", "reset"],
        )
    ]
    if forbidden or any(entry["command"] == "ssh" for entry in later):
        raise CheckError(f"require-existing/preserve mutated resources: {forbidden}")
    if any(entry["stdin"] for entry in later):
        raise CheckError("require-existing/preserve read stdin")

    before = len(read_log(log))
    config = root / "bootstrap.yml"
    result_dir = root / "bootstrap-result"
    sop_scenario(config, provider="require-existing", pos="require-existing", preparation="bootstrap")
    result = run(
        [sys.executable, "deployment/scripts/reserve_sop.py", str(config), str(result_dir)],
        env=env,
    )
    if result.returncode:
        raise CheckError(f"require-existing/bootstrap fake-CLI path failed:\n{result.stdout}\n{result.stderr}")
    later = read_log(log)[before:]
    forbidden = [
        entry
        for entry in later
        if entry["command"] == "pos"
        and entry["argv"][:2]
        in (
            ["calendar", "create"],
            ["allocations", "allocate"],
            ["allocations", "free"],
            ["nodes", "image"],
            ["nodes", "bootparameter"],
            ["nodes", "reset"],
        )
    ]
    if forbidden or any(entry["command"] == "ssh" for entry in later):
        raise CheckError(f"require-existing/bootstrap reservation layer mutated hosts: {forbidden}")
    bootstrap_evidence = json.loads((result_dir / "reservation-authority.json").read_text())
    if bootstrap_evidence["policies"]["host_preparation"] != "bootstrap":
        raise CheckError("bootstrap fake-CLI evidence lost canonical preparation policy")
    if bootstrap_evidence["host_preparation"]["mode"] != "bootstrap":
        raise CheckError("bootstrap fake-CLI result lost in-place preparation mode")

    return {
        "fresh": "passed",
        "require_existing_preserve": "passed",
        "require_existing_bootstrap": "passed",
        "stdin_eof": "passed",
    }


def r2_command(root: Path, mode: str) -> list[str]:
    return [
        sys.executable,
        "deployment/scripts/reserve_r2lab.py",
        "--host", "faraday.inria.fr",
        "--username", "ci-slice",
        "--known-hosts", str(root / "known_hosts"),
        "--email", "ci@example.invalid",
        "--start", "2026-09-16T00:00",
        "--end", "2026-09-16T02:00",
        "--output", str(root / f"r2-{mode}.json"),
        "--log", str(root / f"r2-{mode}.log"),
        "--mode", mode,
    ]


def check_r2lab(root: Path, env: dict[str, str], state: Path, log: Path) -> dict[str, str]:
    covering = {
        "id": 10,
        "slice_name": "ci-slice",
        "t_from": "2026-09-16T00:00:00+00:00",
        "t_until": "2026-09-16T02:00:00+00:00",
        "start_epoch": 90,
        "end_epoch": 210,
    }
    write_state(state, {"r2_leases": [covering]})
    log.write_text("", encoding="utf-8")
    result = run(r2_command(root, "require-existing"), env=env)
    if result.returncode:
        raise CheckError(f"fake R2Lab existing coverage failed: {result.stderr}")
    calls = read_log(log)
    if any(entry["stdin"] for entry in calls):
        raise CheckError("R2Lab require-existing read or forwarded a password")

    write_state(state, {"r2_leases": [], "r2_book_denied": True})
    log.write_text("", encoding="utf-8")
    result = run(r2_command(root, "book"), env=env, stdin="top-secret\n")
    if result.returncode == 0 or "booking denied by fake provider" not in result.stderr:
        raise CheckError(f"R2Lab booking denial was not propagated: {result.stdout} {result.stderr}")
    calls = read_log(log)
    password_calls = [entry for entry in calls if entry["stdin"] == "top-secret\n"]
    if len(password_calls) != 1:
        raise CheckError(f"R2Lab password stdin transport changed: {password_calls}")
    if any("top-secret" in " ".join(entry["argv"]) for entry in calls):
        raise CheckError("R2Lab password leaked into ssh argv")

    write_state(state, {"r2_leases": [covering, covering | {"id": 11}]})
    log.write_text("", encoding="utf-8")
    result = run(r2_command(root, "book"), env=env)
    if result.returncode == 0 or "multiple owned R2Lab leases cover" not in result.stderr:
        raise CheckError("R2Lab ambiguous coverage did not fail closed")
    if any(entry["stdin"] for entry in read_log(log)):
        raise CheckError("R2Lab ambiguous coverage consumed credentials")
    return {
        "existing_coverage": "passed",
        "booking_denial": "passed",
        "ambiguous": "passed",
        "password_stdin_only": "passed",
    }


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="synthran-fake-cli-") as value:
        root = Path(value)
        state = root / "state.json"
        log = root / "commands.jsonl"
        bindir = make_fake_bin(root)
        env = environment(bindir, state, log)
        result = {
            "schema": "synthran/reservation-cli-fakes/v1",
            "sop": check_sop(root, env, state, log),
            "r2lab": check_r2lab(root, env, state, log),
            "result": "pass",
        }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CheckError, OSError, ValueError, yaml.YAMLError) as exc:
        raise SystemExit(f"reservation-cli-fakes: {exc}") from exc
