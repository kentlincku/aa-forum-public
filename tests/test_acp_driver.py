"""S7：常駐 ACP driver（用假 ACP agent 測，不呼叫真模型）。"""
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import drivers  # noqa: E402
from mbox import contract  # noqa: E402

FAKE = [sys.executable, str(ROOT / "tests" / "fixtures" / "fake_acp_agent.py")]


@pytest.fixture()
def ad(tmp_path, monkeypatch):
    monkeypatch.setattr(drivers.acp.AcpDriver, "BOOT_TIMEOUT", 20)
    made = []

    def make(**kw):
        a = drivers.make("ac", {"driver": "acp", "acp_agent": FAKE, "workdir": str(tmp_path / "w"), **kw}, tmp_path / "mbox")
        made.append(a)
        return a
    yield make
    for a in made:
        a.shutdown(final=True)


def turn(a, head, preview=""):
    r = a.wake(1, head=head, preview=preview)
    assert r.startswith("started"), r
    for _ in range(200):
        if a.health() == "idle":
            break
        time.sleep(0.05)
    a.finish()
    return a.take_turn_result()


def test_persistent_host_keeps_session_and_pid(ad):
    a = ad()
    t1 = turn(a, 1)
    contract.validate("turn_result", t1)
    assert t1["ok"] and t1["driver"] == "acp" and t1["model_used"] == "fake-1"
    pid = a.host_status()["pid"]
    t2 = turn(a, 2)
    assert a.host_status()["pid"] == pid               # 常駐：同一支主機
    assert t2["session_id"] == t1["session_id"]
    assert t1["tokens"]["input"] == 7
    u = a.usage()
    assert u["percent"] == 25 and contract.validate("usage", u)


def test_system_prompt_only_on_first_turn(ad, tmp_path):
    a = ad()
    t1 = turn(a, 1)
    t2 = turn(a, 2)
    # 第一輪提示以人設開頭（不是 wake 提示），第二輪就直接是 wake 提示
    wake_head = drivers.wake_prompt("ac", 1)[:12]
    assert f"first={wake_head}" not in t1["text"] and f"first={wake_head}" in t2["text"]


def test_permission_auto_allowed_and_recorded(ad):
    a = ad()
    t = turn(a, 1, preview="please WRITE")
    assert "WROTE" in t["text"] and "[自動允許的權限] edit:Write x" in t["text"]
    assert "權限自動允許" in (a.state_dir / "acp_host.log").read_text()


def test_host_crash_mid_turn_records_failure_and_revives_same_session(ad):
    a = ad()
    t1 = turn(a, 1)
    sid = t1["session_id"]
    (a.state_dir / "acp_host.revive").unlink(missing_ok=True)
    t2 = turn(a, 2, preview="DIE now")
    assert t2["ok"] is False and ("中途結束" in t2["error"] or "agent 已結束" in t2["error"])
    for _ in range(100):
        if a.host_status():
            break
        time.sleep(0.1)
    assert a.host_status() is not None                 # 自動重開
    t3 = turn(a, 3)
    assert t3["ok"] and t3["session_id"] == sid        # session/load 接回


def test_model_change_restarts_host_and_sets_model(ad):
    a = ad()
    t1 = turn(a, 1)
    pid = a.host_status()["pid"]
    b = ad(model="fake-2")
    t2 = turn(b, 2)
    assert b.host_status()["pid"] != pid
    assert t2["model_used"] == "prov:fake-2" and t2["session_id"] == t1["session_id"]


def test_busy_and_down(ad):
    a = ad()
    assert a.wake(1, head=1, preview="SLOW").startswith("started")
    assert a.health() == "busy" and a.wake(1, head=2).startswith("busy")
    for _ in range(100):
        if a.health() == "idle":
            break
        time.sleep(0.05)
    assert drivers.acp.shutdown_all(a.home) == ["ac"]
    assert a.host_status() is None
    assert a.health() == "idle" and a.host_status() is None   # down 之後不會被拉回


def test_check_reports_missing_adapter(tmp_path):
    a = drivers.make("ac", {"driver": "acp", "acp_agent": ["/nope/acp"], "workdir": str(tmp_path / "w")}, tmp_path / "m")
    assert a.check()[0] is False


def test_shutdown_kills_whole_process_group(ad, tmp_path):
    """agent 的子程序（如 codex-acp 的原生執行檔）在 down 後也不能留下孤兒。"""
    import os
    import subprocess
    wrapper = tmp_path / "wrap.sh"
    marker = tmp_path / "child.pid"
    wrapper.write_text(f"#!/bin/sh\nsleep 300 &\necho $! > {marker}\nexec {sys.executable} {FAKE[1]}\n")
    wrapper.chmod(0o755)
    a = drivers.make("ac", {"driver": "acp", "acp_agent": [str(wrapper)], "workdir": str(tmp_path / "w")}, tmp_path / "mbox")
    a.start()
    child = int(marker.read_text())
    os.kill(child, 0)
    drivers.acp.shutdown_all(a.home)
    time.sleep(1.5)
    alive = subprocess.run(["ps", "-o", "stat=", "-p", str(child)], capture_output=True, text=True).stdout.strip()
    assert alive in ("", "Z"), alive


def test_agent_change_starts_new_session(ad, tmp_path):
    a = ad()
    t1 = turn(a, 1)
    other = ad(acp_agent=[sys.executable, FAKE[1], "--other"])
    t2 = turn(other, 2)
    assert t2["ok"] and t2["session_id"] != t1["session_id"]


def test_retire_closes_host(ad):
    a = ad()
    a.start()
    assert drivers.AcpDriver.retire("ac", a.home) and a.host_status() is None
    assert a.health() == "idle" and a.host_status() is None


def test_ghost_session_refusal_recovers_with_new_session(ad, monkeypatch):
    """hermes 實際發生：新 session 未跑第一輪就重啟 → load「成功」但 prompt 一律 refusal。"""
    monkeypatch.setenv("FAKE_ACP_STRICT", "1")
    a = ad()
    a.start()
    ghost = a.session()
    a.shutdown()                       # 第一輪前重啟：新程序不認得這個 session
    t = turn(a, 1)
    assert t["ok"] and t["session_id"] != ghost and "OK" in t["text"]
    assert "改開新 session" in (a.state_dir / "acp_host.log").read_text()


def test_clearing_model_returns_to_default(ad, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_ACP_MODEL_FILE", str(tmp_path / "model.txt"))
    a = ad()
    turn(a, 1)
    b = ad(model="fake-2")
    assert turn(b, 2)["model_used"] == "prov:fake-2"
    c = ad()                                  # override 清除
    assert turn(c, 3)["model_used"] == "fake-1"


def test_legacy_session_without_default_learns_it(ad, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_ACP_MODEL_FILE", str(tmp_path / "model.txt"))
    a = ad()
    turn(a, 1)
    assert turn(ad(model="fake-2"), 2)["model_used"] == "prov:fake-2"
    (a.state_dir / "acp_default_model").unlink()      # 模擬修正前建立的 session
    t = turn(ad(), 3)
    assert t["model_used"] == "fake-1" and t["session_id"] == turn(ad(), 4)["session_id"]


# ── 補血：寫交棒檔、換新 session ──

def _idle(a):
    for _ in range(200):
        if a.health() == "idle":
            return
        time.sleep(0.05)


def test_refill_full_cycle(ad, monkeypatch):
    monkeypatch.setenv("FAKE_ACP_USED", "6500")        # 65% ≥ 60%
    a = ad()
    t1 = turn(a, 1)
    old_sid = t1["session_id"]
    assert "第 1 讀" in a.refill_tick()                  # 第一讀只記，不動作
    ev = a.refill_tick()                                 # 第二讀 → 請本人寫交棒檔
    assert ev.startswith("補血開始") and a.wake(1, head=9).split(":")[0] in ("refill", "busy")
    _idle(a)
    assert a.wake(1, head=9).startswith("refill")       # 補血期間不送新信
    _idle(a); a.finish(); a.take_turn_result()
    # 驗交棒檔 → 換 session → 送接回。主機可能還沒開始跑寫交棒檔那一輪（health 先回 idle），
    # 此時 refill_tick 回 None、正式環境下一輪再驗；測試裡就輪詢到有結果為止。
    ev = None
    for _ in range(100):
        ev = a.refill_tick()
        if ev:
            break
        _idle(a); a.finish(); a.take_turn_result()
        time.sleep(0.1)
    assert ev and "交棒檔已驗" in ev, ev
    st = a._refill_state()
    assert st["phase"] == "recall_sent" and st["new_session"] != old_sid
    assert Path(st["archive"]).read_text().count("archive.ready") == 1
    _idle(a); a.finish(); t = a.take_turn_result()
    assert t["session_id"] == st["new_session"]
    assert a.refill_tick() == "補血完成：新 session 已接回" and a._refill_state() == {}


def test_refill_needs_two_reads_and_unknown_is_not_zero(ad, monkeypatch):
    monkeypatch.setenv("FAKE_ACP_USED", "6500")
    a = ad()
    assert a.refill_tick() is None                       # 沒有用量（UNKNOWN）→ 不觸發
    turn(a, 1)
    assert "第 1 讀" in a.refill_tick()
    monkeypatch.setenv("FAKE_ACP_USED", "1000")
    (a.state_dir / "acp_usage.json").write_text(json.dumps({"used": 1000, "size": 10000}))
    assert a.refill_tick() is None and a._refill_state().get("over") == 0   # 掉回線下 → 歸零


def test_refill_codex_threshold_is_70(ad):
    assert ad(acp_agent="codex").refill_threshold() == 70
    assert ad(acp_agent="claude").refill_threshold() == 60
    assert ad(refill_percent=80).refill_threshold() == 80


def test_refill_without_handoff_keeps_session_and_fails_after_timeout(ad, monkeypatch):
    monkeypatch.setenv("FAKE_ACP_USED", "6500")
    monkeypatch.setenv("FAKE_ACP_WRITE_HANDOFF", "0")
    a = ad()
    sid = turn(a, 1)["session_id"]
    a.refill_tick(); a.refill_tick()
    _idle(a); a.finish(); a.take_turn_result()
    assert a.refill_tick() is None                       # 沒寫交棒檔 → 不 clear
    st = a._refill_state(); st["started"] -= 1000; a._refill_save(st)
    assert "補血失敗" in a.refill_tick()
    assert a.session() == sid                            # 舊 session 保留


def test_provider_error_detected_as_failure():
    from drivers.acp_host import _provider_error
    bad = "GitHub Copilot rejected this request as malformed.\n\nProvider said: HTTP 400: model \"x\" is not accessible via the /chat/completions endpoint"
    assert _provider_error(bad, {}).startswith("模型或 provider 拒絕")
    assert _provider_error(bad, {"outputTokens": 30}) is None           # 有真的產出 → 不是 provider 錯誤
    assert _provider_error("審查完成，HTTP 400 的處理沒問題", {"inputTokens": 5}) is None
