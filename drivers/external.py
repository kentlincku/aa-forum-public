"""外部程式 driver（S6）：任何語言寫的可執行檔，以 stdin/stdout 交換契約 JSON。核心不需知道它是哪個 agent。

roles.json：{"driver": "external", "exec": "/path/to/my-driver", "workdir": "..."}

協定（契約 v1；子指令放在 argv[1]）：
  <exec> manifest            → stdout：Manifest JSON
  <exec> check               → exit 0＝可用；stdout 第一行為說明
  <exec> wake   < WakeRequest → stdout 最後一行：TurnResult JSON（role/driver 由核心補正）
  <exec> usage  < {"turn":…} → stdout：Usage JSON（選配；manifest capabilities 含 usage_report 才呼叫）
stderr 一律進 turns.log。逾時、非 JSON、契約不符都記成 ok=false 的 TurnResult，不讓 dispatcher 掛掉。
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess

from drivers.base import Headless, system_prompt
from mbox import contract


class ExternalHeadless(Headless):
    name = "external"
    label = "外部 driver"

    def exe(self) -> str | None:
        e = self.cfg.get("exec")
        return os.path.expanduser(e) if e else None

    def binary(self):
        e = self.exe()
        return e if e and os.access(e, os.X_OK) else None

    def _call(self, sub: str, payload: dict | None = None, timeout: float = 30) -> subprocess.CompletedProcess:
        return subprocess.run([self.exe(), sub], input=json.dumps(payload, ensure_ascii=False) if payload is not None else "",
                              capture_output=True, text=True, timeout=timeout, cwd=self.workdir, env=self.env())

    def describe(self) -> dict:
        """外部 driver 的 manifest 由外部程式自報（失敗則回最小 manifest）。"""
        try:
            r = self._call("manifest", timeout=10)
            m = json.loads(r.stdout)
            return contract.validate("manifest", m)
        except Exception:
            return contract.validate("manifest", self.manifest())

    def check(self, timeout=15):
        if not self.binary():
            return False, f"外部 driver 不可執行：{self.exe()}"
        try:
            r = self._call("check", timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, "check 逾時"
        first = (r.stdout.strip().splitlines() or [""])[0]
        return r.returncode == 0, first or f"exit {r.returncode}"

    def wake_request(self, prompt: str) -> dict:
        req = {"contract_version": contract.VERSION, "role": self.role, "prompt": prompt,
               "system_prompt": system_prompt(self.role, self.cfg), "session_id": self.session(),
               "model": self.cfg.get("model"), "provider": self.cfg.get("provider"),
               "cwd": str(self.workdir), "timeout_s": self.cfg.get("timeout_s")}
        return contract.validate("wake_request", req)

    def argv(self, prompt: str) -> list[str]:
        """沿用 Headless.wake 的背景執行／鎖／退避；以 sh 把 WakeRequest 檔餵進 stdin。"""
        req = self.state_dir / "wake_request.json"
        req.write_text(json.dumps(self.wake_request(prompt), ensure_ascii=False))
        return ["/bin/sh", "-c", f'exec {shlex.quote(self.exe() or "false")} wake < {shlex.quote(str(req))}']

    def _last_json(self, out: str) -> dict | None:
        for line in reversed(out.strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except ValueError:
                    return None
        return None

    def parse_session(self, out):
        d = self._last_json(out) or {}
        return d.get("session_id")

    def parse_turn(self, out):
        d = self._last_json(out)
        if d is None:
            return {"error": "外部 driver 未輸出 TurnResult JSON", "ok": False}
        keep = ("session_id", "text", "error", "model_used", "tokens", "duration_ms")
        kw = {k: d[k] for k in keep if k in d}
        if "ok" in d:
            kw["ok"] = bool(d["ok"])
        return kw

    def usage(self, turn=None):
        if "usage_report" not in self.describe().get("capabilities", []):
            return None
        try:
            r = self._call("usage", {"turn": turn}, timeout=10)
            u = json.loads(r.stdout)
            u.setdefault("contract_version", contract.VERSION)
            return contract.validate("usage", u)
        except Exception as e:
            return dict(contract_version="1", source="", reason=f"外部 driver usage 失敗：{type(e).__name__}")
