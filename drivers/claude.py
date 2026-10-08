"""Claude Code driver。"""
from __future__ import annotations

import json

from drivers.base import Headless, system_prompt  # noqa: F401
import drivers as _base  # 經由套件取用，允許測試以 drivers.system_prompt 覆寫


class ClaudeHeadless(Headless):
    """Claude Code：claude -p … --output-format json [--resume SID]"""
    name = "claude"
    bin_default = "claude"
    auth_cmd = ["claude", "auth", "status"]

    def parse_auth(self, rc, out, err):
        import json as _json
        try:
            d = _json.loads(out)
        except ValueError:
            return (False, f"未登入（exit {rc}）") if rc else (None, "無法解析 claude auth status")
        if d.get("loggedIn") is True:
            return True, f"已登入（{d.get('authMethod', '?')}）"   # 不印 email 等帳號細節
        return False, "未登入（claude auth login）"

    def interactive_cmd(self, model=None, prompt=None):
        a = [self.cfg.get("bin") or self.binary() or "claude", "--dangerously-skip-permissions"]
        a += ["--model", model] if model else []
        return a + ([prompt] if prompt else [])

    def argv(self, prompt):
        a = [self.cfg.get("bin", "claude"), "-p", prompt, "--output-format", "json",
             "--append-system-prompt", _base.system_prompt(self.role, self.cfg)]
        a += ["--allowedTools", self.cfg.get("allowed_tools", "Bash(mbox:*) Bash(aaf-chat:*) Read Write Edit Glob Grep")]
        if self.cfg.get("permission_mode"):
            a += ["--permission-mode", self.cfg["permission_mode"]]
        if self.cfg.get("model"):
            a += ["--model", self.cfg["model"]]
        sid = self.session()
        return a + (["--resume", sid] if sid else [])

    def parse_session(self, out):
        try:
            d = json.loads(out.strip().splitlines()[-1])
        except Exception:
            return None
        # 失敗的輪次不記 session（避免 resume 一個壞掉的 session）
        return None if d.get("is_error") else d.get("session_id")

    def parse_error(self, out):
        try:
            d = json.loads(out.strip().splitlines()[-1])
            return d.get("result") if d.get("is_error") else None
        except Exception:
            return "無法解析輸出"


    def parse_turn(self, out):
        try:
            d = json.loads(out.strip().splitlines()[-1])
        except Exception:
            return {"text": out.strip()[-2000:] or None}
        u = d.get("usage") or {}
        models = list((d.get("modelUsage") or {}).keys())
        return {"session_id": d.get("session_id"), "text": d.get("result"),
                "model_used": models[0] if models else None,
                "duration_ms": d.get("duration_ms"),
                "tokens": {"input": u.get("input_tokens"), "output": u.get("output_tokens"),
                           "cache_read": u.get("cache_read_input_tokens"),
                           "cache_write": u.get("cache_creation_input_tokens")}}

    # ---------------- S4：互動畫面（Claude 無可讀的本機用量檔） ----------------
    label = "Claude"
    process_names = ("claude",)

    def usage(self, turn=None):
        if turn and turn.get("tokens", {}).get("input") is not None:
            return dict(contract_version="1", model=turn.get("model_used"), source="最近一輪 TurnResult（無 context 上限，無百分比）",
                        tokens=turn["tokens"], percent=None)
        return dict(contract_version="1", source="", reason="Claude headless 不提供 context 用量")

    def screen_model(self, screen):
        import re
        composers = list(re.finditer(r"^─{5}[^\n]*\n(?=❯)", screen, re.M))
        if not composers:
            return None
        after = screen[composers[-1].end():]
        borders = list(re.finditer(r"^─{5}[^\n]*", after, re.M))
        footer = after[borders[-1].end():] if borders else ""
        m = re.search(r"^\s*\[([^\]\n]{1,100})\][^\n]*\n[ \t]*上下文[ \t]+", footer, re.M)
        return m[1].strip() if m else None

    def room_usage(self, pid, screen):
        import re
        m = re.search(r"^\s*上下文\s+(?:\[[^\]\n]*\]\s*)?(\d{1,3})%\s*(?:←[^\n]*)?$", screen, re.M)
        if m and 0 <= int(m[1]) <= 100:
            return dict(contract_version="1", percent=int(m[1]), measured_at=None, source="終端狀態列；量測時間未知")
        return None

    def screen_activity(self, screen, tail):
        """只看輸入框之上的近期輸出＋頁尾；輸入框內的草稿不當證據。"""
        import re
        from drivers.base import generic_screen_activity
        activity_tail = tail
        composers = list(re.finditer(r"^─{5}[^\n]*\n(?=❯)", screen, re.M))
        if composers:
            activity_tail = ""
        if len(composers) == 1:
            composer = composers[-1]
            borders = list(re.finditer(r"^─{5}[^\n]*", screen[composer.end():], re.M))
            border = borders[-1] if borders else None
            footer = screen[composer.end() + border.end():] if border else ""
            if border and re.search(r"^\s*上下文\s+", footer, re.M):
                recent = "\n".join(screen[:composer.start()].splitlines()[-18:])
                draft = screen[composer.end():composer.end() + border.start()]
                prompt = "❯ " if draft.strip() == "❯" else "❯ queued"
                activity_tail = recent + "\n" + prompt + "\n" + footer
        act = generic_screen_activity(activity_tail)
        if act:
            return act
        if (re.search(r"· done [^\n]+", activity_tail) and re.search(r"^❯\s*$", activity_tail, re.M)
                and not re.search(r"\b\d+\s+(?:shells?|background tasks?)\b", activity_tail)):
            return "idle"
        return None

    @classmethod
    def account_quota(cls):
        from drivers.account_quota import engine_quota
        return engine_quota("Claude")
