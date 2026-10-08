"""P5：公版程式與實例內容分離（AAF_HOME）。"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_paths_default_and_instance(tmp_path, monkeypatch):
    from mbox import paths
    monkeypatch.delenv("AAF_HOME", raising=False)
    monkeypatch.delenv("MBOX_HOME", raising=False)
    monkeypatch.delenv("MBOX_ROLES", raising=False)
    assert paths.instance() == ROOT and paths.var() == ROOT / "var"
    monkeypatch.setenv("AAF_HOME", str(tmp_path))
    assert paths.var() == tmp_path / "var"
    assert paths.roles_file() == tmp_path / "deploy" / "roles.json"
    (tmp_path / "skills" / "x").mkdir(parents=True)
    (tmp_path / "skills" / "x" / "SKILL.md").write_text("i")
    assert paths.resolve("skills/x/SKILL.md") == tmp_path / "skills" / "x" / "SKILL.md"
    assert paths.resolve("contract") == ROOT / "contract"          # 實例沒有 → 回落公版


def test_system_prompt_uses_instance_persona_and_rules(tmp_path, monkeypatch):
    monkeypatch.setenv("AAF_HOME", str(tmp_path))
    (tmp_path / "skills" / "p").mkdir(parents=True)
    (tmp_path / "skills" / "p" / "SKILL.md").write_text("我是實例人設")
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "team.md").write_text("x")
    from drivers.base import system_prompt
    p = system_prompt("r", {"persona_file": "skills/p/SKILL.md"})
    assert "我是實例人設" in p and "團隊教條" not in p
    monkeypatch.setenv("AAF_TEAM_RULES", "rules/team.md")
    p = system_prompt("r", {})
    assert f"以下是團隊教條（{tmp_path / 'rules' / 'team.md'}）" in p and p.rstrip().endswith("x")   # 全文放進提示
    (tmp_path / "rules" / "team.md").unlink()
    assert f"團隊教條：{tmp_path / 'rules' / 'team.md'}" in system_prompt("r", {})                  # 讀不到就退回路徑


def test_aaf_env_file_loaded(tmp_path):
    """bin/aaf 讀實例 .aaf.env（只接受白名單 KEY，不執行 shell）。"""
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy" / "roles.json").write_text(json.dumps({"roles": {"me": {"rank": "human", "driver": "manual"}}}))
    (tmp_path / ".aaf.env").write_text("MBOX_PORT=18999\nEVIL=$(touch pwned)\nAAF_TMUX_PREFIX=zz-\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MBOX_", "AAF_", "AAF_", "AAF_"))}
    env["AAF_HOME"] = str(tmp_path)
    r = subprocess.run([str(ROOT / "bin" / "aaf"), "env"], env=env, capture_output=True, text=True, cwd=tmp_path)
    kv = dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)
    assert kv["MBOX_PORT"] == "18999" and kv["AAF_TMUX_PREFIX"] == "zz-" and "EVIL" not in kv
    assert Path(kv["MBOX_HOME"]).resolve() == (tmp_path / "var").resolve()
    assert Path(kv["AAF_CORE"]).resolve() == ROOT.resolve()
    assert not (tmp_path / "pwned").exists()


def test_bin_mbox_uses_instance_env(tmp_path):
    """bin/mbox 也要讀實例 .aaf.env（否則角色以外的人會寄到別的實例的 broker）。"""
    (tmp_path / ".aaf.env").write_text("MBOX_PORT=1\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MBOX_", "AAF_", "AAF_", "AAF_"))}
    env.update(AAF_HOME=str(tmp_path), MBOX_AGENT="me", MBOX_TOKEN="x")
    r = subprocess.run([str(ROOT / "bin" / "mbox"), "inbox"], env=env, capture_output=True, text=True, timeout=30)
    assert "127.0.0.1:1" in (r.stdout + r.stderr) or r.returncode != 0
    assert "8775" not in (r.stdout + r.stderr)


def test_demo_instance_roles_valid():
    from mbox import roster
    doc = json.loads((ROOT / "examples" / "demo" / "deploy" / "roles.json").read_text())
    assert [r for r, c in doc["roles"].items() if c["rank"] == "human"] == ["me"]
    assert os.access(ROOT / "examples" / "demo" / "bin" / "echo-agent", os.X_OK)


def test_setting_reads_instance_env_file(tmp_path, monkeypatch):
    from mbox import paths
    monkeypatch.setenv("AAF_HOME", str(tmp_path))
    monkeypatch.delenv("AAF_TEAM_RULES", raising=False)
    monkeypatch.delenv("AAF_TMUX_PREFIX", raising=False)
    (tmp_path / ".aaf.env").write_text("# x\nAAF_TEAM_RULES=rules/t.md\nAAF_TMUX_PREFIX=zz-\nEVIL=1\n")
    assert paths.setting("AAF_TEAM_RULES") == "rules/t.md" and paths.tmux_prefix() == "zz-"
    assert paths.setting("EVIL") is None
    monkeypatch.setenv("AAF_TMUX_PREFIX", "env-")
    assert paths.tmux_prefix() == "env-"


def test_role_env_and_agent_bin(tmp_path, monkeypatch):
    """roles.json env 帶入角色程序（可代換 {role}／{user}）；不能蓋身分變數；實例 agent-bin 排 PATH 最前。"""
    import getpass
    import drivers
    inst = tmp_path / "inst"
    (inst / "agent-bin").mkdir(parents=True)
    monkeypatch.setenv("AAF_HOME", str(inst))
    cfg = {"driver": "command", "command": "true", "workdir": str(tmp_path / "w"),
           "env": {"TMUX_PANE": "%civ-{role}", "WHO": "{role}-{user}", "MBOX_AGENT": "evil", "PATH": "/x"}}
    e = drivers.make("builder", cfg, tmp_path / "var").env()
    assert e["TMUX_PANE"] == "%civ-builder" and e["WHO"] == f"builder-{getpass.getuser()}"
    assert e["MBOX_AGENT"] == "builder" and e["PATH"].startswith(str(inst / "agent-bin") + ":")


def test_skill_index_lists_unloaded_skills(tmp_path, monkeypatch):
    """可用 skill 清單：列名稱／說明／路徑；人設與已指派全文的不重列；可關閉；教條過長截斷。"""
    monkeypatch.setenv("AAF_HOME", str(tmp_path))
    sk = tmp_path / "skills"
    for sid, desc in (("persona-a", "人設"), ("build-code", "編譯與上板流程"), ("recall", "接回上下文"), ("assigned", "已指派")):
        (sk / sid).mkdir(parents=True)
        (sk / sid / "SKILL.md").write_text(f"---\nname: {sid}\ndescription: {desc}\n---\n{sid} 全文內容\n")
    (sk / "_config.json").write_text('{"packs": {}, "rooms": {}}')
    from drivers import base
    cfg = {"persona_file": "skills/persona-a/SKILL.md", "skills": ["assigned"]}
    p = base.system_prompt("r", cfg)
    assert "可用的 skill" in p
    assert f"- build-code：編譯與上板流程（{sk / 'build-code' / 'SKILL.md'}）" in p
    assert "- recall：接回上下文" in p
    assert "- persona-a：" not in p and "- assigned：" not in p          # 已整份放進提示的不重列
    assert "build-code 全文內容" not in p and "assigned 全文內容" in p    # 清單只給路徑，不放全文
    assert "可用的 skill" not in base.system_prompt("r", {**cfg, "skill_index": False})
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "big.md").write_text("規" * (base.TEAM_RULES_MAX + 10))
    monkeypatch.setenv("AAF_TEAM_RULES", "rules/big.md")
    p = base.system_prompt("r", cfg)
    assert "（截斷）" in p and f"全文 {base.TEAM_RULES_MAX + 10} 字" in p
