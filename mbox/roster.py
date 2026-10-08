"""roles.json 熱重載（SPEC-simplify-deploy D3、D8、D9）。

dispatcher 每輪呼叫 RosterWatcher.poll()：
  - 內容雜湊沒變：不做事。
  - JSON 壞掉：保留舊設定，告警一次（修好後告警「已恢復」）。
  - 既有角色改 driver／模型／參數：下一輪直接生效（正在跑的那一輪不中斷）。
  - 新增角色（D8）：驗證 → 發 token、加入 broker 名冊 → 開 work/<role>/ → AA Forum 加入所有使用中的群。
      角色有 persona_file 但檔案不存在、或 driver 不認得 → 拒絕新增（不進名單）並告警。
  - 刪除角色（D9）：停止叫醒；正在跑的那一輪讓它跑完並收尾；信箱、token、work、歷史全保留；
      AA Forum 標為停用並公告；該角色未完成的任務通知 lead 改派。
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import sys as _sys
_sys.path.insert(0, str(ROOT)) if str(ROOT) not in _sys.path else None
from mbox import paths as _paths  # noqa: E402
INST = _paths.instance()


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def validate_role(role: str, cfg: dict) -> str | None:
    """回傳拒絕原因；可接受回 None。"""
    import drivers
    if not isinstance(cfg, dict):
        return "設定必須是物件"
    if not role.replace("-", "").replace("_", "").isalnum():
        return "角色 id 只能用英數、-、_"
    name = drivers.resolve(cfg)
    if name not in drivers.DRIVERS:
        return f"不認得的 driver {name}"
    pf = cfg.get("persona_file")
    if pf:
        p = Path(os.path.expanduser(pf))
        p = _paths.resolve(p)
        if not p.is_file():
            return f"角色 skill 檔不存在：{pf}"
    return None


class RosterWatcher:
    def __init__(self, path: Path, roles: dict, home: Path, alert=None, log=print, zk_sync=None):
        self.path = Path(path)
        self.home = home
        self.roles = dict(roles)
        self.rejected: dict[str, str] = {}
        self.retiring: dict[str, dict] = {}      # 已刪除但上一輪還在跑：role → 舊 cfg
        self.alert = alert or (lambda key, level, text: None)
        self.log = log
        self.zk_sync = zk_sync if zk_sync is not None else zk_roster_sync
        self.bad_json = False
        try:
            self.hash = _digest(self.path.read_bytes())
        except OSError:
            self.hash = None

    def poll(self, store) -> bool:
        """有變動並套用回 True。"""
        try:
            raw = self.path.read_bytes()
        except OSError as e:
            return self._broken(f"讀不到 {self.path}：{type(e).__name__}")
        h = _digest(raw)
        if h == self.hash and not self.bad_json:
            return False
        try:
            new = json.loads(raw)["roles"]
            if not isinstance(new, dict):
                raise ValueError("roles 不是物件")
        except (ValueError, KeyError, TypeError) as e:
            self.hash = h
            return self._broken(f"roles.json 格式錯誤，沿用舊設定：{e}")
        if self.bad_json:
            self.bad_json = False
            self.alert("roles.json", "recovered", "roles.json 已恢復可讀")
        self.hash = h
        self._apply(new, store)
        return True

    def _broken(self, why: str) -> bool:
        if not self.bad_json:
            self.bad_json = True
            self.log(why)
            self.alert("roles.json", "problem", why)
        return False

    def _apply(self, new: dict, store):
        from mbox import dispatcher
        old = self.roles
        accepted: dict[str, dict] = {}
        rejected_now: dict[str, str] = {}
        for role, cfg in new.items():
            why = validate_role(role, cfg)
            if why and role not in old:
                rejected_now[role] = why
                continue
            if why:  # 既有角色改壞：保留舊設定
                rejected_now[role] = why + "（沿用舊設定）"
                accepted[role] = old[role]
                continue
            accepted[role] = cfg
        for role, why in rejected_now.items():
            if self.rejected.get(role) != why:
                self.log(f"roles.json：拒絕 {role}：{why}")
                self.alert(f"role:{role}", "problem", f"roles.json 角色 {role} 未套用：{why}")
        for role in set(self.rejected) - set(rejected_now):
            if role in new:
                self.alert(f"role:{role}", "recovered", f"roles.json 角色 {role} 已可套用")
            else:
                self.alert(f"role:{role}", "recovered", f"roles.json 已移除未套用的角色 {role}")
        self.rejected = rejected_now

        added = [r for r in accepted if r not in old]
        removed = [r for r in old if r not in accepted]
        changed = [r for r in accepted if r in old and accepted[r] != old[r]]

        if added:
            dispatcher.setup({r: accepted[r] for r in added}, store, quiet=True)
            for r in added:
                if not dispatcher.drivers.is_manual(accepted[r]):
                    wd = Path(os.path.expanduser(accepted[r].get("workdir", str(INST / "work" / r))))
                    wd.mkdir(parents=True, exist_ok=True)
            self.log(f"roles.json：新增 {', '.join(added)}")
        for r in removed:
            self.retiring[r] = old[r]
            open_tasks = store.open_tasks_for(r)
            lead = next((x for x, c in accepted.items() if c.get("rank") == "lead"), None)
            if lead and open_tasks:
                body = (f"[aaf] 角色 {r} 已從 roles.json 移除（資料保留）。它手上未完成的任務請改派：\n"
                        + "\n".join(f"  #{t['id']} [{t['state']}] {t['title']}" for t in open_tasks))
                try:
                    store.send({"id": "mbox-dispatcher", "rank": "human", "runtime": "system"}, lead, body,
                               kind="system", idem_key=f"roster-removed-{r}-{(self.hash or '')[:12]}")
                except Exception as e:
                    self.log(f"通知 {lead} 改派失敗：{type(e).__name__}: {e}")
            try:  # 常駐型 driver（如 ACP 主機）要一併關掉；資料保留
                closer = getattr(dispatcher.drivers.DRIVERS.get(dispatcher.drivers.resolve(old[r])), "retire", None)
                if closer:
                    closer(r, self.home)
            except Exception as e:
                self.log(f"關閉 {r} 的常駐程序失敗：{type(e).__name__}: {e}")
            self.log(f"roles.json：移除 {r}（停止叫醒；資料保留）")
        if changed:
            self.log(f"roles.json：更新 {', '.join(changed)}（下一輪生效）")
        try:
            for role, before, after in store.sync_runtimes(accepted):
                if after:
                    self.log(f"{role}: 名冊 runtime {before} → {after}（依 roles.json）")
        except Exception as e:
            self.log(f"sync_runtimes 失敗：{type(e).__name__}: {e}")
        if added or removed:
            try:
                self.zk_sync(added, removed)
            except Exception as e:
                self.log(f"AA Forum 名單同步失敗（下次重試）：{type(e).__name__}: {e}")
                self.alert("zk:roster", "problem", f"AA Forum 名單同步失敗：{type(e).__name__}")
                self.pending_zk = (sorted(set(getattr(self, "pending_zk", ([], []))[0]) | set(added)),
                                   sorted(set(getattr(self, "pending_zk", ([], []))[1]) | set(removed)))
        self.roles = accepted

    def retry_zk(self):
        """AA Forum 名單同步失敗時，之後每輪重試（新增／移除皆冪等）。"""
        pend = getattr(self, "pending_zk", None)
        if not pend or not (pend[0] or pend[1]):
            return
        try:
            self.zk_sync(*pend)
        except Exception:
            return
        self.pending_zk = ([], [])
        self.alert("zk:roster", "recovered", "AA Forum 名單已同步")

    def retiring_roles(self) -> dict[str, dict]:
        return dict(self.retiring)

    def retired(self, role: str):
        self.retiring.pop(role, None)


def zk_roster_sync(added: list[str], removed: list[str]):
    """呼叫 AA Forum /api/system/roster（__system__ 憑證）：新增者加入所有使用中的群、移除者標停用並公告。"""
    state = Path(os.environ.get("AAF_SERVER_STATE") or _paths.var() / "server")
    creds = state / "credentials.json"
    if not creds.exists():
        return  # AA Forum 從沒啟動過：下次啟動時 refresh_agents 會自然納入
    token = json.loads(creds.read_text()).get("__system__")
    if not token:
        raise RuntimeError("AA Forum 沒有 __system__ 憑證")
    base = os.environ.get("AAF_CHAT_URL", "http://127.0.0.1:8111").rstrip("/")
    req = urllib.request.Request(base + "/api/system/roster", method="POST",
                                 data=json.dumps({"added": added, "removed": removed}).encode(),
                                 headers={"Authorization": f"Bearer {token}", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())
