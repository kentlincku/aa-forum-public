"""S4：AA Forum 血量改讀 TurnResult＋driver usage()；引擎清單、額度都由 driver 提供；核心不讀 agent 內部檔案。"""
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).parent / "fixtures" / "usage"
sys.path.insert(0, str(ROOT))

import drivers  # noqa: E402
from mbox import contract  # noqa: E402


@pytest.fixture()
def mods(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "server"))
    import member_health
    import runtime
    member_health._cache.clear()
    return runtime, member_health


def fake_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


# ── codex usage：真實 rollout 格式（內容已去除） ──

def test_codex_usage_from_rollout(tmp_path, monkeypatch):
    home = fake_home(tmp_path, monkeypatch)
    sid = "01a10cd6-f957-7610-a810-033c09ce9111"
    d = home / ".codex" / "sessions" / "2026" / "10" / "06"
    d.mkdir(parents=True)
    shutil.copy(FIX / "codex_rollout.jsonl", d / f"rollout-2026-10-06T00-12-57-{sid}.jsonl")
    ad = drivers.make("tc", {"driver": "codex", "workdir": str(tmp_path / "w")}, tmp_path / "mbox")
    u = ad.usage({"session_id": sid})
    contract.validate("usage", u)
    assert u["model"] == "gpt-6.1-sol" and u["context_used"] == 14892 and u["context_limit"] == 258400
    assert u["percent"] == round(100 * 14892 / 258400) and u["measured_at"] > 0
    miss = ad.usage({"session_id": "nope"})
    assert miss["reason"].startswith("找不到") and contract.validate("usage", miss)


def test_pi_usage_from_session(tmp_path, monkeypatch):
    home = fake_home(tmp_path, monkeypatch)
    d = home / ".pi" / "agent" / "sessions" / "--x--"
    d.mkdir(parents=True)
    shutil.copy(FIX / "pi_session.jsonl", d / "2026-10-05T16-12-57-662Z_mbox2-tp-1791216777.jsonl")
    ad = drivers.make("tp", {"driver": "pi", "workdir": str(tmp_path / "w")}, tmp_path / "mbox")
    u = ad.usage({"session_id": "mbox2-tp-1791216777"})
    contract.validate("usage", u)
    assert u["model"] == "Ornith-1.5-35B-A3B-MLX-4bit" and u["provider"] == "omlx"
    assert u["context_used"] == 7430 and u["percent"] is None and "無百分比" in u["source"]


# ── 血量：所有 agent 走同一條路（TurnResult＋usage），缺的欄位標明 ──

def headless(mods, monkeypatch, tmp_path, cfg, turn):
    rt, mh = mods
    monkeypatch.setattr(rt, "MBOX_HOME", tmp_path / "mbox")
    monkeypatch.setattr(rt, "load_roles", lambda: {"r": cfg})
    monkeypatch.setattr(rt, "unread_count", lambda a: 0)
    monkeypatch.setattr(rt, "last_turn", lambda a, ok_only=False: turn)
    return mh.headless_status("r", dict(percent=None))


def test_health_for_command_driver_says_not_provided(mods, monkeypatch, tmp_path):
    r = headless(mods, monkeypatch, tmp_path, {"driver": "command", "command": "true", "workdir": str(tmp_path / "w")},
                 {"ok": True, "exit": 0, "model_used": None, "session_id": None})
    assert r["engine"] == "command（headless）" and r["percent"] is None
    assert r["percent_reason"] == "此 agent 未提供" and r["last_turn"]["ok"] is True


def test_health_codex_headless_uses_turn_and_rollout(mods, monkeypatch, tmp_path):
    home = fake_home(tmp_path, monkeypatch)
    sid = "01a10cd6-f957-7610-a810-033c09ce9111"
    d = home / ".codex" / "sessions" / "2026" / "10" / "06"
    d.mkdir(parents=True)
    shutil.copy(FIX / "codex_rollout.jsonl", d / f"rollout-x-{sid}.jsonl")
    r = headless(mods, monkeypatch, tmp_path, {"driver": "codex", "workdir": str(tmp_path / "w")},
                 {"ok": True, "exit": 0, "model_used": None, "session_id": sid})
    assert r["engine"] == "Codex（headless）" and r["model"] == "gpt-6.1-sol" and r["percent"] == 6


def test_failed_turn_shows_error(mods, monkeypatch, tmp_path):
    r = headless(mods, monkeypatch, tmp_path, {"driver": "command", "command": "true", "workdir": str(tmp_path / "w")},
                 {"ok": False, "exit": 1, "error": "HTTP 429 rate limit", "session_id": None})
    assert r["status"] == "rate_limit" and "429" in r["source"]


# ── 引擎清單、互動指令、額度：由 driver 提供 ──

def test_engine_list_and_detection_from_drivers(mods):
    rt, _ = mods
    assert set(rt.interactive_engines()) == {"hermes", "codex", "claude", "pi"}
    assert rt.detect_engine("codex", "codex --yolo") == "codex"
    assert rt.detect_engine("node", "node /x/bin/claude") == "claude"
    assert rt.detect_engine("python3", "python3 -m http.server") is None


def test_quota_capability_only_on_claude_codex():
    caps = {n: "account_quota" in c.manifest()["capabilities"] for n, c in drivers.DRIVERS.items()}
    assert [n for n, v in caps.items() if v] == ["claude", "codex"]


# ── 核心不讀 agent 內部檔案 ──

def test_server_does_not_touch_agent_internals():
    bad = []
    for f in (ROOT / "server").glob("*.py"):
        text = f.read_text(encoding="utf-8")
        for needle in ("state.db", ".codex/sessions", ".pi/agent", "prev_out.txt", "session.hermes",
                       ".claude/run", "hermes_cli", "active_sessions.json"):
            if needle in text:
                bad.append(f"{f.name}: {needle}")
    assert not bad, bad
