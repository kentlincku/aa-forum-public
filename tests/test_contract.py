"""S1 契約測試：schema 自身有效、各 agent 輸出 → TurnResult 符合契約、核心記錄、dispatcher 串接。"""
import json
import time
from pathlib import Path

import jsonschema
import pytest

import adapters
from mbox import contract
from mbox.core import Store

FIX = Path(__file__).parent / "fixtures" / "turns"


@pytest.mark.parametrize("kind", contract.KINDS)
def test_schemas_are_valid_draft2020(kind):
    jsonschema.Draft202012Validator.check_schema(contract.schema(kind))


def test_turn_result_rejects_unknown_and_missing():
    with pytest.raises(contract.ContractError):
        contract.validate("turn_result", {"contract_version": "1", "role": "x"})
    tr = contract.turn_result("builder", "hermes", 0)
    tr["bogus"] = 1
    with pytest.raises(contract.ContractError):
        contract.validate("turn_result", tr)
    with pytest.raises(contract.ContractError):
        contract.turn_result("builder", "hermes", 0, nope=1)


def test_wake_request_and_manifest_examples():
    contract.validate("wake_request", {"contract_version": "1", "role": "builder", "prompt": "hi", "cwd": "/tmp",
                                       "session_id": None, "model": "m", "env": {"A": "b"}, "timeout_s": 60})
    contract.validate("manifest", {"name": "hermes", "version": "0.1", "contract_version": "1",
                                   "capabilities": ["resume_session", "model_select"], "wake_modes": ["headless", "tmux"]})
    with pytest.raises(contract.ContractError):
        contract.validate("manifest", {"name": "x", "version": "1", "contract_version": "1",
                                       "capabilities": ["fly"], "wake_modes": ["headless"]})
    contract.validate("usage", {"contract_version": "1", "source": "turn_result", "context_used": 10})


def _finish(cls, tmp_path, out_text, exit_code="0", cfg=None):
    cfg = dict(cfg or {}, workdir=str(tmp_path / "wd"))
    ad = cls("builder", cfg, tmp_path / "home")
    ad._output_file().write_text(out_text)
    (ad.state_dir / "last_exit.txt").write_text(exit_code + "\n")
    (ad.state_dir / "turn_started.txt").write_text(repr(time.time() - 2))
    ad.finish()
    tr = ad.take_turn_result()
    assert tr is not None
    contract.validate("turn_result", tr)
    assert ad.take_turn_result() is None  # 取走即刪
    return tr


def test_hermes_real_sample(tmp_path):
    """樣本＝v1 lead 的真實 hermes stream-json 輸出。"""
    tr = _finish(adapters.HermesHeadless, tmp_path, (FIX / "hermes_ok.jsonl").read_text())
    assert tr["driver"] == "hermes" and tr["ok"] is True and tr["exit"] == 0
    assert tr["session_id"] == "20261005_145635_8f36ed"
    assert tr["model_used"] == "gpt-6-astra"
    assert tr["tokens"]["output"] == 137 and tr["tokens"]["cache_read"] == 173874
    assert tr["duration_ms"] == 22920


def test_codex_real_sample(tmp_path):
    """樣本＝v1 debugger 沙箱中 codex exec --json 的真實輸出。"""
    tr = _finish(adapters.CodexHeadless, tmp_path, (FIX / "codex_sample.jsonl").read_text(), cfg={"model": "gpt-x"})
    assert tr["driver"] == "codex"
    assert tr["session_id"] == "01a10b9f-4709-7593-8f8f-60d436fa052f"
    assert tr["tokens"]["input"] == 97161 and tr["tokens"]["cache_read"] == 79744 and tr["tokens"]["output"] == 228
    assert tr["model_used"] == "gpt-x"  # codex 輸出沒有模型，退回設定值
    assert tr["duration_ms"] >= 1000


def test_claude_synthetic_sample(tmp_path):
    """合成樣本（依 claude -p --output-format json 欄位；本機 claude 未登入，無真實樣本）。"""
    out = json.dumps({"type": "result", "is_error": False, "result": "done", "session_id": "s-1",
                      "duration_ms": 1234, "usage": {"input_tokens": 5, "output_tokens": 7,
                      "cache_read_input_tokens": 100, "cache_creation_input_tokens": 3},
                      "modelUsage": {"claude-x": {}}})
    tr = _finish(adapters.ClaudeHeadless, tmp_path, out)
    assert tr["session_id"] == "s-1" and tr["model_used"] == "claude-x" and tr["tokens"]["cache_write"] == 3
    err = json.dumps({"type": "result", "is_error": True, "result": "Not logged in"})
    tr = _finish(adapters.ClaudeHeadless, tmp_path, err, exit_code="1")
    assert tr["ok"] is False and tr["error"] == "Not logged in"


def test_pi_and_failure_paths(tmp_path):
    tr = _finish(adapters.PiHeadless, tmp_path, "回覆內容", cfg={"model": "pm"})
    assert tr["driver"] == "pi" and tr["text"] == "回覆內容" and tr["model_used"] == "pm"
    # hermes 啟動腳本壞掉（exit 126、沒有 result 事件）→ ok False，帶錯誤
    tr = _finish(adapters.HermesHeadless, tmp_path, "", exit_code="126")
    assert tr["ok"] is False and tr["exit"] == 126 and "result" in tr["error"]


def test_store_record_turn(tmp_path):
    s = Store(tmp_path / "m.sqlite3")
    tr = contract.turn_result("builder", "hermes", 0, session_id="a", model_used="m", finished_at=time.time())
    s.record_turn(tr)
    s.record_turn(contract.turn_result("builder", "hermes", 1, error="x"))
    got = s.last_turns("builder", 2)
    assert [g["exit"] for g in got] == [1, 0] and got[1]["session_id"] == "a"
    with pytest.raises(contract.ContractError):
        s.record_turn({"role": "builder"})


def test_dispatcher_records_turn(tmp_path, monkeypatch):
    """dispatcher.once 收尾一個已結束的輪次時，把 TurnResult 寫進核心。"""
    from mbox import dispatcher
    home = tmp_path / "home"
    monkeypatch.setenv("MBOX_HOME", str(home))
    monkeypatch.setattr(dispatcher, "home", lambda: home)
    home.mkdir(parents=True)
    s = Store(home / "mbox.sqlite3")
    cfg = {"adapter": "hermes-headless", "runtime": "hermes", "workdir": str(tmp_path / "wd")}
    ad = adapters.HermesHeadless("builder", cfg, home)
    ad._output_file().write_text((FIX / "hermes_ok.jsonl").read_text())
    (ad.state_dir / "last_exit.txt").write_text("0\n")
    dispatcher.once({"builder": cfg}, s, log=lambda *a: None)
    got = s.last_turns("builder")
    assert got and got[0]["model_used"] == "gpt-6-astra" and got[0]["ok"] is True
