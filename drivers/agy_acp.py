#!/usr/bin/env python3
"""agy（Google Antigravity CLI）的 ACP 轉接（S8）：agy 沒有 ACP 模式，社群轉接器要另做 OAuth；
這支直接用 agy 自己的登入，把 ACP 呼叫轉成 `agy -p --output-format stream-json --conversation <id>`。

- session/new：配一個本地 session id；第一輪 agy 回的 conversation_id 記下來，之後每輪 --conversation 接續。
- session/load：從 state 檔找回 conversation_id。
- session/prompt：串流 text_delta → agent_message_chunk；usage → usage_update＋prompt 回應 usage。
- 權限：依部署決定自動允許 → --dangerously-skip-permissions。
- 模型：session/new 回 models（agy models）；session/set_model 記下，下一輪 --model。
- session/cancel：終止目前的 agy 子程序。
"""
import json
import os
import signal
import subprocess
import sys
import threading
import uuid
from pathlib import Path

STATE = Path(os.environ.get("AGY_ACP_STATE") or Path.home() / ".cache" / "aaf-agy-acp")
STATE.mkdir(parents=True, exist_ok=True)
AGY = os.environ.get("AGY_BIN") or "agy"
out_lock = threading.Lock()
sessions: dict = {}
running: dict = {}


def send(obj):
    with out_lock:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def save(sid):
    (STATE / f"{sid}.json").write_text(json.dumps(sessions[sid]))


def load(sid):
    f = STATE / f"{sid}.json"
    if f.exists():
        sessions[sid] = json.loads(f.read_text())
        return True
    return False


def models():
    try:
        r = subprocess.run([AGY, "models"], capture_output=True, text=True, timeout=60)
        out = []
        for line in r.stdout.splitlines():
            if "\t" in line:
                mid, name = line.split("\t", 1)
                out.append({"modelId": mid.strip(), "name": name.strip()})
        return out
    except Exception:
        return []


def model_info(sid):
    s = sessions[sid]
    return {"currentModelId": s.get("model") or "default",
            "availableModels": [{"modelId": "default", "name": "agy 預設"}] + s.get("_models", [])}


def prompt(mid, p):
    sid = p["sessionId"]
    if sid not in sessions and not load(sid):
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32002, "message": f"session {sid} not found"}})
        return
    s = sessions[sid]
    text = "\n".join(c.get("text", "") for c in p.get("prompt", []) if c.get("type") == "text")
    argv = [AGY, "-p", text, "--output-format", "stream-json", "--dangerously-skip-permissions"]
    if s.get("conversation"):
        argv += ["--conversation", s["conversation"]]
    if s.get("model") and s["model"] != "default":
        argv += ["--model", s["model"]]
    proc = subprocess.Popen(argv, cwd=s["cwd"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True)
    running[sid] = proc
    result, usage = None, {}
    for line in proc.stdout:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        kind = ev.get("event")
        if kind == "init" and ev.get("conversation_id") and not s.get("conversation"):
            s["conversation"] = ev["conversation_id"]
            save(sid)
        elif kind == "step_update":
            st = ev["step_update"]
            if st.get("text_delta") and st.get("step_type") == "agent_response":
                send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": {
                    "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": st["text_delta"]}}}})
            elif st.get("step_type") not in (None, "user_input", "agent_response") and st.get("state") == "ACTIVE":
                send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": {
                    "sessionUpdate": "tool_call", "toolCallId": f"{st.get('step_index')}", "title": st.get("step_type"),
                    "status": "in_progress"}}})
            if st.get("usage"):
                usage = st["usage"]
        elif kind == "result":
            result = ev["result"]
            usage = result.get("usage") or usage
            if result.get("conversation_id") and not s.get("conversation"):
                s["conversation"] = result["conversation_id"]
                save(sid)
    proc.wait()
    running.pop(sid, None)
    if usage.get("input_tokens") is not None:
        send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": {
            "sessionUpdate": "usage_update", "used": usage.get("total_tokens"), "size": None}}})
    tokens = {"inputTokens": usage.get("input_tokens"), "outputTokens": usage.get("output_tokens"),
              "thoughtTokens": usage.get("thinking_tokens"), "cachedReadTokens": usage.get("cache_read_tokens"),
              "totalTokens": usage.get("total_tokens")}
    if proc.returncode < 0 or (result or {}).get("status") == "CANCELLED":
        send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "cancelled", "usage": tokens}})
    elif not result or result.get("status") != "SUCCESS":
        err = (result or {}).get("error") or proc.stderr.read()[-300:] or f"agy exit {proc.returncode}"
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": err}})
    else:
        send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn", "usage": tokens}})


def main():
    for line in sys.stdin:
        try:
            m = json.loads(line)
        except ValueError:
            continue
        meth, mid, p = m.get("method"), m.get("id"), m.get("params") or {}
        if meth == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": 1, "agentInfo": {"name": "aaf-agy-acp", "title": "Antigravity CLI（agy）", "version": "1"},
                "agentCapabilities": {"loadSession": True, "promptCapabilities": {"image": False}}, "authMethods": []}})
        elif meth == "session/new":
            sid = str(uuid.uuid4())
            sessions[sid] = {"cwd": p.get("cwd") or os.getcwd(), "conversation": None, "model": None, "_models": models()}
            save(sid)
            send({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": sid, "models": model_info(sid)}})
        elif meth == "session/load":
            sid = p["sessionId"]
            if load(sid):
                sessions[sid]["cwd"] = p.get("cwd") or sessions[sid]["cwd"]
                sessions[sid]["_models"] = models()
                send({"jsonrpc": "2.0", "id": mid, "result": {"models": model_info(sid)}})
            else:
                send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32002, "message": f"session {sid} not found"}})
        elif meth == "session/set_model":
            sid = p["sessionId"]
            if sid in sessions:
                sessions[sid]["model"] = p.get("modelId")
                save(sid)
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif meth == "session/prompt":
            threading.Thread(target=prompt, args=(mid, p), daemon=True).start()
        elif meth == "session/cancel":
            proc = running.get(p.get("sessionId"))
            if proc:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except OSError:
                    pass
        elif mid is not None:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"不支援 {meth}"}})


if __name__ == "__main__":
    main()
