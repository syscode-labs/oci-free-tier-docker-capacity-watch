from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "worker" / "provision_free_tier_retry.py"
SPEC = importlib.util.spec_from_file_location("provision_free_tier_retry", MODULE_PATH)
assert SPEC and SPEC.loader
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
SPEC.loader.exec_module(mod)


def test_read_profile_values_case_insensitive_and_trim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "config"
    cfg.write_text(
        """
[gf78]
user = user1
tenancy = ten1
region = eu-frankfurt-1
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OCI_CONFIG_FILE", str(cfg))

    values = mod.read_profile_values("  GF78  ")

    assert values == {"user": "user1", "tenancy": "ten1", "region": "eu-frankfurt-1"}


def test_load_profile_defaults_requires_keys(tmp_path: Path) -> None:
    defaults = tmp_path / "defaults.json"
    defaults.write_text('{"ampere_instance_count": 1}', encoding="utf-8")

    with pytest.raises(RuntimeError) as exc:
        mod.load_profile_defaults(defaults)

    assert "missing keys" in str(exc.value).lower()


def test_is_capacity_error_matches_known_patterns() -> None:
    assert mod.is_capacity_error("OutOfHostCapacity right now")
    assert not mod.is_capacity_error("Some unrelated error")


def test_classify_oci_error_categories() -> None:
    assert mod.classify_oci_error("OutOfHostCapacity on AD-1") == "capacity"
    assert mod.classify_oci_error("TooManyRequests: rate limit exceeded") == "throttle"
    assert mod.classify_oci_error("ServiceUnavailable temporary backend issue") == "transient"
    assert mod.classify_oci_error("NotAuthorizedOrNotFound") == "auth"
    assert mod.classify_oci_error("LimitExceeded for service quota") == "quota"
    assert mod.classify_oci_error("UnknownFailure random") == "other"


class _FakeIdentityClient:
    def list_compartments(self, **kwargs):  # noqa: ANN003
        assert kwargs["compartment_id"] == "ocid1.tenancy.oc1..example"
        return SimpleNamespace(
            data=[
                {
                    "id": "ocid1.compartment.oc1..abc",
                    "name": "Default",
                    "lifecycle_state": "ACTIVE",
                }
            ]
        )


class _FakeNetworkClient:
    pass


class _FakeComputeClient:
    def create_compute_capacity_report(self, details):
        items = details.shape_availabilities
        assert len(items) == 1
        assert items[0].instance_shape == "VM.Standard.A1.Flex"
        return SimpleNamespace(
            data={
                "shape_availabilities": [
                    {
                        "availability_status": "AVAILABLE",
                    }
                ]
            }
        )


class _FakeLbClient:
    pass


def _fake_list_call_get_all_results(fn, **kwargs):  # noqa: ANN001, ANN003
    return SimpleNamespace(data=fn(**kwargs).data)


def test_oci_sdk_mapping_list_and_capacity_report(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod.oci.identity, "IdentityClient", lambda _cfg: _FakeIdentityClient())
    monkeypatch.setattr(mod.oci.core, "VirtualNetworkClient", lambda _cfg: _FakeNetworkClient())
    monkeypatch.setattr(mod.oci.core, "ComputeClient", lambda _cfg: _FakeComputeClient())
    monkeypatch.setattr(mod.oci.load_balancer, "LoadBalancerClient", lambda _cfg: _FakeLbClient())
    monkeypatch.setattr(mod, "list_call_get_all_results", _fake_list_call_get_all_results)

    cli = mod.OciCli(profile="gf78", config={"region": "eu-frankfurt-1"})

    compartments = cli.run(
        [
            "iam",
            "compartment",
            "list",
            "--compartment-id",
            "ocid1.tenancy.oc1..example",
            "--access-level",
            "ACCESSIBLE",
            "--compartment-id-in-subtree",
            "true",
        ]
    )
    assert compartments["data"][0]["lifecycle-state"] == "ACTIVE"

    report = cli.run(
        [
            "compute",
            "compute-capacity-report",
            "create",
            "--availability-domain",
            "AD-1",
            "--compartment-id",
            "ocid1.tenancy.oc1..example",
            "--shape-availabilities",
            '[{"instance-shape":"VM.Standard.A1.Flex"}]',
        ]
    )
    assert report["data"]["shape-availabilities"][0]["availability-status"] == "AVAILABLE"


def test_load_profile_defaults_new_schema(tmp_path: Path) -> None:
    defaults = tmp_path / "defaults.json"
    defaults.write_text(
        json.dumps({
            "ampere_node_names": ["ampere1", "ampere2"],
            "ampere_ocpus_per_instance": 1,
            "ampere_memory_per_instance": 6,
            "ampere_boot_volume_size": 50,
            "micro_node_names": ["micro1"],
            "micro_boot_volume_size": 50,
            "enable_free_lb": False,
            "lb_display_name": "free-tier-lb",
        }),
        encoding="utf-8",
    )
    result = mod.load_profile_defaults(defaults)
    assert result["ampere_node_names"] == ["ampere1", "ampere2"]
    assert result["micro_node_names"] == ["micro1"]


def test_load_profile_defaults_rejects_old_schema(tmp_path: Path) -> None:
    defaults = tmp_path / "defaults.json"
    defaults.write_text(json.dumps({"ampere_instance_count": 4}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="missing keys"):
        mod.load_profile_defaults(defaults)


def _minimal_defaults() -> dict[str, Any]:
    return {
        "ampere_node_names": ["ampere1"],
        "ampere_ocpus_per_instance": 1,
        "ampere_memory_per_instance": 6,
        "ampere_boot_volume_size": 50,
        "micro_node_names": ["micro1"],
        "micro_boot_volume_size": 50,
        "enable_free_lb": True,
        "lb_display_name": "free-tier-lb",
    }


def test_load_accounts_minimal(tmp_path: Path) -> None:
    accounts_file = tmp_path / "accounts.json"
    accounts_file.write_text(
        json.dumps([{
            "profile": "myprofile",
            "compartment_id": "ocid1.compartment.oc1..abc",
        }]),
        encoding="utf-8",
    )
    states = mod.load_accounts(accounts_file, _minimal_defaults())
    assert len(states) == 1
    s = states[0]
    assert s.profile == "myprofile"
    assert s.compartment_id == "ocid1.compartment.oc1..abc"
    assert s.existing_subnet_id is None
    assert s.report_output == Path("./state/myprofile-import.tf")
    assert s.ampere_names == ["ampere1"]  # fallback from defaults
    assert s.enable_free_lb is True       # fallback from defaults


def test_load_accounts_per_account_override(tmp_path: Path) -> None:
    accounts_file = tmp_path / "accounts.json"
    accounts_file.write_text(
        json.dumps([{
            "profile": "p1",
            "compartment_id": "ocid1.compartment.oc1..x",
            "existing_subnet_id": "ocid1.subnet.oc1..y",
            "report_output": "/tmp/p1-import.tf",
            "ampere_node_names": ["cp-1", "cp-2"],
            "micro_node_names": [],
            "enable_free_lb": False,
        }]),
        encoding="utf-8",
    )
    states = mod.load_accounts(accounts_file, _minimal_defaults())
    s = states[0]
    assert s.existing_subnet_id == "ocid1.subnet.oc1..y"
    assert s.report_output == Path("/tmp/p1-import.tf")
    assert s.ampere_names == ["cp-1", "cp-2"]
    assert s.micro_names == []
    assert s.enable_free_lb is False


def test_load_accounts_requires_profile_and_compartment(tmp_path: Path) -> None:
    accounts_file = tmp_path / "accounts.json"
    accounts_file.write_text(json.dumps([{"profile": "p1"}]), encoding="utf-8")
    with pytest.raises(RuntimeError, match="compartment_id"):
        mod.load_accounts(accounts_file, _minimal_defaults())


def test_load_accounts_rejects_empty_list(tmp_path: Path) -> None:
    accounts_file = tmp_path / "accounts.json"
    accounts_file.write_text("[]", encoding="utf-8")
    with pytest.raises(RuntimeError, match="at least one account"):
        mod.load_accounts(accounts_file, _minimal_defaults())


_FAKE_VNIC_ATTACHMENT = {
    "vnic_id": "ocid1.vnic.oc1..fakevnic",
    "lifecycle_state": "ATTACHED",
}

_FAKE_PRIVATE_IP = {
    "id": "ocid1.privateip.oc1..fakepip",
}

_FAKE_PUBLIC_IP = {
    "id": "ocid1.publicip.oc1..fakepubip",
    "ip_address": "10.0.0.1",
    "lifecycle_state": "ASSIGNED",
}


class _FakeComputeClientWithVnic(_FakeComputeClient):
    def list_vnic_attachments(self, **kwargs):
        return SimpleNamespace(data=[_FAKE_VNIC_ATTACHMENT])


class _FakeNetworkClientWithPrivateIp(_FakeNetworkClient):
    def list_private_ips(self, **kwargs):
        return SimpleNamespace(data=[_FAKE_PRIVATE_IP])

    def create_public_ip(self, details):
        assert details.lifetime == "RESERVED"
        assert details.private_ip_id == "ocid1.privateip.oc1..fakepip"
        return SimpleNamespace(data=_FAKE_PUBLIC_IP)

    def get_public_ip(self, public_ip_id):
        assert public_ip_id == "ocid1.publicip.oc1..fakepubip"
        return SimpleNamespace(data=_FAKE_PUBLIC_IP)


def test_oci_sdk_vnic_and_reserved_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod.oci.identity, "IdentityClient", lambda _cfg: _FakeIdentityClient())
    monkeypatch.setattr(mod.oci.core, "VirtualNetworkClient", lambda _cfg: _FakeNetworkClientWithPrivateIp())
    monkeypatch.setattr(mod.oci.core, "ComputeClient", lambda _cfg: _FakeComputeClientWithVnic())
    monkeypatch.setattr(mod.oci.load_balancer, "LoadBalancerClient", lambda _cfg: _FakeLbClient())
    monkeypatch.setattr(mod, "list_call_get_all_results", _fake_list_call_get_all_results)

    cli = mod.OciCli(profile="gf78", config={"region": "eu-frankfurt-1"})

    vnic_result = cli.run([
        "compute", "vnic-attachment", "list",
        "--compartment-id", "ocid1.compartment.oc1..x",
        "--instance-id", "ocid1.instance.oc1..x",
    ])
    assert vnic_result["data"][0]["vnic-id"] == "ocid1.vnic.oc1..fakevnic"

    pip_result = cli.run([
        "network", "private-ip", "list",
        "--vnic-id", "ocid1.vnic.oc1..fakevnic",
    ])
    assert pip_result["data"][0]["id"] == "ocid1.privateip.oc1..fakepip"

    pub_result = cli.run([
        "network", "public-ip", "create",
        "--compartment-id", "ocid1.compartment.oc1..x",
        "--display-name", "ampere1-ip",
        "--private-ip-id", "ocid1.privateip.oc1..fakepip",
    ])
    assert pub_result["data"]["id"] == "ocid1.publicip.oc1..fakepubip"

    get_result = cli.run([
        "network", "public-ip", "get",
        "--public-ip-id", "ocid1.publicip.oc1..fakepubip",
    ])
    assert get_result["data"]["ip-address"] == "10.0.0.1"


def test_oci_sdk_mapping_unsupported_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod.oci.identity, "IdentityClient", lambda _cfg: _FakeIdentityClient())
    monkeypatch.setattr(mod.oci.core, "VirtualNetworkClient", lambda _cfg: _FakeNetworkClient())
    monkeypatch.setattr(mod.oci.core, "ComputeClient", lambda _cfg: _FakeComputeClient())
    monkeypatch.setattr(mod.oci.load_balancer, "LoadBalancerClient", lambda _cfg: _FakeLbClient())

    cli = mod.OciCli(profile="gf78", config={"region": "eu-frankfurt-1"})

    with pytest.raises(mod.OciCliError):
        cli.run(["database", "db-system", "list"])
