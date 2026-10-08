"""mbox CLI：任何能跑 shell 的 agent 都能用。純 stdlib，Python 3.9 也可以執行。

身分來源（依序）：--token、MBOX_TOKEN、$MBOX_HOME/tokens/<MBOX_AGENT>
broker URL：--url、MBOX_URL，預設 http://127.0.0.1:8775

  mbox send <to> [<body>|-]  [--kind chat|task|result] [--reply N] [--attach PATH]... [--idem KEY]
  mbox inbox [--all] [--peek] [--json]
  mbox ack <id> [read|done|rejected]
  mbox thread <id> | status <id> | agents | me
  mbox task post <title> [--spec TEXT|-] [--to AGENT]
  mbox task list [--state S] [--mine]
  mbox task claim <id>
  mbox task done|blocked|cancel <id> [<result>|-] [--attach PATH]...
  mbox hb [alive|idle|busy] [--ctx PCT]
  mbox unread                        # 只印未讀數（給 hook 用，不改狀態）
  mbox admin add <id> [--runtime R] [--rank lead|worker|human]   # 直接寫 DB、存 token
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def home() -> Path:
    return Path(os.environ.get("MBOX_HOME") or Path(os.path.expanduser(os.environ.get("AAF_HOME") or str(Path(__file__).resolve().parent.parent))) / "var")


def token(a) -> str | None:
    if a.token:
        return a.token
    if os.environ.get("MBOX_TOKEN"):
        return os.environ["MBOX_TOKEN"]
    ag = os.environ.get("MBOX_AGENT")
    if ag:
        f = home() / "tokens" / ag
        if f.exists():
            return f.read_text().strip()
    return None


def call(a, method: str, path: str, body: dict | None = None):
    url = (a.url or os.environ.get("MBOX_URL") or "http://127.0.0.1:8775").rstrip("/") + path
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    t = token(a)
    if t:
        req.add_header("Authorization", "Bearer " + t)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error")
        except Exception:
            msg = str(e)
        sys.exit(f"mbox: {e.code} {msg}")
    except urllib.error.URLError as e:
        sys.exit(f"mbox: 連不到 broker（{url}）：{e.reason}")


def text_arg(v: str | None) -> str:
    return sys.stdin.read() if v in (None, "-") else v


def fmt_ts(t) -> str:
    return time.strftime("%m-%d %H:%M:%S", time.localtime(t)) if t else "-"


def show_msgs(rows, as_json: bool):
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return
    if not rows:
        print("（沒有訊息）")
    for m in rows:
        extra = []
        if m.get("reply_to"):
            extra.append(f"回覆 #{m['reply_to']}")
        if m.get("task_id"):
            extra.append(f"任務 #{m['task_id']}")
        if m.get("state"):
            extra.append(m["state"])
        print(f"── 信件 #{m['id']} [{m['kind']}] {m['sender']} → {m['recipient']}  串 {m['thread_id']}  {fmt_ts(m['created_at'])}"
              + (f"  ({', '.join(extra)})" if extra else ""))
        print(m["body"])
        for p in m.get("attachments") or []:
            print(f"  📎 {p}")
    if rows:
        print(f"\n讀完請 `mbox ack <id> read`；處理完 `mbox ack <id> done`。回覆用 `mbox send <對象> --reply <id> ...`")


def current_room():
    """群組範圍：MBOX_ROOM 優先；否則讀 dispatcher 為本輪寫下的 current_room（本輪工作來自哪一群）。"""
    if os.environ.get("MBOX_ROOM"):
        return os.environ["MBOX_ROOM"] if os.environ["MBOX_ROOM"] != "none" else None
    me, home = os.environ.get("MBOX_AGENT"), os.environ.get("MBOX_HOME")
    if me and home:
        try:
            return open(os.path.join(home, "roles", me, "current_room")).read().strip() or None
        except OSError:
            return None
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mbox")
    ap.add_argument("--url")
    ap.add_argument("--token")
    sp = ap.add_subparsers(dest="cmd", required=True)

    s = sp.add_parser("send"); s.add_argument("to"); s.add_argument("body", nargs="?")
    s.add_argument("--kind", default="chat"); s.add_argument("--reply", type=int)
    s.add_argument("--attach", action="append"); s.add_argument("--idem"); s.add_argument("--mission")
    s = sp.add_parser("inbox"); s.add_argument("--all", action="store_true"); s.add_argument("--peek", action="store_true")
    s.add_argument("--json", action="store_true"); s.add_argument("--limit", type=int, default=50)
    s = sp.add_parser("ack"); s.add_argument("id", type=int); s.add_argument("state", nargs="?", default="read")
    s = sp.add_parser("thread"); s.add_argument("id", type=int)
    s = sp.add_parser("status"); s.add_argument("id", type=int)
    sp.add_parser("agents"); sp.add_parser("me"); sp.add_parser("unread")
    s = sp.add_parser("hb"); s.add_argument("status", nargs="?", default="alive"); s.add_argument("--ctx", type=float)

    t = sp.add_parser("task").add_subparsers(dest="tcmd", required=True)
    x = t.add_parser("post"); x.add_argument("title"); x.add_argument("--spec"); x.add_argument("--to")
    x.add_argument("--idem"); x.add_argument("--mission")
    x = t.add_parser("list"); x.add_argument("--state"); x.add_argument("--mine", action="store_true")
    x = t.add_parser("claim"); x.add_argument("id", type=int)
    for n in ("done", "blocked", "cancel"):
        x = t.add_parser(n); x.add_argument("id", type=int); x.add_argument("result", nargs="?", default="")
        x.add_argument("--attach", action="append")

    ad = sp.add_parser("admin").add_subparsers(dest="acmd", required=True)
    x = ad.add_parser("add"); x.add_argument("id"); x.add_argument("--runtime", default="generic")
    x.add_argument("--rank", default="worker"); x.add_argument("--db")

    a = ap.parse_args(argv)
    c = a.cmd
    if c == "send":
        r = call(a, "POST", "/v1/send", {"to": a.to, "body": text_arg(a.body), "kind": a.kind, "reply_to": a.reply,
                                         "attachments": a.attach, "idem_key": a.idem, "mission_id": a.mission,
                                         "source_room": current_room()})
        print(f"已送出 #{r['id']}（串 {r['thread_id']}）" + ("〔重複，未重送〕" if r.get("duplicate") else ""))
    elif c == "inbox":
        q = f"/v1/inbox?limit={a.limit}" + ("&all=1" if a.all else "") + ("&peek=1" if a.peek else "")
        if os.environ.get("MBOX_LANE"):          # 每群獨立工作階段：只看本群（或不屬於任何群）的信
            q += "&lane=" + os.environ["MBOX_LANE"]
        show_msgs(call(a, "GET", q), a.json)
    elif c == "ack":
        r = call(a, "POST", "/v1/ack", {"message_id": a.id, "state": a.state}); print(f"#{a.id} → {r['state']}")
    elif c == "thread":
        show_msgs(call(a, "GET", f"/v1/thread/{a.id}"), False)
    elif c == "status":
        for d in call(a, "GET", f"/v1/status/{a.id}"):
            print(f"{d['recipient']:<12} {d['state']:<10} {fmt_ts(d['updated_at'])}")
    elif c == "agents":
        for g in call(a, "GET", "/v1/agents"):
            proxy = "(代)" if g.get("heartbeat_source") == "proxy" else ""
            print(f"{g['id']:<12} {g['runtime']:<10} {g['rank']:<7} {g['status']:<8} 未讀 {g['unread']:<3} 心跳 {fmt_ts(g['last_heartbeat'])}{proxy}")
    elif c == "me":
        print(json.dumps(call(a, "GET", "/v1/me"), ensure_ascii=False))
    elif c == "unread":
        me = call(a, "GET", "/v1/me")
        print(next((g["unread"] for g in call(a, "GET", "/v1/agents") if g["id"] == me["id"]), 0))
    elif c == "hb":
        r = call(a, "POST", "/v1/heartbeat", {"status": a.status, "context_pct": a.ctx}); print(f"ok，未讀 {r['unread']}")
    elif c == "task":
        if a.tcmd == "post":
            r = call(a, "POST", "/v1/tasks", {"title": a.title, "spec": text_arg(a.spec) if a.spec == "-" else (a.spec or ""),
                                              "assignee": a.to, "idem_key": a.idem, "mission_id": a.mission,
                                              "source_room": current_room()})
            print(f"任務 #{r['task_id']} 已發布" + ("〔重複〕" if r.get("duplicate") else ""))
        elif a.tcmd == "list":
            q = "/v1/tasks?" + (f"state={a.state}&" if a.state else "") + ("mine=1" if a.mine else "")
            for k in call(a, "GET", q):
                print(f"#{k['id']:<4} {k['state']:<9} {k['owner']:>8} → {k['assignee'] or '-':<8} {k['title']}")
        elif a.tcmd == "claim":
            r = call(a, "POST", f"/v1/tasks/{a.id}/claim"); print(f"已認領 #{a.id}，租約到 {fmt_ts(r['lease_until'])}")
        else:
            st = {"cancel": "cancelled"}.get(a.tcmd, a.tcmd)
            res = text_arg(a.result) if a.result == "-" else a.result
            r = call(a, "POST", f"/v1/tasks/{a.id}/update", {"state": st, "result": res, "attachments": a.attach})
            print(f"任務 #{a.id} → {r['state']}")
    elif c == "admin":
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from mbox.core import Store
        h = home(); (h / "tokens").mkdir(mode=0o700, parents=True, exist_ok=True)
        tok = Store(a.db or h / "mbox.sqlite3").add_agent(a.id, a.runtime, a.rank)
        f = h / "tokens" / a.id
        f.write_text(tok); f.chmod(0o600)
        print(f"已建立 {a.id}（{a.runtime}/{a.rank}），token 存在 {f}")


if __name__ == "__main__":
    main()
