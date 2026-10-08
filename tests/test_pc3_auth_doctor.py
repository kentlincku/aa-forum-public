"""P3：doctor 認證總檢（不實連網路、不碰真的 Keychain 與 CLI 登入）。"""
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from mbox import doctor  # noqa: E402
import drivers  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "var"
    (h / "tokens").mkdir(parents=True)
    monkeypatch.setenv("MBOX_HOME", str(h))
    monkeypatch.setenv("AAF_SERVER_STATE", str(h / "server"))
    monkeypatch.setenv("AAF_SECRETS_DIR", str(tmp_path / "sec"))
    monkeypatch.setenv("AAF_SECRETS_NO_KEYCHAIN", "1")
    monkeypatch.setattr(doctor, "home", lambda: h)
    return h


ROLES = {"me": {"rank": "human", "driver": "manual"},
         "w": {"rank": "worker", "driver": "command", "command": "true"}}


def test_missing_everything(home):
    r = doctor.auth_checks(ROLES, {"secrets": ["demo-s"]}, ssh_probe=False)
    assert r["auth:AA Forum 帳號"][0] is False
    assert r["auth:機密 demo-s"] == (False, "缺少：bin/aaf secret set demo-s")


def test_all_good(home):
    (home / "server").mkdir()
    (home / "server" / "accounts.json").write_text(json.dumps({"users": {"u": {}}}))
    for x in ("me", "w", "server"):
        f = home / "tokens" / x
        f.write_text("t"); os.chmod(f, 0o600)
    from mbox import secret_store
    secret_store.set_value("demo-s", "v")
    r = doctor.auth_checks(ROLES, {"secrets": ["demo-s"]}, ssh_probe=False)
    assert all(ok for ok, _ in r.values()), r
    assert "v" not in json.dumps(r["auth:機密 demo-s"])


def test_bad_token_permission(home):
    f = home / "tokens" / "w"
    f.write_text("t"); os.chmod(f, 0o644)
    ok, d = doctor.auth_checks(ROLES, None, ssh_probe=False)["auth:token 權限"]
    assert not ok and "w(0o644)" in d


def test_driver_auth_reported(home, monkeypatch):
    roles = {"a": {"rank": "worker", "driver": "codex"}}
    monkeypatch.setattr(drivers.DRIVERS["codex"], "auth_check", lambda self, timeout=20: (False, "未登入（codex login）"))
    r = doctor.auth_checks(roles, None, ssh_probe=False)
    assert r["auth:登入 codex"] == (False, "未登入（codex login）")


def test_parse_auth_outputs():
    from drivers.claude import ClaudeHeadless
    from drivers.codex import CodexHeadless
    from drivers.hermes import HermesHeadless
    c = ClaudeHeadless.parse_auth(None, 0, json.dumps({"loggedIn": True, "authMethod": "x", "email": "a@b"}), "")
    assert c == (True, "已登入（x）")
    assert ClaudeHeadless.parse_auth(None, 1, json.dumps({"loggedIn": False}), "")[0] is False
    assert CodexHeadless.parse_auth(None, 0, "Logged in using ChatGPT\n", "")[0] is True
    assert CodexHeadless.parse_auth(None, 1, "Not logged in\n", "")[0] is False
    h = HermesHeadless.parse_auth(None, 0, "copilot (2 credentials):\n  #1 x\n", "")
    assert h == (True, "已設定憑證：copilot")
    assert HermesHeadless.parse_auth(None, 0, "", "")[0] is False
