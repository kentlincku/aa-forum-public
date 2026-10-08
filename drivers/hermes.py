"""Hermes Agent driver（context 用量等內部資料只在這裡讀；S4 起提供 usage()）。"""
from __future__ import annotations

import json

from drivers.base import Headless, system_prompt  # noqa: F401
import drivers as _base  # 經由套件取用，允許測試以 drivers.system_prompt 覆寫


class HermesHeadless(Headless):
    """Hermes Agent：hermes chat -Q -q ... --format stream-json [--resume SID]"""
    name = "hermes"
    bin_default = "hermes"
    auth_cmd = ["hermes", "auth", "list"]

    def parse_auth(self, rc, out, err):
        provs = [l.split(" (")[0] for l in out.splitlines() if l and not l.startswith(" ") and "credentials" in l]
        if rc == 0 and provs:
            return True, "已設定憑證：" + "、".join(provs)
        return False, "沒有任何 provider 憑證（hermes auth add / hermes setup）"

    def interactive_cmd(self, model=None, prompt=None):
        a = [self.cfg.get("bin") or self.binary() or "hermes", "--yolo"]
        a += ["-m", model] if model else []
        return a + (["chat", "-q", prompt] if prompt else [])

    def argv(self, prompt):
        sid = self.session()
        full = prompt if sid else _base.system_prompt(self.role, self.cfg) + "\n\n" + prompt
        a = [self.cfg.get("bin", "hermes"), "chat", "-Q", "-q", full, "--format", "stream-json",
             "-t", self.cfg.get("toolsets", "terminal,file"), "--ignore-rules"]
        if self.cfg.get("yolo", True):
            a.append("--yolo")
        if self.cfg.get("model"):
            a += ["-m", self.cfg["model"]]
        if self.cfg.get("provider"):
            a += ["--provider", self.cfg["provider"]]
        return a + (["--resume", sid] if sid else [])

    def _result(self, out):
        for line in reversed(out.splitlines()):
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("type") == "result":
                return d
        return None

    def parse_session(self, out):
        d = self._result(out)
        return d.get("session_id") if d and d.get("exit_code") == 0 else None


    def parse_turn(self, out):
        d = self._result(out) or {}
        model = None
        for line in out.splitlines():
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get("type") == "system" and ev.get("subtype") == "init":
                model = ev.get("model")
                break
        tk = d.get("tokens") or {}
        return {"session_id": d.get("session_id"), "text": d.get("text"), "model_used": model,
                "duration_ms": d.get("duration_ms"),
                "tokens": {"input": tk.get("input"), "output": tk.get("output"),
                           "cache_read": tk.get("cache_read"), "cache_write": tk.get("cache_write")}}

    def parse_error(self, out):
        d = self._result(out)
        if not d:
            return "沒有 result 事件"
        return None if d.get("exit_code") == 0 else (d.get("text") or f"exit_code={d.get('exit_code')}")

    # ---------------- S4：用量（只在 driver 內讀 Hermes 內部檔案） ----------------
    label = "Hermes"
    process_names = ("hermes",)

    @classmethod
    def matches_process(cls, comm, args):
        return super().matches_process(comm, args) or hs_launcher(comm, args)

    def usage(self, turn=None):
        """headless：session（核心對照）→ state.db 用量；分母依「實際生效模型」。回 contract usage 或 {reason}。"""
        from drivers import hermes_state as hs
        sid = self.session()
        if not sid:
            return dict(contract_version="1", source="尚無 Hermes session", reason="尚無 Hermes session")
        eff = self._effective_model(sid, turn)
        roles_model = self.cfg["_roles_model"] if "_roles_model" in self.cfg else self.cfg.get("model")
        ctx = hs.hermes_session_context(sid, cfg_window=self.cfg.get("context_length"),
                                        cfg_model=roles_model, effective=eff)
        return _usage_from_ctx(ctx)

    def _effective_model(self, sid, turn):
        """產生目前用量的那一輪所用的 (model, provider, 層名)。
        1. 最近一輪 TurnResult（session 相符）的 model_used；override 不同時註明下一輪生效。
        2. 尚無本 session 的輪次：override → roles.json。 3. 都沒有 → None（state.db sessions.model）。"""
        from mbox.overrides import load_override
        over = load_override(self.role, self.home)
        roles_model, roles_provider = self.cfg.get("_roles_model"), self.cfg.get("_roles_provider")
        if "_roles_model" not in self.cfg:
            roles_model, roles_provider = self.cfg.get("model"), self.cfg.get("provider")
        init = turn.get("model_used") if turn and turn.get("session_id") == sid else None
        if init:
            layer = "最近一輪 init"
            if over.get("model") and over["model"] != init:
                layer += f'；override {over["model"]} 下一輪生效，目前仍為 {init}'
            provider = (over.get("provider") if over.get("model") == init
                        else roles_provider if roles_model == init else None)
            return init, provider, layer
        if over.get("model"):
            return over["model"], over.get("provider") or roles_provider, "AA Forum override（尚無本 session 的 init）"
        if roles_model:
            return roles_model, roles_provider, "roles.json"
        return None

    def room_usage(self, pid, screen):
        """tmux 房互動 Hermes：active_sessions → 壓縮鏈尾 → state.db；不行就讀畫面狀態列。"""
        from drivers import hermes_state as hs
        sid, why = hs.hermes_session_for_pid(pid)
        depth = 0
        if sid:
            sid, depth = hs.hermes_session_tip(sid)
        bar = hs.hermes_status_bar(screen)
        eff = (bar["model"], None, "終端狀態列") if bar else None
        ctx = hs.hermes_session_context(sid, effective=eff) if sid else dict(reason=why)
        if "percent" in ctx:
            chain = f"（經壓縮鏈 {depth} 層）" if depth else ""
            u = _usage_from_ctx(ctx)
            u["source"] = f"tmux 房 Hermes session {sid}{chain}；{ctx['source']}"
            return u
        if bar:
            return dict(contract_version="1", model=bar["model"], percent=bar["percent"], measured_at=None,
                        source=f'終端狀態列 ~{bar["used"]}/{bar["window"]}；量測時間未知（state.db 不可用：{ctx["reason"]}）')
        return dict(contract_version="1", source="", reason=f'state.db 不可用：{ctx["reason"]}；畫面也無 Hermes 狀態列')


def hs_launcher(comm, args):
    from drivers.hermes_state import is_hermes_launcher
    return is_hermes_launcher(comm, args)


def _usage_from_ctx(ctx):
    if "percent" not in ctx:
        return dict(contract_version="1", source="", reason=ctx["reason"])
    return dict(contract_version="1", model=ctx["model"], provider=ctx.get("provider"),
                context_used=ctx.get("used"), context_limit=ctx.get("window"),
                percent=ctx["percent"], measured_at=ctx.get("measured_at"), source=ctx["source"])


def _hermes_model_catalog(cls):
    from drivers.hermes_catalog import model_catalog
    return model_catalog()


HermesHeadless.model_catalog = classmethod(_hermes_model_catalog)
