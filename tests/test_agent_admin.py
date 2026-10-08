"""S8：網頁加入 agent —— ACP 目錄、安裝（確認後才裝）、連線檢查、新增角色。npm 一律以假指令代替，不連網。"""
import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from drivers import acp_catalog  # noqa: E402

FAKE_AGENT = ROOT / "tests" / "fixtures" / "fake_acp_agent.py"


@pytest.fixture()
def vendor(tmp_path, monkeypatch):
    """把 vendor/acp 換成暫存目錄；npm 換成假腳本（install 時在 .bin 放 ACP 執行檔）。"""
    v = tmp_path / "vendor"
    monkeypatch.setattr(acp_catalog, "VENDOR", v)
    monkeypatch.setattr(acp_catalog, "VENDOR_BIN", v / "node_modules" / ".bin")
    monkeypatch.setenv("MBOX_HOME", str(tmp_path / "mbox"))
    npm = tmp_path / "npm"
    npm.write_text(f"""#!/bin/sh
if [ "$1" = view ]; then
  case "$2" in
    nope*) echo "404 not found" >&2; exit 1 ;;
    nobin*) echo '{{"name":"nobin","version":"1.0.0"}}' ;;
    *) echo '{{"name":"@x/my-acp","version":"2.1.0","bin":{{"my-acp":"dist/i.js","other":"o.js"}}}}' ;;
  esac
  exit 0
fi
if [ "$1" = install ]; then
  for a in "$@"; do last=$a; done
  echo "added 1 package ($last)"
  mkdir -p node_modules/.bin
  case "$last" in
    *gemini*) b=gemini ;; *my-acp*) b=my-acp ;; *fail*) exit 7 ;; *) b=x ;;
  esac
  printf '#!/bin/sh\\nexec {sys.executable} {FAKE_AGENT}\\n' > node_modules/.bin/$b; chmod +x node_modules/.bin/$b
  exit 0
fi
""")
    npm.chmod(0o755)
    monkeypatch.setattr(acp_catalog.shutil, "which", lambda n, *a, **k: str(npm) if n == "npm" else None)
    acp_catalog._jobs.clear()
    return v


def wait_job(jid):
    for _ in range(100):
        s = acp_catalog.job_status(jid)
        if s["state"] != "running":
            return s
        time.sleep(0.05)
    raise AssertionError("安裝逾時")


def test_status_lists_builtin_with_install_command(vendor):
    st = {a["id"]: a for a in acp_catalog.status()}
    assert {"hermes", "claude", "codex", "pi", "gemini"} <= set(st)
    g = st["gemini"]
    assert g["installed"] is False and g["install_cmd"][-1] == "@google/gemini-cli@0.62.0"
    assert "--save-exact" in g["install_cmd"]


def test_install_builtin_then_ready(vendor):
    job = acp_catalog.start_install("gemini")
    s = wait_job(job["id"])
    assert s["state"] == "done" and "added 1 package" in s["output"]
    assert acp_catalog.resolve_cmd("gemini")[1:] == ["--acp"]
    assert acp_catalog.probe("gemini")["ok"] is True


def test_install_failure_reported(vendor, monkeypatch):
    monkeypatch.setitem(acp_catalog.CATALOG, "bad", dict(label="bad", acp="nothere", source="vendor", npm="fail-pkg@1.0.0"))
    s = wait_job(acp_catalog.start_install("bad")["id"])
    assert s["state"] == "failed" and s["exit"] == 7


def test_custom_npm_plan_and_install(vendor):
    p = acp_catalog.plan_custom("@x/my-acp")
    assert p["npm"] == "@x/my-acp@2.1.0" and p["acp"] == "my-acp" and p["id"] == "my-acp"
    s = wait_job(acp_catalog.start_install(p["id"], custom=p)["id"])
    assert s["state"] == "done"
    assert "my-acp" in acp_catalog.all_agents() and acp_catalog.resolve_cmd("my-acp")
    with pytest.raises(ValueError):
        acp_catalog.plan_custom("nope-pkg")
    with pytest.raises(ValueError, match="沒有可執行檔"):
        acp_catalog.plan_custom("nobin")
    for bad in ("x; rm -rf /", "../evil", "a b", "--registry=http://x"):
        with pytest.raises(ValueError):
            acp_catalog.plan_custom(bad)


def test_only_one_install_at_a_time(vendor):
    acp_catalog._jobs["x"] = dict(state="running")
    with pytest.raises(ValueError, match="另一個安裝"):
        acp_catalog.start_install("gemini")


def test_probe_uninstalled(vendor):
    assert acp_catalog.probe("gemini") == dict(ok=False, error="尚未安裝")


# ── AA Forum API ──

@pytest.fixture()
def zk(tmp_path, monkeypatch, vendor):
    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps({"roles": {"owner": {"driver": "manual", "rank": "human"},
                                           "lead": {"driver": "acp", "acp_agent": "hermes", "rank": "lead"}}}))
    monkeypatch.syspath_prepend(str(ROOT / "server"))
    import runtime as rt
    import agent_admin
    monkeypatch.setattr(rt, "ROLES_FILE", roles)
    skills = tmp_path / "skills"
    (skills / "sample-x").mkdir(parents=True)
    (skills / "sample-x" / "SKILL.md").write_text("---\nname: sample-x\ndescription: 測試\n---\n")
    monkeypatch.setattr(rt, "ROOT", tmp_path)
    monkeypatch.setattr(rt, "INST", tmp_path)
    who = {"id": "Owner"}
    monkeypatch.setattr(agent_admin, "_identity", lambda request: who["id"])
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(agent_admin.router)
    return dict(c=TestClient(app), roles=roles, who=who, base=tmp_path)


def test_api_requires_owner(zk):
    zk["who"]["id"] = "lead"
    assert zk["c"].get("/api/agents/catalog").status_code == 403
    assert zk["c"].post("/api/agents/install", json={"agent": "gemini", "confirm": True}).status_code == 403


def test_api_install_needs_confirm(zk):
    assert zk["c"].post("/api/agents/install", json={"agent": "gemini"}).status_code == 400
    r = zk["c"].post("/api/agents/install", json={"agent": "gemini", "confirm": True})
    assert r.status_code == 200
    s = wait_job(r.json()["id"])
    assert s["state"] == "done"
    out = zk["c"].get(f"/api/agents/install/{r.json()['id']}").json()
    assert "added 1 package" in out["output"]


def test_api_custom_plan_is_rechecked(zk):
    """前端傳回的計畫不可信：伺服器重新查 npm，不能被塞入任意指令。"""
    r = zk["c"].post("/api/agents/install", json={"plan": {"npm": "x; rm -rf /", "id": "evil"}, "confirm": True})
    assert r.status_code == 400


def test_api_add_role_with_new_persona_and_existing_skill(zk):
    c = zk["c"]
    r = c.post("/api/agents/roles", json={"role": "scout", "agent": "gemini", "persona": "你是斥候。"})
    assert r.status_code == 400 and "尚未安裝" in r.json()["detail"]
    wait_job(acp_catalog.start_install("gemini")["id"])
    r = c.post("/api/agents/roles", json={"role": "scout", "agent": "gemini", "label": "斥候 scout",
                                          "persona": "你是斥候，負責先探路。", "model": "gemini-3.8-flash"})
    assert r.status_code == 200, r.text
    doc = json.loads(zk["roles"].read_text())["roles"]["scout"]
    assert doc == {"runtime": "acp-gemini", "rank": "worker", "persona_file": "skills/scout/SKILL.md",
                   "driver": "acp", "acp_agent": "gemini", "label": "斥候 scout", "model": "gemini-3.8-flash"}
    assert "你是斥候" in (zk["base"] / "skills/scout/SKILL.md").read_text()
    assert c.post("/api/agents/roles", json={"role": "scout", "agent": "gemini", "skill": "sample-x"}).status_code == 409
    # 用本測試已裝好的 gemini（假 ACP）；hermes 是否安裝依機器而定（Linux 容器裡沒有）
    r = c.post("/api/agents/roles", json={"role": "scout2", "agent": "gemini", "skill": "sample-x", "rank": "lead"})
    assert r.status_code == 200 and json.loads(zk["roles"].read_text())["roles"]["scout2"]["persona_file"] == "skills/sample-x/SKILL.md"
    for bad in ({"role": "Bad!", "agent": "hermes", "skill": "sample-x"},
                {"role": "s3", "agent": "hermes", "skill": "../etc"},
                {"role": "s4", "agent": "hermes"},
                {"role": "s5", "agent": "hermes", "skill": "sample-x", "model": "a b"}):
        assert c.post("/api/agents/roles", json=bad).status_code in (400, 422), bad


def test_catalog_reports_login_separately_from_install(monkeypatch, tmp_path):
    """「ACP 已安裝」不等於「已登入」：catalog 實際問 agent 本體的登入狀態。"""
    from drivers import acp_catalog
    import drivers
    monkeypatch.setattr(acp_catalog, "resolve_cmd", lambda aid: ["/bin/true"] if aid in ("codex", "pi") else None)
    monkeypatch.setattr(drivers.DRIVERS["codex"], "auth_check", lambda self, timeout=20: (False, "未登入（codex login）"))
    import pathlib
    monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: tmp_path))
    by = {a["id"]: a for a in acp_catalog.status()}
    assert by["codex"]["installed"] is True and by["codex"]["logged_in"] is False
    assert by["pi"]["logged_in"] is False                      # 沒有 ~/.pi/agent/models.json
    assert by["claude"]["installed"] is False and by["claude"]["logged_in"] is None
