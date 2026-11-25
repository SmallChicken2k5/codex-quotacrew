"""Local Antigravity profiles and the official CLI status-line input."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import BinaryIO, TextIO

from codex_account_manager.core.paths import paths

STATUS_COMMAND = "antigravity-status"
MAX_STATUS_BYTES = 256 * 1024
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
_BUCKET = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,95}")


def normalize_email(value: object) -> str | None:
    if isinstance(value, str):
        email = value.strip().lower()
        if len(email) <= 254 and email.isprintable() and _EMAIL.fullmatch(email):
            return email
    return None


@dataclass(frozen=True)
class QuotaBucket:
    name: str
    fraction: float | None
    amount: int | float | None
    reset: datetime | None
    disabled: bool


def parse_buckets(value: object) -> tuple[QuotaBucket, ...]:
    if not isinstance(value, dict):
        return ()
    buckets = []
    for name, item in list(value.items())[:100]:
        if not isinstance(name, str) or not _BUCKET.fullmatch(name) or not isinstance(item, dict):
            continue
        fraction = item.get("remaining_fraction")
        amount = item.get("remaining_amount")
        if (
            not isinstance(fraction, (int, float))
            or isinstance(fraction, bool)
            or not 0 <= fraction <= 1
        ):
            fraction = None
        if (
            not isinstance(amount, (int, float))
            or isinstance(amount, bool)
            or not 0 <= amount <= 1e15
        ):
            amount = None
        reset = None
        stamp = item.get("reset_time")
        if isinstance(stamp, str) and len(stamp) <= 48:
            try:
                parsed = datetime.fromisoformat(stamp)
                if parsed.tzinfo is not None:
                    reset = parsed.astimezone(UTC)
            except (ValueError, OverflowError):
                pass
        disabled = item.get("disabled") is True
        if fraction is not None or amount is not None or reset is not None or disabled:
            buckets.append(QuotaBucket(name, fraction, amount, reset, disabled))
    return tuple(sorted(buckets, key=lambda bucket: bucket.name))


@dataclass(frozen=True)
class AntigravityProfile:
    email: str
    alias: str
    preferred: bool
    observed_at: int | None
    quota: tuple[QuotaBucket, ...]


class AntigravityProfiles:
    async def list(self) -> list[AntigravityProfile]:
        from codex_account_manager.storage.database import connect

        async with connect() as db:
            rows = await (
                await db.execute(
                    "SELECT email, alias, preferred, observed_at, quota "
                    "FROM antigravity_profiles ORDER BY preferred DESC, alias COLLATE NOCASE, email"
                )
            ).fetchall()
        return [
            AntigravityProfile(
                email, alias, bool(preferred), observed, parse_buckets(json.loads(quota or "{}"))
            )
            for email, alias, preferred, observed, quota in rows
        ]

    async def save(self, email: str, alias: str, *, existing: bool = False) -> None:
        from codex_account_manager.storage.database import connect

        identity = normalize_email(email)
        if identity is None:
            raise ValueError("Enter the email used to sign in to Antigravity.")
        alias = alias.strip() or identity
        if len(alias) > 254 or not alias.isprintable():
            raise ValueError("Enter a valid account label.")
        async with connect() as db:
            if existing:
                await db.execute(
                    "UPDATE antigravity_profiles SET alias = ? WHERE email = ?", (alias, identity)
                )
            else:
                try:
                    await db.execute(
                        "INSERT INTO antigravity_profiles (email, alias) VALUES (?, ?)",
                        (identity, alias),
                    )
                except sqlite3.IntegrityError:
                    raise ValueError("This Antigravity account is already saved.") from None
            await db.commit()

    async def remove(self, email: str) -> None:
        from codex_account_manager.storage.database import connect

        async with connect() as db:
            await db.execute("DELETE FROM antigravity_profiles WHERE email = ?", (email,))
            await db.commit()

    async def toggle_preferred(self, email: str) -> None:
        from codex_account_manager.storage.database import connect

        async with connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    "SELECT preferred FROM antigravity_profiles WHERE email = ?", (email,)
                )
            ).fetchone()
            if row is None:
                return
            await db.execute("UPDATE antigravity_profiles SET preferred = 0 WHERE preferred = 1")
            if not row[0]:
                await db.execute(
                    "UPDATE antigravity_profiles SET preferred = 1 WHERE email = ?", (email,)
                )
            await db.commit()


def record_statusline(payload: object) -> bool:
    """Accept quota from a known email, retaining only the documented quota fields."""
    if not isinstance(payload, dict) or payload.get("product") != "antigravity":
        return False
    email = normalize_email(payload.get("email"))
    quota = parse_buckets(payload.get("quota"))
    if email is None or not quota or not paths.db_path.is_file():
        return False
    observed = int(time.time())
    data = json.dumps(
        {
            bucket.name: {
                "remaining_fraction": bucket.fraction,
                "remaining_amount": bucket.amount,
                "reset_time": bucket.reset.isoformat() if bucket.reset else None,
                "disabled": bucket.disabled,
            }
            for bucket in quota
        },
        separators=(",", ":"),
    )
    with closing(sqlite3.connect(paths.db_path.as_uri() + "?mode=rw", uri=True, timeout=0.5)) as db:
        row = db.execute(
            "SELECT observed_at, quota FROM antigravity_profiles WHERE email = ?", (email,)
        ).fetchone()
        if row is None:
            return False
        # Status-line events can arrive many times per second. Keep a short heartbeat.
        if row[1] == data and row[0] is not None and row[0] > observed - 15:
            return True
        db.execute(
            "UPDATE antigravity_profiles SET observed_at = ?, quota = ? WHERE email = ? "
            "AND (observed_at IS NULL OR observed_at <= ?)",
            (observed, data, email, observed),
        )
        db.commit()
    return True


def statusline_command(stdin: BinaryIO, stdout: TextIO) -> None:
    try:
        raw = stdin.read(MAX_STATUS_BYTES + 1)
        saved = len(raw) <= MAX_STATUS_BYTES and record_statusline(json.loads(raw))
    except (ValueError, RecursionError, OSError, sqlite3.Error):
        saved = False
    stdout.write(
        "QuotaCrew: quota synced\n"
        if saved
        else "QuotaCrew: waiting for a saved account and quota\n"
    )
