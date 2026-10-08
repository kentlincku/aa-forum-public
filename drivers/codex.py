"""Codex CLI driver。"""
from __future__ import annotations

import json

from drivers.base import Headless, system_prompt  # noqa: F401
import drivers as _base  # 經由套件取用，允許測試以 drivers.system_prompt 覆寫


class CodexHeadless(Headless):
    """Codex CLI：codex exec --json … [resume SID]"""
    name = "codex"
    bin_default = "codex"
    auth_cmd = ["codex", "login", "status"]

    def parse_auth(self, rc, out, err):
        text = (out + err).strip()
        if rc == 0 and "Logged in" in text:
            return True, text.splitlines()[0][:80]
        return False, "未登入（codex login）"

    def interactive_cmd(self, model=None, prompt=None):
        a = [self.cfg.get("bin") or self.binary() or "codex", "--dangerously-bypass-approvals-and-sandbox"]
        a += ["--model", model] if model else []
        return a + ([prompt] if prompt else [])

    def argv(self, prompt):
        b = self.cfg.get("bin", "codex")
        sid = self.session()
        full = prompt if sid else _base.system_prompt(self.role, self.cfg) + "\n\n" + prompt
        opts = ["--json", "--skip-git-repo-check", "-s", self.cfg.get("sandbox", "workspace-write"),
                "-c", 'shell_environment_policy.inherit="all"',
                "-c", "sandbox_workspace_write.network_access=true"]
        if self.cfg.get("model"):
            opts += ["-m", self.cfg["model"]]
        return [b, "exec", *opts, "resume", sid, full] if sid else [b, "exec", *opts, full]

    def parse_session(self, out):
        for line in out.splitlines():
            try:
                ev = json.loads(line)
            except Exception:
                continue
            sid = ev.get("thread_id") or ev.get("session_id") or (ev.get("msg") or {}).get("session_id")
            if sid:
                return sid
        return None


    def parse_turn(self, out):
        sid, text, err, tok = None, None, None, {}
        for line in out.splitlines():
            try:
                ev = json.loads(line)
            except Exception:
                continue
            typ = ev.get("type")
            sid = sid or ev.get("thread_id") or ev.get("session_id")
            item = ev.get("item") or {}
            if typ == "item.completed" and item.get("type") == "agent_message" and item.get("text"):
                text = item["text"]
            elif typ == "turn.completed":
                u = ev.get("usage") or {}
                tok = {"input": u.get("input_tokens"), "output": u.get("output_tokens"),
                       "cache_read": u.get("cached_input_tokens"),
                       "cache_write": u.get("cache_write_input_tokens"),
                       "reasoning": u.get("reasoning_output_tokens")}
            elif typ in ("error", "turn.failed"):
                err = ev.get("message") or (ev.get("error") or {}).get("message") or typ
        r = {"session_id": sid, "text": text, "tokens": tok}
        if err:
            r["error"] = err
        return r

    # ---------------- S4：用量（只在 driver 內讀 Codex 內部檔案） ----------------
    label = "Codex"
    process_names = ("codex",)

    @staticmethod
    def _sessions_root():
        from pathlib import Path
        return (Path.home() / ".codex" / "sessions").resolve()

    def _rollout_for(self, sid):
        """headless：依 thread id 找 rollout 檔（檔名結尾為 <thread_id>.jsonl）；取最新一個。"""
        root = self._sessions_root()
        if not sid or not root.is_dir():
            return None
        hits = sorted(root.glob(f"*/*/*/rollout-*-{sid}.jsonl"), key=lambda p: p.stat().st_mtime)
        return hits[-1] if hits else None

    @staticmethod
    def rollout_usage(path, now=None):
        """讀 rollout 尾端 2MB：最後一筆 token_count 與 turn_context 的 model。回 usage 或 None。"""
        import datetime
        import os
        import time as _t
        now = now or _t.time()
        with path.open("rb") as stream:
            stream.seek(max(0, os.fstat(stream.fileno()).st_size - 2_000_000))
            lines = stream.read().decode("utf-8", errors="replace").splitlines()
        model = None
        for line in reversed(lines):
            if '"turn_context"' in line:
                try:
                    model = (json.loads(line).get("payload") or {}).get("model")
                except ValueError:
                    pass
                if model:
                    break
        for line in reversed(lines):
            if '"token_count"' not in line:
                continue
            try:
                record = json.loads(line)
                if not isinstance(record, dict) or record.get("type") != "event_msg":
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict) or payload.get("type") != "token_count":
                    continue
                info = payload.get("info")
                if not isinstance(info, dict) or not isinstance(info.get("last_token_usage"), dict):
                    continue
                used = info["last_token_usage"]["total_tokens"]
                total = info["model_context_window"]
                stamp = record["timestamp"]
                if not isinstance(stamp, str):
                    continue
                parsed = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    continue
                at = parsed.timestamp()
                if not (type(used) is int and type(total) is int and 0 <= used <= total and total > 0):
                    continue
                if not 0 < at <= now + 60:
                    continue
            except (ValueError, KeyError, TypeError, OverflowError):
                continue
            return dict(contract_version="1", model=model, context_used=used, context_limit=total,
                        percent=round(100 * used / total), measured_at=at,
                        source="Codex rollout token_count；資料 " + parsed.isoformat(timespec="seconds"))
        return None

    def usage(self, turn=None):
        sid = (turn or {}).get("session_id") or self.session()
        path = self._rollout_for(sid)
        if not path:
            return dict(contract_version="1", source="", reason="找不到本 session 的 Codex rollout 檔")
        try:
            u = self.rollout_usage(path)
        except OSError:
            u = None
        return u or dict(contract_version="1", source="", reason="rollout 尚無 token_count")

    def room_usage(self, pid, screen):
        """tmux 房：只讀本程序開啟的 rollout（lsof）；帳號最新檔可能屬於別房。退而讀畫面頁尾。"""
        import re
        import subprocess
        from pathlib import Path
        try:
            root = self._sessions_root()
            paths = set()
            lsof = subprocess.run(["lsof", "-Fn", "-p", str(pid)], capture_output=True, text=True, timeout=3)
            for line in lsof.stdout.splitlines():
                if not line.startswith("n/"):
                    continue
                try:
                    path = Path(line[1:]).resolve(strict=True)
                    if path.is_relative_to(root) and path.name.startswith("rollout-") and path.suffix == ".jsonl":
                        paths.add(path)
                except OSError:
                    continue
            if len(paths) == 1:
                u = self.rollout_usage(paths.pop())
                if u:
                    u["source"] = "本房前景程序開啟的 rollout token_count；" + u["source"].split("；", 1)[-1]
                    return u
        except OSError:
            pass
        m = (re.search(r"\b(\d{1,3})% context left\b", screen) or re.search(r"\bContext (\d{1,3})% left\b", screen))
        if m and 0 <= int(m[1]) <= 100:
            return dict(contract_version="1", percent=100 - int(m[1]), measured_at=None, source="終端狀態列；量測時間未知")
        return None

    def screen_model(self, screen):
        import re
        if re.search(r"^─{5}[^\n]*\n❯", screen, re.M):
            return None
        prompts = list(re.finditer(r"^›[^\n]*\n", screen, re.M))
        if not prompts:
            return None
        footer = screen[prompts[-1].end():]
        m = re.search(
            r"\n[ \t]*\n[ \t]*(gpt-[a-zA-Z0-9._-]+|o[134](?:-[a-zA-Z0-9._-]+)?)[ \t]+(?:minimal|low|medium|high|xhigh|max|ultra)[ \t]*[·•][^\n]+(?:\n[ \t]*)*\Z",
            "\n" + footer)
        return m[1] if m else None

    @classmethod
    def account_quota(cls):
        from drivers.account_quota import engine_quota
        return engine_quota("Codex")
