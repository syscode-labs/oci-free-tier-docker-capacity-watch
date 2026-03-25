"""Telegram bot for OCI watcher — /status, /setdaily, daily report."""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, date, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from provision_free_tier_retry import AccountState, BotContext

_TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


def _tg(token: str, method: str, **params: Any) -> dict[str, Any]:
    """Call a Telegram Bot API method. Returns parsed JSON."""
    url = _TELEGRAM_API.format(token=token, method=method)
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def _minutes_ago(dt: datetime | None) -> str:
    if dt is None:
        return "never"
    delta = int((datetime.now() - dt).total_seconds() / 60)
    if delta < 1:
        return "just now"
    return f"{delta}m ago"


def format_status(ctx: "BotContext") -> str:
    """Build a human-readable status message from BotContext."""
    with ctx.lock:
        accounts = list(ctx.accounts)
        cycle = ctx.cycle
        done = ctx.done
        last_cycle_at = ctx.last_cycle_at
        last_error = ctx.last_error

    pending = [a for a in accounts if not a.done]

    if done or (accounts and all(a.done for a in accounts)):
        header = f"🟢 All provisioned — {len(accounts)}/{len(accounts)} accounts done"
    elif cycle == 0:
        header = "🔄 Starting up — no cycles completed yet"
    else:
        header = (
            f"🔄 Cycle #{cycle} — {len(pending)}/{len(accounts)} account(s) pending"
            f" · last cycle {_minutes_ago(last_cycle_at)}"
        )

    lines = [header, ""]
    for account in accounts:
        icon = "✅" if account.done else "⏳"
        lines.append(f"{account.profile} {icon}")
        n_ampere = len(account.created_ampere)
        n_micro = len(account.created_micro)
        lines.append(f"  A1: {n_ampere}/{len(account.ampere_names)} instances")
        lines.append(f"  Micro: {n_micro}/{len(account.micro_names)}")
        lines.append("")

    if last_error:
        lines.append(f"⚠️ Last error: {last_error}")

    return "\n".join(lines).rstrip()


def _parse_daily_time(value: str) -> tuple[int, int] | None:
    """Parse 'HH:MM' → (hour, minute) or None on invalid input."""
    try:
        parts = value.strip().split(":")
        if len(parts) != 2:
            return None
        h, m = int(parts[0]), int(parts[1])
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except (ValueError, AttributeError):
        pass
    return None


class TelegramBot(threading.Thread):
    """Long-polling Telegram bot thread."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        ctx: "BotContext",
        state_dir: Path,
        daily_time: str = "08:00",
        daily_tz: str = "UTC",
    ) -> None:
        super().__init__(daemon=True, name="telegram-bot")
        self._token = token
        self._chat_id = chat_id
        self._ctx = ctx
        self._state_dir = state_dir
        self._daily_file = state_dir / "daily_status_time.txt"
        self._daily_tz = _resolve_tz(daily_tz)
        self._daily_hour, self._daily_minute = self._load_daily_time(daily_time)
        self._last_daily_fired: tuple[date, int, int] | None = None
        self._offset = 0

    def _load_daily_time(self, default: str) -> tuple[int, int]:
        if self._daily_file.exists():
            saved = self._daily_file.read_text(encoding="utf-8").strip()
            parsed = _parse_daily_time(saved)
            if parsed:
                return parsed
        parsed = _parse_daily_time(default)
        return parsed if parsed else (8, 0)

    def _send(self, text: str) -> None:
        try:
            _tg(self._token, "sendMessage", chat_id=self._chat_id, text=text)
        except Exception as exc:  # noqa: BLE001
            print(f"[bot] send failed: {exc}", flush=True)

    def _handle_command(self, text: str) -> None:
        cmd = text.strip().lower().split()[0] if text.strip() else ""
        if cmd in ("/status", "/status@oci_watcher_bot"):
            self._send(format_status(self._ctx))
        elif cmd.startswith("/setdaily"):
            parts = text.strip().split(maxsplit=1)
            if len(parts) < 2:
                self._send("Usage: /setdaily HH:MM (e.g. /setdaily 08:00)")
                return
            parsed = _parse_daily_time(parts[1])
            if parsed is None:
                self._send(f"Invalid time '{parts[1]}'. Use HH:MM format.")
                return
            self._daily_hour, self._daily_minute = parsed
            self._daily_file.write_text(f"{parsed[0]:02d}:{parsed[1]:02d}", encoding="utf-8")
            self._send(f"Daily status set to {parsed[0]:02d}:{parsed[1]:02d} UTC ✅")
        elif cmd in ("/help", "/help@oci_watcher_bot"):
            self._send(
                "/status — current watcher state\n"
                "/setdaily HH:MM — set daily report time (UTC)\n"
                "/help — this message"
            )
        elif cmd.startswith("/"):
            self._send("Unknown command. Try /help")

    def _check_daily(self) -> None:
        now = datetime.now(self._daily_tz)
        today = now.date()
        fired_key = (today, self._daily_hour, self._daily_minute)
        if (
            now.hour == self._daily_hour
            and now.minute == self._daily_minute
            and self._last_daily_fired != fired_key
        ):
            self._last_daily_fired = fired_key
            msg = f"📅 Daily status — {today.isoformat()}\n\n{format_status(self._ctx)}"
            self._send(msg)

    def run(self) -> None:
        print("[bot] started", flush=True)
        while True:
            try:
                result = _tg(
                    self._token,
                    "getUpdates",
                    offset=self._offset,
                    timeout=30,
                    allowed_updates="message",
                )
                for update in result.get("result", []):
                    self._offset = update["update_id"] + 1
                    msg = update.get("message", {})
                    text = msg.get("text", "")
                    if text:
                        self._handle_command(text)
                self._check_daily()
            except urllib.error.URLError as exc:
                print(f"[bot] network error: {exc}", flush=True)
                time.sleep(10)
            except Exception as exc:  # noqa: BLE001
                print(f"[bot] unexpected error: {exc}", flush=True)
                time.sleep(5)


def _resolve_tz(tz_name: str) -> Any:
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        return timezone.utc


def make_bot_from_env(ctx: "BotContext", state_dir: Path) -> "TelegramBot | None":
    """Create TelegramBot from env vars, or return None if not configured."""
    import os
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return None
    return TelegramBot(
        token=token,
        chat_id=chat_id,
        ctx=ctx,
        state_dir=state_dir,
        daily_time=os.environ.get("DAILY_STATUS_TIME", "08:00"),
        daily_tz=os.environ.get("DAILY_STATUS_TZ", "UTC"),
    )
