"""同一則信連續叫醒失敗 → guardian 收到一次告警（e2e G4：agent 起不來時信停在 queued，原本沒人知道）。"""
import json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mbox.core import Store  # noqa: E402
import drivers  # noqa: E402


def test_stuck_wake_alerts_guardian_once(tmp_path):
    st = Store(tmp_path / "mbox.sqlite3")
    for r, rank in (("guardian", "worker"), ("builder", "worker"), ("owner", "human")):
        st.add_agent(r, "x", rank)
    ad = drivers.make("builder", {"driver": "command", "command": "false"}, tmp_path)
    for i in range(6):
        ad._bo_save({**ad._bo(), "next": 0})          # 跳過等待
        ad.wake(1, head=42)
        end = time.time() + 10
        while ad.health() == "busy" and time.time() < end:
            time.sleep(0.05)
        ad.finish()
    alerts = [m for m in st.inbox({"id": "guardian"}) if "叫醒失敗告警" in m["body"]]
    assert len(alerts) == 1
    assert "builder" in alerts[0]["body"] and "#42" in alerts[0]["body"]
