"""Recover a completed goal from bounded, correlated Codex lifecycle records."""

from __future__ import annotations

import json
import re
from pathlib import Path

from codex_account_manager.adapters.interfaces import GoalInfo
from codex_account_manager.core.files import resolved_path

_MAX_TAIL_BYTES = 4 * 1024 * 1024
_COMPLETE_CALL = re.compile(
    r"tools\.update_goal\(\s*\{\s*status\s*:\s*['\"]complete['\"]\s*\}\s*\)"
)


def _completed_goal(value: object, thread_id: str) -> GoalInfo | None:
    if not isinstance(value, dict):
        return None
    if (
        value.get("threadId") != thread_id
        or value.get("status") not in ("complete", "completed")
        or not isinstance(value.get("objective"), str)
        or not value["objective"]
    ):
        return None
    budget = value.get("tokenBudget")
    used = value.get("tokensUsed", 0)
    if budget is not None and (type(budget) is not int or budget < 0):
        return None
    if type(used) is not int or used < 0:
        return None
    return GoalInfo(thread_id, value["objective"], value["status"], True, budget, used)


def read_completed_goal(
    rollout: str, codex_home: Path, thread_id: str, turn_id: str
) -> GoalInfo | None:
    """Require an explicit goal result and the same successfully completed turn.

    A missing current goal, final prose, or a completed turn alone proves nothing.
    Never execute recorded calls or search message text for completion phrases.
    """
    path = resolved_path(rollout)
    if path.suffix != ".jsonl" or not path.is_relative_to(resolved_path(codex_home / "sessions")):
        return None
    try:
        before = path.stat()
        offset = max(0, before.st_size - _MAX_TAIL_BYTES)
        with path.open("rb") as stream:
            stream.seek(offset)
            tail = stream.read(_MAX_TAIL_BYTES)
        after = path.stat()
    except OSError:
        return None
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or not tail.endswith(b"\n")
    ):
        return None
    lines = tail.splitlines()
    if offset:
        lines = lines[1:]
    candidate = None
    completed = False
    active_turn = None
    calls: set[str] = set()
    for line in lines:
        try:
            event = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(event, dict) or not isinstance(event.get("payload"), dict):
            return None
        payload = event["payload"]
        kind = payload.get("type")
        if event.get("type") == "event_msg":
            if kind == "thread_goal_updated":
                if payload.get("threadId") != thread_id:
                    return None
                candidate = _completed_goal(payload.get("goal"), thread_id)
                completed = False
            elif kind == "task_started":
                active_turn = payload.get("turn_id")
                candidate = None
                completed = False
                calls.clear()
            elif kind in {"task_complete", "task_completed", "turn_aborted"}:
                completed = (
                    kind != "turn_aborted"
                    and payload.get("turn_id") == turn_id
                    and candidate is not None
                )
            continue
        if event.get("type") != "response_item":
            continue
        attribution = payload.get("internal_chat_message_metadata_passthrough")
        record_turn = attribution.get("turn_id") if isinstance(attribution, dict) else active_turn
        if record_turn != turn_id:
            continue
        call_id = payload.get("call_id")
        if not isinstance(call_id, str):
            continue
        if kind in {"function_call", "custom_tool_call"}:
            name = payload.get("name")
            if name in {"update_goal", "functions.update_goal"}:
                try:
                    args = json.loads(payload.get("arguments", ""))
                except (ValueError, TypeError):
                    continue
                if isinstance(args, dict) and args.get("status") == "complete":
                    calls.add(call_id)
            elif name in {"exec", "functions.exec"}:
                code = payload.get("input")
                if isinstance(code, str) and _COMPLETE_CALL.search(code):
                    calls.add(call_id)
        elif kind in {"function_call_output", "custom_tool_call_output"} and call_id in calls:
            calls.discard(call_id)
            output = payload.get("output")
            parts = [{"text": output}] if isinstance(output, str) else output
            if not isinstance(parts, list):
                continue
            candidate = None
            completed = False
            for part in parts:
                if not isinstance(part, dict) or not isinstance(part.get("text"), str):
                    continue
                try:
                    result = json.loads(part["text"])
                except ValueError:
                    continue
                if isinstance(result, dict) and "goal" in result:
                    candidate = _completed_goal(result["goal"], thread_id)
    return candidate if completed else None
