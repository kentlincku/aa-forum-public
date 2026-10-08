"""S6：外部程式 driver（stdin/stdout 交換契約 JSON）。"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import drivers  # noqa: E402
from mbox import contract  # noqa: E402

ECHO = str(ROOT / "examples" / "external-driver" / "echo-driver.sh")


def make(tmp_path, exe=ECHO, **kw):
    return drivers.make("ext", {"driver": "external", "exec": exe, "workdir": str(tmp_path / "w"), **kw}, tmp_path / "mbox")


def run_turn(ad, head=1):
    assert ad.wake(1, head=head).startswith("started")
    for _ in range(100):
        if ad.health() == "idle":
            break
        time.sleep(0.05)
    ad.finish()
    return ad.take_turn_result()


def test_manifest_check_and_usage(tmp_path):
    ad = make(tmp_path)
    m = ad.describe()
    assert m["name"] == "echo-driver" and "usage_report" in m["capabilities"]
    assert ad.check() == (True, "echo-driver ok")
    u = ad.usage(None)
    assert u["percent"] == 10 and contract.validate("usage", u)


def test_wake_produces_valid_turn_result(tmp_path):
    ad = make(tmp_path, model="m-x")
    tr = run_turn(ad)
    contract.validate("turn_result", tr)
    assert tr["ok"] is True and tr["driver"] == "external" and tr["role"] == "ext"
    assert tr["model_used"] == "m-x" and tr["text"] == f"收到 {len(drivers.wake_prompt('ext', 1))} 字"
    assert ad.session().startswith("echo-")
    req = json.loads((ad.state_dir / "wake_request.json").read_text())
    assert contract.validate("wake_request", req) and req["cwd"] == str(tmp_path / "w")
    sid = ad.session()
    tr2 = run_turn(ad, head=2)  # 第二輪（新的一則）帶回 session
    assert tr2["session_id"] == sid


def test_bad_external_driver_is_contained(tmp_path):
    bad = tmp_path / "bad.sh"
    bad.write_text("#!/bin/sh\n[ \"$1\" = wake ] && { echo not-json; exit 0; }\n[ \"$1\" = manifest ] && echo '{oops'\nexit 3\n")
    bad.chmod(0o755)
    ad = make(tmp_path, exe=str(bad))
    assert ad.describe()["name"] == "external"          # manifest 壞 → 退回最小 manifest
    ok, why = ad.check()
    assert ok is False
    tr = run_turn(ad)
    contract.validate("turn_result", tr)
    assert tr["ok"] is False and "未輸出 TurnResult" in tr["error"]


def test_reported_ok_cannot_override_nonzero_exit(tmp_path):
    liar = tmp_path / "liar.sh"
    liar.write_text("#!/bin/sh\necho '{\"ok\": true, \"text\": \"fine\"}'\nexit 1\n")
    liar.chmod(0o755)
    tr = run_turn(make(tmp_path, exe=str(liar)))
    assert tr["exit"] == 1 and tr["ok"] is False


def test_missing_exec(tmp_path):
    ok, why = make(tmp_path, exe=str(tmp_path / "nope")).check()
    assert ok is False and "不可執行" in why
