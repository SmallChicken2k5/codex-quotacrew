"""Verify install/uninstall on a disposable GitHub-hosted Windows runner only."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path


def verify_antigravity_callback(cli: Path, database: Path) -> None:
    email = "installer-test@example.test"
    with sqlite3.connect(database) as db:
        db.execute(
            "INSERT INTO antigravity_profiles(email, alias) VALUES (?, 'Upgrade test')", (email,)
        )
    db.close()
    data = {
        "product": "antigravity",
        "email": email,
        "quota": {"gemini-weekly": {"remaining_fraction": 0.5}},
    }
    shell = Path(os.environ["SystemRoot"]) / "System32/cmd.exe"
    result = subprocess.run(
        f'"{shell}" /d /c ""{cli}" antigravity-status"',
        executable=str(shell),
        input=json.dumps(data),
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    with sqlite3.connect(database) as db:
        saved = db.execute(
            "SELECT quota FROM antigravity_profiles WHERE email = ?", (email,)
        ).fetchone()
    db.close()
    if result.stdout.strip() != "QuotaCrew: quota synced" or not saved or not saved[0]:
        raise RuntimeError("Installed Antigravity status-line callback did not save quota.")
    if json.loads(saved[0])["gemini-weekly"]["remaining_fraction"] != 0.5:
        raise RuntimeError("Installed Antigravity callback saved an unexpected quota value.")
    print("Installed CLI accepted an Antigravity quota snapshot through Windows cmd.exe.")


def main() -> None:
    if (
        sys.platform != "win32"
        or os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
    ):
        raise SystemExit(
            "Installer verification requires a disposable GitHub-hosted Windows runner."
        )

    import winreg

    import win32com.client

    def startup_command() -> str | None:
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
            ) as key:
                return winreg.QueryValueEx(key, "CodexAccountManager")[0]
        except FileNotFoundError:
            return None

    installers = list(Path("installer/Output").glob("QuotaCrew-Setup-*.exe"))
    if len(installers) != 1:
        raise RuntimeError("Expected exactly one freshly built installer.")
    shell = win32com.client.Dispatch("WScript.Shell")
    desktop_link = Path(shell.SpecialFolders("Desktop")) / "QuotaCrew.lnk"
    menu_dir = Path(shell.SpecialFolders("Programs")) / "QuotaCrew"
    menu_link = menu_dir / "QuotaCrew.lnk"
    data_dir = Path(os.environ["LOCALAPPDATA"]) / "CodexAccountManager"
    if (
        data_dir.exists()
        or desktop_link.exists()
        or menu_dir.exists()
        or startup_command() is not None
    ):
        raise RuntimeError(
            "Refusing to modify an existing application installation or data directory."
        )
    data_dir.mkdir()
    sentinel = data_dir / "installer-preservation-test.txt"
    sentinel.write_bytes(b"Synthetic user data must survive installation and removal.\n")
    expected = sentinel.read_bytes()
    database = data_dir / "accounts.db"
    saved_rows = {}
    with tempfile.TemporaryDirectory(
        prefix="account-manager-install-", dir=os.environ["RUNNER_TEMP"]
    ) as temporary:
        destination = Path(temporary) / "QuotaCrew Test"
        flags = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-"]
        try:
            previous = os.environ.get("QUOTACREW_PREVIOUS_INSTALLER")
            if previous:
                subprocess.run(
                    [
                        str(Path(previous).resolve(strict=True)),
                        *flags,
                        f"/DIR={destination}",
                        "/TASKS=",
                    ],
                    check=True,
                    timeout=180,
                )
                if not (destination / "QuotaCrew.exe").is_file():
                    raise RuntimeError("Previous version did not install for upgrade verification.")
                if sentinel.read_bytes() != expected:
                    raise RuntimeError("Previous installation modified user data.")
                old_cli = str(destination / "cli/cx.exe")
                subprocess.run(
                    [old_cli, "profile", "add", "Upgrade Çalışma"], check=True, timeout=60
                )
                with sqlite3.connect(database) as db:
                    profile_id = db.execute("SELECT id FROM profiles").fetchone()[0]
                    db.execute(
                        "INSERT INTO profile_accounts (profile_id,account_id,bound_at) VALUES (?,?,?)",
                        (profile_id, "synthetic-workspace", "2026-01-01"),
                    )
                    columns = {row[1] for row in db.execute("PRAGMA table_info(profile_accounts)")}
                    if "user_id" in columns:
                        db.execute("UPDATE profile_accounts SET user_id='synthetic-user'")
                    db.execute(
                        "INSERT OR REPLACE INTO settings VALUES ('monitor_enabled','false','2026-01-01')"
                    )
                    for table in ("profiles", "profile_accounts", "settings"):
                        cursor = db.execute(f"SELECT * FROM {table} ORDER BY 1")
                        saved_rows[table] = (
                            [column[0] for column in cursor.description],
                            cursor.fetchall(),
                        )
            subprocess.run(
                [
                    str(installers[0].resolve()),
                    *flags,
                    f"/DIR={destination}",
                    "/LANG=english",
                    f"/LOG={Path(temporary) / 'install.log'}",
                    "/TASKS=desktopicon,startupicon",
                ],
                check=True,
                timeout=180,
            )
            executable = destination / "QuotaCrew.exe"
            uninstaller = destination / "unins000.exe"
            if not executable.is_file() or not uninstaller.is_file():
                raise RuntimeError("Installed application or uninstaller is missing.")
            for link, target in (
                (desktop_link, executable),
                (menu_link, executable),
                (menu_dir / "Uninstall QuotaCrew.lnk", uninstaller),
            ):
                if not link.is_file():
                    raise RuntimeError(
                        f"Requested shortcut is missing: {link}; "
                        + (Path(temporary) / "install.log").read_text(errors="replace")
                    )
                shortcut = shell.CreateShortcut(str(link))
                if Path(shortcut.TargetPath).resolve() != target.resolve():
                    raise RuntimeError(f"Shortcut points to the wrong executable: {link.name}")
            if startup_command() != f'"{executable}"':
                raise RuntimeError("Start-with-Windows entry is missing or incorrectly quoted.")
            installed_cli = destination / "cli/cx.exe"
            subprocess.run([str(installed_cli), "--help"], check=True, timeout=60)
            subprocess.run([str(installed_cli), "init"], check=True, timeout=60)
            verify_antigravity_callback(installed_cli, database)
            if saved_rows:
                with sqlite3.connect(database) as db:
                    if db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                        raise RuntimeError("Upgraded account database is invalid.")
                    for table, (columns, rows) in saved_rows.items():
                        actual = db.execute(
                            f"SELECT {','.join(columns)} FROM {table} ORDER BY 1"
                        ).fetchall()
                        if actual != rows:
                            raise RuntimeError(f"Upgrade changed saved {table}.")
                db.close()
                expected_database = database.read_bytes()
            if sentinel.read_bytes() != expected:
                raise RuntimeError("Installation modified existing user data.")
            legacy_executable = destination / "CodexAccountManager.exe"
            legacy_executable.write_bytes(b"synthetic legacy executable; never executed")
            legacy_menu = menu_dir.with_name("Codex Account Manager")
            legacy_menu.mkdir()
            legacy_links = (
                desktop_link.with_name("Codex Account Manager.lnk"),
                menu_dir.parent / "Codex Account Manager.lnk",
                legacy_menu / "Codex Account Manager.lnk",
                legacy_menu / "Uninstall Codex Account Manager.lnk",
            )
            for link in legacy_links:
                shortcut = shell.CreateShortcut(str(link))
                shortcut.TargetPath = str(legacy_executable)
                shortcut.Save()
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0,
                winreg.KEY_SET_VALUE,
            ) as key:
                winreg.SetValueEx(
                    key, "CodexAccountManager", 0, winreg.REG_SZ, f'"{legacy_executable}"'
                )
            subprocess.run(
                [str(installers[0].resolve()), *flags, f"/DIR={destination}", "/TASKS="],
                check=True,
                timeout=180,
            )
            if (
                legacy_executable.exists()
                or any(link.exists() for link in legacy_links)
                or legacy_menu.exists()
                or not executable.is_file()
                or startup_command() != f'"{executable}"'
                or sentinel.read_bytes() != expected
            ):
                raise RuntimeError(
                    "QuotaCrew migration did not preserve data and startup registration."
                )
        finally:
            uninstaller = destination / "unins000.exe"
            if uninstaller.is_file():
                subprocess.run([str(uninstaller), *flags], check=True, timeout=180)
        if (
            (destination / "QuotaCrew.exe").exists()
            or desktop_link.exists()
            or menu_dir.exists()
            or startup_command() is not None
        ):
            raise RuntimeError(
                "Uninstall left application files, shortcuts or startup entry behind."
            )
        if sentinel.read_bytes() != expected:
            raise RuntimeError("Uninstall modified existing user data.")
        if saved_rows and database.read_bytes() != expected_database:
            raise RuntimeError("Reinstallation or uninstall modified the account database.")
    sentinel.unlink()
    # Account fixtures remain on this disposable runner for failure diagnostics.
    print("Installation, CLI, shortcuts, startup entry and user-data preservation verified.")


if __name__ == "__main__":
    main()
