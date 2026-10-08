"""S3：roles.json 熱重載（D3、D8、D9）與 doctor 告警（D4、D5）。"""
import json
import time
from pathlib import Path

import pytest

import drivers
from mbox import dispatcher, doctor
from mbox.core import Store
from mbox.roster import RosterWatcher, validate_role

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(dispatcher, "home", lambda: home)
    monkeypatch.setattr(doctor, "home", lambda: home)
    monkeypatch.setattr(drivers, "tmux_room_exists", lambda _: False)
    store = Store(home / "mbox.sqlite3")
    roles_path = tmp_path / "roles.json"
    roles = {"owner": {"rank": "human", "driver": "manual"},
             "lead": {"rank": "lead", "driver": "command", "command": "true", "workdir": str(tmp_path / "w/lead")},
             "builder": {"rank": "worker", "driver": "command", "command": "true", "workdir": str(tmp_path / "w/builder")}}
    roles_path.write_text(json.dumps({"roles": roles}))
    dispatcher.setup(roles, store, quiet=True)
    alerts, logs, zk = [], [], []
    w = RosterWatcher(roles_path, roles, home, alert=lambda *a: alerts.append(a), log=logs.append,
                      zk_sync=lambda added, removed: zk.append((list(added), list(removed))))
    return dict(home=home, store=store, path=roles_path, roles=roles, w=w, alerts=alerts, logs=logs, zk=zk, tmp=tmp_path)


def write(world, roles):
    world["path"].write_text(json.dumps({"roles": roles}))


def test_no_change_no_work(world):
    assert world["w"].poll(world["store"]) is False


def test_change_model_applies_next_round(world):
    r = dict(world["roles"])
    r["builder"] = dict(r["builder"], model="m2")
    write(world, r)
    assert world["w"].poll(world["store"]) is True
    assert world["w"].roles["builder"]["model"] == "m2" and world["zk"] == []


def test_add_role_provisions_everything(world):
    r = dict(world["roles"])
    r["newbie"] = {"rank": "worker", "driver": "command", "command": "true", "workdir": str(world["tmp"] / "w/newbie")}
    write(world, r)
    world["w"].poll(world["store"])
    assert "newbie" in world["w"].roles
    assert (world["home"] / "tokens" / "newbie").exists()
    assert "newbie" in {a["id"] for a in world["store"].agents()}
    assert (world["tmp"] / "w/newbie").is_dir()
    assert world["zk"] == [(["newbie"], [])]
    # 新角色收得到信、會被叫醒
    world["store"].send({"id": "owner", "rank": "human"}, "newbie", "hi")
    out = []
    dispatcher.once(world["w"].roles, world["store"], log=out.append)
    assert any("newbie" in l and "started" in l for l in out)


def test_add_role_rejected_when_skill_missing(world):
    r = dict(world["roles"])
    r["ghost"] = {"rank": "worker", "driver": "hermes", "persona_file": "skills/ghost/SKILL.md"}
    r["weird"] = {"rank": "worker", "driver": "nope"}
    write(world, r)
    world["w"].poll(world["store"])
    assert "ghost" not in world["w"].roles and "weird" not in world["w"].roles
    assert not (world["home"] / "tokens" / "ghost").exists()
    keys = [a[0] for a in world["alerts"]]
    assert "role:ghost" in keys and "role:weird" in keys
    assert "skill 檔不存在" in validate_role("ghost", r["ghost"])
    # 同樣錯誤不重複告警
    world["path"].write_text(world["path"].read_text() + " ")
    world["w"].poll(world["store"])
    assert [a[0] for a in world["alerts"]].count("role:ghost") == 1


def test_remove_role_keeps_data_notifies_lead(world):
    s = world["store"]
    t = s.post_task({"id": "lead", "rank": "lead"}, "do it", assignee="builder")
    s.claim_task({"id": "builder", "rank": "worker"}, t["task_id"])
    t2 = s.post_task({"id": "lead", "rank": "lead"}, "not claimed yet", assignee="builder")
    r = {k: v for k, v in world["roles"].items() if k != "builder"}
    write(world, r)
    world["w"].poll(s)
    assert "builder" not in world["w"].roles and "builder" in world["w"].retiring_roles()
    assert (world["home"] / "tokens" / "builder").exists()          # token 保留
    assert "builder" in {a["id"] for a in s.agents()}                # 名冊身分保留
    lead_inbox = s.inbox({"id": "lead"}, mark=False)
    assert any("builder" in m["body"] and f"#{t['task_id']}" in m["body"] and f"#{t2['task_id']}" in m["body"]
               for m in lead_inbox)
    assert world["zk"] == [([], ["builder"])]
    # 不再叫醒
    s.send({"id": "owner", "rank": "human"}, "builder", "還在嗎")
    out = []
    dispatcher.once(world["w"].roles, s, log=out.append)
    assert not any(l.startswith("builder") or " builder:" in l for l in out)
    dispatcher.drain_retiring(world["w"], s, log=out.append)
    assert "builder" not in world["w"].retiring_roles()


def test_deleting_rejected_role_is_not_called_applicable(world):
    r = dict(world["roles"])
    r["ghost"] = {"rank": "worker", "driver": "nope"}
    write(world, r)
    world["w"].poll(world["store"])
    write(world, world["roles"])
    world["w"].poll(world["store"])
    assert world["alerts"][-1][2] == "roles.json 已移除未套用的角色 ghost"


def test_broken_json_keeps_old_and_alerts_once(world):
    world["path"].write_text("{broken")
    assert world["w"].poll(world["store"]) is False
    assert world["w"].poll(world["store"]) is False
    assert world["w"].roles == world["roles"]
    assert [a[1] for a in world["alerts"]] == ["problem"]
    write(world, world["roles"])
    world["w"].poll(world["store"])
    assert [a[1] for a in world["alerts"]] == ["problem", "recovered"]


def test_zk_sync_failure_retried(world):
    calls = []

    def flaky(added, removed):
        calls.append((added, removed))
        if len(calls) == 1:
            raise OSError("zk down")
    world["w"].zk_sync = flaky
    r = dict(world["roles"])
    r["newbie"] = {"rank": "worker", "driver": "command", "command": "true", "workdir": str(world["tmp"] / "w/n")}
    write(world, r)
    world["w"].poll(world["store"])
    assert "newbie" in world["w"].roles and ("zk:roster", "problem") == world["alerts"][-1][:2]
    world["w"].retry_zk()
    assert calls[-1] == (["newbie"], []) and world["alerts"][-1][:2] == ("zk:roster", "recovered")


# ── doctor ──

def test_cheap_checks_and_transitions(world, monkeypatch):
    monkeypatch.setenv("MBOX_PORT", "1")       # 沒人聽的 port
    monkeypatch.setenv("AAF_SERVER_PORT", "2")
    res = doctor.cheap_checks(world["roles"])
    assert res["port:broker"][0] is False and res["var:writable"][0] is True
    assert res["tokens"][0] is True and res["dispatcher:heartbeat"][0] is False
    assert doctor.evaluate(res) == []                              # 第一次失敗：寬限（可能只是重啟空窗）
    first = doctor.evaluate(res)
    assert {k for k, lvl, _ in first if lvl == "problem"} >= {"port:broker", "port:server"}
    assert doctor.evaluate(res) == []                              # 未恢復前不重複
    res["port:broker"] = (True, "ok")
    assert doctor.evaluate(res) == [("port:broker", "recovered", "已恢復：port:broker（ok）")]


def test_driver_check_catches_broken_runtime(world, tmp_path):
    broken = tmp_path / "hermes-broken"
    broken.write_text("#!/bin/sh\necho 'cannot execute' >&2\nexit 126\n")
    broken.chmod(0o755)
    roles = {"owner": {"rank": "human", "driver": "manual"}, "lead": {"driver": "hermes", "bin": str(broken)}}
    res = doctor.driver_checks(roles)
    (k, (ok, why)), = res.items()
    assert k.startswith("driver:hermes") and not ok and "126" in why


def test_flush_sends_mbox_and_keeps_on_failure(world, monkeypatch):
    s = world["store"]
    doctor.enqueue("port:broker", "problem", "異常：port:broker")
    posted = []
    monkeypatch.setattr(doctor, "_zk_announce", lambda text, key: (_ for _ in ()).throw(OSError("zk down")))
    assert doctor.flush(s, world["roles"], log=lambda *a: None) == 0
    assert any("[aaf doctor]" in m["body"] for m in s.inbox({"id": "owner"}, mark=False))  # mbox 已送
    assert doctor._queue_file().read_text().strip()                                            # AA Forum 未送：保留
    monkeypatch.setattr(doctor, "_zk_announce", lambda text, key: posted.append(text) or 1)
    assert doctor.flush(s, world["roles"], log=lambda *a: None) == 1
    assert posted == ["異常：port:broker"] and not doctor._queue_file().read_text().strip()
    n = len([m for m in s.inbox({"id": "owner"}, mark=False) if "[aaf doctor]" in m["body"]])
    assert n == 1                                                                              # 重送不重複寄 mbox


def test_doctor_tick_rate_limits_driver_checks(world, monkeypatch):
    calls = []
    monkeypatch.setattr(doctor, "driver_checks", lambda roles: calls.append(1) or {})
    monkeypatch.setattr(doctor, "flush", lambda *a, **k: 0)
    d = doctor.Doctor(driver_every=600)
    for _ in range(5):
        d.tick(world["store"], world["roles"], log=lambda *a: None)
    assert len(calls) == 1
