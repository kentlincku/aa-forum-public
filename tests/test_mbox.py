import json
import os
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from mbox.broker import make_handler  # noqa: E402
from mbox.core import MboxError, Store  # noqa: E402


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "t.db", lease_s=1)


def mk(store, i, rank="worker"):
    tok = store.add_agent(i, "x", rank)
    return store.auth(tok), tok


def test_multiline_long_message_roundtrip(store):
    a, _ = mk(store, "lead", "lead"); f, _ = mk(store, "builder")
    body = "第一行\n第二行\t含 tab\x1b[31m控制字元\n" + "長" * 5000
    store.send(a, "builder", body)
    got = store.inbox(f)
    assert len(got) == 1 and got[0]["body"] == body
    assert store.inbox(f, unread_only=True)[0]["state"] == "delivered"


def test_idempotent_send(store):
    a, _ = mk(store, "lead", "lead"); mk(store, "builder")
    r1 = store.send(a, "builder", "hi", idem_key="k1")
    r2 = store.send(a, "builder", "hi", idem_key="k1")
    assert r2["duplicate"] and r1["id"] == r2["id"]
    assert store.unread_count("builder") == 1


def test_ack_monotonic_and_scoped(store):
    a, _ = mk(store, "lead", "lead"); f, _ = mk(store, "builder"); l, _ = mk(store, "reviewer")
    m = store.send(a, "builder", "x")["id"]
    assert store.ack(f, m, "done")["changed"]
    assert not store.ack(f, m, "read")["changed"]  # 不倒退
    with pytest.raises(MboxError):
        store.ack(l, m, "read")  # 不是給 reviewer 的
    assert store.unread_count("builder") == 0


def test_identity_cannot_be_spoofed(tmp_path):
    s = Store(tmp_path / "h.db")
    ta = s.add_agent("lead", "claude", "lead"); s.add_agent("builder", "codex", "worker")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(s))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    import urllib.request, urllib.error
    def post(tok, path, body):
        rq = urllib.request.Request(url + path, json.dumps(body).encode(), method="POST",
                                    headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(rq) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    assert post("bogus", "/v1/send", {"to": "builder", "body": "x"})[0] == 401
    code, r = post(ta, "/v1/send", {"to": "builder", "body": "x", "sender": "owner"})  # 試圖自填 sender
    assert code == 200
    assert s.inbox({"id": "builder"})[0]["sender"] == "lead"
    srv.shutdown()


def test_worker_cannot_task_lead(store):
    a, _ = mk(store, "lead", "lead"); f, _ = mk(store, "builder")
    with pytest.raises(MboxError) as e:
        store.post_task(f, "幫我做", assignee="lead")
    assert e.value.code == 403
    store.send(f, "lead", "可以聊天")  # 一般訊息可以


def test_task_lifecycle_and_lease_expiry(store):
    a, _ = mk(store, "lead", "lead"); f, _ = mk(store, "builder"); l, _ = mk(store, "reviewer")
    t = store.post_task(a, "寫 hello.py", "印出 hi")["task_id"]
    assert store.unread_count("builder") == 1 and store.unread_count("reviewer") == 1  # 公開任務
    store.claim_task(f, t)
    with pytest.raises(MboxError):
        store.claim_task(l, t)
    time.sleep(1.5)  # lease（1 秒）過期；留 0.5 秒餘裕，容器裡較慢時 1.1 秒會偶發不足
    assert store.tasks(l, state="open")[0]["id"] == t
    store.claim_task(l, t)
    with pytest.raises(MboxError):
        store.update_task(f, t, "done", "我做的")  # 已不是 builder 的
    r = store.update_task(l, t, "done", "完成", attachments=["/tmp/hello.py"])
    res = [m for m in store.inbox(a) if m["kind"] == "result"]
    assert res and res[0]["attachments"] == ["/tmp/hello.py"] and res[0]["task_id"] == t
    assert r["state"] == "done"


def test_threading(store):
    a, _ = mk(store, "lead", "lead"); f, _ = mk(store, "builder")
    m1 = store.send(a, "builder", "問題")
    m2 = store.send(f, "lead", "回答", reply_to=m1["id"])
    assert m2["thread_id"] == m1["id"]
    assert [m["body"] for m in store.thread(a, m1["id"])] == ["問題", "回答"]


def test_broadcast_excludes_sender(store):
    a, _ = mk(store, "lead", "lead"); mk(store, "builder"); mk(store, "reviewer")
    r = store.send(a, "@all", "大家好")
    assert sorted(r["recipients"]) == ["builder", "reviewer"]


def test_cli_and_mcp_end_to_end(tmp_path):
    env = dict(os.environ, MBOX_HOME=str(tmp_path), MBOX_URL="")
    py = sys.executable
    s = Store(tmp_path / "mbox.sqlite3")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(s))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    env["MBOX_URL"] = f"http://127.0.0.1:{srv.server_address[1]}"
    cli = [py, str(ROOT / "mbox" / "cli.py")]
    for who, rank in (("lead", "lead"), ("builder", "worker")):
        subprocess.run(cli + ["admin", "add", who, "--rank", rank], env=env, check=True, capture_output=True)
    body = "多行\n內容\n" + "x" * 3000
    r = subprocess.run(cli + ["send", "builder", "-"], input=body, text=True, env=dict(env, MBOX_AGENT="lead"),
                       capture_output=True)
    assert r.returncode == 0, r.stderr
    # builder 透過 MCP 讀
    reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "inbox", "arguments": {}}}]
    p = subprocess.run([py, str(ROOT / "mbox" / "mcp_server.py")], input="\n".join(json.dumps(x) for x in reqs) + "\n",
                       text=True, env=dict(env, MBOX_AGENT="builder"), capture_output=True)
    outs = [json.loads(l) for l in p.stdout.splitlines()]
    assert [o["id"] for o in outs] == [1, 2, 3]
    assert "send" in {t["name"] for t in outs[1]["result"]["tools"]}
    msgs = json.loads(outs[2]["result"]["content"][0]["text"])
    assert msgs[0]["body"] == body and msgs[0]["sender"] == "lead"
    # stop hook 看得到未讀數（peek 不改狀態後再 ack 為 done 歸零）
    hook = subprocess.run([str(ROOT / "bin" / "mbox-stop-hook")], input="{}", text=True,
                          env=dict(env, MBOX_AGENT="builder"), capture_output=True)
    assert '"decision":"block"' in hook.stdout
    subprocess.run(cli + ["ack", str(msgs[0]["id"]), "done"], env=dict(env, MBOX_AGENT="builder"), check=True, capture_output=True)
    hook = subprocess.run([str(ROOT / "bin" / "mbox-stop-hook")], input="{}", text=True,
                          env=dict(env, MBOX_AGENT="builder"), capture_output=True)
    assert hook.stdout == ""
    srv.shutdown()


def test_broker_inbox_limit_bounds(tmp_path):
    import urllib.request
    store = Store(tmp_path / 'limits.db')
    sender, _ = mk(store, 'lead', 'lead')
    _, token = mk(store, 'builder')
    for i in range(501):
        store.send(sender, 'builder', f'message {i}')
    server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(store))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        for query, expected in [('', 50), ('&limit=1000000', 500), ('&limit=3', 3),
                                ('&limit=0', 0), ('&limit=-1', 0)]:
            req = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/v1/inbox?peek=1{query}',
                                         headers={'Authorization': f'Bearer {token}'})
            with urllib.request.urlopen(req) as response:
                rows = json.load(response)
            assert len(rows) == expected
            assert all(row['state'] == 'queued' for row in rows)
    finally:
        server.shutdown()
        server.server_close()
        store.db.close()


def test_broker_notification_system_auth_required_priority_and_idempotency(tmp_path):
    import urllib.request
    import urllib.error
    store = Store(tmp_path / 'notify.db')
    system = store.add_agent('server', 'system', 'human')
    worker = store.add_agent('builder', 'codex', 'worker')
    server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(store))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    def post(token, body):
        req = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/v1/notifications',
            data=json.dumps(body).encode(), headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)
    try:
        body = dict(to='builder', body='notice', priority='digest', priority_reason='default→digest', idem_key='notification-1')
        assert post(worker, body)[0] == 403
        assert post('bad-token', body)[0] == 401
        for missing in ('priority', 'priority_reason'):
            assert post(system, {k:v for k,v in body.items() if k != missing})[0] == 400
        assert post(system, dict(body, priority='invalid'))[0] == 400
        status, first = post(system, body)
        assert status == 200 and not first['duplicate']
        assert post(system, body)[1]['duplicate']
        rows = store.inbox({'id': 'builder'}, mark=False)
        assert len(rows) == 1 and rows[0]['priority'] == 'digest'
        owner_body = dict(body, idem_key='notification-2', source_owner=True, source_room=2, source_messages=[55])
        assert post(system, owner_body)[0] == 200
        row = store.inbox({'id': 'builder'}, mark=False)[-1]
        assert (row['priority'],row['priority_reason']) == ('must','sender=owner')
        assert row['source_room'] == 2 and json.loads(row['source_messages']) == [55]
    finally:
        server.shutdown()
        server.server_close()
        store.db.close()
