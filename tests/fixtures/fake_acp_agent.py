#!/usr/bin/env python3
"""測試用假 ACP agent：支援 initialize／session/new／session/load／session/set_model／session/prompt／session/cancel。
提示含 WRITE → 先發權限請求；含 DIE → 輪次中途結束程序；含 SLOW → 睡 2 秒。
"""
import json
import os
import sys
import time
import uuid

sessions = {}
model = os.environ.get("FAKE_ACP_MODEL", "fake-1")
nid = [1000]
pending = {}


def send(o):
    sys.stdout.write(json.dumps(o) + "\n")
    sys.stdout.flush()


def read():
    line = sys.stdin.readline()
    if not line:
        sys.exit(0)
    return json.loads(line)


def ask(method, params):
    nid[0] += 1
    send({"jsonrpc": "2.0", "id": nid[0], "method": method, "params": params})
    while True:
        m = read()
        if m.get("id") == nid[0] and "method" not in m:
            return m


for_log = open(os.environ.get("FAKE_ACP_LOG", os.devnull), "a")
while True:
    m = read()
    meth, mid, p = m.get("method"), m.get("id"), m.get("params", {})
    for_log.write(json.dumps(m) + "\n"); for_log.flush()
    if meth == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": 1, "agentCapabilities": {"loadSession": True},
                                                       "agentInfo": {"name": "fake", "version": "1"}}})
    elif meth == "session/new":
        model = os.environ.get("FAKE_ACP_MODEL", "fake-1")   # 新 session 一律用預設（同 hermes）
        sid = "S-" + uuid.uuid4().hex[:6]
        sessions[sid] = []
        send({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": sid, "models": {
            "currentModelId": model, "availableModels": [{"modelId": "fake-1"}, {"modelId": "prov:fake-2"}]}}})
    elif meth == "session/load":
        mf = os.environ.get("FAKE_ACP_MODEL_FILE")
        if mf and os.path.exists(mf):
            model = open(mf).read().strip()
        send({"jsonrpc": "2.0", "id": mid, "result": {"models": {"currentModelId": model,
                                                                 "availableModels": [{"modelId": "fake-1"}, {"modelId": "prov:fake-2"}]}}})
    elif meth == "session/set_model":
        model = p["modelId"]
        if os.environ.get("FAKE_ACP_MODEL_FILE"):
            open(os.environ["FAKE_ACP_MODEL_FILE"], "w").write(model)
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
    elif meth == "session/prompt":
        text = p["prompt"][0]["text"]
        sid = p["sessionId"]
        if os.environ.get("FAKE_ACP_STRICT") and sid not in sessions:
            send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "refusal"}})
            continue
        if "DIE" in text:
            os._exit(9)
        if "[補血]" in text and os.environ.get("FAKE_ACP_WRITE_HANDOFF") != "0":
            import re as _re
            path = _re.search(r"`([^`]+HANDOFF\.md)`", text)
            if path:
                open(path[1], "w").write("mission_state: active\nnext_action: 續做 X（見 result.md）\n"
                                         "continuation_contract: target lead\n<!-- archive.ready -->\n")
        if "SLOW" in text:
            time.sleep(2)
        if "WRITE" in text:
            r = ask("session/request_permission", {"sessionId": sid, "toolCall": {"title": "Write x", "kind": "edit"},
                                                   "options": [{"optionId": "a", "kind": "allow_once"},
                                                               {"optionId": "r", "kind": "reject_once"}]})
            outcome = r["result"]["outcome"]
            reply = "WROTE" if outcome.get("optionId") == "a" else "DENIED"
        else:
            reply = "OK"
        send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": {
            "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": f"{reply} sid={sid} model={model} first={text[:12]}"}}}})
        send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": {
            "sessionUpdate": "usage_update", "used": int(os.environ.get("FAKE_ACP_USED", "2500")), "size": 10000}}})
        send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn", "usage": {"inputTokens": 7, "outputTokens": 3}}})
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "nope"}})
