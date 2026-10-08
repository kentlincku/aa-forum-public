"""P4：通用檢查掛點（mbox 發信／派工、AA Forum 發文），所有 runtime 一視同仁。"""
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from mbox import hooks  # noqa: E402
from mbox.core import MboxError, Store  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    d = tmp_path / "hooks"
    d.mkdir()
    monkeypatch.setenv("AAF_HOOKS", str(d))
    monkeypatch.setenv("MBOX_HOME", str(tmp_path / "var"))
    (tmp_path / "var").mkdir()
    hooks._cache.clear()
    s = Store(tmp_path / "m.sqlite3")
    for a, rank in (("me", "human"), ("w1", "worker"), ("w2", "worker")):
        s.add_agent(a, "test", rank)
    return d, s, tmp_path / "var"


W1 = {"id": "w1", "rank": "worker", "runtime": "test"}


def test_no_hooks_dir_is_noop(env, monkeypatch, tmp_path):
    monkeypatch.setenv("AAF_HOOKS", str(tmp_path / "nope"))
    _, s, _ = env
    assert s.send(W1, "w2", "x" * 3000)["id"]


def test_example_rule_blocks_and_allows(env):
    d, s, _ = env
    shutil.copy(ROOT / "examples" / "hooks" / "10_long_message.py", d)
    with pytest.raises(MboxError) as e:
        s.send(W1, "w2", "x" * 1600)
    assert e.value.code == 422 and "規則擋下" in str(e.value)
    assert s.send(W1, "w2", "x" * 1600 + "\n# long-ok:附完整逐行讀數")["id"]
    assert s.send(W1, "w2", "短訊息")["id"]
    me = {"id": "me", "rank": "human", "runtime": "test"}
    assert s.send(me, "w1", "x" * 1600)["id"]                 # 人不受限


def test_task_post_goes_through_hooks(env):
    d, s, _ = env
    (d / "a.py").write_text("def pre(e):\n    return '不准派' if e['type']=='mbox.task' else None\n")
    with pytest.raises(MboxError):
        s.post_task(W1, "t", "spec", assignee="w2")


def test_broken_rule_fails_open_and_logs(env):
    d, s, var = env
    (d / "bad.py").write_text("def pre(e):\n    raise RuntimeError('boom')\n")
    (d / "syntax.py").write_text("def pre(e) return 1\n")
    assert s.send(W1, "w2", "hi")["id"]
    log = (var / "hooks.log").read_text()
    assert "boom" in log and "載入失敗 syntax.py" in log


def test_system_sender_skips_rules(env):
    d, s, _ = env
    (d / "a.py").write_text("def pre(e):\n    return 'no'\n")
    sysme = {"id": "mbox-dispatcher", "rank": "human", "runtime": "system"}
    assert s.send(sysme, "w1", "alert")["id"]


def test_post_notes_reach_next_wake_prompt(env):
    d, s, var = env
    (d / "a.py").write_text("def post(e):\n    return '記得寫戰報'\n")
    s.send(W1, "w2", "hi")
    s.send(W1, "w2", "hi2")
    notes = hooks.take_notes("w1", var)
    assert notes == ["記得寫戰報"]                             # 去重
    assert hooks.take_notes("w1", var) == []                  # 取一次就清
    from drivers.base import wake_prompt
    p = wake_prompt("w1", 1, "", ["記得寫戰報"])
    assert p.startswith("[規則提醒]") and "記得寫戰報" in p and "mbox inbox" in p


def test_hot_reload(env):
    d, s, _ = env
    f = d / "a.py"
    f.write_text("def pre(e):\n    return 'v1'\n")
    with pytest.raises(MboxError):
        s.send(W1, "w2", "hi")
    import os, time
    f.write_text("def pre(e):\n    return None\n")
    os.utime(f, (time.time() + 5, time.time() + 5))
    assert s.send(W1, "w2", "hi")["id"]


def test_wake_and_turn_end_hooks(tmp_path, monkeypatch):
    """叫醒前 wake() 的文字附在提示前；turn_end() 的提醒存起來下次附上；規則壞掉不影響。"""
    hd = tmp_path / "hooks"
    hd.mkdir()
    (hd / "10_w.py").write_text(
        "def wake(e):\n    return f'[戰報標頭] {e[\"role\"]} 群={e[\"room\"]}'\n"
        "def turn_end(e):\n    return '記得寫戰報' if e['ok'] else None\n")
    (hd / "20_bad.py").write_text("def wake(e):\n    raise RuntimeError('壞')\n")
    monkeypatch.setenv("AAF_HOOKS", str(hd))
    monkeypatch.setenv("MBOX_HOME", str(tmp_path / "var"))
    from mbox import hooks
    hooks._cache.clear()
    assert hooks.run_wake({"type": "wake", "role": "lead", "room": 3}) == ["[戰報標頭] lead 群=3"]
    hooks.run_turn_end({"type": "turn_end", "role": "lead", "room": None, "ok": True})
    assert hooks.take_notes("lead", tmp_path / "var") == ["記得寫戰報"]
