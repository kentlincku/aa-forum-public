"""pi coding agent driver。"""
from __future__ import annotations

import os
import time

from drivers.base import Headless, system_prompt  # noqa: F401
import drivers as _base  # 經由套件取用，允許測試以 drivers.system_prompt 覆寫


class PiHeadless(Headless):
    """pi coding agent：pi -p --session-id <固定 id>，session 由 id 延續，不需解析輸出。"""
    name = "pi"
    bin_default = "pi"

    def interactive_cmd(self, model=None, prompt=None):
        a = [self.cfg.get("bin") or self.binary() or "pi"]
        a += ["--model", model] if model else []
        return a + (["--", prompt] if prompt else [])


    def parse_turn(self, out):
        return {"session_id": self.session(), "text": out.strip()[-2000:] or None}

    def argv(self, prompt):
        sid = self.session()
        if not sid:
            sid = f"{os.environ.get('AAF_TMUX_PREFIX', 'civ-')}{self.role}-{int(time.time())}"
            self.save_session(sid)
        a = [self.cfg.get("bin", "pi"), "-p", "--session-id", sid,
             "--append-system-prompt", _base.system_prompt(self.role, self.cfg)]
        if self.cfg.get("provider"):
            a += ["--provider", self.cfg["provider"]]
        if self.cfg.get("model"):
            a += ["--model", self.cfg["model"]]
        return a + ["--", prompt]

    # ---------------- S4：用量（只在 driver 內讀 pi 的 session 檔） ----------------
    label = "pi"
    process_names = ("pi",)

    def _session_path(self, sid):
        from pathlib import Path
        root = Path.home() / ".pi" / "agent" / "sessions"
        if not sid or not root.is_dir():
            return None
        hits = sorted(root.glob(f"*/*_{sid}.jsonl"), key=lambda p: p.stat().st_mtime)
        return hits[-1] if hits else None

    def usage(self, turn=None):
        """pi session 檔：最後一則 assistant 訊息的 provider／model／usage。pi 不記 context 上限 → 不算百分比。"""
        import datetime
        import json
        sid = (turn or {}).get("session_id") or self.session()
        path = self._session_path(sid)
        if not path:
            return dict(contract_version="1", source="", reason="找不到本 session 的 pi session 檔")
        last = None
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                m = d.get("message") or {}
                if m.get("role") == "assistant" and isinstance(m.get("usage"), dict):
                    last = (d, m)
        except OSError:
            return dict(contract_version="1", source="", reason="pi session 檔不可讀")
        if not last:
            return dict(contract_version="1", source="", reason="pi session 尚無 assistant 用量")
        d, m = last
        u = m["usage"]
        at = None
        try:
            at = datetime.datetime.fromisoformat(str(d.get("timestamp")).replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
        return dict(contract_version="1", model=m.get("model"), provider=m.get("provider"),
                    context_used=u.get("totalTokens"), context_limit=None, percent=None, measured_at=at,
                    tokens={"input": u.get("input"), "output": u.get("output"), "cache_read": u.get("cacheRead"),
                            "cache_write": u.get("cacheWrite"), "reasoning": u.get("reasoning")},
                    source=f"pi session 檔最後回應（{u.get('totalTokens')} tokens；pi 不記 context 上限，無百分比）")

    def room_usage(self, pid, screen):
        import re
        bar = re.search(r"(\d{1,3}(?:\.\d+)?)%/(\d+(?:\.\d+)?[kKmM]?)\b[^\n]*?(?:\(([^)\n]+)\)\s+(\S+))?\s*$", screen, re.M)
        if not bar:
            return None
        return dict(contract_version="1", percent=round(float(bar[1])), model=bar[4] or None,
                    measured_at=None, source="pi 狀態列；量測時間未知")

    def screen_activity(self, screen, tail):
        import re
        from drivers.base import generic_screen_activity
        act = generic_screen_activity(tail)
        if act in ("waiting", "retrying"):
            return act
        if re.search(r"^─+ [⠀-⣿] Working", tail, re.M):
            return "working"
        if re.search(r"%/\d", tail):
            return "idle"
        return act
