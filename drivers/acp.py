"""ACP driver（S7）：每個角色一支常駐 ACP 主機（drivers/acp_host.py），dispatcher 經 unix socket 送提示。

roles.json：
  {"driver": "acp", "acp_agent": "hermes" | "claude" | "codex" | 自訂指令陣列, "model": "...", "workdir": "..."}

- 常駐：第一次叫醒時啟動主機；主機掛掉，下一次 health()／wake() 會重啟並以 session/load 接回同一個 session。
- 模型變更（roles.json 或 AA Forum override）：主機記著啟動時的模型，不同就重啟主機並接回 session、再 set_model。
- 權限：主機一律自動允許（部署決定），每筆記在 TurnResult 的 text 尾註與 acp_host.log。
- 用量：usage_update（used/size）→ usage()，所以 claude／codex 也有百分比。
"""
from __future__ import annotations

import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from drivers.base import ROOT, Headless, system_prompt, wake_prompt
from mbox import contract

VENDOR_BIN = ROOT / "vendor" / "acp" / "node_modules" / ".bin"


def _which(name: str) -> str | None:
    from drivers.base import Adapter
    return Adapter.find_binary(name)


# 各 agent 的 ACP 啟動方式（只在 driver 內）
AGENTS = {
    "hermes": lambda: [_which("hermes-acp") or "hermes-acp"],
    "claude": lambda: [str(VENDOR_BIN / "claude-agent-acp")],
    "codex": lambda: [str(VENDOR_BIN / "codex-acp")],
    "pi": lambda: [_which("pi-acp") or "pi-acp"],      # 全域安裝（npm i -g pi-acp）
}


from drivers.base import WAKE_FAIL_ALERT  # noqa: E402


class AcpDriver(Headless):
    name = "acp"
    label = "ACP"
    BOOT_TIMEOUT = 240
    lanes = True          # 支援每群一個工作階段（SPEC-1.1 §2）
    lane: int | None = None

    def __init__(self, role: str, cfg: dict, home: Path, lane: int | None = None):
        super().__init__(role, cfg, home)
        if lane is not None:
            # 群專用工作階段：狀態、socket、交棒檔各自獨立；工作目錄與帳號仍共用（不是安全邊界）
            self.lane = int(lane)
            self.cfg = {**cfg, "_room": self.lane}
            self.state_dir = home / "roles" / role / "rooms" / str(self.lane)
            self.state_dir.mkdir(parents=True, exist_ok=True)

    def env(self) -> dict:
        e = super().env()
        if self.lane is not None:
            e["MBOX_LANE"] = str(self.lane)      # mbox inbox 只看本群的信
            e["MBOX_ROOM"] = str(self.lane)      # mbox send 自動帶本群
        else:
            e["MBOX_LANE"] = "default"           # 預設工作階段只看不屬於任何群的信
        return e

    def _session_name(self) -> str:
        return self.name if self.lane is None else f"{self.name}#room{self.lane}"

    # ---------- 設定 ----------
    def agent_cmd(self) -> list[str]:
        a = self.cfg.get("acp_agent", "hermes")
        if isinstance(a, list):
            return [os.path.expanduser(x) for x in a]
        from drivers import acp_catalog
        if a not in acp_catalog.all_agents():
            raise SystemExit(f"{self.role}: 不認得的 acp_agent {a}（可用：{', '.join(acp_catalog.all_agents())} 或指令陣列）")
        cmd = acp_catalog.resolve_cmd(a) or [str(VENDOR_BIN / acp_catalog.all_agents()[a]["acp"])]  # 未安裝：交給 check() 報錯
        if a == "codex" and self.cfg.get("model"):
            cmd = cmd + ["-c", f'model="{self.cfg["model"]}"']
        return cmd

    run_desc = "ACP 常駐：主機持有 session，dispatcher 經 socket 送提示"

    def engine_desc(self):
        a = self.cfg.get("acp_agent", "hermes")
        st = "常駐中" if self._host_pid() else "未啟動"
        return f"ACP·{a if isinstance(a, str) else '自訂'}（headless·{st}）"

    def binary(self):
        try:
            b = self.agent_cmd()[0]
        except SystemExit:
            return None
        return b if os.access(b, os.X_OK) else None

    def auth_check(self, timeout=20):
        """ACP 角色的登入狀態＝底層 agent CLI 的登入狀態（委派給對應 driver）。"""
        import drivers as _d
        sub = _d.DRIVERS.get(self.cfg.get("acp_agent", "hermes"))
        if not sub:
            return None, "此 ACP agent 沒有對應的登入檢查"
        return sub(self.role, {}, self.home).auth_check(timeout)

    def check(self, timeout=15):
        b = self.binary()
        if not b:
            return False, f"找不到 ACP 指令（{self.cfg.get('acp_agent', 'hermes')}）；claude/codex 需 cd vendor/acp && npm install"
        return True, b

    def _sock(self) -> str:
        # unix socket 路徑上限約 104 字元：放在 /tmp 底下的短路徑，依使用者與角色隔離
        d = Path(f"/tmp/civ-acp-{os.getuid()}")
        d.mkdir(mode=0o700, exist_ok=True)
        import hashlib
        tag = hashlib.sha1(str(self.home).encode()).hexdigest()[:8]   # hash() 每個程序不同，不能用
        return str(d / (f"{tag}-{self.role}.sock" if self.lane is None else f"{tag}-{self.role}-r{self.lane}.sock"))

    # ---------- 主機管理 ----------
    def _host_pid(self) -> int | None:
        f = self.state_dir / "acp_host.pid"
        try:
            pid = int(f.read_text())
            os.kill(pid, 0)
            return pid
        except (OSError, ValueError):
            return None

    def _call(self, req: dict, timeout: float = 5) -> dict | None:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                s.connect(self._sock())
                s.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode())
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
            return json.loads(buf or b"null")
        except (OSError, ValueError):
            return None

    def host_status(self) -> dict | None:
        return self._call({"op": "status"}) if self._host_pid() else None

    def _spec(self) -> dict:
        return {"cmd": self.agent_cmd(), "model": self.cfg.get("model"), "cwd": str(self.workdir)}

    def start(self) -> str:
        """確保主機在跑且設定相符；回 started／existing／restarted。"""
        spec = self._spec()
        spec_f = self.state_dir / "acp_host.spec.json"
        st = self.host_status()
        if st and st.get("alive"):
            old = json.loads(spec_f.read_text()) if spec_f.exists() else None
            if old == spec:
                return "existing"
            if st.get("busy"):
                return "existing"          # 設定變了但正在跑：這輪跑完再換
            self.shutdown()
            verb = "restarted"
        else:
            if self._host_pid():
                self.shutdown()
            verb = "started"
        # 換 agent（例 codex → hermes）時舊 session 不屬於新 agent：清掉 session，開新的
        old_spec = json.loads(spec_f.read_text()) if spec_f.exists() else None
        if old_spec and old_spec.get("cmd") != spec["cmd"]:
            (self.state_dir / "acp_session").unlink(missing_ok=True)
            (self.state_dir / "acp_usage.json").unlink(missing_ok=True)
        for f in ("acp_host.ready", "acp_host.error", "acp_host.stopped", "acp_host.parked"):
            (self.state_dir / f).unlink(missing_ok=True)
        spec_f.write_text(json.dumps(spec, ensure_ascii=False))
        argv = [sys.executable, "-m", "drivers.acp_host", str(self.state_dir), self._sock(), str(self.workdir),
                spec["model"] or "-", "--", *spec["cmd"]]
        env = self.env()
        env["PYTHONPATH"] = f"{ROOT}:{env.get('PYTHONPATH', '')}"
        with (self.state_dir / "acp_host.out.log").open("a") as out:
            subprocess.Popen(argv, cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                             start_new_session=True)
        end = time.time() + self.BOOT_TIMEOUT
        while time.time() < end:
            if (self.state_dir / "acp_host.ready").exists() and self._call({"op": "status"}):
                return verb
            err = self.state_dir / "acp_host.error"
            if err.exists():
                raise RuntimeError("ACP 主機啟動失敗：" + err.read_text()[:300])
            time.sleep(0.2)
        self.shutdown()
        raise RuntimeError("ACP 主機啟動逾時")

    def shutdown(self, final: bool = False) -> bool:
        """final=True（bin/aaf down）：標記停用，避免被 _revive 拉回。"""
        if final:
            (self.state_dir / "acp_host.stopped").write_text(str(time.time()))
        pid = self._host_pid()
        if not pid:
            return False
        self._call({"op": "shutdown"}, timeout=3)
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except OSError:
                return True
            time.sleep(0.1)
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except OSError:
            pass
        return True

    def stop(self) -> bool:
        """中止正在跑的輪次（不關主機）。"""
        r = self._call({"op": "cancel"})
        return bool(r and r.get("ok"))

    # ---------- dispatcher 介面 ----------
    def health(self) -> str:
        st = self.host_status()
        if st is None:
            pend = self.state_dir / "acp_pending"
            if pend.exists() and not (self.state_dir / "acp_turn.json").exists():
                # 主機在輪次中途掛掉：補一筆失敗結果，讓 dispatcher 記錄並退避
                (self.state_dir / "acp_turn.json").write_text(json.dumps(dict(
                    session_id=self.session(), error="ACP 主機在輪次中途結束", started_at=float(pend.read_text() or 0),
                    finished_at=time.time()), ensure_ascii=False))
            self._revive()
            return "idle"
        return "busy" if st.get("busy") else "idle"

    def _revive(self):
        """常駐：主機曾經啟動過（有 spec）且不是被 down 停掉的，就重開；同一角色 60 秒最多一次。"""
        if not (self.state_dir / "acp_host.spec.json").exists() or (self.state_dir / "acp_host.stopped").exists() \
                or (self.state_dir / "acp_host.parked").exists():
            return
        mark = self.state_dir / "acp_host.revive"
        try:
            if time.time() - float(mark.read_text()) < 60:
                return
        except (OSError, ValueError):
            pass
        mark.write_text(str(time.time()))
        try:
            self.start()
        except Exception as e:
            (self.state_dir / "last_error.txt").write_text(f"ACP 主機重開失敗：{e}")

    def session(self):
        f = self.state_dir / "acp_session"
        return f.read_text().strip() if f.exists() else None

    # ---------------- 補血：context 用量過門檻時寫交棒檔、換新 session ----------------
    # 門檻照原版：Claude ≥60%、Codex ≥70%（其他 agent 用 60%）；連續兩讀過線才進補血。
    REFILL_THRESHOLDS = {"claude": 60, "codex": 70}
    REFILL_DEFAULT = 60

    def refill_threshold(self) -> int:
        a = self.cfg.get("acp_agent", "hermes")
        if "refill_percent" in self.cfg:
            return int(self.cfg["refill_percent"])
        return self.REFILL_THRESHOLDS.get(a if isinstance(a, str) else "", self.REFILL_DEFAULT)

    def _refill_state(self) -> dict:
        f = self.state_dir / "refill.json"
        try:
            return json.loads(f.read_text())
        except (OSError, ValueError):
            return {}

    def _refill_save(self, st: dict):
        (self.state_dir / "refill.json").write_text(json.dumps(st, ensure_ascii=False))

    def refill_tick(self) -> str | None:
        """dispatcher 每輪呼叫（角色 idle 時）。回傳要記 log 的事件或 None。
        階段：（無）→ archive_requested（請本人寫交棒檔）→ 驗交棒檔 → new_session → recall_sent → 完成。"""
        st = self._refill_state()
        u = self.usage()
        pct = u.get("percent") if isinstance(u, dict) else None
        phase = st.get("phase")
        if not phase:
            if pct is None:
                st.pop("over", None); self._refill_save(st); return None     # UNKNOWN 不當 0%，也不觸發
            if pct >= self.refill_threshold():
                st["over"] = st.get("over", 0) + 1
                if st["over"] >= 2:
                    st.update(phase="archive_requested", started=time.time(), percent=pct,
                              session=self.session(), archive=None)
                    self._refill_save(st)
                    return self._request_archive(pct)
                self._refill_save(st)
                return f"context {pct}% ≥ {self.refill_threshold()}%（第 1 讀，下一讀仍過線才補血）"
            if st.get("over"):
                st["over"] = 0; self._refill_save(st)
            return None
        if phase == "archive_requested":
            return self._check_archive(st)
        if phase == "recall_sent":
            st.clear(); self._refill_save(st)
            return "補血完成：新 session 已接回"
        return None

    # ---------------- 閒置關閉（SPEC-1.1 §2：群工作階段閒置 N 小時 → 寫交棒檔 → 關主機；下次有信再開新的並接回） ----------------
    def last_activity(self) -> float:
        times = [f.stat().st_mtime for f in (self.state_dir / "turns.log", self.state_dir / "acp_host.spec.json") if f.exists()]
        return max(times) if times else 0

    def park_tick(self, idle_hours: float) -> str | None:
        """dispatcher 在本群工作階段 idle 且沒有未讀時呼叫。只用於群工作階段；預設工作階段不自動關。"""
        if self.lane is None or idle_hours <= 0 or not self._host_pid():
            return None
        st = self._refill_state()
        phase = st.get("phase")
        if phase in (None, "") :
            if time.time() - self.last_activity() < idle_hours * 3600:
                return None
            hp = self._handoff_path()
            before = hp.stat().st_mtime if hp.exists() else 0
            prompt = (f"[閒置關閉] 群 {self.lane} 這個工作階段已閒置超過 {idle_hours:g} 小時，系統要先關掉它以節省資源。"
                      f"請把目前狀態寫進 `{hp}`（覆寫）：mission_state、done、open_loops、next_action。"
                      "沒有進行中的工作就寫 mission_state: complete。最後一行單獨寫 `<!-- archive.ready -->`，寫完就結束。")
            r = self._call({"op": "prompt", "text": prompt, "system": None}, timeout=10)
            if not r or not r.get("ok"):
                return None
            (self.state_dir / "acp_pending").write_text(str(time.time()))
            st.update(phase="park_requested", started=time.time(), handoff_mtime_before=before)
            self._refill_save(st)
            return f"閒置關閉：群 {self.lane} 閒置逾 {idle_hours:g} 小時，已請寫交棒檔"
        if phase != "park_requested":
            return None
        hp = self._handoff_path()
        ok = (hp.exists() and hp.stat().st_mtime > st.get("handoff_mtime_before", 0)
              and "archive.ready" in hp.read_text(errors="replace"))
        if not ok and time.time() - st.get("started", 0) < 900:
            return None
        archive = None
        if ok:
            archive = self.state_dir / "archives" / f"{time.strftime('%Y%m%d_%H%M%S')}_park.md"
            archive.parent.mkdir(exist_ok=True)
            archive.write_text(hp.read_text(errors="replace"))
            (self.state_dir / "recall_pending").write_text(str(hp))
        self.shutdown()
        (self.state_dir / "acp_host.parked").write_text(str(time.time()))
        (self.state_dir / "acp_session").unlink(missing_ok=True)     # 下次開新 session（不 load 舊的）
        if self.sessions is not None:
            self.sessions.clear_session(self.role, self._session_name())
        st.clear(); self._refill_save(st)
        return (f"閒置關閉：群 {self.lane} 工作階段已關閉（交棒檔存於 {archive.name}）" if archive
                else f"閒置關閉：群 {self.lane} 15 分鐘內沒寫出交棒檔，仍關閉（下次開新 session，不接回）")

    def _handoff_path(self):
        # 同一角色的各群工作階段共用工作目錄：交棒檔要分開，否則互相覆蓋
        return self.workdir / ("HANDOFF.md" if self.lane is None else f"HANDOFF.room{self.lane}.md")

    def _request_archive(self, pct) -> str:
        prompt = (f"[補血] 你的 context 已用 {pct}%（門檻 {self.refill_threshold()}%），系統即將為你換新 session。\n"
                  "請**現在**整理交棒內容（任務未完成就寫清楚怎麼接手；已完成就寫結論與證據），"
                  f"把交棒內容寫進 `{self._handoff_path()}`（覆寫），內容必含：\n"
                  "  mission_state: active | handoff_pending | complete\n"
                  "  done／evidence、open_loops／blockers、next_action（指向外部證據）、"
                  "  continuation_contract（target、完整可送出的 message_ref）\n"
                  "寫完最後一行單獨寫 `<!-- archive.ready -->`。這一輪不要接新工作，寫完就結束。")
        self.start()
        # 先記下舊交棒檔的 mtime 再送請求：agent 很快寫完時，送後才記會記到新檔的 mtime，永遠驗不過
        hp = self._handoff_path()
        before = hp.stat().st_mtime if hp.exists() else 0
        r = self._call({"op": "prompt", "text": prompt, "system": None}, timeout=10)
        if not r or not r.get("ok"):
            return "補血：請求寫交棒檔失敗（主機忙或無回應），下一輪重試"
        (self.state_dir / "acp_pending").write_text(str(time.time()))
        st = self._refill_state(); st["handoff_mtime_before"] = before
        self._refill_save(st)
        return f"補血開始：context {pct}%，已請 {self.role} 寫交棒檔"

    def _check_archive(self, st) -> str | None:
        hp = self._handoff_path()
        ok = (hp.exists() and hp.stat().st_mtime > st.get("handoff_mtime_before", 0) and hp.stat().st_size > 50
              and "archive.ready" in hp.read_text(errors="replace"))
        if not ok:
            if time.time() - st.get("started", 0) > 900:
                st.clear(); st["failed_at"] = time.time(); self._refill_save(st)
                return "補血失敗：15 分鐘內沒寫出合格交棒檔，保留舊 session（通知 lead）"
            return None
        r = self._call({"op": "new_session"}, timeout=200)
        if not r or not r.get("ok"):
            return "補血：換 session 失敗，下一輪重試"
        archive = self.state_dir / "archives" / f"{time.strftime('%Y%m%d_%H%M%S')}_{r['old']}.md"
        archive.parent.mkdir(exist_ok=True)
        archive.write_text(hp.read_text(errors="replace"))
        recall = ("[補血接回] 你剛換了新 session（舊 context 已清空）。請照下面步驟接回："
                  f"先讀 `{hp}`（上一個你寫的交棒檔），再看 mbox inbox 與 task，然後照 next_action 續跑。"
                  "第一句回報：我是誰、從交棒檔讀到的 mission_state 與 next_action。")
        r2 = self._call({"op": "prompt", "text": recall, "system": system_prompt(self.role, self.cfg)}, timeout=10)
        (self.state_dir / "acp_pending").write_text(str(time.time()))
        st.update(phase="recall_sent", new_session=r["session_id"], archive=str(archive))
        self._refill_save(st)
        return f"補血：交棒檔已驗（存檔 {archive.name}），session {r['old'][:8]}→{r['session_id'][:8]}，已送接回指令"

    def wake(self, unread: int, head=None, preview: str = "") -> str:
        if self.health() == "busy":
            return "busy: 上一輪還在跑"
        if self._refill_state().get("phase"):
            return "refill: 補血中，這輪不送新信"
        bo = self._bo()
        if head is not None and bo.get("head") == head:
            if time.time() < bo["next"]:
                return f"backoff: 最舊未讀 #{head} 上一輪沒處理，{int(bo['next'] - time.time())}s 後再試（第 {bo['fails']} 次）"
            bo["fails"] += 1
            if bo["fails"] == WAKE_FAIL_ALERT:
                self._alert_stuck(head, bo["fails"])
        else:
            bo["fails"] = 0
        bo["head"] = head
        bo["next"] = time.time() + min(30 * 2 ** bo["fails"], 1800)
        self._bo_save(bo)
        try:
            how = self.start()
        except Exception as e:
            (self.state_dir / "last_error.txt").write_text(str(e))
            return f"error: {e}"
        from mbox import hooks as _hooks
        prompt = wake_prompt(self.role, unread, preview, _hooks.take_notes(self.role, self.home))
        extra = _hooks.run_wake(dict(type="wake", role=self.role, room=self.lane, unread=unread,
                                     session_new=not (self.state_dir / "acp_session").exists()))
        if extra:
            prompt = "\n\n".join(extra) + "\n\n" + prompt
        recall = self.state_dir / "recall_pending"
        if recall.exists():
            # 閒置關閉後第一次叫醒：新 session，先接回交棒檔再處理新信
            prompt = (f"[接回] 這是你在群 {self.lane} 的工作階段，因閒置關閉後重新開啟（舊 context 已清空）。"
                      f"先讀 `{recall.read_text().strip()}`（上次關閉前你寫的交棒檔），再處理下面的新信。\n\n" + prompt)
        r = self._call({"op": "prompt", "text": prompt, "system": system_prompt(self.role, self.cfg)}, timeout=10)
        if not r or not r.get("ok"):
            return "busy: 上一輪還在跑" if r and r.get("busy") else f"error: {(r or {}).get('error', '主機無回應')}"
        (self.state_dir / "last_error.txt").unlink(missing_ok=True)
        (self.state_dir / "recall_pending").unlink(missing_ok=True)
        (self.state_dir / "acp_pending").write_text(str(time.time()))
        with (self.state_dir / "turns.log").open("a") as log:
            log.write(f"\n===== {time.strftime('%F %T')} acp wake unread={unread}（主機 {how}）\n")
        return f"started acp session={r.get('session_id')} ({how})"

    def finish(self):
        f = self.state_dir / "acp_turn.json"
        pend = self.state_dir / "acp_pending"
        if not f.exists() or not pend.exists() or self.health() == "busy":
            return
        try:
            t = json.loads(f.read_text())
        except ValueError:
            return
        f.unlink(missing_ok=True)
        pend.unlink(missing_ok=True)
        if self.sessions is not None and t.get("session_id"):
            self.sessions.set_session(self.role, self._session_name(), t["session_id"])
        perms = t.get("permissions") or []
        text = t.get("text")
        if perms:
            text = (text or "") + "\n[自動允許的權限] " + "；".join(f"{p.get('kind')}:{p.get('title')}" for p in perms)
        err = t.get("error")
        if err and err.startswith("模型或 provider 拒絕") and self._rollback_model(err):
            err += "（已自動退回原本的模型；這封信下一輪重送）"
        exit_code = 0 if not err else 1
        (self.state_dir / "last_exit.txt").write_text(f"{exit_code}\n")
        if err:
            (self.state_dir / "last_error.txt").write_text(err)
        with (self.state_dir / "turns.log").open("a") as log:
            log.write(json.dumps({k: t.get(k) for k in ("session_id", "model_used", "stop_reason", "permissions",
                                                        "tool_calls", "duration_ms", "error")}, ensure_ascii=False) + "\n")
        tr = contract.turn_result(self.role, self.name, exit_code, session_id=t.get("session_id"),
                                  text=(text or "")[-2000:] or None, error=err, model_used=t.get("model_used"),
                                  tokens=t.get("tokens"), duration_ms=t.get("duration_ms"),
                                  started_at=t.get("started_at"), finished_at=t.get("finished_at"))
        try:
            contract.validate("turn_result", tr)
        except contract.ContractError as e:
            tr = contract.turn_result(self.role, self.name, exit_code, ok=False, error=f"契約驗證失敗：{e}")
        (self.state_dir / "last_turn.json").write_text(json.dumps(tr, ensure_ascii=False))
        try:
            from mbox import hooks as _hooks
            _hooks.run_turn_end(dict(type="turn_end", role=self.role, room=self.lane, ok=not err, error=err,
                                     text=(text or "")[-2000:]))
        except Exception:
            pass

    def _rollback_model(self, err: str) -> bool:
        """模型不能用：移除 AA Forum override（或 roles.json 的 model 由 lead／使用者處理），寫告警檔讓 AA Forum 顯示。"""
        from mbox.overrides import override_path
        f = override_path(self.role, self.home)
        bad = self.cfg.get("model")
        (self.state_dir / "model_alert.json").write_text(json.dumps(
            {"model": bad, "error": err[:400], "at": time.time(), "rolled_back": f.exists()}, ensure_ascii=False))
        if f.exists():
            f.unlink()
            bo = self.state_dir / "backoff.json"
            bo.unlink(missing_ok=True)       # 退回後立刻重送，不必等退避
            return True
        return False

    def usage(self, turn=None):
        f = self.state_dir / "acp_usage.json"
        try:
            u = json.loads(f.read_text())
        except (OSError, ValueError):
            return dict(contract_version="1", source="", reason="ACP 尚未回報用量")
        used, size = u.get("used"), u.get("size")
        pct = round(100 * used / size) if isinstance(used, int) and isinstance(size, int) and size > 0 else None
        return dict(contract_version="1", model=u.get("model"), context_used=used, context_limit=size,
                    percent=pct, measured_at=u.get("at"), source=f"ACP usage_update {used}/{size}")


def _host_dirs(home: Path, role: str | None = None):
    """角色的所有工作階段狀態目錄：預設 roles/<r>/ 與各群 roles/<r>/rooms/<n>/。"""
    for d in sorted((home / "roles").glob(role or "*")):
        if not d.is_dir():
            continue
        yield d.name, None, d
        rooms = d / "rooms"
        if rooms.is_dir():
            for r in sorted(rooms.iterdir()):
                if r.is_dir() and r.name.isdigit():
                    yield d.name, int(r.name), r


def _stub(cls, role, lane, d, home):
    ad = cls.__new__(cls)
    ad.role, ad.cfg, ad.home, ad.state_dir, ad.lane = role, {}, home, d, lane
    return ad


def _retire(cls, role: str, home: Path) -> bool:
    """角色從 roles.json 移除：關掉它所有工作階段的常駐主機並標記停用。"""
    done = False
    for r, lane, d in _host_dirs(home, role):
        done = _stub(cls, r, lane, d, home).shutdown(final=True) or done
    return done


AcpDriver.retire = classmethod(_retire)


def shutdown_all(home: Path) -> list[str]:
    """bin/aaf down：關掉所有角色的 ACP 主機。"""
    done = []
    for role, lane, d in _host_dirs(home):
        if (d / "acp_host.pid").exists() and _stub(AcpDriver, role, lane, d, home).shutdown(final=True):
            done.append(role if lane is None else f"{role}@群{lane}")
    return done


def _cli():
    from mbox import paths as _p
    h = _p.var()
    print("已關閉 ACP 主機：" + ("、".join(shutdown_all(h)) or "無"))
