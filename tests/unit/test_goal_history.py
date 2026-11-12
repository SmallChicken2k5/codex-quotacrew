import json
from unittest.mock import AsyncMock

import pytest

from codex_account_manager.adapters.app_server import CodexAppServer
from codex_account_manager.codex import goal_history
from codex_account_manager.codex.goal_history import read_completed_goal


def _record(kind, **payload):
    return {"type": "response_item", "payload": {"type": kind, **payload}}


def _event(kind, **payload):
    return {"type": "event_msg", "payload": {"type": kind, **payload}}


def _history():
    goal = {
        "threadId": "thread",
        "objective": "Finish the task",
        "status": "complete",
        "tokensUsed": 42,
        "tokenBudget": 500,
    }
    attribution = {"turn_id": "turn"}
    return [
        _event("task_started", turn_id="turn"),
        _record(
            "custom_tool_call",
            name="exec",
            call_id="call",
            input='text(await tools.update_goal({status:"complete"}));',
            internal_chat_message_metadata_passthrough=attribution,
        ),
        _record(
            "custom_tool_call_output",
            call_id="call",
            output=[{"type": "input_text", "text": json.dumps({"goal": goal})}],
            internal_chat_message_metadata_passthrough=attribution,
        ),
        _event("task_complete", turn_id="turn"),
    ]


def _write(tmp_path, history):
    rollout = tmp_path / "sessions" / "rollout.jsonl"
    rollout.parent.mkdir(exist_ok=True)
    rollout.write_text("".join(json.dumps(x) + "\n" for x in history), encoding="utf-8")
    return rollout


def test_explicit_correlated_completion_survives_missing_current_goal(tmp_path):
    rollout = _write(tmp_path, _history())
    goal = read_completed_goal(str(rollout), tmp_path, "thread", "turn")
    assert goal is not None
    assert (goal.status, goal.objective, goal.token_budget, goal.tokens_used) == (
        "complete",
        "Finish the task",
        500,
        42,
    )


@pytest.mark.parametrize(
    "case",
    [
        "missing_result",
        "missing_call",
        "wrong_call",
        "wrong_thread",
        "wrong_turn",
        "failed_goal",
        "failed_turn",
        "unfinished_turn",
        "new_turn",
        "new_goal",
        "cleared_goal",
        "message_only",
        "wrong_tool",
        "wrong_attribution",
        "malformed_result",
    ],
)
def test_missing_goal_does_not_imply_success(tmp_path, case):
    history = _history()
    if case == "missing_result":
        history.pop(2)
    elif case == "missing_call":
        history.pop(1)
    elif case == "wrong_call":
        history[2]["payload"]["call_id"] = "other"
    elif case == "wrong_thread":
        result = json.loads(history[2]["payload"]["output"][0]["text"])
        result["goal"]["threadId"] = "other"
        history[2]["payload"]["output"][0]["text"] = json.dumps(result)
    elif case == "wrong_turn":
        history[-1]["payload"]["turn_id"] = "other"
    elif case == "failed_goal":
        result = json.loads(history[2]["payload"]["output"][0]["text"])
        result["goal"]["status"] = "paused"
        history[2]["payload"]["output"][0]["text"] = json.dumps(result)
    elif case == "failed_turn":
        history[-1]["payload"]["type"] = "turn_aborted"
    elif case == "unfinished_turn":
        history.pop()
    elif case == "new_turn":
        history.append(_event("task_started", turn_id="new"))
    elif case in {"new_goal", "cleared_goal"}:
        goal = {"threadId": "thread", "objective": "New work", "status": "active"}
        history.append(
            _event(
                "thread_goal_updated", threadId="thread", goal=goal if case == "new_goal" else None
            )
        )
    elif case == "message_only":
        history[2]["payload"]["type"] = "message"
    elif case == "wrong_tool":
        history[1]["payload"]["name"] = "shell"
    elif case == "wrong_attribution":
        history[2]["payload"]["internal_chat_message_metadata_passthrough"]["turn_id"] = "other"
    elif case == "malformed_result":
        history[2]["payload"]["output"][0]["text"] = "not json"
    rollout = _write(tmp_path, history)
    assert read_completed_goal(str(rollout), tmp_path, "thread", "turn") is None


def test_direct_goal_tool_and_native_lifecycle_event(tmp_path):
    history = _history()
    history[1] = _record(
        "function_call",
        name="update_goal",
        call_id="call",
        arguments=json.dumps({"status": "complete"}),
    )
    history[2]["payload"]["output"] = history[2]["payload"]["output"][0]["text"]
    history[2]["payload"]["type"] = "function_call_output"
    rollout = _write(tmp_path, history)
    assert read_completed_goal(str(rollout), tmp_path, "thread", "turn") is not None
    goal = json.loads(history[2]["payload"]["output"])["goal"]
    rollout = _write(
        tmp_path,
        [history[0], _event("thread_goal_updated", threadId="thread", goal=goal), history[-1]],
    )
    assert read_completed_goal(str(rollout), tmp_path, "thread", "turn") is not None


def test_truncated_or_corrupt_history_and_outside_path_are_unknown(tmp_path):
    rollout = _write(tmp_path, _history())
    assert read_completed_goal(str(rollout), tmp_path / "other", "thread", "turn") is None
    complete = rollout.read_bytes()
    rollout.write_bytes(complete[:-1])
    assert read_completed_goal(str(rollout), tmp_path, "thread", "turn") is None
    rollout.write_bytes(b"invalid\n" + complete)
    assert read_completed_goal(str(rollout), tmp_path, "thread", "turn") is None


def test_bounded_tail_can_correlate_a_long_turn_without_its_start(tmp_path, monkeypatch):
    history = _history()
    history.insert(1, _record("message", content="padding" * 1000))
    rollout = _write(tmp_path, history)
    monkeypatch.setattr(goal_history, "_MAX_TAIL_BYTES", 2000)
    assert read_completed_goal(str(rollout), tmp_path, "thread", "turn") is not None


async def test_adapter_completion_read_is_read_only_and_checks_identity(tmp_path, monkeypatch):
    rollout = _write(tmp_path, _history())
    adapter = CodexAppServer(tmp_path)
    request = AsyncMock(return_value={"thread": {"id": "thread", "path": str(rollout)}})
    monkeypatch.setattr(adapter, "_request_with_discovery_retry", request)
    assert await adapter.completed_goal("thread", "turn") is not None
    request.assert_awaited_once_with("thread/read", {"threadId": "thread", "includeTurns": False})
    request.return_value["thread"]["id"] = "other"
    assert await adapter.completed_goal("thread", "turn") is None


@pytest.mark.parametrize("status", [[], {}, 0, None])
def test_malformed_goal_status_is_unknown(tmp_path, status):
    history = _history()
    result = json.loads(history[2]["payload"]["output"][0]["text"])
    result["goal"]["status"] = status
    history[2]["payload"]["output"][0]["text"] = json.dumps(result)
    rollout = _write(tmp_path, history)
    assert read_completed_goal(str(rollout), tmp_path, "thread", "turn") is None
