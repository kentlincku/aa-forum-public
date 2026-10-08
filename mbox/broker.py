"""mbox broker：純 stdlib HTTP/JSON 服務。只綁 127.0.0.1。

路由（皆需 Authorization: Bearer <token>，除 /health）：
  GET  /health
  GET  /v1/me
  GET  /v1/agents
  POST /v1/send          {to, body, kind?, reply_to?, thread_id?, attachments?, mission_id?, idem_key?}
  GET  /v1/inbox?all=0&limit=50&peek=0  limit 限制 0–500（負值視為 0）
  POST /v1/notifications {to, body, priority, priority_reason, idem_key?, source_owner?, source_room?, source_messages?} (system only)
  GET  /v1/runs?limit=50 (human/system only)
  POST /v1/ack           {message_id, state}
  GET  /v1/thread/<id>
  GET  /v1/status/<message_id>
  POST /v1/tasks         {title, spec?, assignee?, mission_id?, idem_key?}
  GET  /v1/tasks?state=&mine=0
  POST /v1/tasks/<id>/claim
  POST /v1/tasks/<id>/update  {state, result?, attachments?}
  POST /v1/heartbeat     {status, context_pct?}
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .core import MboxError, Store

MAX_REQ = 2 * 1024 * 1024


def _room(b: dict):
    v = b.get("source_room")
    try:
        return int(v) if v not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        return None


def make_handler(store: Store):
    class H(BaseHTTPRequestHandler):
        server_version = "mbox/0.1"

        def log_message(self, fmt, *a):
            if os.environ.get("MBOX_LOG"):
                sys.stderr.write("mbox: " + fmt % a + "\n")

        def _out(self, code: int, obj):
            b = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_REQ:
                raise MboxError(413, "請求過大")
            if not n:
                return {}
            try:
                d = json.loads(self.rfile.read(n))
            except json.JSONDecodeError:
                raise MboxError(400, "JSON 格式錯誤")
            if not isinstance(d, dict):
                raise MboxError(400, "需要 JSON 物件")
            return d

        def _me(self) -> dict:
            a = self.headers.get("Authorization", "")
            return store.auth(a[7:] if a.startswith("Bearer ") else None)

        def _route(self, method: str):
            u = urlsplit(self.path)
            p, q = u.path.rstrip("/"), {k: v[-1] for k, v in parse_qs(u.query).items()}
            try:
                if method == "GET" and p == "/health":
                    return self._out(200, {"ok": True})
                me = self._me()
                b = self._body() if method == "POST" else {}
                m = re.fullmatch(r"/v1/(thread|status)/(\d+)", p)
                t = re.fullmatch(r"/v1/tasks/(\d+)/(claim|update)", p)
                if method == "GET" and p == "/v1/me":
                    r = me
                elif method == "GET" and p == "/v1/agents":
                    r = store.agents()
                elif method == "POST" and p == "/v1/send":
                    r = store.send(me, b.get("to", ""), b.get("body", ""), kind=b.get("kind", "chat"),
                                   reply_to=b.get("reply_to"), thread_id=b.get("thread_id"),
                                   attachments=b.get("attachments"), mission_id=b.get("mission_id"),
                                   idem_key=b.get("idem_key"), source_room=_room(b))
                elif method == "POST" and p == "/v1/room-members":
                    if me['runtime'] != 'system':
                        raise MboxError(403, '群組名單限 system token')
                    for room, agents in (b.get("rooms") or {}).items():
                        store.set_room_members(int(room), [str(a) for a in agents])
                    r = {"ok": True}
                elif method == "GET" and p == "/v1/notifications/acked":
                    if me['runtime'] != 'system':
                        raise MboxError(403, '限 system token')
                    r = store.acked_notifications(float(q.get("since") or 0))
                elif method == "GET" and p == "/v1/room-mirror":
                    if me['runtime'] != 'system':
                        raise MboxError(403, '限 system token')
                    r = store.pending_mirror()
                elif method == "POST" and p == "/v1/room-mirror":
                    if me['runtime'] != 'system':
                        raise MboxError(403, '限 system token')
                    for mid in b.get("done") or []:
                        store.mark_mirror(int(mid), True)
                    for mid in b.get("failed") or []:
                        store.mark_mirror(int(mid), False)
                    r = {"ok": True}
                elif method == "POST" and p == "/v1/notifications":
                    if me['runtime'] != 'system':
                        raise MboxError(403, '通知 API 限 system token')
                    if 'priority' not in b or 'priority_reason' not in b:
                        raise MboxError(400, 'priority 與 priority_reason 必填')
                    r = store.send(me, b.get('to',''), b.get('body',''), kind=b.get('kind','chat'),
                                   idem_key=b.get('idem_key'), priority=b['priority'],
                                   priority_reason=b['priority_reason'], source_owner=b.get('source_owner',False),
                                   source_room=b.get('source_room'), source_messages=b.get('source_messages'))
                elif method == 'GET' and p == '/v1/runs':
                    if me['rank'] != 'human' and me['runtime'] != 'system':
                        raise MboxError(403, '執行記錄限 human/system')
                    r = store.recent_runs(int(q.get('limit',50)))
                elif method == 'GET' and p == '/v1/turns':
                    if me['rank'] != 'human' and me['runtime'] != 'system':
                        raise MboxError(403, '輪次記錄限 human/system')
                    r = store.last_turns(q.get('role', ''), max(1, min(int(q.get('limit', 1)), 50)), q.get('ok') in {'1', 'true'})
                elif method == "GET" and p == "/v1/inbox":
                    # Bound both ends: SQLite treats a negative LIMIT as unlimited.
                    r = store.inbox(me, unread_only=q.get("all") not in {"1", "true"},
                                    limit=max(0, min(int(q.get("limit", 50)), 500)), mark=q.get("peek") not in {"1", "true"},
                                    lane=q.get("lane") or None)
                elif method == "POST" and p == "/v1/ack":
                    r = store.ack(me, int(b.get("message_id", 0)), b.get("state", "read"))
                elif method == "GET" and m:
                    r = (store.thread if m[1] == "thread" else store.status)(me, int(m[2]))
                elif method == "POST" and p == "/v1/tasks":
                    r = store.post_task(me, b.get("title", ""), b.get("spec", ""), b.get("assignee"),
                                        b.get("mission_id"), b.get("idem_key"), source_room=_room(b))
                elif method == "GET" and p == "/v1/tasks":
                    r = store.tasks(me, q.get("state") or None, q.get("mine") in {"1", "true"})
                elif method == "POST" and t and t[2] == "claim":
                    r = store.claim_task(me, int(t[1]))
                elif method == "POST" and t:
                    r = store.update_task(me, int(t[1]), b.get("state", ""), b.get("result"), b.get("attachments"))
                elif method == "POST" and p == "/v1/heartbeat":
                    r = store.heartbeat(me, b.get("status", "alive"), b.get("context_pct"))
                else:
                    raise MboxError(404, f"沒有這個路由：{method} {p}")
                self._out(200, r)
            except MboxError as e:
                self._out(e.code, {"error": e.msg})
            except sqlite3.OperationalError as e:
                self._out(503, {"error": f"資料庫暫不可用：{type(e).__name__}"})
            except (ValueError, TypeError) as e:
                self._out(400, {"error": f"參數錯誤：{e}"})

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

    return H


def default_home() -> Path:
    return Path(os.environ.get("MBOX_HOME") or Path(os.path.expanduser(os.environ.get("AAF_HOME") or str(Path(__file__).resolve().parent.parent))) / "var")


def serve(db: Path, host: str, port: int):
    srv = ThreadingHTTPServer((host, port), make_handler(Store(db)))
    print(f"mbox broker on http://{host}:{port}  db={db}", flush=True)
    srv.serve_forever()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mbox-broker")
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("MBOX_PORT", 8775)))
    a = ap.parse_args(argv)
    home = default_home()
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    serve(a.db or home / "mbox.sqlite3", a.host, a.port)


if __name__ == "__main__":
    main()
