"""SPEC-1.1 §3：skill 管理（新增／編輯／刪除保護／包／指派／版本／組合載入／權限）。"""
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def inst(tmp_path, monkeypatch):
    home = tmp_path / "inst"
    (home / "deploy").mkdir(parents=True)
    (home / "skills" / "persona-a").mkdir(parents=True)
    (home / "skills" / "persona-a" / "SKILL.md").write_text("---\nname: persona-a\ndescription: 人設 A\n---\n人設內容 A\n")
    for sid in ("build", "flash", "docs"):
        (home / "skills" / sid).mkdir()
        (home / "skills" / sid / "SKILL.md").write_text(f"---\nname: {sid}\ndescription: {sid} 說明\n---\n{sid} 內容\n")
    roles = {"roles": {"me": {"rank": "human", "driver": "manual"},
                       "lead": {"rank": "lead", "driver": "command", "persona_file": "skills/persona-a/SKILL.md"}}}
    (home / "deploy" / "roles.json").write_text(json.dumps(roles))
    monkeypatch.setenv("AAF_HOME", str(home))
    monkeypatch.setenv("MBOX_HOME", str(home / "var"))
    monkeypatch.setenv("MBOX_ROLES", str(home / "deploy" / "roles.json"))
    sys.path.insert(0, str(ROOT))
    from mbox import skills
    return home, skills


def test_crud_history_restore(inst):
    home, sk = inst
    assert sk.save("newone", "---\nname: newone\ndescription: 新\n---\nv1\n", "me", create=True)["changed"]
    with pytest.raises(sk.SkillError) as e:
        sk.save("newone", "x", "me", create=True)
    assert e.value.status == 409
    with pytest.raises(sk.SkillError):
        sk.save("Bad Name", "x", "me", create=True)
    r = sk.save("newone", "---\nname: newone\ndescription: 新\n---\nv2\n", "me", note="改第二版")
    assert r["changed"] and r["previous"]
    assert sk.save("newone", sk.read("newone"), "me")["changed"] is False       # 內容相同不留版本
    versions, log = sk.history("newone")
    assert len(versions) == 1 and "v1" in sk.read_version("newone", versions[0]["version"])
    assert [e["action"] for e in log] == ["create", "edit"] and log[1]["note"] == "改第二版"
    sk.restore("newone", versions[0]["version"], "me")
    assert "v1" in sk.read("newone")
    assert len(sk.history("newone")[0]) == 2                                   # 還原前的 v2 也留了一版
    assert sk.delete("newone", "me")["previous"]
    assert "newone" not in sk.skill_ids()
    assert any(e["action"] == "delete" for e in sk.history("newone")[1])        # 刪除後紀錄仍在


def test_delete_blocked_when_in_use(inst):
    home, sk = inst
    # 人設使用中
    with pytest.raises(sk.SkillError) as e:
        sk.delete("persona-a", "me")
    assert e.value.status == 409 and "角色 lead（人設）" in str(e.value)
    # 在包裡
    sk.set_pack("工程", ["build", "flash"], "me")
    with pytest.raises(sk.SkillError) as e:
        sk.delete("build", "me")
    assert "包 工程" in str(e.value)
    # 包被群使用時，包不能刪
    sk.assign_room(3, [], ["工程"], "me")
    with pytest.raises(sk.SkillError) as e:
        sk.delete_pack("工程", "me")
    assert "群 3" in str(e.value)
    # 角色直接指派
    sk.assign_role("lead", ["docs"], [], "me")
    with pytest.raises(sk.SkillError) as e:
        sk.delete("docs", "me")
    assert "角色 lead" in str(e.value)
    # 取消指派後就能刪
    sk.assign_role("lead", [], [], "me")
    assert sk.delete("docs", "me")["ok"]
    roles = json.loads((home / "deploy" / "roles.json").read_text())["roles"]["lead"]
    assert "skills" not in roles and roles["persona_file"] == "skills/persona-a/SKILL.md"   # 不動人設


def test_assign_validates(inst):
    home, sk = inst
    with pytest.raises(sk.SkillError):
        sk.assign_role("lead", ["nope"], [], "me")
    with pytest.raises(sk.SkillError):
        sk.assign_room(1, [], ["沒有這包"], "me")
    with pytest.raises(sk.SkillError):
        sk.assign_role("ghost", [], [], "me")
    with pytest.raises(sk.SkillError):
        sk.set_pack("x", ["nope"], "me")


def test_compose_order_and_dedupe(inst):
    home, sk = inst
    sk.set_pack("工程", ["build", "flash"], "me")
    sk.assign_role("lead", ["docs", "persona-a"], ["工程"], "me")
    sk.assign_room(5, ["flash", "docs"], [], "me")
    cfg = json.loads((home / "deploy" / "roles.json").read_text())["roles"]["lead"]
    # 人設不重複載入；角色 skill → 角色包 → 群（重複的 flash、docs 只載一次）
    assert [s for s, _ in sk.compose(cfg)] == ["docs", "build", "flash"]
    assert [s for s, _ in sk.compose(cfg, 5)] == ["docs", "build", "flash"]
    sk.assign_role("lead", [], [], "me")
    cfg = json.loads((home / "deploy" / "roles.json").read_text())["roles"]["lead"]
    assert [s for s, _ in sk.compose(cfg, 5)] == ["flash", "docs"]
    assert sk.compose(cfg, 6) == []


def test_system_prompt_includes_assigned_skills(inst):
    home, sk = inst
    from drivers import base
    sk.set_pack("工程", ["build"], "me")
    sk.assign_role("lead", [], ["工程"], "me")
    sk.assign_room(7, ["docs"], [], "me")
    cfg = json.loads((home / "deploy" / "roles.json").read_text())["roles"]["lead"]
    p = base.system_prompt("lead", cfg)
    assert "人設內容 A" in p and "build 內容" in p and "docs 內容" not in p
    p7 = base.system_prompt("lead", {**cfg, "_room": 7})
    assert p7.index("人設內容 A") < p7.index("build 內容") < p7.index("docs 內容")
    # 設定檔壞掉：不影響叫醒，只是沒有額外 skill
    (home / "skills" / "_config.json").write_text("{壞掉")
    assert "人設內容 A" in base.system_prompt("lead", cfg)


def test_listing_reports_usage(inst):
    home, sk = inst
    sk.set_pack("工程", ["build"], "me")
    sk.assign_room(2, ["docs"], [], "me")
    d = sk.listing()
    by = {s["id"]: s for s in d["skills"]}
    assert by["build"]["packs"] == ["工程"] and "包 工程" in by["build"]["used_by"]
    assert "群 2" in by["docs"]["used_by"] and by["flash"]["used_by"] == []
    assert d["rooms"] == {"2": {"skills": ["docs"], "packs": []}}
    assert "_config.json" not in by


def test_snapshots_same_millisecond_not_overwritten(inst, monkeypatch):
    home, sk = inst
    monkeypatch.setattr(sk.time, "time", lambda: 1_800_000_000.123)
    sk.save("build", "a\n", "me")
    sk.save("build", "b\n", "me")
    sk.save("build", "c\n", "me")
    versions, _ = sk.history("build")
    assert len(versions) == 3 and len({v["version"] for v in versions}) == 3
    contents = {sk.read_version("build", v["version"]) for v in versions}
    assert "a\n" in contents and "b\n" in contents and any("build 內容" in c for c in contents)
