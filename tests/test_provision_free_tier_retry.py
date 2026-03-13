from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from worker import provision_free_tier_retry as mod


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


def test_oci_sdk_mapping_unsupported_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod.oci.identity, "IdentityClient", lambda _cfg: _FakeIdentityClient())
    monkeypatch.setattr(mod.oci.core, "VirtualNetworkClient", lambda _cfg: _FakeNetworkClient())
    monkeypatch.setattr(mod.oci.core, "ComputeClient", lambda _cfg: _FakeComputeClient())
    monkeypatch.setattr(mod.oci.load_balancer, "LoadBalancerClient", lambda _cfg: _FakeLbClient())

    cli = mod.OciCli(profile="gf78", config={"region": "eu-frankfurt-1"})

    with pytest.raises(mod.OciCliError):
        cli.run(["database", "db-system", "list"])
