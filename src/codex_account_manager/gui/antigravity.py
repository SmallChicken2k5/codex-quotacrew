"""Account catalog and manual sign-in handoff for Antigravity."""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from PySide6.QtCore import QProcess, QSignalBlocker, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidgetItem,
    QVBoxLayout,
)

from codex_account_manager.antigravity import (
    STATUS_COMMAND,
    AntigravityProfile,
    AntigravityProfiles,
    normalize_email,
)
from codex_account_manager.core.windows_shell import launch_from_explorer
from codex_account_manager.gui.async_runner import AsyncRunner
from codex_account_manager.gui.i18n import format_timestamp, tr
from codex_account_manager.gui.view_base import BaseView, page_panel, table_widget, view_header

AUTH_GUIDE = "https://antigravity.google/docs/install/"
DOWNLOADS = "https://antigravity.google/download"
SETUP_GUIDE = "https://antigravity.google/docs/cli/commands/statusline/"
FRESH_SECONDS = 15 * 60


def antigravity_executable() -> Path | None:
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
        system = Path(os.environ.get("ProgramFiles", "C:/Program Files"))
        choices = [
            local / "Programs/antigravity/Antigravity.exe",
            Path.home() / "AppData/Local/Programs/antigravity/Antigravity.exe",
            system / "Antigravity/Antigravity.exe",
        ]
    elif sys.platform == "darwin":
        choices = [Path("/Applications/Antigravity.app")]
    else:
        executable = shutil.which("antigravity")
        choices = [Path(executable)] if executable else []
    return next(
        (path for path in choices if path.is_file() or path.suffix == ".app" and path.is_dir()),
        None,
    )


def statusline_setup() -> str | None:
    directory = Path(sys.executable).parent
    name = "cx.exe" if sys.platform == "win32" else "cx"
    located = shutil.which("cx")
    choices = [directory / "cli" / name, directory / name]
    if located:
        choices.append(Path(located))
    executable = next((path for path in choices if path.is_file()), None)
    if executable is None:
        return None
    if sys.platform == "win32":
        # cmd.exe expands these even within double-quoted paths.
        if any(character in str(executable) for character in "%!\r\n"):
            return None
        command = f'""{executable}" {STATUS_COMMAND}"'
    else:
        command = f"{shlex.quote(str(executable))} {STATUS_COMMAND}"
    return f"/statusline {command}"


def _text(text: str, style: str = "Muted") -> QLabel:
    label = QLabel(text)
    label.setObjectName(style)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


class ProfileDialog(QDialog):
    def __init__(self, parent, profile: AntigravityProfile | None):
        super().__init__(parent)
        self.setWindowTitle(tr("Edit account" if profile else "Add Antigravity account"))
        self.setMinimumWidth(445)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.email = QLineEdit(profile.email if profile else "")
        self.email.setReadOnly(profile is not None)
        self.email.setMaxLength(254)
        self.alias = QLineEdit(profile.alias if profile else "")
        self.alias.setMaxLength(80)
        form.addRow(tr("Google account email"), self.email)
        form.addRow(tr("Display name (optional)"), self.alias)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        save = buttons.button(QDialogButtonBox.StandardButton.Save)
        save.setText(tr("Save"))
        save.setEnabled(profile is not None)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("Cancel"))
        self.email.textChanged.connect(
            lambda value: save.setEnabled(normalize_email(value) is not None)
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class AntigravityView(BaseView):
    def __init__(self, runner: AsyncRunner):
        super().__init__()
        self.runner = runner
        self.store = AntigravityProfiles()
        self._profiles: list[AntigravityProfile] = []
        self._busy = False
        self._reload_pending = False
        self._dialogs: set[QDialog] = set()

        self.add = QPushButton(tr("Add account"))
        self.add.setObjectName("Primary")
        self.add.clicked.connect(lambda: self._edit())
        self._root.addWidget(
            view_header(
                "Antigravity",
                tr("Manage saved Google accounts and their last reported quotas."),
                self.add,
            )
        )

        panel, body = page_panel()
        self._root.addWidget(panel, 1)
        self.accounts = table_widget(
            [tr("Account"), tr("Google account email"), tr("Last quota update")]
        )
        self.accounts.fit_columns((136, 188, 155), (1, 2, 1))
        self.accounts.empty.set_content(
            tr("No Antigravity accounts saved"), tr("Add a Google account to track its quota here.")
        )

        toolbar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Search by account name or email"))
        self.search.setAccessibleName(self.search.placeholderText())
        self.search.textChanged.connect(self._show_accounts)
        toolbar.addWidget(self.search, 1)
        setup = QPushButton(tr("Connect CLI"))
        setup.clicked.connect(self._show_setup)
        toolbar.addWidget(setup)
        reload_button = QPushButton(tr("Refresh"))
        reload_button.clicked.connect(self.refresh)
        toolbar.addWidget(reload_button)
        body.addLayout(toolbar)
        body.addWidget(self.accounts, 3)

        actions = QHBoxLayout()
        self.switch = QPushButton(tr("Switch manually"))
        self.switch.setObjectName("Primary")
        self.switch.clicked.connect(self._show_switch)
        actions.addWidget(self.switch)
        self.pin = QPushButton(tr("Set preferred"))
        self.pin.clicked.connect(self._toggle_preferred)
        actions.addWidget(self.pin)
        actions.addStretch()
        self.edit = QPushButton(tr("Rename"))
        self.edit.clicked.connect(self._edit_selected)
        actions.addWidget(self.edit)
        self.remove = QPushButton(tr("Remove"))
        self.remove.clicked.connect(self._remove_selected)
        actions.addWidget(self.remove)
        body.addLayout(actions)

        self.quota_title = _text(tr("Model quotas"), "FieldTitle")
        body.addWidget(self.quota_title)
        self.quotas = table_widget([tr("Quota group"), tr("Remaining"), tr("Resets at")])
        self.quota_title.setBuddy(self.quotas)
        self.quotas.fit_columns((166, 118, 155), (2, 1, 1))
        self.quotas.empty.set_content(
            tr("No quota snapshot"),
            tr("Connect the Antigravity CLI to receive quota from the signed-in account."),
        )
        self.quotas.setMinimumHeight(124)
        self.quotas.verticalHeader().setDefaultSectionSize(36)
        body.addWidget(self.quotas, 2)
        self.feedback = _text("")
        self.feedback.hide()
        body.addWidget(self.feedback)

        self.timer = QTimer(self)
        self.timer.setInterval(5000)
        self.timer.timeout.connect(self.refresh)
        self.accounts.itemSelectionChanged.connect(self._show_selected)
        self._show_selected()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.timer.start()

    def hideEvent(self, event) -> None:
        self.timer.stop()
        super().hideEvent(event)

    def refresh(self) -> None:
        if not self._busy:
            self._busy = True
            self.runner.submit(self.store.list(), self._loaded, self._failed)

    def _changed(self, _result: None) -> None:
        if self._busy:
            self._reload_pending = True
        else:
            self.refresh()

    def _loaded(self, profiles: list[AntigravityProfile]) -> None:
        self._busy = False
        if self._reload_pending:
            self._reload_pending = False
            self.refresh()
            return
        self.feedback.hide()
        self._profiles = profiles
        self._show_accounts()

    def _failed(self, error: Exception) -> None:
        self._busy = False
        self._display_error(error)
        if self._reload_pending:
            self._reload_pending = False
            self.refresh()

    def _display_error(self, error: Exception) -> None:
        self.feedback.setText(tr(str(error)))
        self.feedback.show()

    def _select_profile(self) -> AntigravityProfile | None:
        item = self.accounts.item(self.accounts.currentRow(), 0)
        return next(
            (
                profile
                for profile in self._profiles
                if item is not None and item.data(Qt.ItemDataRole.UserRole) == profile.email
            ),
            None,
        )

    def _show_accounts(self) -> None:
        selected = self.accounts.item(self.accounts.currentRow(), 0)
        selected_email = selected.data(Qt.ItemDataRole.UserRole) if selected is not None else None
        query = self.search.text().strip().casefold()
        accounts = [
            profile
            for profile in self._profiles
            if query in profile.email.casefold() or query in profile.alias.casefold()
        ]
        self.accounts.empty.set_content(
            tr("No matching accounts") if self._profiles else tr("No Antigravity accounts saved"),
            tr("Try a different search.")
            if self._profiles
            else tr("Add a Google account to track its quota here."),
        )
        index = 0
        with QSignalBlocker(self.accounts):
            self.accounts.setRowCount(len(accounts))
            for row, profile in enumerate(accounts):
                item = self._row(
                    self.accounts,
                    row,
                    (
                        "★ " + profile.alias if profile.preferred else profile.alias,
                        profile.email,
                        self._observation(profile),
                    ),
                )
                item.setData(Qt.ItemDataRole.UserRole, profile.email)
                if selected_email == profile.email:
                    index = row
            if accounts:
                self.accounts.selectRow(index)
        self._show_selected()

    @staticmethod
    def _row(table, row: int, values: tuple[str, str, str]) -> QTableWidgetItem:
        items = [QTableWidgetItem(value) for value in values]
        for column, item in enumerate(items):
            item.setToolTip(item.text())
            table.setItem(row, column, item)
        return items[0]

    @staticmethod
    def _observation(profile: AntigravityProfile) -> str:
        if profile.observed_at is None:
            return tr("Never received")
        observed = datetime.fromtimestamp(profile.observed_at, UTC)
        stamp = format_timestamp(observed, year=False)
        if (datetime.now(UTC) - observed).total_seconds() >= FRESH_SECONDS:
            return tr("Stale · {time}", time=stamp)
        return stamp

    def _show_selected(self) -> None:
        profile = self._select_profile()
        for button in (self.switch, self.pin, self.edit, self.remove):
            button.setEnabled(profile is not None)
        self.pin.setText(
            tr("Clear preferred" if profile and profile.preferred else "Set preferred")
        )
        buckets = profile.quota if profile else ()
        self.quota_title.setText(
            tr("Model quotas for {account}", account=profile.alias)
            if profile
            else tr("Model quotas")
        )
        self.quotas.setRowCount(len(buckets))
        for row, bucket in enumerate(buckets):
            remaining = tr("Unknown")
            if bucket.disabled:
                remaining = tr("Unavailable")
            elif bucket.fraction is not None:
                remaining = f"{bucket.fraction:.2%}"
            elif bucket.amount is not None:
                remaining = f"{bucket.amount:,}"
            self._row(
                self.quotas,
                row,
                (
                    bucket.name,
                    remaining,
                    format_timestamp(bucket.reset, year=False) if bucket.reset else "—",
                ),
            )

    def _edit(self, profile: AntigravityProfile | None = None) -> None:
        dialog = ProfileDialog(self, profile)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.runner.submit(
                self.store.save(
                    dialog.email.text(), dialog.alias.text(), existing=profile is not None
                ),
                self._changed,
                self._display_error,
            )
        dialog.deleteLater()

    def _edit_selected(self) -> None:
        if profile := self._select_profile():
            self._edit(profile)

    def _toggle_preferred(self) -> None:
        if profile := self._select_profile():
            self.runner.submit(
                self.store.toggle_preferred(profile.email), self._changed, self._display_error
            )

    def _remove_selected(self) -> None:
        if (profile := self._select_profile()) and QMessageBox.question(
            self,
            tr("Remove account"),
            tr("Remove this saved account and its quota snapshot from QuotaCrew?"),
        ) == QMessageBox.StandardButton.Yes:
            self.runner.submit(self.store.remove(profile.email), self._changed, self._display_error)

    def _dialog(self, title: str, description: str) -> tuple[QDialog, QVBoxLayout]:
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setMinimumWidth(460)
        root = QVBoxLayout(dialog)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)
        root.addWidget(_text(title, "H2"))
        root.addWidget(_text(description))
        self._dialogs.add(dialog)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.finished.connect(lambda _: self._dialogs.discard(dialog))
        return dialog, root

    def _show_switch(self) -> None:
        if not (profile := self._select_profile()):
            return
        dialog, root = self._dialog(
            tr("Switch Antigravity account"),
            tr(
                "In Antigravity, open Settings → Account, sign out if needed, then sign in with Google and choose the address below. For the CLI, type /logout and reopen agy to sign in."
            ),
        )
        target = QLineEdit(profile.email)
        target.setReadOnly(True)
        target.setAccessibleName(tr("Google account email"))
        root.addWidget(target)
        actions = QHBoxLayout()
        copy = QPushButton(tr("Copy email"))
        copy.clicked.connect(lambda: QApplication.clipboard().setText(profile.email))
        actions.addWidget(copy)
        help_button = QPushButton(tr("Sign-in help"))
        help_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(AUTH_GUIDE)))
        actions.addWidget(help_button)
        actions.addStretch()
        open_app = QPushButton(tr("Open Antigravity"))
        open_app.setObjectName("Primary")
        open_app.clicked.connect(lambda: self._open_client(dialog))
        actions.addWidget(open_app)
        root.addLayout(actions)
        dialog.show()

    def _open_client(self, dialog: QDialog) -> None:
        path = antigravity_executable()
        try:
            if path is not None:
                if sys.platform == "win32":
                    launch_from_explorer(path, [])
                else:
                    executable, args = (
                        ("open", [str(path)]) if path.suffix == ".app" else (str(path), [])
                    )
                    if not QProcess.startDetached(executable, args)[0]:
                        raise OSError("Antigravity did not open.")
                return
        except Exception:
            QMessageBox.warning(
                dialog,
                "Antigravity",
                tr("Could not open Antigravity. Open it from your application menu."),
            )
            return
        QDesktopServices.openUrl(QUrl(DOWNLOADS))

    def _show_setup(self) -> None:
        command = statusline_setup()
        dialog, root = self._dialog(
            tr("Connect Antigravity CLI"),
            tr(
                "Paste this command at the agy prompt, then run /usage. Save any existing custom status line before replacing it. To restore the default, run /statusline reset."
            ),
        )
        if command:
            field = QLineEdit(command)
            field.setReadOnly(True)
            field.setAccessibleName(tr("CLI setup command"))
            root.addWidget(field)
            copy = QPushButton(tr("Copy command"))
            copy.clicked.connect(lambda: QApplication.clipboard().setText(command))
            root.addWidget(copy)
        else:
            root.addWidget(
                _text(
                    tr(
                        "The QuotaCrew CLI was not found at a usable path. Install the Windows Setup package or put cx on PATH."
                    )
                )
            )
        help_button = QPushButton(tr("Status line help"))
        help_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(SETUP_GUIDE)))
        root.addWidget(help_button)
        dialog.show()
