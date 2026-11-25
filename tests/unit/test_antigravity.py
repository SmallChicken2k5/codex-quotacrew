import asyncio
import json
import os
import subprocess
import sys
import time
from contextlib import closing
from io import BytesIO, StringIO
from pathlib import Path

import pytest

from codex_account_manager.antigravity import (
    MAX_STATUS_BYTES,
    AntigravityProfiles,
    parse_buckets,
    record_statusline,
    statusline_command,
)


def status(email, remaining=0.43):
    return {
        "product": "antigravity",
        "email": email,
        "quota": {
            "gemini-weekly": {"remaining_fraction": remaining, "reset_time": "2026-11-28T10:15:00Z"}
        },
        "conversation_title": "sensitive example",
        "access_token": "fixture-never-store-token",
    }


async def test_profiles_keep_independent_quota_and_one_preference(migrated_db):
    store = AntigravityProfiles()
    await store.save("A@example.test", "Personal")
    await store.save("B@example.test", "Studio")
    with pytest.raises(ValueError, match="already saved"):
        await store.save("a@EXAMPLE.test", "Duplicate")
    assert not record_statusline(status("unknown@example.test"))
    assert record_statusline(status("A@example.test", 0))
    assert record_statusline(status("B@example.test", 0.43))
    assert not record_statusline({**status("A@example.test"), "quota": {}})
    await asyncio.gather(
        store.toggle_preferred("a@example.test"), store.toggle_preferred("b@example.test")
    )
    assert sum(profile.preferred for profile in await store.list()) == 1
    await store.save("a@example.test", "Renamed", existing=True)
    profiles = {profile.email: profile for profile in await AntigravityProfiles().list()}
    assert profiles["a@example.test"].alias == "Renamed"
    assert profiles["a@example.test"].quota[0].fraction == 0
    assert profiles["b@example.test"].quota[0].fraction == 0.43
    assert profiles["b@example.test"].quota[0].reset.isoformat() == "2026-11-28T10:15:00+00:00"
    assert profiles["a@example.test"].observed_at > 0
    import sqlite3

    with closing(sqlite3.connect(migrated_db.db_path)) as db:
        stored = repr(db.execute("SELECT * FROM antigravity_profiles").fetchall())
    assert "fixture-never-store-token" not in stored
    assert "sensitive example" not in stored
    assert "unknown@example.test" not in stored
    await store.remove("b@example.test")
    assert not record_statusline(status("b@example.test"))
    assert len(await store.list()) == 1


def test_buckets_distinguish_zero_unknown_and_disabled():
    buckets = parse_buckets(
        {
            "disabled": {"disabled": True},
            "numeric": {"remaining_amount": 12},
            "zero": {"remaining_fraction": 0},
            "bad": {
                "remaining_fraction": True,
                "remaining_amount": float("nan"),
                "reset_time": "2026-10-13",
            },
            "<script>": {"remaining_fraction": 1},
        }
    )
    assert [item.name for item in buckets] == ["disabled", "numeric", "zero"]
    assert buckets[0].disabled and buckets[0].fraction is None
    assert buckets[1].amount == 12 and buckets[1].fraction is None
    assert buckets[2].fraction == 0


@pytest.mark.parametrize("raw", [b"broken json", b"[1, 2]", b" " * (MAX_STATUS_BYTES + 1)])
def test_bad_callback_payload_is_ignored(raw):
    output = StringIO()
    statusline_command(BytesIO(raw), output)
    assert output.getvalue() == "QuotaCrew: waiting for a saved account and quota\n"


async def test_cli_process_saves_snapshot_through_stdin(migrated_db):
    home = migrated_db.data_dir.parent
    env = os.environ.copy()
    env.update(
        {
            "WIN_PD_OVERRIDE_LOCAL_APPDATA": str(home),
            "XDG_DATA_HOME": str(home),
            "HOME": str(home),
            "USERPROFILE": str(home),
        }
    )
    from codex_account_manager.core.paths import APP_NAME

    # The application fixture uses its own custom directory name.
    data = home / APP_NAME
    if sys.platform == "darwin":
        data = home / "Library/Application Support" / APP_NAME
    data.mkdir(parents=True)
    import sqlite3

    with (
        closing(sqlite3.connect(migrated_db.db_path)) as original,
        closing(sqlite3.connect(data / "accounts.db")) as isolated,
    ):
        original.backup(isolated)
        isolated.execute(
            "INSERT INTO antigravity_profiles (email, alias) VALUES (?, ?)",
            ("a@example.test", "Personal"),
        )
        isolated.commit()
    result = subprocess.run(
        [sys.executable, "-m", "codex_account_manager.cli.entrypoint", "antigravity-status"],
        input=json.dumps(status("a@example.test")),
        env=env,
        capture_output=True,
        text=True,
        timeout=8,
        check=True,
    )
    assert result.stdout.strip() == "QuotaCrew: quota synced"
    with closing(sqlite3.connect(data / "accounts.db")) as db:
        assert (
            json.loads(db.execute("SELECT quota FROM antigravity_profiles").fetchone()[0])[
                "gemini-weekly"
            ]["remaining_fraction"]
            == 0.43
        )


async def test_manual_switch_and_quota_view(qt_app, migrated_db, monkeypatch):
    from PySide6.QtWidgets import QLineEdit, QPushButton
    from scripts.render_preview import PreviewRunner

    from codex_account_manager.gui import antigravity as ui
    from codex_account_manager.gui.main_window import MainWindow

    store = AntigravityProfiles()
    await store.save("a@example.test", "Personal")
    await store.save("b@example.test", "Studio")
    await store.toggle_preferred("b@example.test")
    time_before = time.time() - 3600
    with monkeypatch.context() as clock:
        clock.setattr("codex_account_manager.antigravity.time.time", lambda: time_before)
        record_statusline(status("b@example.test", 0.49))
    record_statusline(status("a@example.test", 0))
    calls = []
    monkeypatch.setattr(
        ui, "antigravity_executable", lambda: Path("C:/Antigravity/Antigravity.exe")
    )
    monkeypatch.setattr(ui, "launch_from_explorer", lambda path, args: calls.append((path, args)))
    if sys.platform != "win32":
        monkeypatch.setattr(ui.QProcess, "startDetached", lambda *args: (not calls.append(args), 1))
    window = MainWindow(PreviewRunner())
    try:
        index = next(
            i for i in range(window.nav.count()) if window.nav.item(i).text() == "Antigravity"
        )
        window.nav.setCurrentRow(index)
        view = window.antigravity
        assert window.stack.currentWidget() is view and window.topbar.isHidden()
        view._loaded(await store.list())
        assert view.accounts.rowCount() == 2
        assert view.accounts.item(0, 0).text() == "★ Studio"
        assert view.accounts.item(0, 2).text().startswith("Stale")
        assert view.quotas.item(0, 1).text() == "49.00%"
        view.switch.click()
        switch = next(iter(view._dialogs))
        assert switch.findChild(QLineEdit).text() == "b@example.test"
        assert not calls
        button = next(
            button
            for button in switch.findChildren(QPushButton)
            if button.text() == "Open Antigravity"
        )
        button.click()
        assert len(calls) == 1
        switch.close()
        view.search.setText("personal")
        assert view.accounts.rowCount() == 1 and view.quotas.item(0, 1).text() == "0.00%"
        view.search.setText("missing")
        assert not view.switch.isEnabled()
        window.nav.setCurrentRow(0)
        assert window.stack.currentWidget() is window.dashboard and not window.topbar.isHidden()
    finally:
        window.dispose()
        window.deleteLater()
        qt_app.processEvents()
