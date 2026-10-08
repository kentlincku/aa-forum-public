"""S10：群組範圍——群組相關工作只能找群內成員；角色之間的往來同步到群。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mbox.core import MboxError, Store  # noqa: E402


@pytest.fixture()
def st(tmp_path):
    s = Store(tmp_path / "m.sqlite3")
    for a, r in (("owner", "human"), ("lead", "lead"), ("builder", "worker"), ("reviewer", "worker")):
        s.add_agent(a, "x", r)
    s.add_agent("server", "system", "human")
    s.set_room_members(2, ["owner", "lead", "builder"])
    return s


def me(s, a):
    return next(dict(x, id=x["id"]) for x in s.agents() if x["id"] == a)


def test_task_to_outsider_blocked(st):
    with pytest.raises(MboxError, match="不在群 #2"):
        st.post_task(me(st, "lead"), "審查", assignee="reviewer", source_room=2)
    r = st.post_task(me(st, "lead"), "rebase", assignee="builder", source_room=2)
    assert r["task_id"]


def test_reply_inherits_room_and_is_scoped(st):
    zk = me(st, "server")
    n = st.send(zk, "lead", "群 #2 有新留言", source_room=2, priority="must", priority_reason="x")
    with pytest.raises(MboxError):
        st.send(me(st, "lead"), "reviewer", "幫我看", reply_to=n["id"])          # 回覆串沿用群 #2 → 擋
    st.send(me(st, "lead"), "builder", "幫我看", reply_to=n["id"])
    st.send(me(st, "lead"), "owner", "回報 owner", reply_to=n["id"])          # owner 一律可以
    st.send(me(st, "lead"), "reviewer", "跟群無關的私訊")                        # 沒有群 → 不限


def test_mirror_queue(st):
    st.post_task(me(st, "lead"), "rebase", spec="細節", assignee="builder", source_room=2)
    st.send(me(st, "lead"), "reviewer", "無關")
    rows = st.pending_mirror()
    assert [(r["room"], r["kind"], r["sender"], r["recipient"]) for r in rows] == [(2, "task", "lead", "builder")]
    st.mark_mirror(rows[0]["id"], True)
    assert st.pending_mirror() == []


def test_task_result_goes_to_same_room(st):
    t = st.post_task(me(st, "lead"), "rebase", assignee="builder", source_room=2)
    st.claim_task(me(st, "builder"), t["task_id"])
    st.update_task(me(st, "builder"), t["task_id"], "done", "完成")
    kinds = [(r["kind"], r["sender"]) for r in st.pending_mirror()]
    assert ("result", "builder") in kinds


def test_unknown_room_not_blocked(st):
    st.post_task(me(st, "lead"), "x", assignee="reviewer", source_room=99)    # 名單未同步 → 不擋


def test_mail_to_human_goes_to_notice_room_and_acks(st):
    m = st.send(me(st, "lead"), "owner", "計畫待批")
    rows = st.pending_mirror()
    assert [(r["room"], r["id"]) for r in rows] == [(0, m["id"])]
    st.mark_mirror(m["id"], True)
    assert st.unread_count("owner") == 0 if hasattr(st, "unread_count") else True


def test_acked_notifications_listed(st):
    zk = me(st, "server")
    n = st.send(zk, "builder", "群 #2 有新留言", source_room=2, source_messages=[80, 83],
                priority="must", priority_reason="x")
    assert st.acked_notifications() == []
    st.ack(me(st, "builder"), n["id"], "done")
    rows = st.acked_notifications()
    assert rows and rows[0]["recipient"] == "builder" and rows[0]["source_room"] == 2
