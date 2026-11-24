import sys
from pathlib import Path
from textwrap import dedent

import pytest

from codex_account_manager.accounts.service import AccountService
from codex_account_manager.adapters import app_server
from codex_account_manager.core import protection
from tests.fakes import account_auth


@pytest.mark.parametrize("session_api", ["supported", "method_not_found", "unknown_variant"])
async def test_account_service_negotiates_identity_with_old_and_new_servers(
    migrated_db, tmp_path, monkeypatch, session_api
):
    home = migrated_db.shared_codex_home
    source = home / "auth.json"
    source.write_bytes(account_auth("W1", "U1"))
    stub = tmp_path / "app_server.py"
    stub.write_text(
        dedent("""\
        import json
        import sys

        experimental = False
        mode = sys.argv[1]
        for line in sys.stdin:
            request = json.loads(line)
            if "id" not in request:
                continue
            method = request["method"]
            response = {"id": request["id"]}
            if method == "initialize":
                experimental = request["params"]["capabilities"]["experimentalApi"]
                response["result"] = {"userAgent": "synthetic-test-server"}
            elif method == "accountSession/list":
                if mode == "method_not_found":
                    response["error"] = {"code": -32601, "message": "Method not found"}
                elif mode == "unknown_variant":
                    response["error"] = {
                        "code": -32600,
                        "message": "Invalid request: unknown variant `accountSession/list`, expected one of `initialize`, `account/read`",
                    }
                elif not experimental:
                    response["error"] = {
                        "code": -32600,
                        "message": "accountSession/list requires experimentalApi capability",
                    }
                else:
                    response["result"] = {
                        "activeSessionId": "S1",
                        "sessions": [{"sessionId": "S1", "isActive": True,
                                      "userId": "U1", "selectedWorkspaceAccountId": "W1"}],
                    }
            elif method == "account/read":
                assert request["params"]["refreshToken"] is False
                response["result"] = {"account": {"type": "chatgpt", "planType": "plus"}}
            elif method == "account/rateLimits/read":
                response["result"] = {"accountId": "W1", "ordinaryUsageAllowed": True}
            else:
                response["error"] = {"code": -32601, "message": "Method not found"}
            print(json.dumps(response), flush=True)
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        app_server, "codex_command", lambda *args: [sys.executable, "-u", str(stub), session_api]
    )
    accounts = AccountService()
    snapshot = await accounts.read_snapshot(home)
    assert snapshot.ordinary_usage_allowed is True
    assert snapshot.identity.owner_key == ("chatgpt", "U1", "W1")
    profile = await accounts.import_profile("Imported", source)
    assert profile.identity.owner_key == snapshot.identity.owner_key
    assert (await accounts.list_profiles()) == [profile]
    assert source.read_bytes() == account_auth("W1", "U1")
    assert accounts.credentials.read_profile(profile.codex_home) == source.read_bytes()
    if protection.available():
        assert not (Path(profile.codex_home) / "auth.json").exists()
        assert (Path(profile.codex_home) / "auth.dpapi").read_bytes().startswith(protection.MAGIC)
