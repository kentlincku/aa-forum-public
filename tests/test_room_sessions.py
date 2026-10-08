"""SPEC-1.1 §2：每個群裡的每個角色一個工作階段（用假 ACP agent，不呼叫真模型）。

session 鍵＝（角色, 群）：同一角色在不同群各一個；同群不同角色也各一個；不屬於任何群的信進角色的預設工作階段。
"""
import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import drivers  # noqa: E402
from mbox import dispatcher  # noqa: E402
from mbox.core import Store  # noqa: E402

FAKE = [sys.executable, str(ROOT / "tests" / "fixtures" / "fake_acp_agent.py")]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(drivers.acp.AcpDriver, "BOOT_TIMEOUT", 20)
    home = tmp_path / "var"
    home.mkdir()
    monkeypatch.setattr(dispatcher, "home", lambda: home)
    monkeypatch.setenv("AAF_HOME", str(tmp_path))
    store = Store(home / "mbox.sqlite3")
    for r, rank in (("me", "human"), ("lead", "lead"), ("builder", "worker")):
        store.add_agent(r, "x", rank)
    roles = {r: {"driver": "acp", "acp_agent": FAKE, "workdir": str(tmp_path / "w" / r), "rank": "worker"}
             for r in ("lead", "builder")}
    yield home, store, roles
    drivers.acp.shutdown_all(home)
    store.db.close()


def _send(store, to, body, room=None):
    return store.send({"id": "me", "rank": "human"}, to, body, source_room=room)


def _settle(home, store, roles, rounds=60):
    """跑 dispatcher 直到所有工作階段 idle（假 agent 回 OK 後就結束一輪）。"""
    logs = []
    for _ in range(rounds):
        dispatcher.once(roles, store, log=logs.append, digest_minutes=0)
        busy = [d for _, _, d in drivers.acp._host_dirs(home) if (d / "acp_pending").exists()]
        if not busy:
            break
        time.sleep(0.1)
    dispatcher.once(roles, store, log=logs.append, digest_minutes=0)
    return logs


def _sessions(store, role):
    rows = store.db.execute("SELECT driver, session_id FROM sessions WHERE role=?", (role,)).fetchall()
    return {r[0]: r[1] for r in rows}


def test_each_role_in_each_room_has_own_session(env):
    home, store, roles = env
    _send(store, "lead", "群2 的事", room=2)
    _send(store, "lead", "群3 的事", room=3)
    _send(store, "lead", "私信", room=None)
    _send(store, "builder", "群2 給 builder", room=2)
    _settle(home, store, roles)
    a, f = _sessions(store, "lead"), _sessions(store, "builder")
    assert set(a) == {"acp", "acp#room2", "acp#room3"}, a
    assert set(f) == {"acp#room2"}, f
    sids = [a["acp"], a["acp#room2"], a["acp#room3"], f["acp#room2"]]
    assert len(set(sids)) == 4                                        # 四個工作階段全部不同
    # 各自一支常駐主機
    pids = {json.dumps([r, l]): int((d / "acp_host.pid").read_text())
            for r, l, d in drivers.acp._host_dirs(home) if (d / "acp_host.pid").exists()}
    assert len(pids) == 4 and len(set(pids.values())) == 4


def test_room_session_only_sees_its_room(env):
    """群 2 的工作階段收到的提示只含群 2 的信；不會看到群 3 或私信。"""
    home, store, roles = env
    _send(store, "lead", "SECRET-ROOM2", room=2)
    _send(store, "lead", "SECRET-ROOM3", room=3)
    _send(store, "lead", "SECRET-DM", room=None)
    _settle(home, store, roles)
    # 每個工作階段都跑過一輪
    turns = store.db.execute("SELECT session_id, result FROM turn_results WHERE role='lead'").fetchall()
    assert len(turns) == 3
    # 每個工作階段只拿到自己的信
    for lane, want, others in ((2, "SECRET-ROOM2", ("SECRET-ROOM3", "SECRET-DM")),
                               (3, "SECRET-ROOM3", ("SECRET-ROOM2", "SECRET-DM")),
                               ("default", "SECRET-DM", ("SECRET-ROOM2", "SECRET-ROOM3"))):
        bodies = [m["body"] for m in store.inbox({"id": "lead"}, unread_only=False, mark=False, lane=lane)]
        assert bodies == [want], (lane, bodies)


def test_mbox_cli_env_scoped_to_lane(env):
    home, store, roles = env
    a2 = drivers.make("lead", roles["lead"], home, lane=2)
    a0 = drivers.make("lead", roles["lead"], home)
    e2, e0 = a2.env(), a0.env()
    assert e2["MBOX_LANE"] == "2" and e2["MBOX_ROOM"] == "2"
    assert e0["MBOX_LANE"] == "default" and e0.get("MBOX_ROOM") != "2"
    assert a2._handoff_path().name == "HANDOFF.room2.md" and a0._handoff_path().name == "HANDOFF.md"
    assert a2._sock() != a0._sock()
    assert a2.cfg["_room"] == 2 and "_room" not in a0.cfg


def test_non_acp_driver_keeps_single_session(env, tmp_path):
    home, store, roles = env
    cfg = {"driver": "command", "command": "true", "workdir": str(tmp_path / "w" / "c")}
    assert not drivers.supports_lanes(cfg)
    ad = drivers.make("c", cfg, home, lane=5)
    assert ad.state_dir == home / "roles" / "c"
    # 也可以在 roles.json 關掉
    assert not drivers.supports_lanes({**roles["lead"], "room_sessions": False})


def test_idle_room_session_parks_and_recalls(env, monkeypatch, tmp_path):
    home, store, roles = env
    log = tmp_path / "fake_acp.log"
    monkeypatch.setenv("FAKE_ACP_LOG", str(log))
    _send(store, "lead", "群2 第一件事", room=2)
    _settle(home, store, roles)
    d = home / "roles" / "lead" / "rooms" / "2"
    first_sid = _sessions(store, "lead")["acp#room2"]
    for m in store.inbox({"id": "lead"}, mark=False):            # 真 agent 會 ack done；假 agent 不會
        store.ack({"id": "lead"}, m["id"], "done")
    assert (d / "acp_host.pid").exists()
    # 模擬閒置 2 小時，門檻 1 小時
    monkeypatch.setenv("AAF_SESSION_IDLE_HOURS", "1")
    old = time.time() - 7200
    for f in (d / "turns.log", d / "acp_host.spec.json"):
        os.utime(f, (old, old))
    # 假 agent 只在提示含「[補血]」時寫交棒檔；閒置關閉用自己的提示 → 手動模擬 agent 寫檔
    logs = []
    dispatcher.once(roles, store, log=logs.append, digest_minutes=0)
    assert any("已請寫交棒檔" in l for l in logs), logs
    ad = drivers.make("lead", roles["lead"], home, lane=2)
    hp = ad._handoff_path()
    time.sleep(0.05)
    hp.write_text("mission_state: complete\nnext_action: 無\n<!-- archive.ready -->\n")
    for _ in range(50):
        dispatcher.once(roles, store, log=logs.append, digest_minutes=0)
        if (d / "acp_host.parked").exists():
            break
        time.sleep(0.1)
    assert (d / "acp_host.parked").exists(), logs
    assert not ad._host_pid()                                  # 主機已關
    assert "acp#room2" not in _sessions(store, "lead")
    assert (d / "recall_pending").exists() and list((d / "archives").glob("*_park.md"))
    # 預設工作階段不自動關
    dflt = drivers.make("lead", roles["lead"], home)
    assert dflt.park_tick(1) is None
    # 下次有信：開新 session、先接回
    (d / "backoff.json").unlink(missing_ok=True)
    _send(store, "lead", "群2 第二件事", room=2)
    _settle(home, store, roles)
    new_sid = _sessions(store, "lead")["acp#room2"]
    assert new_sid != first_sid
    assert not (d / "recall_pending").exists() and not (d / "acp_host.parked").exists()
    sent = [json.loads(l) for l in log.read_text().splitlines()]
    prompts = ["".join(c.get("text", "") for c in m["params"]["prompt"]) for m in sent if m.get("method") == "session/prompt"]
    last = prompts[-1]
    assert "[接回]" in last and "HANDOFF.room2.md" in last and "群2 第二件事" in last, last[-500:]
    assert "群2 第一件事" not in last                                  # 新 session 只帶新信，不帶舊對話


def test_parked_session_not_revived_by_health(env):
    home, store, roles = env
    _send(store, "lead", "x", room=4)
    _settle(home, store, roles)
    ad = drivers.make("lead", roles["lead"], home, lane=4)
    ad.shutdown()
    (ad.state_dir / "acp_host.parked").write_text(str(time.time()))
    (ad.state_dir / "acp_host.revive").unlink(missing_ok=True)
    assert ad.health() == "idle" and not ad._host_pid()        # 不會被 _revive 拉回


def test_down_shuts_all_room_hosts(env):
    home, store, roles = env
    _send(store, "lead", "a", room=2)
    _send(store, "lead", "b", room=None)
    _settle(home, store, roles)
    done = drivers.acp.shutdown_all(home)
    assert set(done) == {"lead", "lead@群2"}


def test_room_skills_only_in_that_room_session(env, monkeypatch, tmp_path):
    """群指派的 skill 只出現在該群的工作階段；預設工作階段不會載入。"""
    home, store, roles = env
    log = tmp_path / "fake_acp.log"
    monkeypatch.setenv("FAKE_ACP_LOG", str(log))
    from mbox import skills
    (tmp_path / "skills" / "room-only").mkdir(parents=True)
    (tmp_path / "skills" / "room-only" / "SKILL.md").write_text("ROOM-ONLY-SKILL-BODY\n")
    skills.assign_room(6, ["room-only"], [], "me")
    _send(store, "lead", "群6 工作", room=6)
    _send(store, "lead", "私信工作", room=None)
    _settle(home, store, roles)
    sent = [json.loads(l) for l in log.read_text().splitlines()]
    prompts = ["".join(c.get("text", "") for c in m["params"]["prompt"]) for m in sent if m.get("method") == "session/prompt"]
    room6 = [p for p in prompts if "群6 工作" in p]
    dm = [p for p in prompts if "私信工作" in p]
    assert room6 and "ROOM-ONLY-SKILL-BODY" in room6[0]
    assert dm and "ROOM-ONLY-SKILL-BODY" not in dm[0]
