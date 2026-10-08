"""SPEC-1.0 §1：aaf init 建實例骨架、--version、account ensure 非互動提示。"""
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def clean_env(**kw):
    e = {k: v for k, v in os.environ.items() if not k.startswith(("MBOX_", "AAF_", "AAF_", "AAF_"))}
    e.update(kw)
    return e


def run(*args, **kw):
    return subprocess.run([str(ROOT / "bin" / "aaf"), *args], capture_output=True, text=True,
                          env=clean_env(**kw), timeout=60)


def test_version():
    r = run("--version")
    assert r.returncode == 0 and r.stdout.strip() == "aaf " + (ROOT / "VERSION").read_text().strip()


def test_init_demo(tmp_path):
    d = tmp_path / "team"
    r = run("init", str(d), "--user=alice")
    assert r.returncode == 0, r.stderr
    roles = json.loads((d / "deploy" / "roles.json").read_text())["roles"]
    assert list(roles) == ["alice", "echo"] and roles["alice"]["rank"] == "human"
    env = (d / ".aaf.env").read_text()
    assert "MBOX_PORT=" in env and "AAF_SERVER_PORT=" in env
    assert os.access(d / "bin" / "echo-agent", os.X_OK) and (d / "skills" / "demo-echo" / "SKILL.md").exists()
    assert (d / ".git").is_dir()
    assert run("init", str(d)).returncode != 0          # 非空目錄拒絕


def test_init_blank_and_bad_user(tmp_path):
    r = run("init", str(tmp_path / "b"), "--blank", "--user=bob")
    assert r.returncode == 0
    assert list(json.loads((tmp_path / "b" / "deploy" / "roles.json").read_text())["roles"]) == ["bob"]
    assert run("init", str(tmp_path / "c"), "--user=1bad").returncode != 0


def test_account_ensure_noninteractive_points_to_web_setup(tmp_path):
    d = tmp_path / "t"
    assert run("init", str(d), "--user=alice").returncode == 0
    r = subprocess.run([str(ROOT / "bin" / "aaf"), "account", "ensure"], capture_output=True, text=True,
                       env=clean_env(AAF_HOME=str(d)), stdin=subprocess.DEVNULL, timeout=60)
    assert r.returncode == 0 and "/app/" in r.stdout and "account create" in r.stdout


def test_init_example_team_installs_five_roles_backed_by_echo(tmp_path):
    import json
    r = run("init", str(tmp_path / "t"), "--example=team", "--user=bob")
    assert r.returncode == 0, r.stderr
    roles = json.loads((tmp_path / "t/deploy/roles.json").read_text())["roles"]
    team = ["lead", "builder", "reviewer", "debugger", "guardian"]
    assert set(team) <= set(roles) and roles["bob"]["rank"] == "human"
    for rid in team:   # 不花額度：先用 echo；建議的真 agent 記著
        assert roles[rid]["driver"] == "command" and roles[rid]["suggested_agent"] in ("codex", "pi")
        assert (tmp_path / "t" / roles[rid]["persona_file"]).is_file()
    bad = run("init", str(tmp_path / "x"), "--blank", "--example=team")
    assert bad.returncode != 0 and not (tmp_path / "x").exists()
