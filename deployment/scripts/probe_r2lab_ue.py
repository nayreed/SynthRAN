#!/usr/bin/env python3
"""Read an R2Lab UE's identity and session without changing modem state."""

import argparse
import ipaddress
import json
import re
import shlex
import shutil
import subprocess


def read_command(argv, evidence):
    result = subprocess.run(
        argv, capture_output=True, text=True, timeout=30, check=False
    )
    evidence[shlex.join(argv)] = {
        "rc": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
    if result.returncode:
        raise ValueError(f"Read-only probe failed: {shlex.join(argv)}")
    return result.stdout


def verify_observations(
    contract, links, modem, subscriber="", connection="", ip_config="", manager=""
):
    tunnel = contract["tunnel"]
    interface = tunnel["interface"]
    if len(links) != 1 or links[0].get("ifname") != interface:
        raise ValueError(f"Expected exactly one interface named {interface}")
    if "UP" not in links[0].get("flags", []):
        raise ValueError(f"Interface {interface} is down")
    addresses = [
        entry["local"]
        for entry in links[0].get("addr_info", [])
        if entry.get("family") == "inet" and entry.get("scope") == "global"
    ]
    if len(addresses) != 1:
        raise ValueError(
            f"Expected one global IPv4 address on {interface}, got {addresses}"
        )
    address = ipaddress.ip_address(addresses[0])
    network = ipaddress.ip_network(contract["address_cidr"], strict=False)
    if address not in network or address in (
        network.network_address,
        network.broadcast_address,
    ):
        raise ValueError(f"Address {address} does not identify a UE in {network}")

    contexts = re.findall(r'\+CGDCONT:\s*\d+,"[^"]+","([^"]+)"([^+\r\n]*)', modem)
    selected_contexts = [tail for dnn, tail in contexts if dnn == contract["dnn"]]
    if not selected_contexts:
        raise ValueError(
            f"R2Lab diagnostics do not show the configured DNN {contract['dnn']}"
        )
    contract_sd = str(contract["sd"]).upper()
    if contract_sd not in {"EMPTY", "FFFFFF"}:
        nssai = f"{int(contract['sst']):02x}.{contract_sd.lower()}"
        slice_apns = {contract["dnn"].lower()}
        # RM500Q firmware reports the configured eMBB context with this suffix.
        if int(contract["sst"]) == 1:
            slice_apns.add(f"{contract['dnn']}_EMBB{contract_sd}".lower())
        if not any(
            dnn.lower() in slice_apns and f'"{nssai}"' in tail.lower()
            for dnn, tail in contexts
        ):
            raise ValueError(
                f"R2Lab diagnostics do not show NSSAI {nssai} for {contract['dnn']}"
            )

    session = None
    if tunnel["mode"] == "mbim":
        identities = re.findall(r"Subscriber ID:\s*'([0-9]+)'", subscriber)
        sessions = re.findall(r"Session ID:\s*'?([0-9]+)'?", connection)
        if sessions != [str(tunnel["mbim_session"])]:
            raise ValueError("MBIM returned a different or unreadable session ID")
        if not re.search(r"Activation state:\s*'activated'", connection, re.IGNORECASE):
            raise ValueError(
                "MBIM session is not activated; reuse will not reattach it"
            )
        modem_addresses = re.findall(r"IP \[\d+\]:\s*'([0-9.]+)/[0-9]+'", ip_config)
        if modem_addresses != addresses:
            raise ValueError(
                "The modem's session address differs from the host interface"
            )
        session = int(sessions[0])
    else:
        identities = sorted(set(re.findall(r"\bIMSI:\s*([0-9]{15})\b", modem)))
        if not re.search(
            r'\+QCFG:\s*"usbnet",\s*0\b|\bUSB Mode:\s*0\s*\(QMI\)',
            modem,
            re.IGNORECASE,
        ):
            raise ValueError("R2Lab diagnostics do not confirm QMI USB mode")
        processes = manager.strip().splitlines()
        if len(processes) != 1:
            raise ValueError("Expected one active quectel-CM process")
        args = shlex.split(processes[0])[1:]
        if "-s" not in args or args[args.index("-s") + 1 : args.index("-s") + 2] != [
            contract["dnn"]
        ]:
            raise ValueError("The active QMI connection manager uses a different DNN")

    if identities != [contract["imsi"]]:
        raise ValueError(
            f"Observed SIM identity {identities} differs from the deployment contract"
        )
    return {
        "device": contract["device"],
        "index": contract["index"],
        "imsi": identities[0],
        "slice": contract["slice"],
        "sst": str(contract["sst"]),
        "sd": str(contract["sd"]),
        "dnn": contract["dnn"],
        "host": tunnel["host"],
        "interface": links[0]["ifname"],
        "address": str(address),
        "mode": tunnel["mode"],
        "mbim_session": session,
        "modem_verified": True,
    }


def probe(contract, evidence):
    tunnel = contract["tunnel"]
    links = json.loads(
        read_command(
            ["ip", "-j", "-4", "address", "show", "dev", tunnel["interface"]], evidence
        )
    )
    helper = (
        "check-ue"
        if tunnel["mode"] == "mbim" and shutil.which("check-ue")
        else "qhat-check"
    )
    modem = read_command([helper], evidence)
    if tunnel["mode"] == "mbim":
        base = ["mbimcli", "-p", "-d", "/dev/cdc-wdm0"]
        session = tunnel["mbim_session"]
        subscriber = read_command(base + ["--query-subscriber-ready-status"], evidence)
        connection = read_command(
            base + [f"--query-connection-state={session}"], evidence
        )
        ip_config = read_command(
            base + [f"--query-ip-configuration={session}"], evidence
        )
        return verify_observations(
            contract, links, modem, subscriber, connection, ip_config
        )
    manager = read_command(["pgrep", "-a", "-x", "quectel-CM"], evidence)
    return verify_observations(contract, links, modem, manager=manager)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", required=True, type=json.loads)
    args = parser.parse_args()
    result = {"verified": False, "commands": {}}
    try:
        result["binding"] = probe(args.contract, result["commands"])
        result["verified"] = True
    except (ValueError, KeyError, OSError, subprocess.TimeoutExpired) as error:
        result["error"] = str(error)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
