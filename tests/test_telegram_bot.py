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


def test_parse_daily_time_valid() -> None:
    assert bmod._parse_daily_time("08:00") == (8, 0)
    assert bmod._parse_daily_time("23:59") == (23, 59)
    assert bmod._parse_daily_time("00:00") == (0, 0)


def test_parse_daily_time_invalid() -> None:
    assert bmod._parse_daily_time("25:00") is None
    assert bmod._parse_daily_time("8") is None
    assert bmod._parse_daily_time("abc") is None
    assert bmod._parse_daily_time("") is None


def test_daily_fires_once_per_day(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Daily report fires once when time matches, not on subsequent calls same day."""
    ctx = pmod.BotContext(accounts=[], cycle=1, done=True)
    bot = bmod.TelegramBot(
        token="fake", chat_id="123", ctx=ctx,
        state_dir=tmp_path, daily_time="09:00",
    )
    sent: list[str] = []
    monkeypatch.setattr(bot, "_send", lambda text: sent.append(text))

    from datetime import date

    current_date: list[date] = [date(2026, 3, 25)]

    class _FakeNow:
        def __init__(self, h: int, m: int) -> None:
            self.hour = h
            self.minute = m
        def date(self) -> date:
            return current_date[0]

    class _FakeDatetime:
        @staticmethod
        def now(tz=None) -> _FakeNow:
            return _FakeNow(9, 0)

    # First call at 09:00 — should fire
    monkeypatch.setattr(bmod, "datetime", _FakeDatetime)
    bot._check_daily()
    assert len(sent) == 1
    assert "Daily status" in sent[0]

    # Second call same minute same day — should NOT fire again
    bot._check_daily()
    assert len(sent) == 1

    # Third call on next day — should fire again
    current_date[0] = date(2026, 3, 26)
    bot._check_daily()
    assert len(sent) == 2


def test_setdaily_persists(tmp_path: Path) -> None:
    ctx = pmod.BotContext(accounts=[], cycle=0, done=False)
    bot = bmod.TelegramBot(
        token="fake", chat_id="123", ctx=ctx,
        state_dir=tmp_path, daily_time="08:00",
    )
    sent: list[str] = []
    bot._send = lambda text: sent.append(text)  # type: ignore[method-assign]

    bot._handle_command("/setdaily 14:30")
    assert bot._daily_hour == 14
    assert bot._daily_minute == 30
    assert (tmp_path / "daily_status_time.txt").read_text() == "14:30"
    assert "14:30" in sent[0]


def test_make_bot_from_env_no_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    ctx = pmod.BotContext(accounts=[], cycle=0, done=False)
    assert bmod.make_bot_from_env(ctx, tmp_path) is None


def test_make_bot_from_env_with_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok123")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "456")
    ctx = pmod.BotContext(accounts=[], cycle=0, done=False)
    bot = bmod.make_bot_from_env(ctx, tmp_path)
    assert bot is not None
    assert bot._token == "tok123"
    assert bot._chat_id == "456"
