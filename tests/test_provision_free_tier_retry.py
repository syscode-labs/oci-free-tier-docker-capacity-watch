from __future__ import annotations

import base64
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


def _make_cli_with_reserved_ip(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(mod.oci.identity, "IdentityClient", lambda _cfg: _FakeIdentityClient())
    monkeypatch.setattr(mod.oci.core, "VirtualNetworkClient", lambda _cfg: _FakeNetworkClientWithPrivateIp())
    monkeypatch.setattr(mod.oci.core, "ComputeClient", lambda _cfg: _FakeComputeClientWithVnic())
    monkeypatch.setattr(mod.oci.load_balancer, "LoadBalancerClient", lambda _cfg: _FakeLbClient())
    monkeypatch.setattr(mod, "list_call_get_all_results", _fake_list_call_get_all_results)
    return mod.OciCli(profile="gf78", config={"region": "eu-frankfurt-1"})


def test_get_private_ip_id(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _make_cli_with_reserved_ip(monkeypatch)
    pip_id = mod.get_private_ip_id(cli, "ocid1.compartment.oc1..x", "ocid1.instance.oc1..x")
    assert pip_id == "ocid1.privateip.oc1..fakepip"


def test_create_reserved_public_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _make_cli_with_reserved_ip(monkeypatch)
    pub_ip_id, ip_addr = mod.create_reserved_public_ip(
        cli, "ocid1.compartment.oc1..x", "ampere1-ip", "ocid1.privateip.oc1..fakepip"
    )
    assert pub_ip_id == "ocid1.publicip.oc1..fakepubip"
    assert ip_addr == "10.0.0.1"


def test_scan_available_ads_filters_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_capacity_available(oci, tenancy_ocid, ad, shape, shape_config=None):
        calls.append(ad)
        return ad == "AD-1"

    monkeypatch.setattr(mod, "capacity_available", fake_capacity_available)
    result = mod.scan_available_ads(object(), "ocid.tenancy", ["AD-1", "AD-2", "AD-3"], "VM.Standard.A1.Flex")
    assert result == ["AD-1"]
    assert set(calls) == {"AD-1", "AD-2", "AD-3"}


def test_scan_available_ads_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "capacity_available", lambda *a, **kw: False)
    result = mod.scan_available_ads(object(), "ocid.tenancy", ["AD-1"], "VM.Standard.A1.Flex")
    assert result == []


def test_generate_import_report(tmp_path: Path) -> None:
    output_path = tmp_path / "imports.tf"
    mod.generate_import_report(
        output_path=output_path,
        networking={
            "vcn_id": "ocid1.vcn.x",
            "igw_id": "ocid1.igw.x",
            "route_table_id": "ocid1.rt.x",
            "security_list_id": "ocid1.sl.x",
            "subnet_id": "ocid1.subnet.x",
        },
        lb_id="ocid1.lb.x",
        ampere_instances=[("ampere1", "ocid1.instance.a1", "ocid1.pip.a1")],
        micro_instances=[("micro1", "ocid1.instance.m1", "ocid1.pip.m1")],
    )
    content = output_path.read_text(encoding="utf-8")
    assert "to = oci_core_vcn.free_tier_vcn[0]" in content
    assert 'id = "ocid1.vcn.x"' in content
    assert "to = oci_core_instance.ampere_instance[0]" in content
    assert "to = oci_core_public_ip.ampere_instance[0]" in content
    assert "to = oci_core_instance.micro_instance[0]" in content
    assert "to = oci_load_balancer_load_balancer.free_tier_lb[0]" in content


def test_generate_import_report_skips_none(tmp_path: Path) -> None:
    output_path = tmp_path / "imports.tf"
    mod.generate_import_report(
        output_path=output_path,
        networking=None,
        lb_id=None,
        ampere_instances=[("ampere1", "ocid1.instance.a1", "ocid1.pip.a1")],
        micro_instances=[],
    )
    content = output_path.read_text(encoding="utf-8")
    assert "oci_core_vcn" not in content
    assert "oci_load_balancer" not in content
    assert "ampere_instance[0]" in content


def test_load_accounts_create_compartment(tmp_path: Path) -> None:
    accounts_file = tmp_path / "accounts.json"
    accounts_file.write_text(
        json.dumps([{
            "profile": "new-acct",
            "create_compartment": True,
            "compartment_name": "free-tier-homelab",
        }]),
        encoding="utf-8",
    )
    states = mod.load_accounts(accounts_file, _minimal_defaults())
    s = states[0]
    assert s.create_compartment is True
    assert s.compartment_name == "free-tier-homelab"
    assert s.compartment_id is None


def test_load_accounts_create_compartment_requires_name(tmp_path: Path) -> None:
    accounts_file = tmp_path / "accounts.json"
    accounts_file.write_text(
        json.dumps([{"profile": "p1", "create_compartment": True}]),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="compartment_name"):
        mod.load_accounts(accounts_file, _minimal_defaults())


def test_generate_import_report_with_iam_ids(tmp_path: Path) -> None:
    output_path = tmp_path / "imports.tf"
    mod.generate_import_report(
        output_path=output_path,
        networking=None,
        lb_id=None,
        ampere_instances=[("k8s-cp-1", "ocid1.instance.a1", "ocid1.pip.a1")],
        micro_instances=[],
        iam_ids={
            "compartment_id": "ocid1.compartment.oc1..managed",
            "group_id": "ocid1.group.oc1..g1",
            "user_id": "ocid1.user.oc1..u1",
            "membership_id": "ocid1.membership.oc1..m1",
            "policy_id": "ocid1.policy.oc1..p1",
            "api_key_fingerprint": "aa:bb:cc:dd",
        },
    )
    content = output_path.read_text(encoding="utf-8")
    assert "to = oci_identity_compartment.managed[0]" in content
    assert 'id = "ocid1.compartment.oc1..managed"' in content
    assert "to = oci_identity_group.free_tier[0]" in content
    assert "to = oci_identity_user.free_tier[0]" in content
    assert "to = oci_identity_user_group_membership.free_tier[0]" in content
    assert 'id = "ocid1.membership.oc1..m1"' in content
    assert "to = oci_identity_policy.free_tier[0]" in content
    assert "to = oci_identity_api_key.free_tier[0]" in content
    assert 'id = "ocid1.user.oc1..u1/aa:bb:cc:dd"' in content


_FAKE_GROUP = {"id": "ocid1.group.oc1..g1", "name": "homelab-managers", "lifecycle_state": "ACTIVE"}
_FAKE_USER = {"id": "ocid1.user.oc1..u1", "name": "homelab-user", "lifecycle_state": "ACTIVE"}
_FAKE_MEMBERSHIP = {"id": "ocid1.membership.oc1..m1", "user_id": "ocid1.user.oc1..u1"}
_FAKE_POLICY = {"id": "ocid1.policy.oc1..p1", "name": "homelab-policy"}
_FAKE_COMPARTMENT = {"id": "ocid1.compartment.oc1..managed", "name": "free-tier-homelab", "lifecycle_state": "ACTIVE"}


class _FakeIdentityClientIam(_FakeIdentityClient):
    def list_compartments(self, **kwargs):
        return SimpleNamespace(data=[])  # no existing compartment → will create

    def create_compartment(self, details):
        return SimpleNamespace(data={"id": "ocid1.compartment.oc1..managed", "name": details.name})

    def list_groups(self, **kwargs):
        return SimpleNamespace(data=[])

    def create_group(self, details):
        return SimpleNamespace(data=_FAKE_GROUP)

    def list_users(self, **kwargs):
        return SimpleNamespace(data=[])

    def create_user(self, details):
        return SimpleNamespace(data=_FAKE_USER)

    def add_user_to_group(self, details):
        return SimpleNamespace(data={**_FAKE_MEMBERSHIP, "id": "ocid1.membership.oc1..m1"})

    def list_policies(self, **kwargs):
        return SimpleNamespace(data=[])

    def create_policy(self, details):
        assert "manage all-resources in compartment" in details.statements[0]
        return SimpleNamespace(data=_FAKE_POLICY)


def _make_iam_cli(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(mod.oci.identity, "IdentityClient", lambda _cfg: _FakeIdentityClientIam())
    monkeypatch.setattr(mod.oci.core, "VirtualNetworkClient", lambda _cfg: _FakeNetworkClient())
    monkeypatch.setattr(mod.oci.core, "ComputeClient", lambda _cfg: _FakeComputeClient())
    monkeypatch.setattr(mod.oci.load_balancer, "LoadBalancerClient", lambda _cfg: _FakeLbClient())
    monkeypatch.setattr(mod, "list_call_get_all_results", _fake_list_call_get_all_results)
    return mod.OciCli(profile="gf78", config={"region": "eu-frankfurt-1"})


def test_ensure_iam_setup_creates_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _make_iam_cli(monkeypatch)
    account = mod.AccountState(
        profile="new-acct",
        compartment_id=None,
        existing_subnet_id=None,
        report_output=Path("./state/new-acct-import.tf"),
        ampere_names=["k8s-cp-1"],
        micro_names=[],
        enable_free_lb=False,
        lb_display_name="free-tier-lb",
        create_compartment=True,
        compartment_name="free-tier-homelab",
    )
    ids = mod.ensure_iam_setup(cli, account, "ocid1.tenancy.oc1..t1")
    assert ids["compartment_id"] == "ocid1.compartment.oc1..managed"
    assert ids["group_id"] == "ocid1.group.oc1..g1"
    assert ids["user_id"] == "ocid1.user.oc1..u1"
    assert ids["membership_id"] == "ocid1.membership.oc1..m1"
    assert ids["policy_id"] == "ocid1.policy.oc1..p1"
    assert account.compartment_id == "ocid1.compartment.oc1..managed"


def test_ensure_iam_setup_idempotent_existing_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    """When all resources already exist, ensure_iam_setup returns their existing IDs."""

    class _ExistingIdentityClient(_FakeIdentityClientIam):
        def list_compartments(self, **kwargs):
            return SimpleNamespace(data=[_FAKE_COMPARTMENT])

        def list_groups(self, **kwargs):
            return SimpleNamespace(data=[_FAKE_GROUP])

        def list_users(self, **kwargs):
            return SimpleNamespace(data=[_FAKE_USER])

        def add_user_to_group(self, details):
            # Simulate 409 already-member error
            raise mod.oci.exceptions.ServiceError(
                status=409, code="Conflict",
                headers={}, message="already member", request_id="r1",
            )

        def list_user_group_memberships(self, **kwargs):
            return SimpleNamespace(data=[{"id": "ocid1.membership.oc1..existing"}])

        def list_policies(self, **kwargs):
            return SimpleNamespace(data=[_FAKE_POLICY])

    monkeypatch.setattr(mod.oci.identity, "IdentityClient", lambda _cfg: _ExistingIdentityClient())
    monkeypatch.setattr(mod.oci.core, "VirtualNetworkClient", lambda _cfg: _FakeNetworkClient())
    monkeypatch.setattr(mod.oci.core, "ComputeClient", lambda _cfg: _FakeComputeClient())
    monkeypatch.setattr(mod.oci.load_balancer, "LoadBalancerClient", lambda _cfg: _FakeLbClient())
    monkeypatch.setattr(mod, "list_call_get_all_results", _fake_list_call_get_all_results)

    cli = mod.OciCli(profile="gf78", config={"region": "eu-frankfurt-1"})
    account = mod.AccountState(
        profile="new-acct",
        compartment_id=None,
        existing_subnet_id=None,
        report_output=Path("./state/new-acct-import.tf"),
        ampere_names=["k8s-cp-1"],
        micro_names=[],
        enable_free_lb=False,
        lb_display_name="free-tier-lb",
        create_compartment=True,
        compartment_name="free-tier-homelab",
    )
    ids = mod.ensure_iam_setup(cli, account, "ocid1.tenancy.oc1..t1")
    assert ids["compartment_id"] == "ocid1.compartment.oc1..managed"
    assert ids["group_id"] == "ocid1.group.oc1..g1"
    assert ids["user_id"] == "ocid1.user.oc1..u1"
    assert ids["membership_id"] == "ocid1.membership.oc1..existing"
    assert ids["policy_id"] == "ocid1.policy.oc1..p1"


def test_push_report_to_github_create(monkeypatch: pytest.MonkeyPatch) -> None:
    """New file: GET returns 404, PUT creates it."""
    calls: list[tuple[str, bytes | None]] = []

    class _FakeResponse:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def read(self) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    def fake_urlopen(req):
        calls.append((req.get_method(), req.data))
        if req.get_method() == "GET" or (isinstance(req, mod.urllib.request.Request) and req.data is None):
            raise mod.urllib.error.HTTPError(url="", code=404, msg="Not Found", hdrs=None, fp=None)
        return _FakeResponse(json.dumps({"commit": {"sha": "abc1234def"}}).encode())

    # Override GET to raise 404, PUT to succeed
    get_called = []
    put_called = []

    original_urlopen = mod.urllib.request.urlopen

    def patched_urlopen(req):
        method = req.get_method() if hasattr(req, "get_method") else "GET"
        if req.data is None:
            get_called.append(True)
            raise mod.urllib.error.HTTPError(url="", code=404, msg="Not Found", hdrs=None, fp=None)
        put_called.append(json.loads(req.data))
        return _FakeResponse(json.dumps({"commit": {"sha": "abc1234"}}).encode())

    monkeypatch.setattr(mod.urllib.request, "urlopen", patched_urlopen)

    mod.push_report_to_github(
        content="import {}\n",
        repo="org/infra",
        path="oci/test-import.tf",
        branch="main",
        token="ghp_fake",
        commit_message="chore: test",
    )

    assert len(put_called) == 1
    payload = put_called[0]
    assert payload["branch"] == "main"
    assert payload["message"] == "chore: test"
    assert "sha" not in payload  # new file — no existing SHA
    assert base64.b64decode(payload["content"]).decode() == "import {}\n"


def test_push_report_to_github_update(monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing file: GET returns SHA, PUT includes it."""
    put_called = []

    class _FakeResponse:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def read(self) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    def patched_urlopen(req):
        if req.data is None:  # GET
            return _FakeResponse(json.dumps({"sha": "existing-sha-123"}).encode())
        put_called.append(json.loads(req.data))  # PUT
        return _FakeResponse(json.dumps({"commit": {"sha": "new-sha-456"}}).encode())

    monkeypatch.setattr(mod.urllib.request, "urlopen", patched_urlopen)

    mod.push_report_to_github(
        content="import {}\n",
        repo="org/infra",
        path="oci/test-import.tf",
        branch="main",
        token="ghp_fake",
        commit_message="chore: update",
    )

    assert len(put_called) == 1
    assert put_called[0]["sha"] == "existing-sha-123"


def test_load_accounts_report_push(tmp_path: Path) -> None:
    accounts_file = tmp_path / "accounts.json"
    accounts_file.write_text(
        json.dumps([{
            "profile": "myprofile",
            "compartment_id": "ocid1.compartment.oc1..abc",
            "report_push": {
                "github_repo": "org/infra",
                "github_path": "oci/myprofile-import.tf",
                "github_branch": "deploy",
            },
        }]),
        encoding="utf-8",
    )
    states = mod.load_accounts(accounts_file, _minimal_defaults())
    s = states[0]
    assert s.report_push_github_repo == "org/infra"
    assert s.report_push_github_path == "oci/myprofile-import.tf"
    assert s.report_push_github_branch == "deploy"


def test_oci_sdk_mapping_unsupported_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod.oci.identity, "IdentityClient", lambda _cfg: _FakeIdentityClient())
    monkeypatch.setattr(mod.oci.core, "VirtualNetworkClient", lambda _cfg: _FakeNetworkClient())
    monkeypatch.setattr(mod.oci.core, "ComputeClient", lambda _cfg: _FakeComputeClient())
    monkeypatch.setattr(mod.oci.load_balancer, "LoadBalancerClient", lambda _cfg: _FakeLbClient())

    cli = mod.OciCli(profile="gf78", config={"region": "eu-frankfurt-1"})

    with pytest.raises(mod.OciCliError):
        cli.run(["database", "db-system", "list"])
