"""ACP 常駐主機（S7）：每個角色一支，常駐持有 agent 的 ACP 連線與 session。

    python -m drivers.acp_host <state_dir> <sock> <cwd> <model|-> -- <agent ACP 指令...>

- 啟動：initialize → 有舊 session 且 agent 支援 loadSession 就 session/load，否則 session/new；需要時 session/set_model。
- 控制（unix socket，一行 JSON 一個請求）：
    {"op":"prompt","text":...,"system":...}  → 背景開一輪；忙碌時回 busy
    {"op":"status"}                           → {busy, session_id, model, pid, started_at, turn_started_at}
    {"op":"cancel"}                           → session/cancel
    {"op":"shutdown"}                         → 結束 agent 與自己
- 每輪結束寫 state_dir/acp_turn.json（driver 轉成 TurnResult）；usage_update 寫 acp_usage.json；
  串流文字附加到 acp_stream.log（AA Forum 日後可即時顯示）。
- 權限請求：一律自動 allow（優先 allow_once），每一筆記進該輪的 permissions 與 acp_host.log（部署決定：自動允許）。
"""
from __future__ import annotations

import json
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path


_PROVIDER_FAIL = ("rejected this request", "is not accessible via", "unsupported_api_for_model",
                  "model not found", "model_not_found", "does not exist", "not supported", "HTTP 400", "HTTP 401",
                  "HTTP 403", "HTTP 404", "invalid model")


def _provider_error(text: str, usage: dict) -> str | None:
    """agent 把 provider 錯誤當成「回覆」吐出來（stopReason 仍是 end_turn、沒有任何 token 用量）→ 判為失敗。"""
    if usage.get("outputTokens") or usage.get("inputTokens"):
        return None
    low = (text or "").lower()
    if any(k.lower() in low for k in _PROVIDER_FAIL):
        line = next((x for x in (text or "").splitlines() if "provider said" in x.lower() or "http" in x.lower()), "")
        return "模型或 provider 拒絕：" + (line.strip() or text.strip()[:200])
    return None


class AgentGone(RuntimeError):
    pass


class Host:
    def __init__(self, state_dir: Path, sock: str, cwd: str, model: str | None, cmd: list[str]):
        self.state_dir, self.sock_path, self.cwd, self.want_model, self.cmd = state_dir, sock, cwd, model, cmd
        self.log = (state_dir / "acp_host.log").open("a", encoding="utf-8")
        self.inbox: "queue.Queue[dict]" = queue.Queue()
        self.pending: dict[int, "queue.Queue[dict]"] = {}
        self.nid = 0
        self.lock = threading.Lock()
        self.turn_lock = threading.Lock()
        self.busy = False
        self.turn: dict | None = None
        self.turn_started_at = None
        self.session_id = None
        self.model = None
        self.caps: dict = {}
        self.started_at = time.time()
        self.alive = True
        self.first_prompt_pending = False
        self.resumed = False

    # ---------- JSON-RPC ----------
    def _log(self, msg: str):
        self.log.write(time.strftime("%F %T ") + msg + "\n")
        self.log.flush()

    def _send(self, obj: dict):
        line = json.dumps(obj, ensure_ascii=False)
        with self.lock:
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()

    def request(self, method: str, params: dict, timeout: float | None = 120):
        with self.lock:
            self.nid += 1
            my = self.nid
            q: "queue.Queue[dict]" = queue.Queue()
            self.pending[my] = q
        self._send({"jsonrpc": "2.0", "id": my, "method": method, "params": params})
        try:
            m = q.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(f"{method} 逾時")
        finally:
            self.pending.pop(my, None)
        if m.get("_gone"):
            raise AgentGone("agent 已結束")
        if "error" in m:
            raise RuntimeError(f"{method}：{json.dumps(m['error'], ensure_ascii=False)[:400]}")
        return m.get("result")

    def _reader(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if "method" in m:
                try:
                    self._handle(m)
                except Exception as e:  # 單一通知壞掉不能讓主機掛掉
                    self._log(f"處理 {m.get('method')} 失敗：{e}")
            elif m.get("id") in self.pending:
                self.pending[m["id"]].put(m)
        self.alive = False
        for q in list(self.pending.values()):
            q.put({"_gone": True})
        self._log(f"agent 結束 exit={self.proc.wait()}")

    def _stderr(self):
        with (self.state_dir / "acp_agent.stderr.log").open("a", encoding="utf-8") as f:
            for line in self.proc.stderr:
                f.write(line)
                f.flush()

    def _handle(self, m: dict):
        meth = m["method"]
        if meth == "session/update":
            u = m["params"].get("update", {})
            kind = u.get("sessionUpdate")
            t = self.turn
            if kind == "agent_message_chunk":
                text = (u.get("content") or {}).get("text") or ""
                if t is not None:
                    t["text"] += text
                with (self.state_dir / "acp_stream.log").open("a", encoding="utf-8") as f:
                    f.write(text)
            elif kind == "usage_update":
                meta = u.get("_meta") or {}
                if meta.get("_claude/model") and not self.want_model:
                    self.model = meta["_claude/model"]          # 只回報在 _meta 的 agent（claude）
                usage = dict(used=u.get("used"), size=u.get("size"), at=time.time(),
                             model=self.model, session_id=self.session_id)
                (self.state_dir / "acp_usage.json").write_text(json.dumps(usage, ensure_ascii=False))
            elif kind in ("tool_call",) and t is not None:
                t["tool_calls"] += 1
        elif meth == "session/request_permission":
            opts = m["params"].get("options", [])
            tool = m["params"].get("toolCall") or {}
            pick = (next((o for o in opts if o.get("kind") == "allow_once"), None)
                    or next((o for o in opts if o.get("kind", "").startswith("allow")), None))
            rec = dict(title=tool.get("title"), kind=tool.get("kind"), decision=pick.get("kind") if pick else "cancelled")
            if self.turn is not None:
                self.turn["permissions"].append(rec)
            self._log("權限自動允許：" + json.dumps(rec, ensure_ascii=False))
            outcome = {"outcome": "selected", "optionId": pick["optionId"]} if pick else {"outcome": "cancelled"}
            self._send({"jsonrpc": "2.0", "id": m["id"], "result": {"outcome": outcome}})
        elif "id" in m:
            self._send({"jsonrpc": "2.0", "id": m["id"],
                        "error": {"code": -32601, "message": "aaf ACP host 不提供此 client 能力"}})

    # ---------- 生命週期 ----------
    def boot(self):
        self.proc = subprocess.Popen(self.cmd, cwd=self.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, bufsize=1, start_new_session=True)
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._stderr, daemon=True).start()
        init = self.request("initialize", {"protocolVersion": 1, "clientCapabilities": {
            "fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
            "clientInfo": {"name": "aaf-acp-host", "version": "1"}}, timeout=180)
        self.caps = init.get("agentCapabilities") or {}
        old_f = self.state_dir / "acp_session"
        old = old_f.read_text().strip() if old_f.exists() else None
        res = None
        if old and self.caps.get("loadSession"):
            try:
                res = self.request("session/load", {"sessionId": old, "cwd": self.cwd, "mcpServers": []}, timeout=180) or {}
                self.session_id = old
                self.resumed = True
                self._log(f"接回 session {old}")
            except Exception as e:
                self._log(f"session/load 失敗，改開新 session：{e}")
        if not self.session_id:
            res = self.request("session/new", {"cwd": self.cwd, "mcpServers": []}, timeout=180) or {}
            self.session_id = res["sessionId"]
            self.first_prompt_pending = True
            self._log(f"新 session {self.session_id}")
        old_f.write_text(self.session_id)
        models = (res or {}).get("models") or {}
        self.model = models.get("currentModelId")
        # 另一種回報方式（pi-acp）：configOptions 裡 category=model 的選單
        self.model_option = next((o for o in (res or {}).get("configOptions") or []
                                  if o.get("category") == "model" or o.get("id") == "model"), None)
        if not self.model and self.model_option:
            self.model = self.model_option.get("currentValue")
        # 沒指定模型（例：AA Forum override 剛清除）→ 回到 agent 的預設模型；session/load 會沿用上次 set_model 的模型，
        # 所以預設值要從「新 session」那次記下來（acp_default_model）。
        dflt_f = self.state_dir / "acp_default_model"
        if self.first_prompt_pending and self.model:
            dflt_f.write_text(self.model)
        if not self.want_model and not dflt_f.exists() and models and self.caps.get("loadSession"):
            # 舊 session（修正前建立）沒記預設：開一個暫時 session 問 agent 的預設模型（只問一次）
            try:
                probe = self.request("session/new", {"cwd": self.cwd, "mcpServers": []}, timeout=180) or {}
                d = (probe.get("models") or {}).get("currentModelId")
                if d:
                    dflt_f.write_text(d)
                    self._log(f"記下預設模型 {d}（暫時 session {probe.get('sessionId')}）")
            except Exception as e:
                self._log(f"查預設模型失敗：{e}")
        if not self.want_model and dflt_f.exists() and self.model and self.model != dflt_f.read_text().strip():
            self.want_model = dflt_f.read_text().strip()
            self._log(f"未指定模型，回到預設 {self.want_model}")
        if self.want_model:
            self._set_model(models)

    def _set_model(self, models: dict):
        """依 agent 回報的方式切模型（config option 優先，見 acp_catalog.model_switch）。失敗時記錄並沿用目前模型。"""
        from drivers.acp_catalog import model_switch
        want = self.want_model
        opt = getattr(self, "model_option", None)
        # currentValue／currentModelId 用 host 自己追蹤的 self.model（session/load 後可能已切過）
        session = {"models": dict(models or {}, currentModelId=self.model) if models else {},
                   "configOptions": [dict(opt, category="model", currentValue=self.model)] if opt else []}
        try:
            sw = model_switch(session, want)
        except ValueError as e:
            self._log(f"模型 {want} 不在 agent 清單（沿用 {self.model}）：{e}")
            return
        if sw is None:
            if not models and not opt:
                self.model = want        # 不回報清單（codex-acp：模型由啟動參數決定）→ 直接記設定值
            return
        method, params, target = sw
        if target == self.model:
            return
        try:
            self.request(method, {"sessionId": self.session_id, **params}, timeout=60)
            self.model = target
            self._log(f"模型切到 {target}（{method}）")
        except Exception as e:
            self._log(f"{method} {target} 失敗（沿用 {self.model}）：{e}")

    def _prompt(self, prompt: str):
        return self.request("session/prompt", {"sessionId": self.session_id,
                                               "prompt": [{"type": "text", "text": prompt}]}, timeout=None)

    def _new_session(self):
        res = self.request("session/new", {"cwd": self.cwd, "mcpServers": []}, timeout=180) or {}
        self.session_id = res["sessionId"]
        (self.state_dir / "acp_session").write_text(self.session_id)
        self._log(f"新 session {self.session_id}")
        if self.want_model:
            self._set_model(res.get("models") or {})

    def run_turn(self, text: str, system: str | None):
        t0 = time.time()
        self.turn = dict(text="", permissions=[], tool_calls=0)
        prompt = text
        if self.first_prompt_pending and system:
            # ACP 沒有 system prompt 欄位：新 session 的第一輪把人設放在最前面
            prompt = system + "\n\n---\n\n" + text
        result, err = None, None
        try:
            result = self._prompt(prompt)
            if (result or {}).get("stopReason") == "refusal" and self.turn["text"] == "" and self.turn["tool_calls"] == 0:
                # 接回的 session 其實不存在（例：hermes 新 session 在第一輪前就重啟，未存檔）→ agent 直接 refusal。
                # 開新 session、帶人設重送一次。
                self._log(f"session {self.session_id} 拒絕（無輸出），改開新 session 重送")
                self._new_session()
                prompt = (system + "\n\n---\n\n" + text) if system else text
                result = self._prompt(prompt)
            elif (getattr(self, "resumed", False) and _provider_error(self.turn["text"], (result or {}).get("usage") or {})
                  and self.turn["tool_calls"] == 0):
                # 接回的舊 session 第一輪就被 provider 拒絕（實測：hermes 從 DB 還原 session 後切 custom provider，
                # 沿用了舊 base_url → 401/400）。開新 session、帶人設重送一次；新 session 正常就不會再走這裡。
                self._log(f"session {self.session_id} 接回後 provider 拒絕（{_provider_error(self.turn['text'], {})}），改開新 session 重送")
                self.turn = dict(text="", permissions=[], tool_calls=0)
                self._new_session()
                prompt = (system + "\n\n---\n\n" + text) if system else text
                result = self._prompt(prompt)
            self.resumed = False
            self.first_prompt_pending = False
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
        usage = (result or {}).get("usage") or {}
        stop = (result or {}).get("stopReason")
        out = dict(session_id=self.session_id, model_used=self.model, text=self.turn["text"][-2000:] or None,
                   stop_reason=stop, permissions=self.turn["permissions"], tool_calls=self.turn["tool_calls"],
                   duration_ms=int((time.time() - t0) * 1000), started_at=t0, finished_at=time.time(),
                   tokens=dict(input=usage.get("inputTokens"), output=usage.get("outputTokens"),
                               cache_read=usage.get("cachedReadTokens"), cache_write=usage.get("cachedWriteTokens"),
                               reasoning=usage.get("thoughtTokens")),
                   error=err or (None if stop in (None, "end_turn") else f"stopReason={stop}")
                   or _provider_error(self.turn["text"], usage))
        tmp = self.state_dir / "acp_turn.json.tmp"
        tmp.write_text(json.dumps(out, ensure_ascii=False))
        tmp.replace(self.state_dir / "acp_turn.json")
        self.turn = None
        self.busy = False
        self.turn_started_at = None
        self.turn_lock.release()

    # ---------- 控制 socket ----------
    def serve(self):
        try:
            os.unlink(self.sock_path)
        except FileNotFoundError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self.sock_path)
        os.chmod(self.sock_path, 0o600)
        srv.listen(8)
        srv.settimeout(1)
        while self.alive:
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            with conn:
                try:
                    req = json.loads(conn.makefile().readline() or "{}")
                    resp = self.dispatch(req)
                except Exception as e:
                    resp = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                try:
                    conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode())
                except OSError:
                    pass
                if req.get("op") == "shutdown":
                    break
        srv.close()
        try:
            os.unlink(self.sock_path)
        except FileNotFoundError:
            pass
        self.stop_agent()

    def dispatch(self, req: dict) -> dict:
        op = req.get("op")
        if op == "status":
            return dict(ok=True, busy=self.busy, session_id=self.session_id, model=self.model, pid=os.getpid(),
                        agent_pid=self.proc.pid, alive=self.alive, started_at=self.started_at,
                        turn_started_at=self.turn_started_at, want_model=self.want_model)
        if op == "prompt":
            if not self.alive:
                return dict(ok=False, error="agent 已結束")
            if not self.turn_lock.acquire(blocking=False):
                return dict(ok=False, busy=True, error="上一輪還在跑")
            self.busy = True
            self.turn_started_at = time.time()
            (self.state_dir / "acp_turn.json").unlink(missing_ok=True)
            threading.Thread(target=self.run_turn, args=(req["text"], req.get("system")), daemon=True).start()
            return dict(ok=True, started=True, session_id=self.session_id)
        if op == "new_session":
            # 補血的「clear」：開新 session（人設在下一輪隨提示送出）；忙碌時拒絕
            if self.busy:
                return dict(ok=False, busy=True, error="輪次進行中，不能換 session")
            old = self.session_id
            self._new_session()
            self.first_prompt_pending = True
            (self.state_dir / "acp_usage.json").unlink(missing_ok=True)
            self._log(f"補血：session {old} → {self.session_id}")
            return dict(ok=True, old=old, session_id=self.session_id)
        if op == "cancel":
            if self.busy:
                self._send({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": self.session_id}})
            return dict(ok=True)
        if op == "shutdown":
            return dict(ok=True)
        return dict(ok=False, error=f"未知 op {op}")

    def stop_agent(self):
        """關閉 agent：先關 stdin 讓它自行結束，再對整個 process group 送 TERM／KILL。
        （codex-acp 的 node 包裝程序會先退出、留下原生子程序，所以一律清整個 group。）"""
        proc = getattr(self, "proc", None)
        if proc is None:
            return
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                return
            except OSError:
                return
            time.sleep(1)


def main(argv: list[str]):
    sep = argv.index("--")
    state_dir, sock, cwd, model = Path(argv[0]), argv[1], argv[2], argv[3]
    host = Host(state_dir, sock, cwd, None if model == "-" else model, argv[sep + 1:])
    pid_f = state_dir / "acp_host.pid"
    pid_f.write_text(str(os.getpid()))
    signal.signal(signal.SIGTERM, lambda *a: (_ for _ in ()).throw(SystemExit(0)))
    try:
        host.boot()
        (state_dir / "acp_host.ready").write_text(json.dumps(dict(session_id=host.session_id, model=host.model)))
        host.serve()
    except BaseException as e:
        host._log(f"主機結束：{type(e).__name__}: {e}")
        (state_dir / "acp_host.error").write_text(f"{type(e).__name__}: {e}")
        host.stop_agent()
    finally:
        try:
            os.unlink(sock)
        except (FileNotFoundError, OSError):
            pass
        (state_dir / "acp_host.ready").unlink(missing_ok=True)
        if pid_f.exists() and pid_f.read_text().strip() == str(os.getpid()):
            pid_f.unlink(missing_ok=True)


if __name__ == "__main__":
    main(sys.argv[1:])
