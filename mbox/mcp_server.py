"""mbox MCP server（stdio，JSON-RPC 2.0，純 stdlib）。把 broker 的 HTTP API 包成 MCP 工具。

設定（以 Claude Code 為例）：
  claude mcp add mbox -e MBOX_AGENT=lead -- python3 /path/aa-forum/mbox/mcp_server.py
身分由 MBOX_TOKEN 或 $MBOX_HOME/tokens/$MBOX_AGENT 決定，agent 無法自己指定 sender。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROTO = "2025-06-18"


def _home() -> Path:
    return Path(os.environ.get("MBOX_HOME") or Path(os.path.expanduser(os.environ.get("AAF_HOME") or str(Path(__file__).resolve().parent.parent))) / "var")


def _token():
    if os.environ.get("MBOX_TOKEN"):
        return os.environ["MBOX_TOKEN"]
    ag = os.environ.get("MBOX_AGENT")
    f = _home() / "tokens" / (ag or "")
    return f.read_text().strip() if ag and f.exists() else None


def _call(method, path, body=None):
    url = os.environ.get("MBOX_URL", "http://127.0.0.1:8775").rstrip("/") + path
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body, ensure_ascii=False).encode(),
                                 method=method, headers={"Content-Type": "application/json"})
    t = _token()
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
        raise RuntimeError(f"{e.code} {msg}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"連不到 broker：{e.reason}")


def S(props: dict, req=()):
    return {"type": "object", "properties": props, "required": list(req), "additionalProperties": False}


I, T = {"type": "integer"}, {"type": "string"}
TOOLS = {
    "send": ("送訊息給另一個角色（或 '@all'）。長文、多行都可以；大型產出請寫檔並用 attachments 附路徑。",
             S({"to": T, "body": T, "kind": {"enum": ["chat", "task", "result"]}, "reply_to": I,
                "attachments": {"type": "array", "items": T}, "idem_key": T, "mission_id": T}, ["to", "body"]),
             lambda a: _call("POST", "/v1/send", a)),
    "inbox": ("讀取自己的未讀訊息（會標成 delivered）。讀完記得 ack。",
              S({"all": {"type": "boolean"}, "limit": I}),
              lambda a: _call("GET", f"/v1/inbox?limit={a.get('limit', 50)}" + ("&all=1" if a.get("all") else ""))),
    "ack": ("回報訊息狀態：read（已讀）、done（已處理）、rejected（拒絕）。",
            S({"message_id": I, "state": {"enum": ["read", "done", "rejected"]}}, ["message_id"]),
            lambda a: _call("POST", "/v1/ack", a)),
    "thread": ("讀取整個討論串。", S({"thread_id": I}, ["thread_id"]),
               lambda a: _call("GET", f"/v1/thread/{a['thread_id']}")),
    "agents": ("列出所有角色、runtime、狀態和未讀數。", S({}), lambda a: _call("GET", "/v1/agents")),
    "post_task": ("發布任務。assignee 留空表示公開給所有人認領。",
                  S({"title": T, "spec": T, "assignee": T, "mission_id": T, "idem_key": T}, ["title"]),
                  lambda a: _call("POST", "/v1/tasks", a)),
    "list_tasks": ("列出任務。", S({"state": {"enum": ["open", "claimed", "done", "blocked", "cancelled"]},
                                   "mine": {"type": "boolean"}}),
                   lambda a: _call("GET", "/v1/tasks?" + (f"state={a['state']}&" if a.get("state") else "")
                                   + ("mine=1" if a.get("mine") else ""))),
    "claim_task": ("認領任務（取得租約，重複呼叫可續租）。", S({"task_id": I}, ["task_id"]),
                   lambda a: _call("POST", f"/v1/tasks/{a['task_id']}/claim")),
    "update_task": ("回報任務結果，會自動通知發起人。",
                    S({"task_id": I, "state": {"enum": ["done", "blocked", "cancelled"]}, "result": T,
                       "attachments": {"type": "array", "items": T}}, ["task_id", "state"]),
                    lambda a: _call("POST", f"/v1/tasks/{a.pop('task_id')}/update", a)),
    "heartbeat": ("回報自身狀態。", S({"status": {"enum": ["alive", "idle", "busy"]}, "context_pct": {"type": "number"}}),
                  lambda a: _call("POST", "/v1/heartbeat", a)),
}


def handle(msg: dict):
    m, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None  # notification
    if m == "initialize":
        res = {"protocolVersion": msg.get("params", {}).get("protocolVersion", PROTO),
               "capabilities": {"tools": {}}, "serverInfo": {"name": "mbox", "version": "0.1.0"}}
    elif m == "ping":
        res = {}
    elif m == "tools/list":
        res = {"tools": [{"name": n, "description": d, "inputSchema": s} for n, (d, s, _) in TOOLS.items()]}
    elif m == "tools/call":
        p = msg.get("params", {})
        tool = TOOLS.get(p.get("name"))
        if not tool:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "unknown tool"}}
        try:
            out = tool[2](dict(p.get("arguments") or {}))
            res = {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False, indent=1)}]}
        except Exception as e:
            res = {"content": [{"type": "text", "text": f"錯誤：{e}"}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {m}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": res}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            out = handle(json.loads(line))
        except json.JSONDecodeError:
            out = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        if out is not None:
            sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
