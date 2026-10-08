#!/usr/bin/env python3
"""ACP 實測用最小 client（S6）：stdio JSON-RPC。initialize → session/new → session/prompt，
記錄 session id、通知種類、權限請求、stop reason、用量欄位。權限請求一律回「拒絕／取消」（只做量測，不放行工具）。

用法：tools/acp_probe.py <名稱> <cwd> -- <agent 指令...>
輸出：一行 JSON 摘要到 stdout；完整訊息流到 --log 檔。
"""
import json
import os
import subprocess
import sys
import threading
import time
import queue

PROMPT = os.environ.get("ACP_PROBE_PROMPT") or "這是 ACP 連線測試。請先用工具讀取目前目錄的檔案清單（若有權限請求就照流程請求），然後只回覆 OK。"


def main():
    sep = sys.argv.index("--")
    name, cwd = sys.argv[1], os.path.abspath(sys.argv[2])
    log_path = next((a.split("=", 1)[1] for a in sys.argv[3:sep] if a.startswith("--log=")), f"acp-{name}.jsonl")
    cmd = sys.argv[sep + 1:]
    p = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, bufsize=1, start_new_session=True)
    log = open(log_path, "w", encoding="utf-8")
    inbox: "queue.Queue[dict]" = queue.Queue()
    stderr_tail = []

    def reader():
        for line in p.stdout:
            line = line.strip()
            if not line:
                continue
            log.write("<< " + line + "\n"); log.flush()
            try:
                inbox.put(json.loads(line))
            except ValueError:
                pass

    def err_reader():
        for line in p.stderr:
            stderr_tail.append(line.rstrip()[:300])
            del stderr_tail[:-20]

    threading.Thread(target=reader, daemon=True).start()
    threading.Thread(target=err_reader, daemon=True).start()
    nid = [0]
    summary = dict(agent=name, cmd=" ".join(cmd), updates={}, permission_requests=[], errors=[])

    def send(obj):
        s = json.dumps(obj, ensure_ascii=False)
        log.write(">> " + s + "\n"); log.flush()
        p.stdin.write(s + "\n"); p.stdin.flush()

    def request(method, params, timeout):
        nid[0] += 1
        my = nid[0]
        send({"jsonrpc": "2.0", "id": my, "method": method, "params": params})
        end = time.time() + timeout
        while time.time() < end:
            try:
                m = inbox.get(timeout=0.5)
            except queue.Empty:
                if p.poll() is not None:
                    raise RuntimeError(f"agent 已結束 exit={p.returncode}")
                continue
            if m.get("id") == my and "method" not in m:
                if "error" in m:
                    raise RuntimeError(f"{method} 錯誤：{json.dumps(m['error'], ensure_ascii=False)[:300]}")
                return m.get("result")
            handle(m)
        raise TimeoutError(f"{method} 逾時 {timeout}s")

    def handle(m):
        meth = m.get("method")
        if meth == "session/update":
            u = m["params"].get("update", {})
            kind = u.get("sessionUpdate", "?")
            summary["updates"][kind] = summary["updates"].get(kind, 0) + 1
            if kind in ("usage_update",) or "usage" in u:
                summary.setdefault("usage_updates", []).append(u)
            if kind == "agent_message_chunk":
                c = u.get("content", {})
                summary["text"] = summary.get("text", "") + (c.get("text") or "")
            if kind == "current_mode_update" or kind == "config_option_update":
                summary.setdefault("mode_updates", []).append(u)
        elif meth == "session/request_permission":
            opts = m["params"].get("options", [])
            tool = m["params"].get("toolCall", {})
            summary["permission_requests"].append(dict(title=tool.get("title"), kind=tool.get("kind"),
                                                       options=[o.get("kind") for o in opts]))
            reject = next((o for o in opts if o.get("kind") in ("reject_once", "reject_always")), None)
            outcome = {"outcome": "selected", "optionId": reject["optionId"]} if reject else {"outcome": "cancelled"}
            send({"jsonrpc": "2.0", "id": m["id"], "result": {"outcome": outcome}})
        elif "id" in m and meth:
            # fs/terminal 等 client 能力我們沒宣告，回錯誤
            send({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32601, "message": "probe client 不支援"}})

    t0 = time.time()
    try:
        init = request("initialize", {"protocolVersion": 1, "clientCapabilities": {
            "fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
            "clientInfo": {"name": "aaf-acp-probe", "version": "0.1"}}, 60)
        summary["initialize"] = dict(protocolVersion=init.get("protocolVersion"),
                                     agentInfo=init.get("agentInfo"),
                                     agentCapabilities=init.get("agentCapabilities"),
                                     authMethods=[a.get("id") for a in init.get("authMethods", [])])
        new = request("session/new", {"cwd": cwd, "mcpServers": []}, 90)
        summary["session_id"] = new.get("sessionId")
        summary["session_new_extra"] = {k: (v if k != "models" else {
            "current": (v or {}).get("currentModelId"),
            "available": len((v or {}).get("availableModels", []))}) for k, v in new.items() if k != "sessionId"}
        t1 = time.time()
        res = request("session/prompt", {"sessionId": new["sessionId"],
                                         "prompt": [{"type": "text", "text": PROMPT}]}, 240)
        summary["prompt_result"] = res
        summary["prompt_s"] = round(time.time() - t1, 1)
        time.sleep(1)
        while not inbox.empty():
            handle(inbox.get())
    except Exception as e:
        summary["errors"].append(f"{type(e).__name__}: {e}")
    finally:
        summary["total_s"] = round(time.time() - t0, 1)
        try:
            p.stdin.close()
            p.wait(timeout=5)
        except Exception:
            pass
        import signal
        try:
            os.killpg(p.pid, signal.SIGKILL)   # 清整個 group（codex-acp 原生子程序）
        except OSError:
            pass
        summary["stderr_tail"] = stderr_tail[-5:]
        summary["text"] = (summary.get("text") or "")[:300]
        print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
