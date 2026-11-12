from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from codex_account_manager.adapters.app_server import CodexAppServer
from codex_account_manager.domain.models import AccountSnapshot, Profile, ProfileHealth
from codex_account_manager.domain.states import QuotaState
from codex_account_manager.monitoring.warmup import Warmup, scheduled_poll_delay
from codex_account_manager.storage.repositories import SettingsRepository


def health(profile: Profile, now: datetime) -> ProfileHealth:
    return ProfileHealth(
        alias=profile.alias,
        profile_id=profile.id,
        plan_type="plus",
        primary_used_percent=0,
        secondary_used_percent=7,
        primary_resets_at=int((now + timedelta(hours=5)).timestamp()),
        secondary_resets_at=int((now + timedelta(days=7)).timestamp()),
        primary_window_minutes=300,
        secondary_window_minutes=10080,
        ordinary_usage_allowed=True,
        auth_present=True,
        account_match=True,
        is_active=False,
        quota_state=QuotaState.AVAILABLE,
        last_checked_at=now,
        identity=profile.identity,
    )


async def test_warmup_all_verified_profiles_and_deduplicate_after_restart(migrated_db, tmp_path):
    now = datetime(2026, 11, 9, 8, 0)
    profiles = [
        Profile(
            alias=name, codex_home=str(tmp_path / name), bound_account_id=name, bound_user_id=name
        )
        for name in ("active", "other", "limited", "wrong-owner")
    ]
    by_alias = {profile.alias: profile for profile in profiles}
    accounts = SimpleNamespace(
        profiles=SimpleNamespace(get_by_alias=AsyncMock(side_effect=by_alias.get)),
        credentials=SimpleNamespace(active_identity_hint=lambda: profiles[0].identity),
    )
    requests = []

    def server_factory(home):
        profile = next((p for p in profiles if p.codex_home == home), profiles[0])
        record = health(profile, now)
        server = AsyncMock()
        server.__aenter__.return_value = server
        server.read_account.return_value = AccountSnapshot(
            account_id=profile.bound_account_id,
            account_type="chatgpt",
            email=None,
            plan_type="plus",
            ordinary_usage_allowed=True,
            primary_used_percent=record.primary_used_percent,
            primary_resets_at=record.primary_resets_at,
            primary_window_minutes=record.primary_window_minutes,
            secondary_used_percent=record.secondary_used_percent,
            secondary_resets_at=record.secondary_resets_at,
            secondary_window_minutes=record.secondary_window_minutes,
            identity=profiles[0].identity if profile.alias == "wrong-owner" else profile.identity,
        )

        async def send(_cwd):
            requests.append(profile.alias)
            return profile.alias != "other"  # A failed turn still consumes this slot.

        server.run_warmup_turn.side_effect = send
        return server

    settings = SettingsRepository()
    await settings.set_many({"warmup_mode": "scheduled", "warmup_times": "08:00, 13:00"})
    records = [health(profile, now) for profile in profiles]
    records[2] = replace(records[2], secondary_used_percent=100)
    for _ in range(2):
        await Warmup(accounts, server=server_factory).run(records, now)
    assert requests == ["active", "other"]
    await Warmup(accounts, server=server_factory).run(records, now + timedelta(hours=4))
    assert len(requests) == 2
    await Warmup(accounts, server=server_factory).run(records, now + timedelta(hours=5))
    assert requests == ["active", "other", "active", "other"]
    await settings.set("monitor_enabled", "false")
    await Warmup(accounts, server=server_factory).run(records, now + timedelta(days=1))
    assert len(requests) == 4
    await settings.set_many({"monitor_enabled": "true", "warmup_mode": "reset"})
    await Warmup(accounts, server=server_factory).run(records, now)
    await Warmup(accounts, server=server_factory).run(records, now + timedelta(minutes=30))
    assert len(requests) == 6
    await Warmup(accounts, server=server_factory).run(records, now + timedelta(hours=5))
    assert len(requests) == 8


def test_scheduled_poll_uses_the_next_configured_local_time():
    assert scheduled_poll_delay("08:00, 13:00", datetime(2026, 11, 9, 7, 58, 30)) == 90
    assert scheduled_poll_delay("08:00", datetime(2026, 11, 9, 23, 0)) == 9 * 3600
    assert scheduled_poll_delay("25:00", datetime(2026, 11, 9, 7, 0)) is None


async def test_adapter_only_sends_after_ephemeral_thread_confirmed(monkeypatch, tmp_path):
    server = CodexAppServer(tmp_path)
    rpc = AsyncMock(return_value={"thread": {"id": "thread-id", "ephemeral": False}})
    monkeypatch.setattr(server, "_request", rpc)
    from pytest import raises

    from codex_account_manager.core.errors import AppServerError

    with raises(AppServerError, match="temporary warm-up conversation"):
        await server.run_warmup_turn(str(tmp_path))
    assert rpc.await_count == 1
    rpc.side_effect = [
        {"thread": {"id": "thread-id", "ephemeral": True}},
        {"turn": {"id": "turn-id", "status": "completed"}},
    ]
    assert await server.run_warmup_turn(str(tmp_path))
    assert rpc.await_args.args[0] == "turn/start"
    assert rpc.await_args.args[1]["threadId"] == "thread-id"
