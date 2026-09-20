from __future__ import annotations
import copy, ipaddress, re
from pathlib import Path
import yaml

from .host_preparation import (
    BOOTSTRAP,
    FRESH,
    validate_preparation_mode,
)
from .profile_validation import (
    validate_network_profile,
    validate_ue_catalog,
    validate_ue_slice_assignments,
)

SUPPORTED_CORES = {"oai", "open5gs", "free5gc"}
SUPPORTED_RANS = {"oai", "srsran", "ueransim"}
SUPPORTED_PLATFORMS = {"rfsim", "r2lab"}
SUPPORTED_R2LAB_RADIOS = {"n300", "n320"}
_RESERVATION_MODES = {"create", "require-existing", "disabled"}
_PROVIDER_MODES = {"create", "require-existing", "disabled"}
_R2LAB_RESERVATION_MODES = {"book", "require-existing", "disabled"}
_TRANSPORT_KEYS = {"mode", "interface", "mbim_session"}
_INSECURE_SSH = re.compile(
    r"(?:StrictHostKeyChecking\s*=\s*no|UserKnownHostsFile\s*=\s*/dev/null)", re.I
)
_SAFE_PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_SAFE_PROVIDER_CONTEXT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_DURATION = re.compile(r"[1-9][0-9]*(?:m|h)\Z")


def _network_profile_source(deployment: dict) -> Path:
    if deployment.get("network_profile_file"):
        return Path(deployment["network_profile_file"])
    name = str(deployment.get("network_profile", ""))
    return Path("deployment/group_vars/all") / f"network_profile_{name}.yaml"


def _ue_catalog_source(deployment: dict) -> Path:
    return Path(deployment.get("ue_catalog_file") or "deployment/group_vars/all/ue_catalog.yaml")


def _topology_source(deployment: dict) -> Path:
    return Path(deployment.get("topology_file") or "deployment/topology.yml")


def _validate_selected_transport(
    selected: dict, topology_source: Path, ran: str, core: str
) -> None:
    context = f"selected topology transport for {ran} + {core}"
    transport = selected.get("transport")
    if not isinstance(transport, dict):
        raise ValueError(f"{context} must be a mapping: {topology_source}")

    for key in ("bridges", "workload_interfaces", "colocated", "split", "endpoints"):
        if not isinstance(transport.get(key), dict):
            raise ValueError(f"{context}.{key} must be a mapping: {topology_source}")

    bridges = transport["bridges"]
    declared_bridges: dict[str, set[str]] = {}
    for role in ("core", "ran"):
        values = bridges.get(role)
        if (
            not isinstance(values, list)
            or not all(isinstance(item, str) and item for item in values)
            or len(values) != len(set(values))
        ):
            raise ValueError(
                f"{context}.bridges.{role} must be a unique list of names: "
                f"{topology_source}"
            )
        declared_bridges[role] = set(values)

    interfaces = transport["workload_interfaces"]
    for role in ("core", "ran"):
        values = interfaces.get(role)
        if not isinstance(values, dict):
            raise ValueError(
                f"{context}.workload_interfaces.{role} must be a mapping: "
                f"{topology_source}"
            )
        if not isinstance(values.get("n3"), str) or not values["n3"]:
            raise ValueError(
                f"{context}.workload_interfaces.{role}.n3 must be a non-empty "
                f"interface name: {topology_source}"
            )
        for plane, interface in values.items():
            if not isinstance(interface, str) or not interface:
                raise ValueError(
                    f"{context}.workload_interfaces.{role}.{plane} must be a "
                    f"non-empty interface name: {topology_source}"
                )
            if interface not in {"physical", "primary"} and interface not in declared_bridges[role]:
                raise ValueError(
                    f"{context}.workload_interfaces.{role}.{plane} references "
                    f"undeclared bridge {interface!r}: {topology_source}"
                )

    all_bridges = declared_bridges["core"] | declared_bridges["ran"]
    for mode in ("colocated", "split"):
        addresses = transport[mode].get("addresses")
        if not isinstance(addresses, dict):
            raise ValueError(
                f"{context}.{mode}.addresses must be a mapping: {topology_source}"
            )
        for bridge, value in addresses.items():
            if bridge not in all_bridges:
                raise ValueError(
                    f"{context}.{mode}.addresses references undeclared bridge "
                    f"{bridge!r}: {topology_source}"
                )
            if isinstance(value, dict):
                if set(value) != {"core", "ran"}:
                    raise ValueError(
                        f"{context}.{mode}.addresses.{bridge} must define exactly "
                        f"core and ran: {topology_source}"
                    )
                candidates = value.values()
            else:
                candidates = (value,)
            for candidate in candidates:
                try:
                    ipaddress.ip_interface(str(candidate))
                except ValueError as error:
                    raise ValueError(
                        f"{context}.{mode}.addresses.{bridge} contains invalid "
                        f"interface {candidate!r}: {topology_source}"
                    ) from error

    split = transport["split"]
    gre = split.get("gre", [])
    patches = split.get("patches", [])
    if (
        not isinstance(gre, list)
        or not all(isinstance(bridge, str) and bridge for bridge in gre)
        or len(gre) != len(set(gre))
    ):
        raise ValueError(f"{context}.split.gre must be a unique list: {topology_source}")
    if not isinstance(patches, list):
        raise ValueError(f"{context}.split.patches must be a list: {topology_source}")

    for bridge in gre:
        address = split["addresses"].get(bridge)
        if bridge not in declared_bridges["core"] or bridge not in declared_bridges["ran"]:
            raise ValueError(
                f"{context}.split.gre bridge {bridge!r} must exist on core and ran: "
                f"{topology_source}"
            )
        if not isinstance(address, dict) or set(address) != {"core", "ran"}:
            raise ValueError(
                f"{context}.split.gre bridge {bridge!r} requires core/ran addresses: "
                f"{topology_source}"
            )

    required_patch = {"a", "b", "a_port", "b_port"}
    for patch in patches:
        if not isinstance(patch, dict) or not required_patch <= set(patch):
            raise ValueError(f"{context}.split.patches contains a malformed patch: {topology_source}")
        if not all(isinstance(patch[key], str) and patch[key] for key in required_patch):
            raise ValueError(f"{context}.split.patches requires non-empty names: {topology_source}")
        for bridge_key in ("a", "b"):
            bridge = patch[bridge_key]
            if bridge not in declared_bridges["core"] or bridge not in declared_bridges["ran"]:
                raise ValueError(
                    f"{context}.split.patches references bridge {bridge!r} that is "
                    f"not declared on both split hosts: {topology_source}"
                )

    endpoints = transport["endpoints"]
    if not endpoints or not all(
        isinstance(value, str) and value for value in endpoints.values()
    ):
        raise ValueError(
            f"{context}.endpoints must contain non-empty endpoint values: {topology_source}"
        )


def _validate_selected_topology(deployment: dict) -> None:
    topology_source = _topology_source(deployment)
    if not topology_source.is_file():
        raise ValueError(f"topology not found: {topology_source}")

    topologies = yaml.safe_load(topology_source.read_text(encoding="utf-8"))
    if not isinstance(topologies, dict):
        raise ValueError(f"topology document must be a mapping: {topology_source}")

    schema_version = topologies.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version < 1
    ):
        raise ValueError(
            f"topology schema_version must be a positive integer: {topology_source}"
        )

    ran = str(deployment.get("ran", "")).lower()
    core = str(deployment.get("core", "")).lower()
    try:
        selected = topologies["rans"][ran][core]
    except (KeyError, TypeError) as error:
        raise ValueError(
            f"no asserted topology contract for {ran} + {core}: {topology_source}"
        ) from error
    if not isinstance(selected, dict):
        raise ValueError(
            f"selected topology contract for {ran} + {core} must be a mapping: "
            f"{topology_source}"
        )
    if not isinstance(selected.get("network"), dict) or not selected["network"]:
        raise ValueError(
            f"selected topology contract for {ran} + {core} requires a non-empty "
            f"network mapping: {topology_source}"
        )
    _validate_selected_transport(selected, topology_source, ran, core)


def _mapping(value, label: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    return dict(value)


def _legacy_enabled(value, label: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be boolean when provided")
    return value


def _normalize_reservation_policy(deployment: dict) -> None:
    """Materialize all resource-acquisition choices before any remote mutation."""

    reservation = _mapping(deployment.get("reservation"), "deployment.reservation")
    reservation_enabled = _legacy_enabled(
        reservation.get("enabled"), "deployment.reservation.enabled", True
    )
    mode = reservation.get("mode")
    if mode is None:
        mode = "create" if reservation_enabled else "disabled"
    if mode not in _RESERVATION_MODES:
        raise ValueError(
            "deployment.reservation.mode must be create, require-existing, or disabled"
        )
    if "enabled" in reservation and reservation_enabled != (mode != "disabled"):
        raise ValueError(
            "deployment.reservation.enabled conflicts with deployment.reservation.mode"
        )

    host_preparation = reservation.get("host_preparation")
    if host_preparation is None:
        host_preparation = "fresh" if mode != "disabled" else "preserve"
    host_preparation = validate_preparation_mode(host_preparation)
    if mode == "disabled" and host_preparation in {BOOTSTRAP, FRESH}:
        raise ValueError(
            f"{host_preparation} host preparation requires "
            "deployment.reservation.mode create or require-existing"
        )

    duration = reservation.get("duration_minutes", 120)
    if not isinstance(duration, int) or isinstance(duration, bool) or duration < 1:
        raise ValueError("deployment.reservation.duration_minutes must be a positive integer")
    image = reservation.get("image", "ubuntu-jammy")
    if not isinstance(image, str) or not image.strip():
        raise ValueError("deployment.reservation.image must be a non-empty string")

    # node_pool belonged to the retired substitution policy. Resource identities
    # now come exclusively from deployment.nodes and are immutable during acquisition.
    reservation.pop("node_pool", None)
    reservation.update(
        {
            "mode": mode,
            "host_preparation": host_preparation,
            "enabled": mode != "disabled",
            "duration_minutes": duration,
            "image": image,
        }
    )
    deployment["reservation"] = reservation

    provider = _mapping(deployment.get("provider"), "deployment.provider")
    provider_mode = provider.get("mode", "disabled")
    if provider_mode not in _PROVIDER_MODES:
        raise ValueError(
            "deployment.provider.mode must be create, require-existing, or disabled"
        )
    if provider_mode != "disabled":
        for key in ("project", "experiment"):
            value = provider.get(key)
            if not isinstance(value, str) or _SAFE_PROVIDER_CONTEXT.fullmatch(value) is None:
                raise ValueError(
                    f"deployment.provider.{key} is required and must contain safe context characters"
                )
        experiment_duration = provider.get("experiment_duration", "4h")
        if not isinstance(experiment_duration, str) or _DURATION.fullmatch(experiment_duration) is None:
            raise ValueError(
                "deployment.provider.experiment_duration must look like 30m or 4h"
            )
        provider["experiment_duration"] = experiment_duration
    provider["mode"] = provider_mode
    deployment["provider"] = provider

    r2lab = _mapping(
        deployment.get("r2lab_reservation"), "deployment.r2lab_reservation"
    )
    legacy_default = deployment.get("platform") == "r2lab"
    r2lab_enabled = _legacy_enabled(
        r2lab.get("enabled"), "deployment.r2lab_reservation.enabled", legacy_default
    )
    r2lab_mode = r2lab.get("mode")
    if r2lab_mode is None:
        r2lab_mode = "book" if r2lab_enabled else "disabled"
    if r2lab_mode not in _R2LAB_RESERVATION_MODES:
        raise ValueError(
            "deployment.r2lab_reservation.mode must be book, require-existing, or disabled"
        )
    if deployment.get("platform") != "r2lab" and r2lab_mode != "disabled":
        raise ValueError("R2Lab reservation must be disabled unless deployment.platform is r2lab")
    if "enabled" in r2lab and r2lab_enabled != (r2lab_mode != "disabled"):
        raise ValueError(
            "deployment.r2lab_reservation.enabled conflicts with deployment.r2lab_reservation.mode"
        )
    r2lab_duration = r2lab.get("duration_minutes", 120)
    if (
        not isinstance(r2lab_duration, int)
        or isinstance(r2lab_duration, bool)
        or r2lab_duration < 1
    ):
        raise ValueError(
            "deployment.r2lab_reservation.duration_minutes must be a positive integer"
        )
    r2lab.update(
        {
            "mode": r2lab_mode,
            "enabled": r2lab_mode != "disabled",
            "duration_minutes": r2lab_duration,
        }
    )
    deployment["r2lab_reservation"] = r2lab


def _validate_ssh_policy(deployment: dict, host_vars: dict) -> None:
    for host, values in host_vars.items():
        if values is not None and not isinstance(values, dict):
            raise ValueError(f"deployment.host_vars.{host} must be a mapping")
        values = values or {}
        for key in ("ansible_ssh_common_args", "ansible_ssh_extra_args"):
            if _INSECURE_SSH.search(str(values.get(key, ""))):
                raise ValueError(
                    f"deployment.host_vars.{host}.{key} disables SSH server verification"
                )
        for key in ("ansible_host_key_checking", "ansible_ssh_host_key_checking"):
            if key in values and str(values[key]).strip().lower() in {
                "0",
                "false",
                "no",
                "off",
            }:
                raise ValueError(
                    f"deployment.host_vars.{host}.{key} cannot disable SSH server verification"
                )

    ansible_vars = deployment.get("ansible_vars", {})
    if ansible_vars is not None and not isinstance(ansible_vars, dict):
        raise ValueError("deployment.ansible_vars must be a mapping when provided")
    ansible_vars = ansible_vars or {}
    for key in ("ansible_ssh_common_args", "ansible_ssh_extra_args"):
        if _INSECURE_SSH.search(str(ansible_vars.get(key, ""))):
            raise ValueError(f"deployment.ansible_vars.{key} disables SSH server verification")
    for key in ("ansible_host_key_checking", "ansible_ssh_host_key_checking"):
        if key in ansible_vars and str(ansible_vars[key]).strip().lower() in {
            "0",
            "false",
            "no",
            "off",
        }:
            raise ValueError(
                f"deployment.ansible_vars.{key} cannot disable SSH server verification"
            )


def load_scenario(path: str | Path, *, deployment_only: bool = False) -> dict:
    source = Path(path).resolve()
    with source.open(encoding="utf-8") as stream:
        data = yaml.safe_load(stream)
    if not isinstance(data, dict):
        raise ValueError("scenario must be a mapping")
    if not isinstance(data.get("deployment"), dict):
        raise ValueError("scenario requires mapping: deployment")
    if deployment_only:
        data = {"deployment": data["deployment"]}
    dep = data["deployment"]

    legacy = sorted(key for key in ("profile", "profile_file", "ue_profiles") if key in dep)
    if legacy:
        raise ValueError(
            "legacy deployment fields are no longer supported: "
            + ", ".join(legacy)
            + "; use network_profile and explicit ue_slices"
        )

    if dep.get("core") not in SUPPORTED_CORES:
        raise ValueError("unsupported core")
    if str(dep.get("ran", "")).lower() not in SUPPORTED_RANS:
        raise ValueError("unsupported RAN")
    if dep.get("platform") not in SUPPORTED_PLATFORMS:
        raise ValueError(
            "unsupported platform; supported platforms are: "
            + ", ".join(sorted(SUPPORTED_PLATFORMS))
        )
    if dep.get("platform") == "r2lab" and dep.get("ru") not in SUPPORTED_R2LAB_RADIOS:
        raise ValueError(
            "unsupported R2Lab radio; supported radios are: "
            + ", ".join(sorted(SUPPORTED_R2LAB_RADIOS))
        )
    if dep.get("radio") not in (None, {}):
        raise ValueError(
            "deployment.radio is not a supported effective configuration surface; "
            "use documented deployment fields/ansible_vars that are actually rendered"
        )

    if "r2lab_experiment_nodes" in dep:
        raise ValueError(
            "deployment.r2lab_experiment_nodes is disabled; auxiliary R2Lab "
            "sensor/edge/RF hosts are not part of the current testbed contract"
        )

    nodes = dep.get("nodes")
    if not isinstance(nodes, dict):
        raise ValueError("deployment.nodes must be a mapping")
    for role in ("core", "ran"):
        if not isinstance(nodes.get(role), str) or not nodes[role]:
            raise ValueError(f"deployment.nodes.{role} must be a non-empty host name")
    if "broker" in nodes and (not isinstance(nodes["broker"], str) or not nodes["broker"]):
        raise ValueError("deployment.nodes.broker must be a non-empty host name when provided")

    split_transport = nodes["core"] != nodes["ran"]
    if "bridge_enabled" in dep:
        if not isinstance(dep["bridge_enabled"], bool):
            raise ValueError("deployment.bridge_enabled must be boolean when provided")
        if dep["bridge_enabled"] != split_transport:
            raise ValueError(
                "deployment.bridge_enabled is derived from node placement and cannot "
                "override the authoritative transport mode"
            )
    dep["bridge_enabled"] = split_transport

    profile_name = dep.get("network_profile")
    if not isinstance(profile_name, str) or not _SAFE_PROFILE_NAME.fullmatch(profile_name):
        raise ValueError("deployment.network_profile must be a safe non-empty profile name")

    ues = dep.get("ues", [])
    if (
        not isinstance(ues, list)
        or not ues
        or not all(
            isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name)
            for name in ues
        )
    ):
        raise ValueError("deployment.ues must be a non-empty list of names")
    if len(ues) != len(set(ues)):
        raise ValueError("deployment.ues must contain unique names")

    _normalize_reservation_policy(dep)

    host_vars = dep.get("host_vars", {})
    if host_vars is not None and not isinstance(host_vars, dict):
        raise ValueError("deployment.host_vars must be a mapping when provided")
    host_vars = host_vars or {}
    _validate_ssh_policy(dep, host_vars)
    for name in ues:
        values = host_vars.get(name, {}) or {}
        conflicts = sorted(_TRANSPORT_KEYS & set(values))
        if conflicts:
            raise ValueError(
                f"deployment.host_vars.{name} cannot override canonical UE transport fields: "
                + ", ".join(conflicts)
                + "; configure UE transport in deployment/group_vars/all/ue_catalog.yaml"
            )

    for key in ("entrypoint", "config"):
        value = data.get("experiment", {}).get(key)
        if value:
            data["experiment"][key] = str((source.parent / value).resolve())
    for key in ("network_profile_file", "ue_catalog_file", "topology_file"):
        if dep.get(key):
            dep[key] = str((source.parent / dep[key]).resolve())

    network_profile_source = _network_profile_source(dep)
    if not network_profile_source.is_file():
        raise ValueError(f"network profile not found: {network_profile_source}")
    network_profile = yaml.safe_load(network_profile_source.read_text(encoding="utf-8"))
    validate_network_profile(network_profile)

    catalog_source = _ue_catalog_source(dep)
    if not catalog_source.is_file():
        raise ValueError(f"UE catalog not found: {catalog_source}")
    catalog = yaml.safe_load(catalog_source.read_text(encoding="utf-8"))
    validate_ue_catalog(catalog)
    catalog_ues = catalog["ues"]
    for name in ues:
        if name not in catalog_ues:
            raise ValueError(f"selected UE {name!r} is absent from the UE catalog")
        ue_platform = str(catalog_ues[name].get("platform", "")).lower()
        if ue_platform != dep["platform"]:
            raise ValueError(
                f"selected UE {name!r} belongs to platform {ue_platform!r}, "
                f"not {dep['platform']!r}"
            )

    _validate_selected_topology(dep)
    validate_ue_slice_assignments(dep.get("ue_slices"), ues, network_profile)

    data["_source_directory"] = str(source.parent)
    return data


def redacted(data: dict) -> dict:
    clean = copy.deepcopy(data)
    clean.pop("_source_directory", None)
    secret = re.compile(
        r"password|secret|token|credential|private_key|full_key|(?:^|_)opc(?:$|_)",
        re.I,
    )

    def walk(value):
        if isinstance(value, dict):
            return {
                k: ("<redacted>" if secret.search(str(k)) else walk(v))
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [walk(v) for v in value]
        return value

    return walk(clean)
