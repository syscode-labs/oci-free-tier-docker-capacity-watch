#!/usr/bin/env python3
"""Provision OCI Always Free resources in a dedicated compartment with retry logic.

- Creates/uses a dedicated compartment and basic public network stack.
- Reads compute/LB profile defaults from a JSON profile file.
- Launches VM.Standard.A1.Flex and VM.Standard.E2.1.Micro instances.
- Retries launches on capacity errors until targets are met.
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
import math
from pathlib import Path
from typing import Any

import oci
from oci.exceptions import ServiceError
from oci.pagination import list_call_get_all_results


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


class OciCliError(RuntimeError):
    pass


class OciCli:
    def __init__(self, profile: str, region: str | None = None, config: dict[str, Any] | None = None) -> None:
        self.profile = profile
        self.region = region
        config_file = os.environ.get("OCI_CONFIG_FILE")
        self.config = config or oci.config.from_file(file_location=config_file, profile_name=profile)
        if region:
            self.config["region"] = region
        self.identity_client = oci.identity.IdentityClient(self.config)
        self.network_client = oci.core.VirtualNetworkClient(self.config)
        self.compute_client = oci.core.ComputeClient(self.config)
        self.lb_client = oci.load_balancer.LoadBalancerClient(self.config)

    @staticmethod
    def _flag(args: list[str], name: str, default: str | None = None) -> str | None:
        try:
            idx = args.index(name)
        except ValueError:
            return default
        if idx + 1 >= len(args):
            raise OciCliError(f"Missing value for {name}")
        return args[idx + 1]

    @staticmethod
    def _require_flag(args: list[str], name: str) -> str:
        value = OciCli._flag(args, name)
        if value is None:
            raise OciCliError(f"Missing required argument: {name}")
        return value

    @staticmethod
    def _to_bool(value: str | None, default: bool = False) -> bool:
        if value is None:
            return default
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}

    @staticmethod
    def _to_cli_dict(value: Any) -> Any:
        if isinstance(value, dict):
            return {key.replace("_", "-"): OciCli._to_cli_dict(val) for key, val in value.items()}
        if isinstance(value, list):
            return [OciCli._to_cli_dict(item) for item in value]
        return value

    def _data(self, payload: Any) -> dict[str, Any]:
        return {"data": self._to_cli_dict(oci.util.to_dict(payload))}

    def _list_all(self, fn: Any, **kwargs: Any) -> dict[str, Any]:
        payload = list_call_get_all_results(fn, **kwargs).data
        return self._data(payload)

    def _load_balancer_create_details(self, args: list[str]) -> Any:
        shape_details_raw = json.loads(self._require_flag(args, "--shape-details"))
        subnet_ids = json.loads(self._require_flag(args, "--subnet-ids"))
        shape_details = oci.load_balancer.models.ShapeDetails(
            minimum_bandwidth_in_mbps=int(shape_details_raw["minimumBandwidthInMbps"]),
            maximum_bandwidth_in_mbps=int(shape_details_raw["maximumBandwidthInMbps"]),
        )
        return oci.load_balancer.models.CreateLoadBalancerDetails(
            compartment_id=self._require_flag(args, "--compartment-id"),
            display_name=self._require_flag(args, "--display-name"),
            shape_name=self._require_flag(args, "--shape-name"),
            shape_details=shape_details,
            subnet_ids=subnet_ids,
            is_private=self._to_bool(self._flag(args, "--is-private"), default=False),
        )

    def _launch_instance_details(self, args: list[str]) -> Any:
        ssh_key_file = self._require_flag(args, "--ssh-authorized-keys-file")
        ssh_public_key = Path(ssh_key_file).read_text(encoding="utf-8").strip()
        launch_details = oci.core.models.LaunchInstanceDetails(
            availability_domain=self._require_flag(args, "--availability-domain"),
            compartment_id=self._require_flag(args, "--compartment-id"),
            shape=self._require_flag(args, "--shape"),
            display_name=self._require_flag(args, "--display-name"),
            source_details=oci.core.models.InstanceSourceViaImageDetails(
                source_type="image",
                image_id=self._require_flag(args, "--image-id"),
                boot_volume_size_in_gbs=int(self._require_flag(args, "--boot-volume-size-in-gbs")),
            ),
            create_vnic_details=oci.core.models.CreateVnicDetails(
                subnet_id=self._require_flag(args, "--subnet-id"),
                assign_public_ip=self._to_bool(self._flag(args, "--assign-public-ip"), default=True),
            ),
            metadata={"ssh_authorized_keys": ssh_public_key},
        )
        shape_config = self._flag(args, "--shape-config")
        if shape_config:
            shape_config_data = json.loads(shape_config)
            launch_details.shape_config = oci.core.models.LaunchInstanceShapeConfigDetails(
                ocpus=float(shape_config_data["ocpus"]),
                memory_in_gbs=float(shape_config_data["memoryInGBs"]),
            )
        return launch_details

    def run(self, args: list[str], expect_json: bool = True) -> Any:
        try:
            command = tuple(args[:3])
            if command == ("iam", "compartment", "list"):
                return self._list_all(
                    self.identity_client.list_compartments,
                    compartment_id=self._require_flag(args, "--compartment-id"),
                    name=self._flag(args, "--name"),
                    access_level=self._flag(args, "--access-level"),
                    compartment_id_in_subtree=self._to_bool(self._flag(args, "--compartment-id-in-subtree")),
                    lifecycle_state=self._flag(args, "--lifecycle-state"),
                )
            if command == ("iam", "compartment", "create"):
                details = oci.identity.models.CreateCompartmentDetails(
                    compartment_id=self._require_flag(args, "--compartment-id"),
                    name=self._require_flag(args, "--name"),
                    description=self._require_flag(args, "--description"),
                )
                return self._data(self.identity_client.create_compartment(details).data)
            if command == ("iam", "availability-domain", "list"):
                return self._list_all(
                    self.identity_client.list_availability_domains,
                    compartment_id=self._require_flag(args, "--compartment-id"),
                )
            if command == ("network", "vcn", "list"):
                return self._list_all(
                    self.network_client.list_vcns,
                    compartment_id=self._require_flag(args, "--compartment-id"),
                )
            if command == ("network", "vcn", "create"):
                details = oci.core.models.CreateVcnDetails(
                    compartment_id=self._require_flag(args, "--compartment-id"),
                    display_name=self._require_flag(args, "--display-name"),
                    cidr_blocks=[self._require_flag(args, "--cidr-block")],
                    dns_label=self._require_flag(args, "--dns-label"),
                )
                return self._data(self.network_client.create_vcn(details).data)
            if command == ("network", "internet-gateway", "list"):
                return self._list_all(
                    self.network_client.list_internet_gateways,
                    compartment_id=self._require_flag(args, "--compartment-id"),
                )
            if command == ("network", "internet-gateway", "create"):
                details = oci.core.models.CreateInternetGatewayDetails(
                    compartment_id=self._require_flag(args, "--compartment-id"),
                    vcn_id=self._require_flag(args, "--vcn-id"),
                    display_name=self._require_flag(args, "--display-name"),
                    is_enabled=self._to_bool(self._flag(args, "--is-enabled"), default=True),
                )
                return self._data(self.network_client.create_internet_gateway(details).data)
            if command == ("network", "route-table", "list"):
                return self._list_all(
                    self.network_client.list_route_tables,
                    compartment_id=self._require_flag(args, "--compartment-id"),
                )
            if command == ("network", "route-table", "create"):
                route_rules_raw = json.loads(self._require_flag(args, "--route-rules"))
                route_rules = [
                    oci.core.models.RouteRule(
                        destination=rule["destination"],
                        destination_type=rule["destinationType"],
                        network_entity_id=rule["networkEntityId"],
                    )
                    for rule in route_rules_raw
                ]
                details = oci.core.models.CreateRouteTableDetails(
                    compartment_id=self._require_flag(args, "--compartment-id"),
                    vcn_id=self._require_flag(args, "--vcn-id"),
                    display_name=self._require_flag(args, "--display-name"),
                    route_rules=route_rules,
                )
                return self._data(self.network_client.create_route_table(details).data)
            if command == ("network", "security-list", "list"):
                return self._list_all(
                    self.network_client.list_security_lists,
                    compartment_id=self._require_flag(args, "--compartment-id"),
                )
            if command == ("network", "security-list", "create"):
                ingress_raw = json.loads(self._require_flag(args, "--ingress-security-rules"))
                egress_raw = json.loads(self._require_flag(args, "--egress-security-rules"))
                ingress_rules = [
                    oci.core.models.IngressSecurityRule(
                        source=rule["source"],
                        source_type=rule.get("sourceType", "CIDR_BLOCK"),
                        protocol=rule["protocol"],
                        tcp_options=oci.core.models.TcpOptions(
                            destination_port_range=oci.core.models.PortRange(
                                min=int(rule["tcpOptions"]["destinationPortRange"]["min"]),
                                max=int(rule["tcpOptions"]["destinationPortRange"]["max"]),
                            )
                        )
                        if "tcpOptions" in rule
                        else None,
                    )
                    for rule in ingress_raw
                ]
                egress_rules = [
                    oci.core.models.EgressSecurityRule(
                        destination=rule["destination"],
                        destination_type=rule.get("destinationType", "CIDR_BLOCK"),
                        protocol=rule["protocol"],
                        tcp_options=oci.core.models.TcpOptions(
                            destination_port_range=oci.core.models.PortRange(
                                min=int(rule["tcpOptions"]["destinationPortRange"]["min"]),
                                max=int(rule["tcpOptions"]["destinationPortRange"]["max"]),
                            )
                        )
                        if "tcpOptions" in rule
                        else None,
                    )
                    for rule in egress_raw
                ]
                details = oci.core.models.CreateSecurityListDetails(
                    compartment_id=self._require_flag(args, "--compartment-id"),
                    vcn_id=self._require_flag(args, "--vcn-id"),
                    display_name=self._require_flag(args, "--display-name"),
                    ingress_security_rules=ingress_rules,
                    egress_security_rules=egress_rules,
                )
                return self._data(self.network_client.create_security_list(details).data)
            if command == ("network", "subnet", "list"):
                return self._list_all(
                    self.network_client.list_subnets,
                    compartment_id=self._require_flag(args, "--compartment-id"),
                )
            if command == ("network", "subnet", "create"):
                details = oci.core.models.CreateSubnetDetails(
                    compartment_id=self._require_flag(args, "--compartment-id"),
                    vcn_id=self._require_flag(args, "--vcn-id"),
                    display_name=self._require_flag(args, "--display-name"),
                    cidr_block=self._require_flag(args, "--cidr-block"),
                    dns_label=self._require_flag(args, "--dns-label"),
                    route_table_id=self._require_flag(args, "--route-table-id"),
                    security_list_ids=json.loads(self._require_flag(args, "--security-list-ids")),
                )
                return self._data(self.network_client.create_subnet(details).data)
            if command == ("network", "private-ip", "list"):
                return self._list_all(
                    self.network_client.list_private_ips,
                    vnic_id=self._flag(args, "--vnic-id"),
                    subnet_id=self._flag(args, "--subnet-id"),
                    ip_address=self._flag(args, "--ip-address"),
                )
            if command == ("network", "public-ip", "create"):
                details = oci.core.models.CreatePublicIpDetails(
                    compartment_id=self._require_flag(args, "--compartment-id"),
                    lifetime="RESERVED",
                    display_name=self._require_flag(args, "--display-name"),
                    private_ip_id=self._require_flag(args, "--private-ip-id"),
                )
                return self._data(self.network_client.create_public_ip(details).data)
            if command == ("network", "public-ip", "get"):
                return self._data(
                    self.network_client.get_public_ip(
                        public_ip_id=self._require_flag(args, "--public-ip-id")
                    ).data
                )
            if command == ("lb", "load-balancer", "get"):
                return self._data(
                    self.lb_client.get_load_balancer(
                        load_balancer_id=self._require_flag(args, "--load-balancer-id")
                    ).data
                )
            if command == ("lb", "load-balancer", "list"):
                return self._list_all(
                    self.lb_client.list_load_balancers,
                    compartment_id=self._require_flag(args, "--compartment-id"),
                )
            if command == ("lb", "load-balancer", "create"):
                details = self._load_balancer_create_details(args)
                return self._data(self.lb_client.create_load_balancer(details).data)
            if command == ("compute", "image", "list"):
                return self._list_all(
                    self.compute_client.list_images,
                    compartment_id=self._require_flag(args, "--compartment-id"),
                    operating_system=self._flag(args, "--operating-system"),
                    operating_system_version=self._flag(args, "--operating-system-version"),
                    shape=self._flag(args, "--shape"),
                    sort_by=self._flag(args, "--sort-by"),
                    sort_order=self._flag(args, "--sort-order"),
                )
            if command == ("compute", "instance", "list"):
                return self._list_all(
                    self.compute_client.list_instances,
                    compartment_id=self._require_flag(args, "--compartment-id"),
                )
            if command == ("compute", "vnic-attachment", "list"):
                return self._list_all(
                    self.compute_client.list_vnic_attachments,
                    compartment_id=self._require_flag(args, "--compartment-id"),
                    instance_id=self._flag(args, "--instance-id"),
                )
            if command == ("compute", "compute-capacity-report", "create"):
                shape_availabilities_raw = json.loads(self._require_flag(args, "--shape-availabilities"))
                shape_availabilities = []
                for item in shape_availabilities_raw:
                    cfg = item.get("instance-shape-config")
                    cfg_model = None
                    if cfg:
                        cfg_model = oci.core.models.CapacityReportInstanceShapeConfig(
                            ocpus=float(cfg["ocpus"]),
                            memory_in_gbs=float(cfg["memory-in-gbs"]),
                        )
                    shape_availabilities.append(
                        oci.core.models.CreateCapacityReportShapeAvailabilityDetails(
                            instance_shape=item["instance-shape"],
                            instance_shape_config=cfg_model,
                        )
                    )
                details = oci.core.models.CreateComputeCapacityReportDetails(
                    availability_domain=self._require_flag(args, "--availability-domain"),
                    compartment_id=self._require_flag(args, "--compartment-id"),
                    shape_availabilities=shape_availabilities,
                )
                return self._data(self.compute_client.create_compute_capacity_report(details).data)
            if command == ("compute", "instance", "launch"):
                details = self._launch_instance_details(args)
                return self._data(self.compute_client.launch_instance(details).data)
            raise OciCliError(f"Unsupported OCI command mapping: {' '.join(args)}")
        except ServiceError as exc:
            message = exc.message or str(exc)
            if exc.code:
                message = f"{exc.code}: {message}"
            raise OciCliError(message) from exc
        except OciCliError:
            raise
        except Exception as exc:
            raise OciCliError(str(exc)) from exc


CAPACITY_PATTERNS = [
    "Out of host capacity",
    "out of host capacity",
    "Out of capacity",
    "OutOfHostCapacity",
]

THROTTLE_PATTERNS = [
    "TooManyRequests",
    "429",
    "throttl",
    "rate limit",
]

TRANSIENT_PATTERNS = [
    "ServiceUnavailable",
    "InternalError",
    "GatewayTimeout",
    "timeout",
    "temporar",
]

AUTH_PATTERNS = [
    "NotAuthenticated",
    "NotAuthorized",
    "Unauthorized",
    "Forbidden",
]

QUOTA_PATTERNS = [
    "LimitExceeded",
    "QuotaExceeded",
    "OutOfQuota",
]


def is_capacity_error(error_text: str) -> bool:
    return any(pat in error_text for pat in CAPACITY_PATTERNS)


def classify_oci_error(error_text: str) -> str:
    lowered = error_text.lower()
    if any(pat.lower() in lowered for pat in CAPACITY_PATTERNS):
        return "capacity"
    if any(pat.lower() in lowered for pat in THROTTLE_PATTERNS):
        return "throttle"
    if any(pat.lower() in lowered for pat in TRANSIENT_PATTERNS):
        return "transient"
    if any(pat.lower() in lowered for pat in AUTH_PATTERNS):
        return "auth"
    if any(pat.lower() in lowered for pat in QUOTA_PATTERNS):
        return "quota"
    return "other"


def read_profile_values(profile: str) -> dict[str, str]:
    config_path = Path(os.environ.get("OCI_CONFIG_FILE", str(Path.home() / ".oci" / "config")))
    parser = configparser.ConfigParser()
    parser.read(config_path)

    normalized = profile.strip()
    if normalized in parser:
        section_name = normalized
    else:
        section_name = ""
        for candidate in parser.sections():
            if candidate.strip().lower() == normalized.lower():
                section_name = candidate
                break
        if not section_name:
            available = ", ".join(parser.sections()) or "(none)"
            raise RuntimeError(
                f"Profile '{profile}' not found in {config_path}. Available: {available}"
            )

    section = parser[section_name]
    required = ["tenancy", "user", "region"]
    missing = [key for key in required if key not in section]
    if missing:
        raise RuntimeError(f"Profile '{profile}' missing required keys: {', '.join(missing)}")

    return {
        "tenancy": section["tenancy"].strip(),
        "user": section["user"].strip(),
        "region": section["region"].strip(),
    }


def parse_bool(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def load_profile_defaults(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    required = [
        "ampere_node_names",
        "ampere_ocpus_per_instance",
        "ampere_memory_per_instance",
        "ampere_boot_volume_size",
        "micro_node_names",
        "micro_boot_volume_size",
        "enable_free_lb",
        "lb_display_name",
    ]
    missing = [k for k in required if k not in data]
    if missing:
        raise RuntimeError(f"Profile defaults file '{path}' missing keys: {', '.join(missing)}")

    for key in ("ampere_node_names", "micro_node_names"):
        if not isinstance(data[key], list) or not all(isinstance(n, str) for n in data[key]):
            raise RuntimeError(f"Profile key '{key}' must be a list of strings")

    for key in ("ampere_boot_volume_size", "micro_boot_volume_size"):
        if not isinstance(data[key], int) or data[key] <= 0:
            raise RuntimeError(f"Profile key '{key}' must be a positive integer")

    for key in ("ampere_ocpus_per_instance", "ampere_memory_per_instance"):
        if not isinstance(data[key], (int, float)) or float(data[key]) <= 0:
            raise RuntimeError(f"Profile key '{key}' must be a positive number")

    if not isinstance(data["enable_free_lb"], bool):
        raise RuntimeError("Profile key 'enable_free_lb' must be true/false")

    if not isinstance(data["lb_display_name"], str) or not data["lb_display_name"].strip():
        raise RuntimeError("Profile key 'lb_display_name' must be a non-empty string")

    return data


@dataclass
class AccountState:
    profile: str
    compartment_id: str
    existing_subnet_id: str | None
    report_output: Path
    ampere_names: list[str]
    micro_names: list[str]
    enable_free_lb: bool
    lb_display_name: str
    # mutable per-run tracking
    done: bool = False
    created_ampere: list[tuple[str, str, str]] = field(default_factory=list)  # (name, instance_id, pip_id)
    created_micro: list[tuple[str, str, str]] = field(default_factory=list)
    networking_ids: dict[str, str] | None = None
    lb_id: str | None = None
    subnet_id: str | None = None


def load_accounts(path: Path, defaults: dict[str, Any]) -> list[AccountState]:
    """Load accounts.json and merge with profile defaults."""
    entries = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(entries, list) or len(entries) == 0:
        raise RuntimeError(f"accounts file '{path}' must contain at least one account")

    states: list[AccountState] = []
    for i, entry in enumerate(entries):
        profile = entry.get("profile")
        compartment_id = entry.get("compartment_id")
        if not profile:
            raise RuntimeError(f"accounts[{i}] missing required key 'profile'")
        if not compartment_id:
            raise RuntimeError(f"accounts[{i}] missing required key 'compartment_id'")

        report_output = entry.get("report_output", f"./state/{profile}-import.tf")
        states.append(AccountState(
            profile=profile,
            compartment_id=compartment_id,
            existing_subnet_id=entry.get("existing_subnet_id"),
            report_output=Path(report_output),
            ampere_names=entry.get("ampere_node_names", defaults["ampere_node_names"]),
            micro_names=entry.get("micro_node_names", defaults["micro_node_names"]),
            enable_free_lb=entry.get("enable_free_lb", defaults["enable_free_lb"]),
            lb_display_name=entry.get("lb_display_name", defaults["lb_display_name"]),
        ))
    return states


def resolve_ssh_public_key(path_value: str) -> str:
    candidates = [
        os.path.expanduser(path_value),
        str(Path.home() / ".ssh" / "id_ed25519.pub"),
        str(Path.home() / ".ssh" / "id_rsa.pub"),
        str(Path.home() / ".ssh" / "id_ecdsa.pub"),
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    raise RuntimeError(
        "No SSH public key file found. Checked: " + ", ".join(candidates)
    )


def first_match(items: list[dict[str, Any]], key: str, value: str) -> dict[str, Any] | None:
    for item in items:
        if item.get(key) == value:
            return item
    return None


def ensure_compartment(oci: OciCli, tenancy_ocid: str, name: str) -> str:
    compartments = oci.run(
        [
            "iam",
            "compartment",
            "list",
            "--compartment-id",
            tenancy_ocid,
            "--name",
            name,
            "--all",
            "--access-level",
            "ACCESSIBLE",
            "--compartment-id-in-subtree",
            "true",
            "--lifecycle-state",
            "ACTIVE",
        ]
    )["data"]

    for comp in compartments:
        if comp.get("name") == name:
            log(f"Using existing compartment '{name}' ({comp['id']})")
            return comp["id"]

    created = oci.run(
        [
            "iam",
            "compartment",
            "create",
            "--compartment-id",
            tenancy_ocid,
            "--name",
            name,
            "--description",
            "Dedicated compartment for OCI free-tier manager retry provisioning",
        ]
    )["data"]
    compartment_id = created["id"]
    log(f"Created compartment '{name}' ({compartment_id}), waiting for ACTIVE")

    while True:
        listed = oci.run(
            [
                "iam",
                "compartment",
                "list",
                "--compartment-id",
                tenancy_ocid,
                "--name",
                name,
                "--all",
                "--access-level",
                "ACCESSIBLE",
                "--compartment-id-in-subtree",
                "true",
            ]
        )["data"]
        if listed:
            active = [c for c in listed if c.get("lifecycle-state") == "ACTIVE"]
            if active:
                return active[0]["id"]
        time.sleep(5)


def ensure_vcn(oci: OciCli, compartment_id: str, name: str, cidr: str, dns_label: str) -> str:
    vcns = oci.run(["network", "vcn", "list", "--compartment-id", compartment_id, "--all"])["data"]
    vcn = first_match(vcns, "display-name", name)
    if vcn:
        log(f"Using existing VCN '{name}' ({vcn['id']})")
        return vcn["id"]

    created = oci.run(
        [
            "network",
            "vcn",
            "create",
            "--compartment-id",
            compartment_id,
            "--display-name",
            name,
            "--cidr-block",
            cidr,
            "--dns-label",
            dns_label,
        ]
    )["data"]
    log(f"Created VCN '{name}' ({created['id']})")
    return created["id"]


def ensure_igw(oci: OciCli, compartment_id: str, vcn_id: str, name: str) -> str:
    igws = oci.run(["network", "internet-gateway", "list", "--compartment-id", compartment_id, "--all"])["data"]
    for igw in igws:
        if igw.get("display-name") == name and igw.get("vcn-id") == vcn_id:
            log(f"Using existing IGW '{name}' ({igw['id']})")
            return igw["id"]

    created = oci.run(
        [
            "network",
            "internet-gateway",
            "create",
            "--compartment-id",
            compartment_id,
            "--vcn-id",
            vcn_id,
            "--display-name",
            name,
            "--is-enabled",
            "true",
        ]
    )["data"]
    log(f"Created IGW '{name}' ({created['id']})")
    return created["id"]


def ensure_route_table(oci: OciCli, compartment_id: str, vcn_id: str, igw_id: str, name: str) -> str:
    rts = oci.run(["network", "route-table", "list", "--compartment-id", compartment_id, "--all"])["data"]
    for rt in rts:
        if rt.get("display-name") == name and rt.get("vcn-id") == vcn_id:
            log(f"Using existing route table '{name}' ({rt['id']})")
            return rt["id"]

    route_rules = json.dumps(
        [{"destination": "0.0.0.0/0", "destinationType": "CIDR_BLOCK", "networkEntityId": igw_id}]
    )
    created = oci.run(
        [
            "network",
            "route-table",
            "create",
            "--compartment-id",
            compartment_id,
            "--vcn-id",
            vcn_id,
            "--display-name",
            name,
            "--route-rules",
            route_rules,
        ]
    )["data"]
    log(f"Created route table '{name}' ({created['id']})")
    return created["id"]


def ensure_security_list(oci: OciCli, compartment_id: str, vcn_id: str, name: str) -> str:
    sls = oci.run(["network", "security-list", "list", "--compartment-id", compartment_id, "--all"])["data"]
    for sl in sls:
        if sl.get("display-name") == name and sl.get("vcn-id") == vcn_id:
            log(f"Using existing security list '{name}' ({sl['id']})")
            return sl["id"]

    ingress = [
        {
            "protocol": "6",
            "source": "0.0.0.0/0",
            "tcpOptions": {"destinationPortRange": {"min": 22, "max": 22}},
        },
        {
            "protocol": "6",
            "source": "0.0.0.0/0",
            "tcpOptions": {"destinationPortRange": {"min": 80, "max": 80}},
        },
        {
            "protocol": "6",
            "source": "0.0.0.0/0",
            "tcpOptions": {"destinationPortRange": {"min": 443, "max": 443}},
        },
        {"protocol": "1", "source": "0.0.0.0/0"},
    ]
    egress = [{"protocol": "all", "destination": "0.0.0.0/0"}]

    created = oci.run(
        [
            "network",
            "security-list",
            "create",
            "--compartment-id",
            compartment_id,
            "--vcn-id",
            vcn_id,
            "--display-name",
            name,
            "--ingress-security-rules",
            json.dumps(ingress),
            "--egress-security-rules",
            json.dumps(egress),
        ]
    )["data"]
    log(f"Created security list '{name}' ({created['id']})")
    return created["id"]


def ensure_subnet(
    oci: OciCli,
    compartment_id: str,
    vcn_id: str,
    route_table_id: str,
    security_list_id: str,
    name: str,
    cidr: str,
    dns_label: str,
) -> str:
    subnets = oci.run(["network", "subnet", "list", "--compartment-id", compartment_id, "--all"])["data"]
    for subnet in subnets:
        if subnet.get("display-name") == name and subnet.get("vcn-id") == vcn_id:
            log(f"Using existing subnet '{name}' ({subnet['id']})")
            return subnet["id"]

    created = oci.run(
        [
            "network",
            "subnet",
            "create",
            "--compartment-id",
            compartment_id,
            "--vcn-id",
            vcn_id,
            "--display-name",
            name,
            "--cidr-block",
            cidr,
            "--dns-label",
            dns_label,
            "--route-table-id",
            route_table_id,
            "--security-list-ids",
            json.dumps([security_list_id]),
        ]
    )["data"]
    log(f"Created subnet '{name}' ({created['id']})")
    return created["id"]


def wait_load_balancer_active(oci: OciCli, lb_id: str, max_wait_seconds: int = 900) -> dict[str, Any]:
    waited = 0
    while waited <= max_wait_seconds:
        data = oci.run(["lb", "load-balancer", "get", "--load-balancer-id", lb_id])["data"]
        state = data.get("lifecycle-state", "")
        if state == "ACTIVE":
            return data
        if state in {"FAILED", "DELETED"}:
            raise RuntimeError(f"Load balancer entered terminal state: {state}")
        time.sleep(10)
        waited += 10
    raise RuntimeError("Timed out waiting for load balancer to become ACTIVE")


def ensure_free_tier_load_balancer(
    oci: OciCli,
    compartment_id: str,
    subnet_id: str,
    display_name: str,
) -> tuple[str, str | None]:
    lbs = oci.run(["lb", "load-balancer", "list", "--compartment-id", compartment_id, "--all"])["data"]
    for lb in lbs:
        if lb.get("display-name") == display_name:
            lb_id = lb["id"]
            log(f"Using existing Load Balancer '{display_name}' ({lb_id})")
            active = wait_load_balancer_active(oci, lb_id)
            ip_details = active.get("ip-address-details", [])
            ip_address = ip_details[0].get("ip-address") if ip_details else None
            return lb_id, ip_address

    shape_details = json.dumps(
        {
            "minimumBandwidthInMbps": 10,
            "maximumBandwidthInMbps": 10,
        }
    )
    created = oci.run(
        [
            "lb",
            "load-balancer",
            "create",
            "--compartment-id",
            compartment_id,
            "--display-name",
            display_name,
            "--shape-name",
            "flexible",
            "--shape-details",
            shape_details,
            "--subnet-ids",
            json.dumps([subnet_id]),
            "--is-private",
            "false",
        ]
    )["data"]
    lb_id = created["id"]
    log(f"Created Load Balancer '{display_name}' ({lb_id}), waiting for ACTIVE")
    active = wait_load_balancer_active(oci, lb_id)
    ip_details = active.get("ip-address-details", [])
    ip_address = ip_details[0].get("ip-address") if ip_details else None
    return lb_id, ip_address


def get_availability_domains(oci: OciCli, tenancy_ocid: str) -> list[str]:
    ads = oci.run(["iam", "availability-domain", "list", "--compartment-id", tenancy_ocid])["data"]
    return [ad["name"] for ad in ads]


def find_latest_image(oci: OciCli, compartment_id: str, shape: str) -> str:
    images = oci.run(
        [
            "compute",
            "image",
            "list",
            "--compartment-id",
            compartment_id,
            "--operating-system",
            "Canonical Ubuntu",
            "--operating-system-version",
            "22.04",
            "--shape",
            shape,
            "--sort-by",
            "TIMECREATED",
            "--sort-order",
            "DESC",
            "--all",
        ]
    )["data"]
    if not images:
        raise RuntimeError(f"No Ubuntu 22.04 image found for shape {shape}")
    return images[0]["id"]


def list_existing_instances(oci: OciCli, compartment_id: str, name_prefix: str, shape: str) -> list[dict[str, Any]]:
    instances = oci.run(["compute", "instance", "list", "--compartment-id", compartment_id, "--all"])["data"]
    keep_states = {"PROVISIONING", "RUNNING", "STARTING", "STOPPING", "STOPPED"}
    return [
        inst
        for inst in instances
        if inst.get("shape") == shape
        and inst.get("display-name", "").startswith(name_prefix)
        and inst.get("lifecycle-state") in keep_states
    ]


def capacity_available(
    oci: OciCli,
    tenancy_ocid: str,
    availability_domain: str,
    shape: str,
    shape_config: dict[str, Any] | None = None,
) -> bool:
    shape_availability: dict[str, Any] = {"instance-shape": shape}
    if shape_config:
        probe_ocpus = max(1, int(math.floor(float(shape_config["ocpus"]))))
        probe_memory = max(6.0, float(shape_config["memoryInGBs"]))
        shape_availability["instance-shape-config"] = {
            "ocpus": probe_ocpus,
            "memory-in-gbs": probe_memory,
        }

    try:
        report = oci.run(
            [
                "compute",
                "compute-capacity-report",
                "create",
                "--availability-domain",
                availability_domain,
                "--compartment-id",
                tenancy_ocid,
                "--shape-availabilities",
                json.dumps([shape_availability]),
            ]
        )
    except OciCliError as exc:
        category = classify_oci_error(str(exc))
        log(f"Capacity probe failed for {shape} in {availability_domain} [{category}]: {exc}")
        if category in {"capacity", "throttle", "transient"}:
            return False
        raise
    entries = report.get("data", {}).get("shape-availabilities", [])
    if not entries:
        return False
    return entries[0].get("availability-status") == "AVAILABLE"


def launch_instance(
    oci: OciCli,
    *,
    compartment_id: str,
    subnet_id: str,
    ad: str,
    name: str,
    shape: str,
    image_id: str,
    boot_size: int,
    ssh_key_file: str,
    shape_config: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    cmd = [
        "compute",
        "instance",
        "launch",
        "--availability-domain",
        ad,
        "--compartment-id",
        compartment_id,
        "--shape",
        shape,
        "--display-name",
        name,
        "--image-id",
        image_id,
        "--boot-volume-size-in-gbs",
        str(boot_size),
        "--subnet-id",
        subnet_id,
        "--assign-public-ip",
        "true",
        "--ssh-authorized-keys-file",
        ssh_key_file,
    ]

    if shape_config:
        cmd.extend(["--shape-config", json.dumps(shape_config)])

    try:
        data = oci.run(cmd)["data"]
        return True, data["id"]
    except OciCliError as exc:
        return False, str(exc)


def get_private_ip_id(oci: OciCli, compartment_id: str, instance_id: str) -> str:
    """Return the primary private IP OCID for an instance."""
    attachments = oci.run([
        "compute", "vnic-attachment", "list",
        "--compartment-id", compartment_id,
        "--instance-id", instance_id,
    ])["data"]
    attached = [a for a in attachments if a.get("lifecycle-state") == "ATTACHED"]
    if not attached:
        raise RuntimeError(f"No attached VNICs found for instance {instance_id}")
    vnic_id = attached[0]["vnic-id"]
    private_ips = oci.run(["network", "private-ip", "list", "--vnic-id", vnic_id])["data"]
    if not private_ips:
        raise RuntimeError(f"No private IPs found for VNIC {vnic_id}")
    return private_ips[0]["id"]


def create_reserved_public_ip(
    oci: OciCli, compartment_id: str, display_name: str, private_ip_id: str
) -> tuple[str, str]:
    """Create a RESERVED public IP, poll until ASSIGNED. Returns (ocid, ip_address)."""
    created = oci.run([
        "network", "public-ip", "create",
        "--compartment-id", compartment_id,
        "--display-name", display_name,
        "--private-ip-id", private_ip_id,
    ])["data"]
    pub_ip_id = created["id"]
    log(f"Reserved public IP '{display_name}' ({pub_ip_id}), waiting for ASSIGNED")
    for _ in range(30):  # up to 5 minutes
        data = oci.run(["network", "public-ip", "get", "--public-ip-id", pub_ip_id])["data"]
        if data.get("lifecycle-state") == "ASSIGNED":
            ip_address = data.get("ip-address", "")
            log(f"Reserved IP '{display_name}': {ip_address}")
            return pub_ip_id, ip_address
        time.sleep(10)
    raise RuntimeError(f"Timed out waiting for public IP {pub_ip_id} to become ASSIGNED")


def wait_for_instance_running(
    oci: OciCli, compartment_id: str, instance_id: str, max_wait_seconds: int = 600
) -> None:
    """Poll until instance is RUNNING."""
    waited = 0
    while waited <= max_wait_seconds:
        instances = oci.run(["compute", "instance", "list", "--compartment-id", compartment_id])["data"]
        for inst in instances:
            if inst.get("id") == instance_id:
                state = inst.get("lifecycle-state", "")
                if state == "RUNNING":
                    return
                if state in {"TERMINATED", "TERMINATING"}:
                    raise RuntimeError(f"Instance {instance_id} entered terminal state: {state}")
                break
        time.sleep(15)
        waited += 15
    raise RuntimeError(f"Timed out waiting for instance {instance_id} to become RUNNING")


def scan_available_ads(
    oci: OciCli,
    tenancy_ocid: str,
    ads: list[str],
    shape: str,
    shape_config: dict[str, Any] | None = None,
) -> list[str]:
    """Return the subset of ads where shape capacity is confirmed AVAILABLE."""
    available = []
    for ad in ads:
        ok = capacity_available(oci, tenancy_ocid, ad, shape, shape_config)
        log(f"Capacity {shape} in {ad}: {'AVAILABLE' if ok else 'unavailable'}")
        if ok:
            available.append(ad)
    return available


def generate_import_report(
    *,
    output_path: Path,
    networking: dict[str, str] | None,
    lb_id: str | None,
    ampere_instances: list[tuple[str, str, str]],
    micro_instances: list[tuple[str, str, str]],
) -> None:
    """Write a .tf file with tofu import{} blocks for all resources created this run.

    Drop into syscode-infra-private/oci/ and run:
      tofu apply -var-file=<account>.tfvars -chdir=module/tofu/oci
    """
    def block(addr: str, ocid: str) -> list[str]:
        return ["import {", f"  to = {addr}", f'  id = "{ocid}"', "}", ""]

    lines = [
        "# Generated by oci-free-tier-docker-capacity-watch",
        f"# {datetime.now().strftime('%Y-%m-%dT%H:%M:%S')}",
        "#",
        "# Drop this file into syscode-infra-private/oci/ and run:",
        "#   tofu apply -var-file=<account>.tfvars -chdir=module/tofu/oci",
        "",
    ]
    if networking:
        lines += block("oci_core_vcn.free_tier_vcn[0]", networking["vcn_id"])
        lines += block("oci_core_internet_gateway.free_tier_igw[0]", networking["igw_id"])
        lines += block("oci_core_route_table.free_tier_route_table[0]", networking["route_table_id"])
        lines += block("oci_core_security_list.free_tier_security_list[0]", networking["security_list_id"])
        lines += block("oci_core_subnet.free_tier_subnet[0]", networking["subnet_id"])
    if lb_id:
        lines += block("oci_load_balancer_load_balancer.free_tier_lb[0]", lb_id)
    for i, (name, instance_id, pip_id) in enumerate(ampere_instances):
        lines += block(f"oci_core_instance.ampere_instance[{i}]", instance_id)
        lines += block(f"oci_core_public_ip.ampere_instance[{i}]", pip_id)
    for i, (name, instance_id, pip_id) in enumerate(micro_instances):
        lines += block(f"oci_core_instance.micro_instance[{i}]", instance_id)
        lines += block(f"oci_core_public_ip.micro_instance[{i}]", pip_id)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")
    log(f"Import report written to {output_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Provision OCI Always Free resources with retry")
    parser.add_argument("--profile", default="gf78", help="OCI config profile to use")
    parser.add_argument("--compartment-name", default="gf78-free-tier-dedicated", help="Dedicated compartment name")
    parser.add_argument("--region", default="", help="OCI region override (default: from profile)")
    parser.add_argument("--ssh-key-file", default=str(Path.home() / ".ssh" / "id_rsa.pub"))
    parser.add_argument("--retry-seconds", type=int, default=300, help="Seconds between capacity retry cycles")
    parser.add_argument("--max-attempts", type=int, default=0, help="0 = retry forever, otherwise stop after N cycles")
    parser.add_argument(
        "--profile-defaults-file",
        default=os.environ.get("PROFILE_DEFAULTS_FILE", "/app/config/profile.defaults.json"),
        help="JSON file containing compute/LB defaults",
    )
    args = parser.parse_args()

    profile_values = read_profile_values(args.profile)
    region = args.region or profile_values["region"]
    oci = OciCli(profile=args.profile, region=region)

    ssh_key_file = resolve_ssh_public_key(args.ssh_key_file)

    profile_defaults = load_profile_defaults(Path(args.profile_defaults_file))
    ampere_target = int(profile_defaults["ampere_instance_count"])
    micro_target = int(profile_defaults["micro_instance_count"])

    log(f"Profile={args.profile} Region={region}")
    log(
        "VM profile from profile defaults: "
        f"ampere={ampere_target} ({profile_defaults['ampere_ocpus_per_instance']} OCPU/{profile_defaults['ampere_memory_per_instance']}GB each), "
        f"micro={micro_target}"
    )

    tenancy_ocid = profile_values["tenancy"]
    compartment_id = ensure_compartment(oci, tenancy_ocid, args.compartment_name)

    vcn_id = ensure_vcn(
        oci,
        compartment_id,
        name="free-tier-vcn",
        cidr="10.0.0.0/16",
        dns_label="freetier",
    )
    igw_id = ensure_igw(oci, compartment_id, vcn_id, "free-tier-igw")
    route_table_id = ensure_route_table(oci, compartment_id, vcn_id, igw_id, "free-tier-route-table")
    security_list_id = ensure_security_list(oci, compartment_id, vcn_id, "free-tier-security-list")
    subnet_id = ensure_subnet(
        oci,
        compartment_id,
        vcn_id,
        route_table_id,
        security_list_id,
        name="free-tier-subnet",
        cidr="10.0.1.0/24",
        dns_label="subnet",
    )
    enable_free_lb = parse_bool(profile_defaults["enable_free_lb"])
    if enable_free_lb:
        lb_id, lb_ip = ensure_free_tier_load_balancer(
            oci=oci,
            compartment_id=compartment_id,
            subnet_id=subnet_id,
            display_name=str(profile_defaults["lb_display_name"]),
        )
        log(f"Load Balancer: {lb_id}")
        if lb_ip:
            log(f"Load Balancer public IP: {lb_ip}")

    ads = get_availability_domains(oci, tenancy_ocid)
    if not ads:
        raise RuntimeError("No availability domains returned")

    ampere_image_id = find_latest_image(oci, compartment_id, "VM.Standard.A1.Flex")
    micro_image_id = find_latest_image(oci, compartment_id, "VM.Standard.E2.1.Micro")

    log(f"A1 image: {ampere_image_id}")
    log(f"Micro image: {micro_image_id}")

    attempt = 0
    while True:
        attempt += 1
        log(f"Launch cycle #{attempt}")

        existing_ampere = list_existing_instances(oci, compartment_id, "ampere-instance-", "VM.Standard.A1.Flex")
        existing_micro = list_existing_instances(oci, compartment_id, "micro-instance-", "VM.Standard.E2.1.Micro")

        log(f"Existing A1 instances: {len(existing_ampere)}/{ampere_target}")
        log(f"Existing Micro instances: {len(existing_micro)}/{micro_target}")

        for idx in range(len(existing_micro), micro_target):
            name = f"micro-instance-{idx + 1}"
            ad = ads[0]
            if not capacity_available(
                oci=oci,
                tenancy_ocid=tenancy_ocid,
                availability_domain=ad,
                shape="VM.Standard.E2.1.Micro",
            ):
                log(f"Capacity unavailable for VM.Standard.E2.1.Micro in {ad}, skipping launch for now")
                continue
            ok, detail = launch_instance(
                oci,
                compartment_id=compartment_id,
                subnet_id=subnet_id,
                ad=ad,
                name=name,
                shape="VM.Standard.E2.1.Micro",
                image_id=micro_image_id,
                boot_size=int(profile_defaults["micro_boot_volume_size"]),
                ssh_key_file=ssh_key_file,
            )
            if ok:
                log(f"Launched {name}: {detail}")
            else:
                category = classify_oci_error(detail)
                log(f"Launch failed for {name} [{category}]: {detail}")
                if category not in {"capacity", "throttle", "transient"}:
                    raise RuntimeError(f"{category} error launching {name}: {detail}")

        for idx in range(len(existing_ampere), ampere_target):
            name = f"ampere-instance-{idx + 1}"
            ad = ads[idx % len(ads)]
            shape_config = {
                "ocpus": float(profile_defaults["ampere_ocpus_per_instance"]),
                "memoryInGBs": float(profile_defaults["ampere_memory_per_instance"]),
            }
            if not capacity_available(
                oci=oci,
                tenancy_ocid=tenancy_ocid,
                availability_domain=ad,
                shape="VM.Standard.A1.Flex",
                shape_config=shape_config,
            ):
                log(f"Capacity unavailable for VM.Standard.A1.Flex in {ad}, skipping launch for now")
                continue
            ok, detail = launch_instance(
                oci,
                compartment_id=compartment_id,
                subnet_id=subnet_id,
                ad=ad,
                name=name,
                shape="VM.Standard.A1.Flex",
                image_id=ampere_image_id,
                boot_size=int(profile_defaults["ampere_boot_volume_size"]),
                ssh_key_file=ssh_key_file,
                shape_config=shape_config,
            )
            if ok:
                log(f"Launched {name}: {detail}")
            else:
                category = classify_oci_error(detail)
                log(f"Launch failed for {name} [{category}]: {detail}")
                if category not in {"capacity", "throttle", "transient"}:
                    raise RuntimeError(f"{category} error launching {name}: {detail}")

        existing_ampere = list_existing_instances(oci, compartment_id, "ampere-instance-", "VM.Standard.A1.Flex")
        existing_micro = list_existing_instances(oci, compartment_id, "micro-instance-", "VM.Standard.E2.1.Micro")

        if len(existing_ampere) >= ampere_target and len(existing_micro) >= micro_target:
            log("Target profile satisfied. Provisioning complete.")
            log(f"Compartment: {compartment_id}")
            log(f"VCN: {vcn_id}")
            log(f"Subnet: {subnet_id}")
            return 0

        if args.max_attempts > 0 and attempt >= args.max_attempts:
            log("Reached max attempts before satisfying target profile.")
            return 2

        log(f"Capacity not yet sufficient. Sleeping {args.retry_seconds}s before next cycle...")
        time.sleep(args.retry_seconds)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        log(f"ERROR: {exc}")
        sys.exit(1)
