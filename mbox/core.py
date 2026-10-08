"""mbox 核心：SQLite 存放、身分、權限、ack、冪等、任務租約。不認得任何 agent 種類。"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents(
  id TEXT PRIMARY KEY, runtime TEXT NOT NULL DEFAULT 'generic',
  rank TEXT NOT NULL DEFAULT 'worker',          -- lead | worker | human
  token_hash TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'unknown', context_pct REAL,
  last_heartbeat REAL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id INTEGER, reply_to INTEGER,
  sender TEXT NOT NULL, recipient TEXT NOT NULL,   -- agent id 或 '@all'
  kind TEXT NOT NULL DEFAULT 'chat', body TEXT NOT NULL,
  attachments TEXT NOT NULL DEFAULT '[]', mission_id TEXT, task_id INTEGER,
  idem_key TEXT, created_at REAL NOT NULL,
  UNIQUE(sender, idem_key));
CREATE TABLE IF NOT EXISTS deliveries(
  message_id INTEGER NOT NULL, recipient TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'queued', updated_at REAL NOT NULL,
  PRIMARY KEY(message_id, recipient));
CREATE INDEX IF NOT EXISTS ix_deliv ON deliveries(recipient, state);
CREATE INDEX IF NOT EXISTS ix_deliv_age ON deliveries(state, updated_at);
CREATE TABLE IF NOT EXISTS delivery_alerts(
  message_id INTEGER NOT NULL, recipient TEXT NOT NULL, alerted_at REAL NOT NULL,
  PRIMARY KEY(message_id, recipient));
CREATE TABLE IF NOT EXISTS runs(
 id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL, started REAL NOT NULL,
 ended REAL, exit INTEGER, msgs_handled INTEGER NOT NULL DEFAULT 0,
 error TEXT NOT NULL DEFAULT '', message_ids TEXT NOT NULL DEFAULT '[]');
CREATE UNIQUE INDEX IF NOT EXISTS ix_run_active ON runs(role) WHERE ended IS NULL;
CREATE TABLE IF NOT EXISTS turn_results(
 id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL, driver TEXT NOT NULL,
 session_id TEXT, ok INTEGER NOT NULL, exit INTEGER, model_used TEXT,
 finished_at REAL NOT NULL, result TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_turn_role ON turn_results(role, id);
CREATE TABLE IF NOT EXISTS sessions(
 role TEXT NOT NULL, driver TEXT NOT NULL, session_id TEXT NOT NULL, updated_at REAL NOT NULL,
 PRIMARY KEY(role, driver));
CREATE TABLE IF NOT EXISTS priority_alerts(
 message_id INTEGER NOT NULL, recipient TEXT NOT NULL, alerted_at REAL NOT NULL,
 PRIMARY KEY(message_id,recipient));
CREATE TABLE IF NOT EXISTS must_alerts(
 message_id INTEGER NOT NULL, recipient TEXT NOT NULL, alerted_at REAL NOT NULL,
 PRIMARY KEY(message_id,recipient));
CREATE TABLE IF NOT EXISTS delivery_timers(
 message_id INTEGER NOT NULL, recipient TEXT NOT NULL, delivered_at REAL NOT NULL,
 idle_since REAL, idle_seconds REAL NOT NULL DEFAULT 0, last_idle_at REAL, version REAL NOT NULL,
 PRIMARY KEY(message_id,recipient));
CREATE TABLE IF NOT EXISTS dispatch_state(role TEXT PRIMARY KEY, last_wake_at REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS room_members(
  room INTEGER NOT NULL, agent TEXT NOT NULL, PRIMARY KEY(room, agent));
CREATE TABLE IF NOT EXISTS room_mirror(
  message_id INTEGER PRIMARY KEY, room INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
  tries INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, spec TEXT NOT NULL DEFAULT '',
  owner TEXT NOT NULL, assignee TEXT, state TEXT NOT NULL DEFAULT 'open',
  lease_until REAL, result_msg_id INTEGER, mission_id TEXT,
  created_at REAL NOT NULL, updated_at REAL NOT NULL);
"""

KINDS = {"chat", "task", "result", "system"}
DELIV_ORDER = ["queued", "delivered", "read", "done", "rejected"]
TASK_STATES = {"open", "claimed", "done", "blocked", "cancelled"}
MAX_BODY = 1_000_000  # 1MB；大型產出請放檔案、附路徑


class MboxError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code
        self.msg = msg


def _h(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()


class Store:
    def __init__(self, path: str | Path, lease_s: int = 1800, busy_timeout_ms: int | None = None):
        self.path = str(path)
        self.lease_s = lease_s
        self._lock = threading.RLock()
        # Apply one configurable wait budget to connection setup and later queries.
        # MBOX_BUSY_TIMEOUT_MS defaults to SQLite's usual five-second wait.
        timeout_ms = int(os.environ.get("MBOX_BUSY_TIMEOUT_MS", "5000")) if busy_timeout_ms is None else int(busy_timeout_ms)
        if timeout_ms < 0:
            raise ValueError("busy_timeout_ms must be nonnegative")
        self.db = sqlite3.connect(self.path, timeout=timeout_ms / 1000,
                                  check_same_thread=False, isolation_level=None)
        self.db.execute(f"PRAGMA busy_timeout={timeout_ms}")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        agent_cols = {r['name'] for r in self.db.execute('PRAGMA table_info(agents)')}
        if 'heartbeat_source' not in agent_cols:  # self＝角色自報；proxy＝dispatcher 代報（#40 reviewer SHOULD 1）
            self.db.execute("ALTER TABLE agents ADD COLUMN heartbeat_source TEXT")
        if 'self_heartbeat' not in agent_cols:    # 角色最後一次自報時間（代報不會更新）
            self.db.execute("ALTER TABLE agents ADD COLUMN self_heartbeat REAL")
        columns = {r['name'] for r in self.db.execute('PRAGMA table_info(messages)')}
        for name, definition in {
            'priority': "TEXT NOT NULL DEFAULT 'must'",
            'priority_reason': "TEXT NOT NULL DEFAULT 'legacy→must'",
            'source_owner': 'INTEGER NOT NULL DEFAULT 0',
            'source_room': 'INTEGER',
            'source_messages': "TEXT NOT NULL DEFAULT '[]'",
        }.items():
            if name not in columns:
                self.db.execute(f'ALTER TABLE messages ADD COLUMN {name} {definition}')

    # ---------- 交易 ----------
    def _tx(self):
        store = self

        class _T:
            def __enter__(self):
                store._lock.acquire()
                try:
                    store.db.execute("BEGIN IMMEDIATE")
                except BaseException:
                    store._lock.release()
                    raise
                return store.db

            def __exit__(self, et, ev, tb):
                try:
                    store.db.execute("ROLLBACK" if et else "COMMIT")
                finally:
                    store._lock.release()
                return False
        return _T()

    # ---------- 身分 ----------
    def add_agent(self, agent_id: str, runtime: str = "generic", rank: str = "worker") -> str:
        if not agent_id or not agent_id.replace("-", "").replace("_", "").isalnum() or agent_id.startswith("@"):
            raise MboxError(400, "agent id 只能用英數、-、_")
        if rank not in {"lead", "worker", "human"}:
            raise MboxError(400, "rank 必須是 lead|worker|human")
        tok = "mbx_" + secrets.token_urlsafe(24)
        with self._tx() as db:
            if db.execute("SELECT 1 FROM agents WHERE id=?", (agent_id,)).fetchone():
                db.execute("UPDATE agents SET token_hash=?, runtime=?, rank=? WHERE id=?",
                           (_h(tok), runtime, rank, agent_id))
            else:
                db.execute("INSERT INTO agents(id,runtime,rank,token_hash,created_at) VALUES(?,?,?,?,?)",
                           (agent_id, runtime, rank, _h(tok), time.time()))
        return tok

    def auth(self, token: str | None) -> dict:
        if not token:
            raise MboxError(401, "缺少 token")
        r = self.db.execute("SELECT id,runtime,rank FROM agents WHERE token_hash=?", (_h(token),)).fetchone()
        if not r:
            raise MboxError(401, "token 無效")
        return dict(r)

    def agents(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT a.id,a.runtime,a.rank,a.status,a.context_pct,a.last_heartbeat,a.heartbeat_source,"
            " (SELECT COUNT(*) FROM deliveries d WHERE d.recipient=a.id AND d.state IN ('queued','delivered')) unread"
            " FROM agents a ORDER BY a.id").fetchall()
        return [dict(r) for r in rows]

    # ---------- 權限 ----------
    def _check_send(self, me: dict, recipient: str, kind: str):
        if recipient == "@all":
            return
        r = self.db.execute("SELECT rank FROM agents WHERE id=?", (recipient,)).fetchone()
        if not r:
            raise MboxError(404, f"收件者不存在：{recipient}")
        if kind == "task" and me["rank"] == "worker" and r["rank"] in {"lead", "human"}:
            raise MboxError(403, "worker 不能指派任務給 lead/human")

    # ---------- 訊息 ----------
    def send(self, me: dict, to: str, body: str, kind: str = "chat", reply_to: int | None = None,
             thread_id: int | None = None, attachments: list | None = None, mission_id: str | None = None,
             idem_key: str | None = None, task_id: int | None = None,
             priority: str = 'must', priority_reason: str = 'direct→must',
             source_owner: bool = False, source_room: int | None = None,
             source_messages: list | None = None) -> dict:
        if priority not in {'must', 'digest'} or not isinstance(priority_reason, str) or not priority_reason:
            raise MboxError(400, 'priority 必須是 must|digest 且 priority_reason 不可空白')
        if source_owner or (me.get('rank') == 'human' and me.get('runtime') != 'system'
                            and me['id'] != 'mbox-dispatcher'):
            priority, priority_reason = 'must', 'sender=owner'
        elif kind == 'task':
            priority, priority_reason = 'must', 'task'
        if kind not in KINDS:
            raise MboxError(400, f"kind 必須是 {sorted(KINDS)}")
        if not isinstance(body, str) or not body.strip():
            raise MboxError(400, "body 不可空白")
        if len(body.encode()) > MAX_BODY:
            raise MboxError(413, "body 過大，請寫成檔案並用 attachments 附路徑")
        self._check_send(me, to, kind)
        from mbox import hooks as _hooks
        _ev = {"type": "mbox.task" if kind == "task" else "mbox.send", "sender": me["id"], "rank":
               "system" if me.get("runtime") == "system" else me.get("rank"), "to": to, "room": source_room,
               "kind": kind, "body": body, "reply_to": reply_to}
        if to != "@all":
            _r = self.db.execute("SELECT rank FROM agents WHERE id=?", (to,)).fetchone()
            _ev["to_rank"] = _r["rank"] if _r else None
        _why = _hooks.run_pre(_ev)
        if _why:
            raise MboxError(422, f"規則擋下：{_why}")
        with self._tx() as db:
            # 群組範圍：回覆／同串沿用來源群；有群就只准寄給該群成員（群外的人要先在群裡邀請）
            if source_room is None and (reply_to or thread_id):
                anchor = db.execute("SELECT source_room FROM messages WHERE id=?",
                                    (reply_to or thread_id,)).fetchone()
                if anchor and anchor["source_room"] is not None:
                    source_room = anchor["source_room"]
            if source_room is not None and to != "@all" and not self._room_ok(db, source_room, me, to):
                raise MboxError(403, f"{to} 不在群 #{source_room}：群組相關工作只能找群內成員。"
                                     f"需要 {to} 時，請在群裡提出邀請（由使用者或群成員在 AA Forum 按「邀請助手」）。")
            if idem_key:
                old = db.execute("SELECT id,thread_id FROM messages WHERE sender=? AND idem_key=?",
                                 (me["id"], idem_key)).fetchone()
                if old:
                    return {"id": old["id"], "thread_id": old["thread_id"], "duplicate": True}
            if reply_to and not thread_id:
                p = db.execute("SELECT thread_id,id FROM messages WHERE id=?", (reply_to,)).fetchone()
                if not p:
                    raise MboxError(404, f"reply_to 不存在：{reply_to}")
                thread_id = p["thread_id"] or p["id"]
            now = time.time()
            cur = db.execute(
                "INSERT INTO messages(thread_id,reply_to,sender,recipient,kind,body,attachments,mission_id,task_id,idem_key,created_at,priority,priority_reason,source_owner,source_room,source_messages)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (thread_id, reply_to, me["id"], to, kind, body, json.dumps(attachments or [], ensure_ascii=False),
                 mission_id, task_id, idem_key, now, priority, priority_reason, int(source_owner), source_room, json.dumps(source_messages or [])))
            mid = cur.lastrowid
            if not thread_id:
                thread_id = mid
                db.execute("UPDATE messages SET thread_id=? WHERE id=?", (mid, mid))
            rcpts = ([r["id"] for r in db.execute("SELECT id FROM agents WHERE id!=?", (me["id"],))]
                     if to == "@all" else [to])
            db.executemany("INSERT INTO deliveries(message_id,recipient,state,updated_at) VALUES(?,?,?,?)",
                           [(mid, r, "queued", now) for r in rcpts])
            # 群組相關的角色之間往來：排入「同步到群」佇列（AA Forum 自己發的通知不鏡像）
            to_human = to != "@all" and (db.execute("SELECT rank FROM agents WHERE id=?", (to,)).fetchone() or {"rank": ""})["rank"] == "human"
            if source_room is not None and me.get("runtime") != "system" and me["id"] != "mbox-dispatcher":
                db.execute("INSERT OR IGNORE INTO room_mirror(message_id,room,updated_at) VALUES(?,?,?)",
                           (mid, source_room, now))
            elif to_human and to != "server":
                # 給人（rank=human）的信：人不讀 mbox → 轉到 AA Forum「通知」群（room=0 代表通知群），轉完自動 ack
                db.execute("INSERT OR IGNORE INTO room_mirror(message_id,room,updated_at) VALUES(?,?,?)", (mid, 0, now))
        _hooks.run_post({**_ev, "room": source_room, "message_id": mid})
        return {"id": mid, "thread_id": thread_id, "duplicate": False, "recipients": rcpts}

    def _room_ok(self, db, room: int, me: dict, to: str) -> bool:
        members = {r["agent"] for r in db.execute("SELECT agent FROM room_members WHERE room=?", (room,))}
        if not members:
            return True                       # AA Forum 尚未同步這群的名單：不擋（避免誤殺）
        if me.get("runtime") == "system" or me.get("rank") == "human":
            return True
        if to in members:
            return True
        row = db.execute("SELECT rank FROM agents WHERE id=?", (to,)).fetchone()
        return bool(row) and row["rank"] == "human"   # 對人（rank=human）一律可以

    def set_room_members(self, room: int, agents: list[str]):
        with self._tx() as db:
            db.execute("DELETE FROM room_members WHERE room=?", (room,))
            db.executemany("INSERT INTO room_members(room,agent) VALUES(?,?)", [(room, a) for a in agents])

    def pending_mirror(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.db.execute("""SELECT x.room, m.id, m.sender, m.recipient, m.kind, m.body,
                m.task_id, m.reply_to FROM room_mirror x JOIN messages m ON m.id=x.message_id
                WHERE x.state='pending' AND x.tries<5 ORDER BY m.id LIMIT ?""", (limit,))]

    def acked_notifications(self, since: float = 0) -> list[dict]:
        """AA Forum 通知被角色處理完（ack）的紀錄：讓 AA Forum 把該角色在群裡的進度推到這批留言。"""
        with self._lock:
            return [dict(r) for r in self.db.execute("""SELECT d.recipient, m.source_room, m.source_messages, d.updated_at
                FROM deliveries d JOIN messages m ON m.id=d.message_id
                WHERE m.sender='server' AND m.source_room IS NOT NULL AND d.state IN ('read','done','rejected') AND d.updated_at>?
                ORDER BY d.updated_at LIMIT 500""", (since,))]

    def mark_mirror(self, message_id: int, ok: bool):
        with self._tx() as db:
            if ok:   # 已轉到 AA Forum：人的收件匣視為已讀
                db.execute("""UPDATE deliveries SET state='read', updated_at=? WHERE message_id=? AND state IN ('queued','delivered')
                              AND recipient IN (SELECT id FROM agents WHERE rank='human')""", (time.time(), message_id))
            db.execute("UPDATE room_mirror SET state=CASE WHEN ? THEN 'done' ELSE state END, tries=tries+1,"
                       " updated_at=? WHERE message_id=?", (int(ok), time.time(), message_id))

    def _msg(self, r) -> dict:
        d = dict(r)
        d["attachments"] = json.loads(d.get("attachments") or "[]")
        return d

    @staticmethod
    def _lane(lane) -> tuple[str, tuple]:
        """工作階段範圍（SPEC-1.1 §2）：None＝全部；"default"＝不屬於任何群的信；群號＝該群的信。"""
        if lane is None or lane == "":
            return "", ()
        if str(lane) == "default":
            return " AND m.source_room IS NULL", ()
        if not str(lane).isdigit():
            raise MboxError(400, "lane 只能是 default 或群號")
        return " AND m.source_room=?", (int(lane),)

    @staticmethod
    def _lane_key(role: str, lane) -> str:
        return role if lane in (None, "", "default") else f"{role}#room{int(lane)}"

    def unread_lanes(self, role: str) -> list:
        """有未讀的工作階段：["default", 3, 7 ...]。"""
        with self._lock:
            rows = self.db.execute("""SELECT DISTINCT m.source_room FROM deliveries d JOIN messages m ON m.id=d.message_id
                WHERE d.recipient=? AND d.state IN ('queued','delivered')""", (role,)).fetchall()
        return sorted({"default" if r[0] is None else int(r[0]) for r in rows}, key=lambda x: (x != "default", str(x)))

    def inbox(self, me: dict, unread_only: bool = True, limit: int = 50, mark: bool = True, lane=None) -> list[dict]:
        lw, la = self._lane(lane)
        q = ("SELECT m.*, d.state FROM deliveries d JOIN messages m ON m.id=d.message_id WHERE d.recipient=?"
             + (" AND d.state IN ('queued','delivered')" if unread_only else "") + lw + " ORDER BY m.id LIMIT ?")
        if not mark:
            with self._lock:
                return [self._msg(r) for r in self.db.execute(q, (me["id"], *la, int(limit)))]
        with self._tx() as db:
            rows = [self._msg(r) for r in db.execute(q, (me["id"], *la, int(limit)))]
            if mark and rows:
                db.executemany("UPDATE deliveries SET state='delivered',updated_at=? WHERE message_id=? AND recipient=? AND state='queued'",
                               [(time.time(), r["id"], me["id"]) for r in rows])
        return rows

    def unread_count(self, agent_id: str, lane=None) -> int:
        if lane is None:
            return self.db.execute("SELECT COUNT(*) FROM deliveries WHERE recipient=? AND state IN ('queued','delivered')",
                                   (agent_id,)).fetchone()[0]
        lw, la = self._lane(lane)
        with self._lock:
            return self.db.execute("""SELECT COUNT(*) FROM deliveries d JOIN messages m ON m.id=d.message_id
                WHERE d.recipient=? AND d.state IN ('queued','delivered')""" + lw, (agent_id, *la)).fetchone()[0]

    def wake_due(self, role: str, digest_minutes: float = 30, lane=None) -> bool:
        lw, la = self._lane(lane)
        with self._lock:
            row = self.db.execute("""SELECT COUNT(*) n,
                SUM(m.priority='must') must, MIN(m.created_at) oldest,
                COALESCE((SELECT last_wake_at FROM dispatch_state WHERE role=?),0) last_wake
                FROM deliveries d JOIN messages m ON m.id=d.message_id
                WHERE d.recipient=? AND d.state IN ('queued','delivered')""" + lw,
                (self._lane_key(role, lane), role, *la)).fetchone()
        return bool(row['n'] and (row['must'] or
                    time.time() - max(row['oldest'], row['last_wake']) >= digest_minutes * 60))

    def dispatch_messages(self, role: str, limit: int = 50, lane=None) -> list[dict]:
        lw, la = self._lane(lane)
        with self._lock:
            return [self._msg(r) for r in self.db.execute("""SELECT m.*,d.state
                FROM deliveries d JOIN messages m ON m.id=d.message_id
                WHERE d.recipient=? AND d.state IN ('queued','delivered')""" + lw + """
                ORDER BY (m.priority='must') DESC,m.id LIMIT ?""", (role, *la, limit))]

    def mark_dispatched(self, role: str, ids: list[int], delivered: bool = True, lane=None):
        now = time.time()
        with self._tx() as db:
            db.execute("""INSERT INTO dispatch_state(role,last_wake_at) VALUES(?,?)
                ON CONFLICT(role) DO UPDATE SET last_wake_at=excluded.last_wake_at""", (self._lane_key(role, lane),now))
            if delivered:
                db.executemany("""UPDATE deliveries SET state='delivered',updated_at=?
                    WHERE recipient=? AND message_id=? AND state='queued'""", [(now,role,mid) for mid in ids])

    def begin_run(self, role: str, ids: list[int] | None = None):
        with self._lock:
            if self.db.execute('SELECT 1 FROM runs WHERE role=? AND ended IS NULL', (role,)).fetchone():
                return
        with self._tx() as db:
            db.execute("""INSERT OR IGNORE INTO runs(role,started,message_ids)
                SELECT ?,?,? WHERE NOT EXISTS(SELECT 1 FROM runs WHERE role=? AND ended IS NULL)""",
                (role,time.time(),json.dumps(ids or []),role))

    def record_turn(self, tr: dict) -> int:
        """寫入一份已驗證的 TurnResult（契約 v1）。回傳 id。"""
        from mbox import contract
        contract.validate("turn_result", tr)
        with self._tx() as db:
            cur = db.execute("""INSERT INTO turn_results(role,driver,session_id,ok,exit,model_used,finished_at,result)
                VALUES(?,?,?,?,?,?,?,?)""", (tr["role"], tr["driver"], tr.get("session_id"), int(tr["ok"]),
                tr.get("exit"), tr.get("model_used"), tr.get("finished_at") or time.time(),
                json.dumps(tr, ensure_ascii=False)))
            return cur.lastrowid

    def get_session(self, role: str, driver: str) -> str | None:
        with self._lock:
            r = self.db.execute("SELECT session_id FROM sessions WHERE role=? AND driver=?", (role, driver)).fetchone()
        return r[0] if r else None

    def set_session(self, role: str, driver: str, sid: str):
        with self._tx() as db:
            db.execute("""INSERT INTO sessions(role,driver,session_id,updated_at) VALUES(?,?,?,?)
                ON CONFLICT(role,driver) DO UPDATE SET session_id=excluded.session_id, updated_at=excluded.updated_at""",
                       (role, driver, sid, time.time()))

    def clear_session(self, role: str, driver: str):
        with self._tx() as db:
            db.execute("DELETE FROM sessions WHERE role=? AND driver=?", (role, driver))

    def open_tasks_for(self, role: str) -> list[dict]:
        """角色手上未完成的任務：已認領（assignee）或派給它但還沒人認領（任務訊息的收件者）。"""
        with self._lock:
            rows = self.db.execute("""SELECT DISTINCT t.* FROM tasks t
                LEFT JOIN messages m ON m.task_id=t.id AND m.kind='task'
                WHERE t.state IN ('open','claimed','blocked')
                  AND (t.assignee=? OR (t.assignee IS NULL AND m.recipient=?))
                ORDER BY t.id""", (role, role)).fetchall()
        return [dict(r) for r in rows]

    def last_turns(self, role: str, n: int = 1, ok_only: bool = False) -> list[dict]:
        with self._lock:
            rows = self.db.execute("SELECT result FROM turn_results WHERE role=?" + (" AND ok=1" if ok_only else "")
                                   + " ORDER BY id DESC LIMIT ?", (role, n)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def finish_run(self, role: str, exit_code: int | None = None, error: str = ''):
        with self._tx() as db:
            row = db.execute('SELECT id,message_ids FROM runs WHERE role=? AND ended IS NULL', (role,)).fetchone()
            if not row:
                return
            ids = json.loads(row['message_ids'])
            handled = 0
            if ids:
                slots = ','.join('?' for _ in ids)
                handled = db.execute(f"""SELECT COUNT(*) FROM deliveries WHERE recipient=?
                    AND message_id IN ({slots}) AND state IN ('read','done','rejected')""", (role,*ids)).fetchone()[0]
            db.execute('UPDATE runs SET ended=?,exit=?,msgs_handled=?,error=? WHERE id=?',
                       (time.time(),exit_code,handled,error,row['id']))

    def record_run_error(self, role: str, error: str, occurred: float | None = None):
        now = time.time() if occurred is None else occurred
        with self._tx() as db:
            db.execute('INSERT INTO runs(role,started,ended,exit,error) VALUES(?,?,?,1,?)', (role,now,now,error))

    def recent_runs(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.db.execute('''SELECT id,role,started,ended,exit,msgs_handled,error
                FROM runs ORDER BY id DESC LIMIT ?''', (max(0,min(limit,500)),))]

    def _emit_alert(self, row: dict, body: str, table: str = "delivery_alerts",
                    condition: str = "1", params: tuple = ()) -> bool:
        """Short conditional write, shared by scans; no candidate scan in a write tx."""
        if table not in {"delivery_alerts", "must_alerts", "priority_alerts"}:
            raise ValueError("unknown alert table")
        now = time.time()
        with self._tx() as db:
            inserted = db.execute(f"""INSERT OR IGNORE INTO {table}(message_id,recipient,alerted_at)
                SELECT ?,?,? WHERE EXISTS(SELECT 1 FROM agents WHERE id='guardian')
                AND ({condition})""", (row['message_id'], row['recipient'], now, *params)).rowcount
            if not inserted:
                return False
            cur = db.execute("""INSERT INTO messages(sender,recipient,kind,body,created_at)
                VALUES('mbox-dispatcher','guardian','system',?,?)""", (body, now))
            mid = cur.lastrowid
            db.execute("UPDATE messages SET thread_id=? WHERE id=?", (mid, mid))
            db.execute("INSERT INTO deliveries(message_id,recipient,state,updated_at) VALUES(?, 'guardian', 'queued', ?)", (mid, now))
        return True

    def observe_role(self, role: str, state: str):
        """Pause/resume delivered-must clocks using observed adapter health.

        Candidate reads are outside write transactions; optimistic versions prevent
        another dispatcher observation from being overwritten. Busy/unknown time
        is excluded. Idle seconds from earlier idle intervals are retained.
        """
        now = time.time()
        with self._lock:
            rows = [dict(r) for r in self.db.execute("""SELECT d.message_id,d.recipient,d.updated_at,
                t.delivered_at,t.idle_since,t.idle_seconds,t.last_idle_at,t.version
                FROM deliveries d JOIN messages m ON m.id=d.message_id
                LEFT JOIN delivery_timers t ON t.message_id=d.message_id AND t.recipient=d.recipient
                WHERE d.recipient=? AND d.state='delivered' AND m.priority='must'
                AND m.sender!='mbox-dispatcher'""", (role,))]
        for row in rows:
            start, elapsed, last_idle = row['idle_since'], row['idle_seconds'] or 0, row['last_idle_at']
            if state == 'idle' and start is None:
                start = last_idle = now
            elif state != 'idle' and start is not None:
                elapsed += max(0, now-start)
                start = None
            if row['version'] is not None and (start,elapsed,last_idle) == (row['idle_since'],row['idle_seconds'],row['last_idle_at']):
                continue
            with self._tx() as db:
                if row['version'] is None:
                    db.execute("""INSERT OR IGNORE INTO delivery_timers
                        SELECT ?,?,?,?,?,?,? WHERE EXISTS(SELECT 1 FROM deliveries
                        WHERE message_id=? AND recipient=? AND state='delivered')""",
                        (row['message_id'],role,row['updated_at'],start,elapsed,last_idle,now,row['message_id'],role))
                else:
                    db.execute("""UPDATE delivery_timers SET idle_since=?,idle_seconds=?,last_idle_at=?,version=?
                        WHERE message_id=? AND recipient=? AND version=?""",
                        (start,elapsed,last_idle,now,row['message_id'],role,row['version']))

    def alert_must_deliveries(self, minutes: float = 5) -> int:
        """Read-only must scan; only idle time after delivery counts, busy time pauses."""
        if minutes <= 0:
            return 0
        now = time.time()
        with self._lock:
            rows = [dict(r) for r in self.db.execute("""SELECT d.message_id,d.recipient,
                m.priority_reason,m.source_room,m.source_messages,t.*
                FROM deliveries d JOIN messages m ON m.id=d.message_id
                JOIN delivery_timers t ON t.message_id=d.message_id AND t.recipient=d.recipient
                WHERE d.state='delivered' AND m.priority='must' AND m.sender!='mbox-dispatcher'
                AND t.idle_since IS NOT NULL AND t.idle_seconds+MAX(0,?-t.idle_since)>=?
                AND NOT EXISTS(SELECT 1 FROM must_alerts a WHERE a.message_id=d.message_id AND a.recipient=d.recipient)""",
                (now,minutes*60))]
        count = 0
        for row in rows:
            body = (f"must 未回告警：訊息 #{row['message_id']} 給 {row['recipient']}；"
                    f"群 #{row['source_room']}／編號 {row['source_messages']}；priority_reason={row['priority_reason']}；"
                    f"送達時間={row['delivered_at']}；角色轉 idle 時間={row['last_idle_at']}；告警時間={now}；"
                    f"累積 idle 秒數={row['idle_seconds']+max(0,now-row['idle_since']):g}")
            condition = """EXISTS(SELECT 1 FROM deliveries d JOIN messages m ON m.id=d.message_id
                JOIN delivery_timers t ON t.message_id=d.message_id AND t.recipient=d.recipient
                WHERE d.message_id=? AND d.recipient=? AND d.state='delivered' AND m.priority='must'
                AND t.idle_since IS NOT NULL AND t.version=? AND t.idle_seconds+MAX(0,?-t.idle_since)>=?)"""
            count += self._emit_alert(row, body, 'must_alerts', condition,
                (row['message_id'],row['recipient'],row['version'],now,minutes*60))
        return count

    def alert_priority_mismatches(self) -> int:
        """Detect owner messages stored as digest, outside any write transaction."""
        owner = "(m.source_owner=1 OR (a.rank='human' AND a.runtime!='system' AND m.sender!='mbox-dispatcher'))"
        with self._lock:
            rows = [dict(r) for r in self.db.execute(f"""SELECT m.id message_id,d.recipient,m.priority_reason
                FROM messages m JOIN deliveries d ON d.message_id=m.id
                LEFT JOIN agents a ON a.id=m.sender WHERE m.priority='digest' AND {owner}
                AND NOT EXISTS(SELECT 1 FROM priority_alerts p WHERE p.message_id=m.id AND p.recipient=d.recipient)""")]
        count = 0
        for row in rows:
            body = (f"通知漏判告警：owner 訊息 #{row['message_id']} 給 {row['recipient']} 被標為 digest；"
                    f"priority_reason={row['priority_reason']}")
            condition = f"""EXISTS(SELECT 1 FROM messages m LEFT JOIN agents a ON a.id=m.sender
                WHERE m.id=? AND m.priority='digest' AND {owner})"""
            count += self._emit_alert(row, body, 'priority_alerts', condition, (row['message_id'],))
        return count

    def alert_stale_deliveries(self, minutes: float = 30) -> int:
        """Read candidates outside a write transaction; recheck and deduplicate writes.

        Missing guardian leaves deliveries unchanged. Return newly emitted alert count.
        Dispatcher alerts are excluded; a nonpositive threshold disables the scan.
        """
        if minutes <= 0:
            return 0
        now = time.time()
        with self._lock:
            rows = [dict(r) for r in self.db.execute("""SELECT d.message_id,d.recipient,d.updated_at
                FROM deliveries d JOIN messages m ON m.id=d.message_id
                WHERE d.state='delivered' AND d.updated_at<? AND m.sender!='mbox-dispatcher'
                AND NOT EXISTS (SELECT 1 FROM delivery_alerts a
                    WHERE a.message_id=d.message_id AND a.recipient=d.recipient)""", (now - minutes * 60,))]
        count = 0
        for row in rows:
            body = (f"未 ack 老化告警：訊息 #{row['message_id']} 給 {row['recipient']} "
                    f"停在 delivered 已 {(now-row['updated_at']) / 60:.1f} 分鐘（門檻 {minutes:g} 分鐘）。請追蹤處理。")
            condition = "EXISTS(SELECT 1 FROM deliveries WHERE message_id=? AND recipient=? AND state='delivered' AND updated_at=?)"
            count += self._emit_alert(row, body, condition=condition,
                                      params=(row['message_id'], row['recipient'], row['updated_at']))
        return count

    def ack(self, me: dict, message_id: int, state: str = "read") -> dict:
        if state not in DELIV_ORDER[2:]:
            raise MboxError(400, "state 必須是 read|done|rejected")
        with self._tx() as db:
            r = db.execute("SELECT state FROM deliveries WHERE message_id=? AND recipient=?",
                           (message_id, me["id"])).fetchone()
            if not r:
                raise MboxError(404, "沒有這則給你的訊息")
            if DELIV_ORDER.index(state) < DELIV_ORDER.index(r["state"]):
                return {"message_id": message_id, "state": r["state"], "changed": False}
            db.execute("UPDATE deliveries SET state=?,updated_at=? WHERE message_id=? AND recipient=?",
                       (state, time.time(), message_id, me["id"]))
        return {"message_id": message_id, "state": state, "changed": True}

    def thread(self, me: dict, thread_id: int) -> list[dict]:
        rows = self.db.execute(
            "SELECT m.* FROM messages m WHERE m.thread_id=? AND (m.sender=? OR m.recipient IN (?, '@all')"
            " OR EXISTS(SELECT 1 FROM deliveries d WHERE d.message_id=m.id AND d.recipient=?)) ORDER BY m.id",
            (thread_id, me["id"], me["id"], me["id"])).fetchall()
        return [self._msg(r) for r in rows]

    def status(self, me: dict, message_id: int) -> list[dict]:
        m = self.db.execute("SELECT sender FROM messages WHERE id=?", (message_id,)).fetchone()
        if not m or (m["sender"] != me["id"] and me["rank"] != "human"):
            raise MboxError(404, "找不到或無權查看")
        return [dict(r) for r in self.db.execute(
            "SELECT recipient,state,updated_at FROM deliveries WHERE message_id=?", (message_id,))]

    # ---------- 任務 ----------
    def _expire(self, db):
        db.execute("UPDATE tasks SET state='open',assignee=NULL,lease_until=NULL,updated_at=?"
                   " WHERE state='claimed' AND lease_until<?", (time.time(), time.time()))

    def post_task(self, me: dict, title: str, spec: str = "", assignee: str | None = None,
                  mission_id: str | None = None, idem_key: str | None = None, source_room: int | None = None) -> dict:
        if not title.strip():
            raise MboxError(400, "title 不可空白")
        to = assignee or "@all"
        self._check_send(me, to, "task")
        if source_room is not None and to != "@all":
            with self._lock:
                if not self._room_ok(self.db, source_room, me, to):
                    raise MboxError(403, f"{to} 不在群 #{source_room}：群組相關工作只能派給群內成員。"
                                         f"需要 {to} 時，請在群裡提出邀請（由使用者或群成員在 AA Forum 按「邀請助手」）。")
        if idem_key:
            old = self.db.execute("SELECT task_id FROM messages WHERE sender=? AND idem_key=?",
                                  (me["id"], idem_key)).fetchone()
            if old:
                return {"task_id": old["task_id"], "duplicate": True}
        now = time.time()
        with self._tx() as db:
            tid = db.execute("INSERT INTO tasks(title,spec,owner,assignee,mission_id,created_at,updated_at)"
                             " VALUES(?,?,?,?,?,?,?)", (title, spec, me["id"], None, mission_id, now, now)).lastrowid
        body = f"[任務 #{tid}] {title}\n\n{spec}".rstrip()
        m = self.send(me, to, body, kind="task", mission_id=mission_id, idem_key=idem_key, task_id=tid,
                      source_room=source_room)
        return {"task_id": tid, "message_id": m["id"], "duplicate": False}

    def claim_task(self, me: dict, task_id: int) -> dict:
        """認領（open）、續租（自己 claimed），或解除 blocked（#40-4）。
        blocked 時原 assignee 或發起人可重新 claim；發起人 claim 等於接手（assignee 改為發起人），
        並發一則 kind=chat 訊息通知原 assignee。重新 claim 不清除 tasks.result_msg_id（blocked 原因那則訊息
        仍保留在信箱與串裡）；之後回報 done／blocked 時 result_msg_id 才會指向新的回報訊息。"""
        previous = None
        with self._tx() as db:
            self._expire(db)
            t = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not t:
                raise MboxError(404, "任務不存在")
            if t["state"] == "claimed" and t["assignee"] == me["id"]:
                pass  # 續租
            elif t["state"] == "blocked":
                # 解除 blocked（#40-4）：原 assignee 自己，或發起人（接手，assignee 改為發起人）。
                if me["id"] not in (t["assignee"], t["owner"]):
                    raise MboxError(403, f"任務 blocked（{t['assignee']}），只有 assignee 或發起人能重新認領")
            elif t["state"] != "open":
                raise MboxError(409, f"任務狀態為 {t['state']}（{t['assignee']}），無法認領")
            if t["state"] == "blocked" and t["assignee"] and t["assignee"] != me["id"]:
                previous = t["assignee"]
            lease = time.time() + self.lease_s
            db.execute("UPDATE tasks SET state='claimed',assignee=?,lease_until=?,updated_at=? WHERE id=?",
                       (me["id"], lease, time.time(), task_id))
        if previous:
            self.send(me, previous, f"[任務 #{task_id}] 已由發起人 {me['id']} 接手（原為你的 blocked 任務），你不用再處理。",
                      task_id=task_id, idem_key=f"task-{task_id}-takeover-{int(lease)}")
        return {"task_id": task_id, "state": "claimed", "lease_until": lease, "taken_over_from": previous}

    def update_task(self, me: dict, task_id: int, state: str, result: str | None = None,
                    attachments: list | None = None) -> dict:
        if state not in TASK_STATES - {"open", "claimed"}:
            raise MboxError(400, "state 必須是 done|blocked|cancelled")
        t = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not t:
            raise MboxError(404, "任務不存在")
        if state == "cancelled":
            if me["id"] != t["owner"]:
                raise MboxError(403, "只有發起人能取消")
        elif t["assignee"] != me["id"] or t["state"] != "claimed":
            raise MboxError(403, "必須先 claim 才能回報")
        mid = None
        if result or state != "cancelled":
            room = self.db.execute("SELECT source_room FROM messages WHERE task_id=? AND kind='task' ORDER BY id LIMIT 1",
                                   (task_id,)).fetchone()
            m = self.send(me, t["owner"], f"[任務 #{task_id} → {state}] {result or ''}".rstrip(), kind="result",
                          attachments=attachments, mission_id=t["mission_id"], task_id=task_id,
                          idem_key=f"task-{task_id}-{state}", source_room=room["source_room"] if room else None)
            mid = m["id"]
        with self._tx() as db:
            db.execute("UPDATE tasks SET state=?,result_msg_id=COALESCE(?,result_msg_id),lease_until=NULL,updated_at=? WHERE id=?",
                       (state, mid, time.time(), task_id))
        return {"task_id": task_id, "state": state, "result_msg_id": mid}

    def tasks(self, me: dict, state: str | None = None, mine: bool = False) -> list[dict]:
        with self._tx() as db:
            self._expire(db)
        q, a = "SELECT * FROM tasks WHERE 1=1", []
        if state:
            q += " AND state=?"; a.append(state)
        if mine:
            q += " AND (assignee=? OR owner=?)"; a += [me["id"], me["id"]]
        return [dict(r) for r in self.db.execute(q + " ORDER BY id", a)]

    # ---------- 心跳 ----------
    PROXY_AFTER_S = 300  # 角色自報超過 5 分鐘沒更新，dispatcher 才代報

    def proxy_heartbeat(self, role: str, status: str) -> bool:
        """dispatcher 代角色回報心跳（wake → busy；輪次結束 → idle），標 heartbeat_source='proxy'。
        last_heartbeat 此時代表「dispatcher 觀察到」，不是角色本人回報；名冊會顯示「(代)」。
        角色 PROXY_AFTER_S 秒內自報過（self_heartbeat）就不覆蓋：自報優先，代報最多落後角色自報 5 分鐘才接手。
        不動 context_pct。回是否有寫入。"""
        if status not in {"idle", "busy"}:
            raise MboxError(400, "代報 status 必須是 idle|busy")
        now = time.time()
        with self._tx() as db:
            cur = db.execute("""UPDATE agents SET status=?,last_heartbeat=?,heartbeat_source='proxy'
                WHERE id=? AND (self_heartbeat IS NULL OR self_heartbeat < ?)""",
                             (status, now, role, now - self.PROXY_AFTER_S))
        return cur.rowcount > 0

    def sync_runtimes(self, roles: dict) -> list[tuple[str, str, str | None]]:
        """以 roles.json 的 runtime 覆寫名冊 agents.runtime（roles.json 為唯一真實來源）。
        回 [(role, 舊, 新)]：有變動者；roles.json 沒寫 runtime 的角色回 (role, 現值, None)，名冊不動。
        system 身分（如 server）不在 roles.json，不動。"""
        changed = []
        with self._tx() as db:
            for role, cfg in roles.items():
                want = cfg.get("runtime")
                row = db.execute("SELECT runtime FROM agents WHERE id=?", (role,)).fetchone()
                if row and not want:
                    changed.append((role, row["runtime"], None))
                    continue
                if not want or not row or row["runtime"] == want:
                    continue
                db.execute("UPDATE agents SET runtime=? WHERE id=?", (want, role))
                changed.append((role, row["runtime"], want))
        return changed

    # ---------- 心跳 ----------
    def heartbeat(self, me: dict, status: str = "alive", context_pct: float | None = None) -> dict:
        if status not in {"alive", "idle", "busy", "unknown"}:
            raise MboxError(400, "status 必須是 alive|idle|busy|unknown")
        with self._tx() as db:
            now = time.time()
            db.execute("UPDATE agents SET status=?,context_pct=COALESCE(?,context_pct),last_heartbeat=?,"
                       "self_heartbeat=?,heartbeat_source='self' WHERE id=?",
                       (status, context_pct, now, now, me["id"]))
        return {"ok": True, "unread": self.unread_count(me["id"])}
