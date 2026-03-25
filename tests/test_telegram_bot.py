from __future__ import annotations

import importlib.util
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

# Load telegram_bot module
BOT_PATH = Path(__file__).resolve().parents[1] / "worker" / "telegram_bot.py"
BSPEC = importlib.util.spec_from_file_location("telegram_bot", BOT_PATH)
assert BSPEC and BSPEC.loader
bmod = importlib.util.module_from_spec(BSPEC)
sys.modules[BSPEC.name] = bmod
BSPEC.loader.exec_module(bmod)

# Load provision module for AccountState / BotContext
PPATH = Path(__file__).resolve().parents[1] / "worker" / "provision_free_tier_retry.py"
PSPEC = importlib.util.spec_from_file_location("provision_free_tier_retry", PPATH)
assert PSPEC and PSPEC.loader
pmod = importlib.util.module_from_spec(PSPEC)
sys.modules[PSPEC.name] = pmod
PSPEC.loader.exec_module(pmod)


def _make_account(profile: str, done: bool = False) -> pmod.AccountState:
    from pathlib import Path as P
    return pmod.AccountState(
        profile=profile,
        compartment_id="ocid1.compartment.oc1..x",
        existing_subnet_id=None,
        report_output=P(f"./state/{profile}-import.tf"),
        ampere_names=["ampere1", "ampere2"],
        micro_names=["micro1"],
        enable_free_lb=False,
        lb_display_name="free-tier-lb",
        done=done,
        created_ampere=[("ampere1", "ocid1.instance.oc1..a1", "ocid1.pip.oc1..p1")] if done else [],
        created_micro=[("micro1", "ocid1.instance.oc1..m1", "ocid1.pip.oc1..pm1")] if done else [],
    )


def test_format_status_all_done() -> None:
    ctx = pmod.BotContext(
        accounts=[_make_account("syscode", done=True)],
        cycle=3,
        done=True,
        last_cycle_at=datetime(2026, 3, 25, 10, 0, 0),
    )
    msg = bmod.format_status(ctx)
    assert "All provisioned" in msg
    assert "syscode" in msg
    assert "✅" in msg


def test_format_status_pending() -> None:
    ctx = pmod.BotContext(
        accounts=[_make_account("gf78", done=False)],
        cycle=5,
        done=False,
        last_cycle_at=datetime(2026, 3, 25, 10, 0, 0),
    )
    msg = bmod.format_status(ctx)
    assert "Cycle #5" in msg
    assert "pending" in msg
    assert "gf78" in msg


def test_format_status_with_error() -> None:
    ctx = pmod.BotContext(
        accounts=[_make_account("gf78", done=False)],
        cycle=2,
        done=False,
        last_error="OutOfHostCapacity on AD-1",
    )
    msg = bmod.format_status(ctx)
    assert "OutOfHostCapacity" in msg


def test_format_status_no_accounts() -> None:
    ctx = pmod.BotContext(accounts=[], cycle=0, done=False)
    msg = bmod.format_status(ctx)
    assert msg  # non-empty
