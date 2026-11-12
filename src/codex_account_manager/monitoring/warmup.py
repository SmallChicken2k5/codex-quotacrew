"""Opt-in warm-up for verified QuotaCrew profiles."""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Callable
from datetime import datetime
from tempfile import TemporaryDirectory

from codex_account_manager.accounts.service import AccountService, account_read_lock
from codex_account_manager.adapters.app_server import CodexAppServer
from codex_account_manager.core.errors import OperationBusyError
from codex_account_manager.core.events import bus
from codex_account_manager.core.logging import get_logger
from codex_account_manager.core.operation_lock import OperationLock
from codex_account_manager.core.paths import paths
from codex_account_manager.domain.models import AccountSnapshot, ProfileHealth
from codex_account_manager.storage.repositories import SettingsRepository

log = get_logger(__name__)
_CLOCK = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")
_WARMUP_TIMEOUT = 45.0


def parse_times(value: str) -> tuple[str, ...]:
    times = {part.strip() for part in value.split(",") if part.strip()}
    if len(times) > 24 or any(not _CLOCK.fullmatch(part) for part in times):
        raise ValueError("Enter up to 24 local times as HH:MM, separated by commas.")
    return tuple(sorted(times))


def scheduled_poll_delay(value: str, now: datetime) -> float | None:
    try:
        times = parse_times(value)
    except ValueError:
        return None
    if not times:
        return None
    minute = now.hour * 60 + now.minute
    offsets = [(int(t[:2]) * 60 + int(t[3:]) - minute) % 1440 for t in times]
    return max(
        1.0, min(offset or 1440 for offset in offsets) * 60 - now.second - now.microsecond / 1e6
    )


def _windows(
    health: ProfileHealth | AccountSnapshot,
) -> list[tuple[int, float | None, int | None]]:
    return [
        (duration, used, reset)
        for duration, used, reset in (
            (health.primary_window_minutes, health.primary_used_percent, health.primary_resets_at),
            (
                health.secondary_window_minutes,
                health.secondary_used_percent,
                health.secondary_resets_at,
            ),
        )
        if type(duration) is int and duration > 0
    ]


def _available(health: ProfileHealth | AccountSnapshot) -> bool:
    percentages = [
        percent
        for percent in (health.primary_used_percent, health.secondary_used_percent)
        if percent is not None
    ]
    return (
        health.ordinary_usage_allowed is True
        and bool(percentages)
        and all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and 0 <= value < 100
            for value in percentages
        )
    )


def _reset_due(health: ProfileHealth | AccountSnapshot, now: datetime) -> tuple[int, int] | None:
    windows = [window for window in _windows(health) if window[0] in {300, 10080}]
    if not windows:
        return None
    duration, used, reset = min(windows, key=lambda window: window[0])
    if used != 0 or type(reset) is not int or reset <= 0:
        return None
    current = int(now.timestamp())
    # While an unused window reports an old reset, wait one full window after an attempt.
    return duration, reset if reset > current else current + duration * 60


class Warmup:
    def __init__(
        self,
        accounts: AccountService,
        *,
        settings: SettingsRepository | None = None,
        server: Callable[[str], CodexAppServer] = CodexAppServer,
    ):
        self.accounts = accounts
        self.settings = settings or SettingsRepository()
        self.server = server

    async def run(self, health: list[ProfileHealth], now: datetime) -> None:
        mode = await self.settings.get("warmup_mode", "off")
        minute = now.strftime("%H:%M")
        if mode == "scheduled":
            try:
                if minute not in parse_times(await self.settings.get("warmup_times") or ""):
                    return
            except ValueError:
                return
        elif mode != "reset":
            return

        completed = failed = 0
        for account in health:
            if (
                account.stale
                or account.error
                or account.reauth_required
                or account.account_match is not True
                or not account.auth_present
                or not account.identity
                or not account.identity.fingerprint
                or not _available(account)
            ):
                continue
            key = f"warmup:{account.identity.fingerprint}"
            if mode == "scheduled":
                key = f"{key}:at:{minute}"
                mark = now.date().isoformat()
            else:
                window = _reset_due(account, now)
                if window is None:
                    continue
                duration, mark_at = window
                key = f"{key}:reset:{duration}"
                mark = str(mark_at)
            try:
                outcome = await self._attempt(account, mode, key, mark, now)
                completed += outcome is True
                failed += outcome is False
            except OperationBusyError:
                continue
            except Exception:
                failed += 1
                log.warning("Warm-up could not be verified for one saved account.")
        if completed or failed:
            bus.publish("warmup.finished", completed=completed, failed=failed)

    async def _attempt(
        self, account: ProfileHealth, mode: str, key: str, mark: str, now: datetime
    ) -> bool | None:
        with OperationLock(paths.data_dir / "account-operation.lock"):
            if (
                await self.settings.get("monitor_enabled", "true") != "true"
                or await self.settings.get("warmup_mode", "off") != mode
            ):
                return None
            previous = await self.settings.get(key)
            if mode == "scheduled":
                if previous and previous >= mark:
                    return None
            elif previous and previous.isdecimal() and int(previous) > int(now.timestamp()):
                return None

            profile = await self.accounts.profiles.get_by_alias(account.alias)
            if (
                profile is None
                or profile.id != account.profile_id
                or not profile.identity.matches(account.identity)
            ):
                return None
            # Use the shared home only for the current owner so an active profile's
            # saved credentials never race a refresh from the desktop app.
            active = self.accounts.credentials.active_identity_hint()
            if not active.owner_key:
                return None
            home = (
                str(paths.shared_codex_home)
                if profile.identity.matches(active)
                else profile.codex_home
            )
            with account_read_lock(home), TemporaryDirectory(prefix="quotacrew-warmup-") as cwd:
                async with asyncio.timeout(_WARMUP_TIMEOUT), self.server(home) as server:
                    snapshot = await server.read_account(include_identity=True)
                    if not profile.identity.matches(snapshot.identity) or not _available(snapshot):
                        return None
                    if mode == "reset":
                        fresh = _reset_due(snapshot, now)
                        if fresh is None or not key.endswith(f":{fresh[0]}"):
                            return None
                        mark = str(fresh[1])
                    # Persist before starting a turn so a disconnect cannot lead
                    # to resending the same warm-up after restart.
                    await self.settings.set(key, mark)
                    return await server.run_warmup_turn(cwd)
