"""任意指令範本 driver：cfg.command 中的 {prompt} {role} {session} {instance} 會被替換（{instance}＝實例根 AAF_HOME）。"""
from __future__ import annotations

import shlex

from drivers.base import Headless, system_prompt  # noqa: F401
from drivers import base as _base


class CommandHeadless(Headless):
    name = "command"
    label = "command"

    def _fill(self, x: str, prompt: str = "") -> str:
        from mbox import paths
        return (x.replace("{prompt}", prompt).replace("{role}", self.role)
                 .replace("{session}", self.session() or "").replace("{instance}", str(paths.instance())))

    def binary(self):
        try:
            return self._fill(shlex.split(self.cfg["command"])[0])
        except (KeyError, IndexError, ValueError):
            return None

    def check(self, timeout=15):
        b = self.binary()
        import shutil, os
        ok = bool(b) and (os.access(b, os.X_OK) if os.sep in b else bool(shutil.which(b, path=self.env().get("PATH"))))
        return (True, b) if ok else (False, f"找不到指令 {b}")

    """任意 agent：cfg.command 是指令範本，{prompt} {role} {session} 會被替換。"""
    def argv(self, prompt):
        tpl = self.cfg["command"]
        return [self._fill(x, prompt) for x in shlex.split(tpl)]
