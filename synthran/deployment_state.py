from __future__ import annotations

import argparse
import copy
import hashlib
import ipaddress
import json
import os
import re
from pathlib import Path
from typing import Any

import yaml

from .scenario import load_scenario


SCHEMA_VERSION = 2


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def content_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def read_json(path: str | Path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(f"deployment identity is missing: {path}") from error
    except (json.JSONDecodeError, OSError) as error:
        raise ValueError(f"deployment identity is unreadable: {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"deployment identity must be a JSON object: {path}")
    return value


def resolve_scenario(source: str | Path, output: str | Path) -> dict:
    data = load_scenario(source, deployment_only=True)
    data.pop("_source_directory", None)
    _atomic_text(Path(output), yaml.safe_dump(data, sort_keys=False))
    return data


def _slice_map(network_profile: dict) -> dict[str, dict]:
    slices = network_profile.get("slices", [])
    if not isinstance(slices, list):
        raise ValueError("network profile slices must be a list")
    result = {entry.get("name"): entry for entry in slices if isinstance(entry, dict)}
    if None in result or len(result) != len(slices):
        raise ValueError("network profile slices must have unique names")
    return result


def _address_cidr(core: str, selected_slice: dict) -> str:
    prefix_lengths = {"oai": 24, "free5gc": 24, "open5gs": 16}
    try:
        prefix_length = prefix_lengths[core]
    except KeyError as error:
        raise ValueError(f"no UE session-network identity rule for core {core!r}") from error
    prefix = str(selected_slice["ip_prefix"])
    ipaddress.ip_network(prefix + ".0/24", strict=True)
    return f"{prefix}.0/{prefix_length}"


def _session_gateway_target(selected_slice: dict) -> str:
    prefix = str(selected_slice["ip_prefix"])
    target = prefix + ".1"
    ipaddress.ip_address(target)
    return target


def resolve_user_plane_targets(
    scenario: dict,
    network_profile: dict,
    ue_map: list[dict],
    topology: dict,
) -> list[dict]:
    """Seal backend-appropriate user-plane probe endpoints into the UE map.

    OAI basic mode exposes one UPF TUN anchor (tun0) for the primary session
    network and routes additional DNN pools through that same anchor. The OAI
    topology therefore owns one shared UPF probe identity instead of deriving a
    fictitious `<each-dnn>.1` address. Other retained cores keep the existing
    per-session-network gateway contract until their topology adapters define a
    stronger explicit endpoint.
    """

    deployment = scenario["deployment"]
    core = str(deployment["core"]).lower()
    slices = network_profile.get("slices", [])
    resolved = copy.deepcopy(ue_map)

    if core == "oai":
        transport = topology.get("transport", {})
        probe_contract = transport.get("user_plane_probe", {})
        if probe_contract.get("kind") != "oai-upf-tun0":
            raise ValueError(
                "OAI topology must define user_plane_probe kind 'oai-upf-tun0'"
            )
        try:
            session_index = int(probe_contract["session_index"])
            selected_slice = slices[session_index]
        except (KeyError, TypeError, ValueError, IndexError) as error:
            raise ValueError(
                "OAI topology user-plane probe must select a valid session_index"
            ) from error
        target = _session_gateway_target(selected_slice)
        interface = str(probe_contract.get("interface", "tun0"))
        probe = {
            "kind": "oai-upf-tun0",
            "interface": interface,
            "session_index": session_index,
            "address": target,
        }
        for entry in resolved:
            entry["user_plane_probe"] = copy.deepcopy(probe)
            entry["user_plane_target"] = target
        return resolved

    slice_by_name = _slice_map(network_profile)
    for entry in resolved:
        selected_slice = slice_by_name[entry["slice"]]
        target = _session_gateway_target(selected_slice)
        entry["user_plane_probe"] = {
            "kind": "session-network-gateway",
            "address": target,
        }
        entry["user_plane_target"] = target
    return resolved

def expected_user_plane_target(contract: dict) -> str | None:
    """Return the sealed user-plane target, with legacy-contract compatibility."""

    probe = contract.get("user_plane_probe")
    target = probe.get("address") if isinstance(probe, dict) else None
    target = target or contract.get("user_plane_target")
    if target:
        try:
            return str(ipaddress.ip_address(str(target)))
        except ValueError:
            return None
    cidr = str(contract.get("address_cidr", ""))
    literal = cidr.split("/", 1)[0]
    octets = literal.split(".")
    if len(octets) != 4:
        return None
    try:
        ipaddress.ip_address(literal)
        return str(ipaddress.ip_address(".".join(octets[:3] + ["1"])))
    except ValueError:
        return None


def _software_tunnel(ran: str, core: str, device: str, index: int) -> dict:
    if ran == "srsran":
        return {
            "namespace": core,
            "interface": f"tun_srsue{index}",
            "pod_labels": {"app": "srsran", "component": "ue"},
            "identity_file": f"/tmp/ue_{index}.conf",
        }
    if ran == "ueransim":
        match = re.fullmatch(r"uesim([0-9]+)", device)
        if not match or not 1 <= int(match.group(1)) <= 3:
            raise ValueError("the UERANSIM backend supports uesim01 through uesim03")
        return {
            "namespace": core,
            "interface": "uesimtun0",
            "pod_labels": {"component": "ue", "name": f"ue{int(match.group(1))}"},
        }
    if ran == "oai":
        release = "oai-nr-ue" if index == 1 else f"oai-nr-ue{index}"
        return {
            "namespace": core,
            "interface": "oaitun_ue1",
            "pod_name_prefix": release + "-",
        }
    raise ValueError(f"no software-tunnel identity rule for RAN {ran!r}")


def _r2lab_tunnel(device: str, ue_definition: dict) -> dict:
    mode = ue_definition.get("mode", "mbim")
    return {
        "host": device,
        "interface": ue_definition.get("interface", "wwan0"),
        "mode": mode,
        "mbim_session": ue_definition.get("mbim_session", 0) if mode == "mbim" else None,
    }


def _normalized_index(value: Any) -> Any:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return value


def _transport_value(item: dict, key: str) -> Any:
    if key in item:
        return item.get(key)
    tunnel = item.get("tunnel", {})
    return tunnel.get(key) if isinstance(tunnel, dict) else None


def binding_identity(item: dict) -> tuple:
    mode = _transport_value(item, "mode")
    session = _transport_value(item, "mbim_session")
    if mode != "mbim":
        session = None
    return (
        str(item.get("device")) if item.get("device") is not None else None,
        _normalized_index(item.get("index")),
        str(item.get("imsi")) if item.get("imsi") is not None else None,
        str(item.get("slice")) if item.get("slice") is not None else None,
        str(item.get("sst")) if item.get("sst") is not None else None,
        str(item.get("sd")) if item.get("sd") is not None else None,
        str(item.get("dnn")) if item.get("dnn") is not None else None,
        str(_transport_value(item, "host")) if _transport_value(item, "host") is not None else None,
        str(_transport_value(item, "namespace")) if _transport_value(item, "namespace") is not None else None,
        str(_transport_value(item, "interface")) if _transport_value(item, "interface") is not None else None,
        str(mode) if mode is not None else None,
        _normalized_index(session),
    )


def _user_plane_matches_contract(contract: dict, live: dict) -> bool:
    user_plane = live.get("user_plane")
    if not isinstance(user_plane, dict) or user_plane.get("verified") is not True:
        return False
    if user_plane.get("method") != "icmp_echo":
        return False
    address = live.get("address")
    expected_target = expected_user_plane_target(contract)
    if user_plane.get("source_interface") != _transport_value(contract, "interface"):
        return False
    if user_plane.get("source_address") != address:
        return False
    if expected_target is None or user_plane.get("target_address") != expected_target:
        return False
    probe = contract.get("user_plane_probe")
    if isinstance(probe, dict):
        if user_plane.get("target_kind") != probe.get("kind"):
            return False
        if probe.get("interface") is not None and user_plane.get("target_interface") != probe.get("interface"):
            return False
    return True


def bindings_match_deployment(deployment: dict, bindings: list[dict]) -> bool:
    if deployment.get("platform") not in {"rfsim", "r2lab"}:
        return False
    expected = deployment.get("ues", [])
    if len(bindings) != len(expected):
        return False
    by_device = {
        str(item.get("device")): item
        for item in bindings
        if isinstance(item, dict) and item.get("device") is not None
    }
    if len(by_device) != len(bindings):
        return False

    for contract in expected:
        live = by_device.get(str(contract.get("device")))
        if live is None or binding_identity(live) != binding_identity(contract):
            return False
        cidr = contract.get("address_cidr")
        address = live.get("address")
        if cidr:
            if not address:
                return False
            try:
                network = ipaddress.ip_network(str(cidr), strict=False)
                if ipaddress.ip_address(str(address)) not in network:
                    return False
            except ValueError:
                return False
        if not _user_plane_matches_contract(contract, live):
            return False
    return True


def build_ue_map(scenario: dict, network_profile: dict) -> list[dict]:
    deployment = scenario["deployment"]
    platform = str(deployment["platform"]).lower()
    ran = str(deployment["ran"]).lower()
    core = str(deployment["core"]).lower()
    plmn = network_profile["plmn"]
    slices = _slice_map(network_profile)
    result = []
    for index, device in enumerate(deployment["ues"], 1):
        ue = network_profile["ues"][device]
        selected_slice = slices[ue["slice"]]
        entry = {
            "device": device,
            "index": index,
            "imsi": f"{plmn['mcc']}{plmn['mnc']}{ue['imsi_suffix']}",
            "imsi_suffix": str(ue["imsi_suffix"]),
            "slice": ue["slice"],
            "sst": str(selected_slice["sst"]),
            "sd": str(selected_slice["sd"]),
            "dnn": selected_slice["dnn"],
            "address_cidr": _address_cidr(core, selected_slice),
        }
        if platform == "rfsim":
            entry["tunnel"] = _software_tunnel(ran, core, device, index)
        elif platform == "r2lab":
            entry["tunnel"] = _r2lab_tunnel(device, ue)
        else:
            raise ValueError(f"unsupported platform: {platform}")
        result.append(entry)
    return result


def build_manifest(
    scenario: dict,
    network_profile: dict,
    ue_map: list[dict],
    topology: dict | None = None,
) -> dict:
    for ue in ue_map:
        if expected_user_plane_target(ue) is None:
            raise ValueError(
                f"UE {ue.get('device', '<unknown>')} has no resolved user-plane probe target"
            )

    clean_scenario = copy.deepcopy(scenario)
    clean_scenario.pop("_source_directory", None)
    deployment = clean_scenario["deployment"]
    network_definition = copy.deepcopy(network_profile)
    network_definition.pop("ues", None)
    reservation = deployment.get("reservation", {})
    host_preparation = str(reservation.get("host_preparation", ""))
    selected = {
        "core": str(deployment["core"]).lower(),
        "ran": str(deployment["ran"]).lower(),
        "platform": str(deployment["platform"]).lower(),
        "radio_unit": "rfsim" if deployment["platform"] == "rfsim" else deployment.get("ru", deployment["platform"]),
        "reservation_mode": str(reservation.get("mode", "")),
        "host_preparation": host_preparation,
        "pos_image": (
            str(reservation.get("image", ""))
            if host_preparation == "fresh"
            else None
        ),
        "ansible_vars": copy.deepcopy(deployment.get("ansible_vars", {})),
        "host_vars": copy.deepcopy(deployment.get("host_vars", {})),
        "nodes": copy.deepcopy(deployment["nodes"]),
        "bridge_enabled": bool(deployment.get("bridge_enabled", True)),
        "network_profile": deployment["network_profile"],
        "network_profile_hash": content_hash(network_definition),
        "plmn": copy.deepcopy(network_profile["plmn"]),
        "slices": copy.deepcopy(network_profile.get("slices", [])),
        "ues": copy.deepcopy(ue_map),
        "topology": copy.deepcopy(topology or {"namespace": str(deployment["core"]).lower()}),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "candidate",
        "scenario_hash": content_hash(clean_scenario),
        "deployment_hash": content_hash(selected),
        "deployment": selected,
    }


def invalidate(active_path: str | Path, endpoint_path: str | Path | None = None) -> None:
    value = {
        "schema_version": SCHEMA_VERSION,
        "status": "invalidated",
        "invalidated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
    }
    _atomic_text(Path(active_path), json.dumps(value, indent=2, sort_keys=True) + "\n")
    if endpoint_path is not None:
        try:
            Path(endpoint_path).unlink()
        except FileNotFoundError:
            pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m synthran.deployment_state")
    commands = parser.add_subparsers(dest="command", required=True)
    resolve = commands.add_parser("resolve")
    resolve.add_argument("--source", required=True)
    resolve.add_argument("--output", required=True)
    invalid = commands.add_parser("invalidate")
    invalid.add_argument("--active", required=True)
    invalid.add_argument("--endpoint")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        if args.command == "resolve":
            resolve_scenario(args.source, args.output)
        else:
            invalidate(args.active, args.endpoint)
    except ValueError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
