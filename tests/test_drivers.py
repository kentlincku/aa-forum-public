"""S2 driver 測試：註冊表、manifest 契約、舊欄位相容、session 收歸核心、check()、假 driver 全流程。"""
import json
import time
from pathlib import Path

import pytest

import drivers
from mbox import contract
from mbox.core import Store

FIX = Path(__file__).parent / "fixtures" / "turns"


@pytest.mark.parametrize("name", list(drivers.DRIVERS))
def test_every_driver_has_valid_manifest(name):
    m = drivers.DRIVERS[name].manifest()
    contract.validate("manifest", m)
    assert m["name"] == name


@pytest.mark.parametrize("legacy,new", [("hermes-headless", "hermes"), ("codex-headless", "codex"),
                                        ("claude-headless", "claude"), ("pi-headless", "pi"),
                                        ("tmux", "tmux"), ("manual", "manual"), ("command", "command")])
def test_legacy_adapter_field_resolves(legacy, new, tmp_path):
    assert drivers.resolve({"adapter": legacy}) == new
    ad = drivers.make("builder", {"adapter": legacy, "workdir": str(tmp_path / "w"), "command": "echo hi"}, tmp_path)
    assert ad.name == new


def test_driver_field_wins_and_unknown_rejected(tmp_path):
    assert drivers.resolve({"driver": "pi", "adapter": "hermes-headless"}) == "pi"
    with pytest.raises(SystemExit):
        drivers.make("builder", {"driver": "nope", "workdir": str(tmp_path / "w")}, tmp_path)


def test_adapters_is_compat_alias():
    import adapters
    assert adapters is drivers and adapters.HermesHeadless is drivers.HermesHeadless


def test_session_goes_to_core_and_imports_legacy_file(tmp_path):
    home = tmp_path / "home"
    s = Store(tmp_path / "m.sqlite3")
    cfg = {"driver": "hermes", "workdir": str(tmp_path / "w")}
    ad = drivers.make("builder", cfg, home)
    ad._session_file().write_text("LEGACY")          # v1 遺留的 session.hermes
    ad.sessions = s
    assert ad.session() == "LEGACY"                   # 讀檔並匯入核心
    assert s.get_session("builder", "hermes") == "LEGACY"
    ad.save_session("NEW")
    assert s.get_session("builder", "hermes") == "NEW"
    assert ad._session_file().read_text() == "NEW"    # S4 前 AA Forum 仍讀檔，鏡像保留
    # 不同 driver 各自獨立（換 driver 不拿別人的 session）
    other = drivers.make("builder", {"driver": "codex", "workdir": str(tmp_path / "w")}, home)
    other.sessions = s
    assert other.session() is None


def test_pi_session_id_created_once_and_stored(tmp_path, monkeypatch):
    monkeypatch.setattr(drivers, "system_prompt", lambda *a: "SP")
    s = Store(tmp_path / "m.sqlite3")
    ad = drivers.make("tp", {"driver": "pi", "workdir": str(tmp_path / "w")}, tmp_path / "h")
    ad.sessions = s
    a1 = ad.argv("x")
    sid = a1[a1.index("--session-id") + 1]
    assert s.get_session("tp", "pi") == sid and ad.argv("y")[a1.index("--session-id") + 1] == sid


def test_check_reports_missing_and_broken_binary(tmp_path):
    ad = drivers.make("builder", {"driver": "hermes", "bin": str(tmp_path / "nope"), "workdir": str(tmp_path / "w")}, tmp_path)
    ok, why = ad.check()
    assert not ok and "找不到" in why
    broken = tmp_path / "broken"
    broken.write_text("#!/bin/sh\nexit 126\n")
    broken.chmod(0o755)
    ad = drivers.make("builder", {"driver": "hermes", "bin": str(broken), "workdir": str(tmp_path / "w")}, tmp_path)
    ok, why = ad.check()
    assert not ok and "126" in why
    good = tmp_path / "good"
    good.write_text("#!/bin/sh\necho fake 1.0\n")
    good.chmod(0o755)
    ad = drivers.make("builder", {"driver": "codex", "bin": str(good), "workdir": str(tmp_path / "w")}, tmp_path)
    assert ad.check() == (True, "fake 1.0")
    assert drivers.make("k", {"driver": "manual"}, tmp_path).check()[0]


def test_interactive_cmd_by_capability(tmp_path):
    for name in ("hermes", "codex", "claude", "pi"):
        ad = drivers.make("r", {"driver": name, "workdir": str(tmp_path / "w")}, tmp_path)
        assert "interactive_room" in ad.describe()["capabilities"]
        cmd = ad.interactive_cmd(model="m1", prompt="hi")
        assert cmd and "m1" in cmd and "hi" in cmd
    ad = drivers.make("r", {"driver": "command", "command": "echo", "workdir": str(tmp_path / "w")}, tmp_path)
    assert ad.interactive_cmd() is None and "interactive_room" not in ad.describe()["capabilities"]


def test_fake_driver_full_round_through_dispatcher(tmp_path, monkeypatch):
    """以 command driver 模擬一個任意 agent：叫醒 → 跑完 → TurnResult 入核心 → 信件被處理。"""
    from mbox import dispatcher
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(dispatcher, "home", lambda: home)
    monkeypatch.setattr(drivers, "tmux_room_exists", lambda _: False)
    s = Store(home / "mbox.sqlite3")
    s.add_agent("owner", rank="human")
    s.add_agent("fake")
    s.send({"id": "owner", "rank": "human"}, "fake", "ping")
    script = tmp_path / "fake-agent"
    script.write_text("#!/bin/sh\necho \"handled $1\"\n")
    script.chmod(0o755)
    cfg = {"driver": "command", "command": f"{script} {{role}}", "workdir": str(tmp_path / "w")}
    out = []
    dispatcher.once({"fake": cfg}, s, log=out.append)
    assert any("started" in l for l in out)
    for _ in range(50):
        if not (home / "roles" / "fake" / "running.pid").exists():
            break
        time.sleep(0.1)
    dispatcher.once({"fake": cfg}, s, log=out.append)
    tr = s.last_turns("fake")[0]
    assert tr["driver"] == "command" and tr["exit"] == 0 and tr["text"] == "handled fake"


def test_server_headless_check_uses_driver_field(monkeypatch):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
    import runtime as rt
    assert rt.is_headless({"driver": "hermes"}) and rt.is_headless({"adapter": "pi-headless"})
    assert not rt.is_headless({"driver": "tmux"}) and not rt.is_headless({"driver": "manual"})
    roles = json.loads((Path(__file__).resolve().parent / "fixtures" / "roles.five.json").read_text())["roles"]
    assert all("adapter" not in c for c in roles.values())
    assert [r for r, c in roles.items() if rt.is_headless(c)] == [r for r in roles if r != "owner"]
