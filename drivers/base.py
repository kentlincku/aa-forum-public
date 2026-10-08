"""driver 共用基底：契約介面、headless 執行器、tmux／hook 叫醒、系統提示組裝。

核心只透過這裡定義的介面操作 agent；各 agent 的細節住在 drivers/<name>.py。

wake 只送固定格式的叫醒字串（只含未讀數），訊息本文一律由 agent 自己用 mbox 讀。

叫醒等級：
  hook      ：agent 每輪結束時自己查信箱（claude/codex Stop hook）；dispatcher 只在它閒置時補叫
  headless  ：dispatcher 直接驅動一輪（claude -p --resume / codex exec resume / 任意指令範本）
  tmux      ：退回用 tmux send-keys 送固定叫醒字串
  manual    ：不叫醒，只記錄
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

from mbox import contract  # noqa: E402
from mbox import paths as _paths  # noqa: E402


def _inst():
    return _paths.instance()
from mbox.overrides import MODEL_ID_RE, effective_cfg, load_override, override_path  # noqa: E402,F401


def wake_prompt(role: str, unread: int, preview: str = "", notes: list | None = None) -> str:
    if notes:
        head = "[規則提醒] 你上次送出的內容觸發以下提醒（已送出，不需重送；之後照做即可）：\n" + \
            "\n".join(f"- {n}" for n in notes) + "\n\n"
        return head + wake_prompt(role, unread, preview)
    if preview:
        # headless 沒有長度限制：直接附上未讀內容，省掉「先叫醒再讀」那一輪
        return (f"[mbox] 你是 {role}，信箱有 {unread} 則未讀，內容如下（已附全文，不必再執行 mbox inbox）。"
                f"依內容處理；每則處理完用 `mbox ack <信件編號> done`（「── 信件 #N」的 N；"
                f"群組留言的 #編號 是群內編號，不能拿來 ack）。群組留言回在群裡（aaf-chat post），其他回覆用 `mbox send`。\n\n{preview}")
    return (f"[mbox] 你是 {role}，信箱有 {unread} 則未讀。請執行 `mbox inbox` 讀取，"
            f"依內容處理，處理完用 `mbox ack <id> done`，需要回覆就用 `mbox send`。")


PREVIEW_LIMIT = 12000
WAKE_FAIL_ALERT = 3             # 同一則信連續失敗幾次就通知 guardian


def render_preview(msgs: list, included_ids: list | None = None) -> str:
    parts, size = [], 0
    for m in msgs:
        head = (f"── 信件 #{m['id']}（ack 用這個編號）[{m['kind']}] {m['sender']} → {m['recipient']}  串 {m['thread_id']}"
                + (f"  回覆 #{m['reply_to']}" if m.get('reply_to') else "")
                + (f"  任務 #{m['task_id']}" if m.get('task_id') else ""))
        body = m["body"]
        att = "".join(f"\n  📎 {p}" for p in m.get("attachments") or [])
        block = f"{head}\n{body}{att}"
        limit = getattr(sys.modules.get("drivers"), "PREVIEW_LIMIT", PREVIEW_LIMIT)  # 允許 drivers.PREVIEW_LIMIT 覆寫
        if size + len(block) > limit:
            parts.append(f"…還有 {len(msgs) - len(parts)} 則未附（超過長度），請執行 `mbox inbox` 讀取。")
            break
        parts.append(block)
        if included_ids is not None:
            included_ids.append(m['id'])
        size += len(block)
    return "\n\n".join(parts)


class Adapter:
    """契約介面的基底。子類別（driver）至少覆寫 name、argv／parse_*；其餘有預設。"""
    level = "manual"
    name = "manual"
    bin_default: str | None = None

    # ---- 契約：describe / check / status / stop / interactive_cmd ----
    @classmethod
    def manifest(cls) -> dict:
        p = Path(__file__).with_name(f"{cls.name}.manifest.json")
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
        return {"name": cls.name, "version": "0", "contract_version": contract.VERSION,
                "capabilities": [], "wake_modes": ["manual"]}

    def describe(self) -> dict:
        return contract.validate("manifest", self.manifest())

    @staticmethod
    def find_binary(b: str | None, path: str | None = None) -> str | None:
        if not b:
            return None
        if os.sep in b:
            return b if os.access(b, os.X_OK) else None
        found = shutil.which(b, path=path)
        if not found:
            for cand in (Path.home() / ".local/bin" / b, Path("/opt/homebrew/bin") / b):
                if cand.is_file() and os.access(cand, os.X_OK):
                    return str(cand)
        return found

    def binary(self) -> str | None:
        return self.find_binary(self.cfg.get("bin") or self.bin_default, self.env().get("PATH"))

    # 登入檢查：(True/False/None, 說明)；None＝此 driver 無法判斷登入狀態。只讀，不印帳號細節。
    auth_cmd: list[str] | None = None

    def auth_check(self, timeout: float = 20) -> tuple[bool | None, str]:
        if not self.auth_cmd:
            return None, "此 driver 不提供登入檢查"
        b = self.binary()
        if not b:
            return False, "找不到執行檔"
        try:
            r = subprocess.run([b, *self.auth_cmd[1:]], capture_output=True, text=True, timeout=timeout,
                               stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, f"登入檢查失敗：{type(e).__name__}"
        return self.parse_auth(r.returncode, r.stdout or "", r.stderr or "")

    def parse_auth(self, rc: int, out: str, err: str) -> tuple[bool | None, str]:
        return (rc == 0, "已登入" if rc == 0 else f"未登入（exit {rc}）")

    def check(self, timeout: float = 15) -> tuple[bool, str]:
        """能否執行：找得到執行檔且 `<bin> --version` 成功。只讀，不改任何設定。"""
        if not self.bin_default and not self.cfg.get("bin"):
            return True, "無需執行檔"
        b = self.binary()
        if not b:
            return False, f"找不到執行檔 {self.cfg.get('bin') or self.bin_default}"
        try:
            r = subprocess.run([b, "--version"], capture_output=True, text=True, timeout=timeout,
                               stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, f"{b} --version 失敗：{type(e).__name__}"
        out = (r.stdout or r.stderr).strip().splitlines()
        if r.returncode != 0:
            return False, f"{b} --version exit {r.returncode}：{(out or [''])[-1][:200]}"
        return True, (out or [""])[0][:200]

    def status(self) -> str:
        return self.health()

    def stop(self) -> bool:
        return False

    def interactive_cmd(self, model: str | None = None, prompt: str | None = None) -> list[str] | None:
        """互動模式（開房）的啟動指令；不支援回 None。"""
        return None

    label = "未知"
    process_names: tuple[str, ...] = ()

    @classmethod
    def matches_process(cls, comm: str, args: str) -> bool:
        """tmux 房內的前景程序是不是這個 agent（AA Forum 辨識引擎用）。"""
        tokens = [os.path.basename(t) for t in args.split()[:2]]
        return bool(cls.process_names) and (comm in cls.process_names or any(t in cls.process_names for t in tokens))

    def usage(self, turn: dict | None = None) -> dict | None:
        """選配（能力 usage_report）：回 contract usage（context 用量等）。預設不支援。
        turn＝核心記錄的最近一輪 TurnResult；driver 可據以找自家 session 檔。"""
        return None

    def room_usage(self, pid: int, screen: str) -> dict | None:
        """選配：tmux 房內互動程序的用量（pid＝前景程序、screen＝畫面尾端）。回 usage dict 或 None。"""
        return None

    @classmethod
    def model_catalog(cls) -> dict | None:
        """選配（能力 model_catalog）：可選模型目錄 {providers:[{id,label,models:[{id,context}]}], default, warnings}。"""
        return None

    @classmethod
    def account_quota(cls) -> dict | None:
        """選配（能力 account_quota）：本機登入帳號的額度。回 {engine, windows, email, ...} 或 None。"""
        return None

    def screen_model(self, screen: str) -> str | None:
        """選配：從互動畫面辨識目前模型。"""
        return None

    def screen_activity(self, screen: str, tail: str) -> str | None:
        """互動畫面推定活動：waiting／retrying／working／idle；無法判斷回 None。"""
        return generic_screen_activity(tail)

    def __init__(self, role: str, cfg: dict, home: Path):
        self.role, self.cfg, self.home = role, cfg, home
        self.state_dir = home / "roles" / role
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.workdir = Path(os.path.expanduser(cfg.get("workdir", str(_inst() / "work" / role))))
        self.workdir.mkdir(parents=True, exist_ok=True)

    def env(self) -> dict:
        e = dict(os.environ)
        e.update({"MBOX_AGENT": self.role, "MBOX_HOME": str(self.home),
                  "AAF_AGENT": self.role,
                  "AAF_CHAT_URL": self.cfg.get("chat_url", e.get("AAF_CHAT_URL", "http://127.0.0.1:8111")),
                  "AAF_SERVER_STATE": self.cfg.get("server_state", e.get("AAF_SERVER_STATE") or str(self.home / "server")),
                  "MBOX_URL": self.cfg.get("url", os.environ.get("MBOX_URL", "http://127.0.0.1:8775")),
                  "AAF_HOME": str(_inst()),
                  "PATH": f"{ROOT / 'bin'}:{e.get('PATH', '')}"})
        # 實例可放自己的指令（例：把舊工具名轉接到 mbox）在 <實例>/agent-bin，排在 PATH 最前面
        ab = _inst() / "agent-bin"
        if ab.is_dir():
            e["PATH"] = f"{ab}:{e['PATH']}"
        # roles.json 的 env：角色程序額外的環境變數（值裡的 {role}、{user}、{home} 會代換）。不能蓋掉身分相關的變數。
        import getpass
        for k, v in (self.cfg.get("env") or {}).items():
            if k in ("MBOX_AGENT", "MBOX_TOKEN", "MBOX_HOME", "AAF_AGENT", "PATH") or not isinstance(v, str):
                continue
            e[k] = v.replace("{role}", self.role).replace("{user}", getpass.getuser()).replace("{home}", str(Path.home()))
        tok = self.home / "tokens" / self.role
        if tok.exists():
            e["MBOX_TOKEN"] = tok.read_text().strip()
        return e

    def start(self) -> str:
        return "noop"

    def wake(self, unread: int, head=None, preview: str = "") -> str:
        return "manual: 不自動叫醒"

    def health(self) -> str:
        return "unknown"


# ---------------- headless：由 dispatcher 驅動一輪 ----------------
class Headless(Adapter):
    level = "headless"
    name = "command"
    sessions = None  # 核心的 session 對照（Store）；dispatcher 注入。None 時只用檔案。

    def stop(self) -> bool:
        """中止正在跑的輪次（整個 process group）。"""
        import signal
        p = self._lock()
        try:
            pid = int(p.read_text())
            os.killpg(pid, signal.SIGTERM)
            return True
        except (OSError, ValueError):
            return False

    def _lock(self) -> Path:
        return self.state_dir / "running.pid"

    def health(self) -> str:
        p = self._lock()
        if p.exists():
            try:
                os.kill(int(p.read_text()), 0)
                return "busy"
            except (OSError, ValueError):
                p.unlink(missing_ok=True)
        return "idle"

    def _session_file(self):
        # 依 runtime 分開存：換 runtime 時不會拿 codex 的 session id 去 resume hermes
        return self.state_dir / f"session.{self.cfg.get('runtime') or type(self).__name__}"

    def _output_file(self):
        # 未消耗的輸出也屬於產生它的 runtime；不採信舊共用 last_out.txt。
        return self.state_dir / f"last_out.{self.cfg.get('runtime') or type(self).__name__}.txt"

    def session(self) -> str | None:
        if self.sessions is not None:
            sid = self.sessions.get_session(self.role, self.name)
            if sid:
                return sid
        f = self._session_file()
        sid = f.read_text().strip() if f.exists() else None
        if sid and self.sessions is not None:  # 一次性匯入舊檔
            self.sessions.set_session(self.role, self.name, sid)
        return sid or None

    def save_session(self, sid: str | None):
        if sid:
            if self.sessions is not None:
                self.sessions.set_session(self.role, self.name, sid)
            self._session_file().write_text(sid)  # S4 前 AA Forum 仍讀檔，鏡像保留

    def argv(self, prompt: str) -> list[str]:
        raise NotImplementedError

    def parse_session(self, out: str) -> str | None:
        return None

    # ---- 退避：一輪跑完未讀沒減少（失敗、登入過期、模型不理），下一次叫醒延後，指數成長 ----
    def _bo(self) -> dict:
        f = self.state_dir / "backoff.json"
        return json.loads(f.read_text()) if f.exists() else {"fails": 0, "next": 0, "unread": None}

    def _bo_save(self, d: dict):
        (self.state_dir / "backoff.json").write_text(json.dumps(d))

    def _alert_stuck(self, head, fails: int):
        """同一則信連續叫醒失敗 → 通知 guardian 一次（每則信只一次）。以前只有「送達後沒回」才告警，
        角色根本起不來（agent 壞掉、找不到指令）時信停在 queued，guardian 永遠不知道（e2e G4）。"""
        try:
            from mbox.core import Store
            err = ""
            for f in ("last_error.txt", "acp_host.error"):
                p = self.state_dir / f
                if p.exists():
                    err = p.read_text()[:300]
                    break
            lane = getattr(self, "lane", None)
            where = f"{self.role}" + (f"（群 #{lane}）" if lane is not None else "")
            Store(self.home / "mbox.sqlite3").send(
                {"id": "mbox-dispatcher", "rank": "human"}, "guardian",
                f"叫醒失敗告警：{where} 連續 {fails} 次處理不了信件 #{head}。最近錯誤：{err or '（無）'}",
                kind="system", idem_key=f"stuck-{self.role}-{lane}-{head}")
        except Exception:
            pass                               # 告警失敗不能擋住叫醒流程

    def wake(self, unread: int, head: int | None = None, preview: str = "") -> str:
        if self.health() == "busy":
            return "busy: 上一輪還在跑"
        # 退避判準＝最舊那則未讀還是同一則（新信進來不算沒進度）
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
        from mbox import hooks as _hooks
        prompt = wake_prompt(self.role, unread, preview, _hooks.take_notes(self.role, self.home))
        log = (self.state_dir / "turns.log").open("a")
        log.write(f"\n===== {time.strftime('%F %T')} wake unread={unread}\n"); log.flush()
        argv = self.argv(prompt)
        log.write("$ " + shlex.join(argv) + "\n"); log.flush()
        out_f = self._output_file()
        exit_f = self.state_dir / "last_exit.txt"
        exit_f.unlink(missing_ok=True)
        (self.state_dir / "last_error.txt").unlink(missing_ok=True)
        runner = (f"{shlex.join(argv)} > {shlex.quote(str(out_f))} 2>> {shlex.quote(str(self.state_dir / 'turns.log'))}; "
                  f"printf '%s\\n' \"$?\" > {shlex.quote(str(exit_f))}; "
                  f"rm -f {shlex.quote(str(self._lock()))}")
        p = subprocess.Popen(["/bin/sh", "-c", runner], cwd=self.workdir, env=self.env(),
                             stdin=subprocess.DEVNULL, start_new_session=True)
        self._lock().write_text(str(p.pid))
        (self.state_dir / "turn_started.txt").write_text(repr(time.time()))
        (self.state_dir / "last_turn.json").unlink(missing_ok=True)
        return f"started pid={p.pid}"

    def finish(self):
        """dispatcher 每輪都會呼叫：只抓本 runtime 上一輪輸出的 session id。"""
        out = self._output_file()
        if out.exists() and self.health() == "idle":
            txt = out.read_text(errors="replace")
            self.save_session(self.parse_session(txt))
            with (self.state_dir / "turns.log").open("a") as log:
                log.write(txt[-4000:] + "\n")
            out.rename(self.state_dir / "prev_out.txt")
            err = self.parse_error(txt)
            if err:
                (self.state_dir / "last_error.txt").write_text(err)
            self._write_turn_result(txt, err)

    # ---- 契約：每輪結束產出 TurnResult，由 dispatcher 寫入核心 ----
    @property
    def driver_name(self) -> str:
        return self.name

    def parse_turn(self, out: str) -> dict:
        """各 agent 覆寫：從輸出取 session_id／text／model_used／tokens／duration_ms。"""
        return {"text": out.strip()[-2000:] or None}

    def _write_turn_result(self, txt: str, err: str | None):
        code = self.state_dir / "last_exit.txt"
        try:
            exit_code = int(code.read_text().strip()) if code.exists() else None
        except ValueError:
            exit_code = None
        sf = self.state_dir / "turn_started.txt"
        try:
            started = float(sf.read_text()) if sf.exists() else None
        except ValueError:
            started = None
        try:
            kw = self.parse_turn(txt) or {}
        except Exception as e:  # 解析失敗不能讓 dispatcher 掛掉
            kw = {"error": f"parse_turn 失敗：{type(e).__name__}: {e}"}
        if err and not kw.get("error"):
            kw["error"] = err
        if "ok" in kw:  # driver 自報的 ok 只能把結果改壞，不能蓋過非零 exit 或錯誤
            kw["ok"] = bool(kw["ok"]) and exit_code == 0 and not kw.get("error")
        if not kw.get("model_used"):
            kw["model_used"] = self.cfg.get("model")
        now = time.time()
        if started and kw.get("duration_ms") is None:
            kw["duration_ms"] = max(0, int((now - started) * 1000))
        tr = contract.turn_result(self.role, self.driver_name, exit_code,
                                  started_at=started, finished_at=now, **kw)
        try:
            contract.validate("turn_result", tr)
        except contract.ContractError as e:
            tr = contract.turn_result(self.role, self.driver_name, exit_code, ok=False,
                                      error=f"契約驗證失敗：{e}", started_at=started, finished_at=now)
        (self.state_dir / "last_turn.json").write_text(json.dumps(tr, ensure_ascii=False))
        sf.unlink(missing_ok=True)

    def take_turn_result(self) -> dict | None:
        """dispatcher 取走上一輪的 TurnResult（取走即刪，避免重複記錄）。"""
        f = self.state_dir / "last_turn.json"
        if not f.exists():
            return None
        try:
            tr = json.loads(f.read_text())
        except ValueError:
            tr = None
        f.unlink(missing_ok=True)
        return tr

    def completion_status(self) -> tuple[int | None, str]:
        code = self.state_dir / 'last_exit.txt'
        error = self.state_dir / 'last_error.txt'
        return (int(code.read_text().strip()) if code.exists() else None,
                error.read_text() if error.exists() else '')

    def parse_error(self, out: str) -> str | None:
        return None


# ---------------- tmux：互動式 agent 的退回叫醒 ----------------
def process_start(pid: int, c_locale: bool = False) -> str:
    """程序啟動時間戳（ps lstart），防 PID 重用。c_locale=True 時固定 LC_ALL=C 以便解析成 epoch。"""
    env = {**os.environ, "LC_ALL": "C"} if c_locale else None
    r = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=2, env=env)
    return r.stdout.strip()


def tmux_room_exists(session: str) -> bool:
    try:
        return subprocess.run([shutil.which("tmux") or "/opt/homebrew/bin/tmux", "has-session", "-t", "=" + session],
                              capture_output=True, timeout=3).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class Tmux(Adapter):
    level = "tmux"
    name = "tmux"
    label = "tmux"

    @property
    def target(self):
        return self.cfg.get("tmux_session", f"{_paths.tmux_prefix()}{self.role}")

    def _tmux(self, *a, **kw):
        return subprocess.run([shutil.which("tmux") or "/opt/homebrew/bin/tmux", *a], capture_output=True, text=True, **kw)

    def start(self) -> str:
        if self._tmux("has-session", "-t", "=" + self.target).returncode == 0:
            return "existing"
        cmd = self.cfg.get("start_command", "")
        env_args = []
        for k, v in self.env().items():
            if k.startswith(("MBOX_", "AAF_")) or k in ("PATH", "AAF_SERVER_STATE"):
                env_args += ["-e", f"{k}={v}"]
        self._tmux("new-session", "-d", "-s", self.target, "-c", str(self.workdir), *env_args, *( [cmd] if cmd else []))
        return "started"

    def _snapshot(self) -> str | None:
        r = self._tmux("capture-pane", "-p", "-t", f"={self.target}:")
        return r.stdout if r.returncode == 0 else None

    def health(self) -> str:
        snap = self._snapshot()
        if snap is None:
            return "down"
        f = self.state_dir / "pane.snap"
        prev = f.read_text() if f.exists() else None
        f.write_text(snap)
        return "idle" if prev == snap else "busy"   # 兩次輪詢畫面沒變視為閒置

    def wake(self, unread: int, head=None, preview: str = "") -> str:
        if self.health() != "idle":
            return "skip: 不閒置（畫面在變或 tmux 不在）"
        msg = self.cfg.get("wake_text") or f"[mbox] 你有 {unread} 則未讀，請執行 mbox inbox"
        self._tmux("send-keys", "-t", f"={self.target}:", "-l", msg)
        time.sleep(0.3)
        self._tmux("send-keys", "-t", f"={self.target}:", "Enter")
        return "sent wake text"


class Hooked(Tmux):
    name = "hook"
    """互動式 claude/codex 已裝 Stop hook：自己會在每輪結束查信箱；dispatcher 只在它閒置時補叫。"""
    level = "hook"


def system_prompt(role: str, cfg: dict) -> str:
    persona = cfg.get("persona", "")
    pf = cfg.get("persona_file")
    if pf:
        pp = Path(os.path.expanduser(pf))
        pp = _paths.resolve(pp)
        if pp.exists():
            persona = "\n\n以下是你的角色定義（SKILL）：\n" + pp.read_text()
    persona += _extra_skills(cfg)
    return (f"你是多角色團隊中的「{role}」。{persona}\n"
            "團隊溝通一律透過 mbox（shell 指令，已在 PATH）：\n"
            "  mbox inbox｜mbox ack <id> read|done｜mbox send <對象> \"內容\" [--reply <id>]\n"
            "  mbox task list｜mbox task claim <id>｜mbox task done <id> \"結果\"｜mbox agents\n"
            "長內容可用 `mbox send <對象> - <<'EOF' … EOF`。大型產出寫到檔案再用 --attach 附路徑。\n"
            "每則處理完的訊息都要 ack done；沒有要處理的就結束這一輪，不要空等。\n"
            "群組（AA Forum）的留言要回在群裡，不要改用 mbox 私訊：\n"
            "  aaf-chat post <群號> [--reply <訊息編號>] --file - <<'EOF' … EOF｜aaf-chat read <群號>｜aaf-chat --help\n"
            + _skill_index(cfg) + _team_rules_block(cfg))


SKILL_INDEX_MAX = 120           # 清單最多列幾個（超過顯示剩幾個與目錄位置）
TEAM_RULES_MAX = 60_000         # 教條全文放進系統提示的上限（字元）；超過只放前段並附路徑


def _skill_index(cfg: dict) -> str:
    """可用 skill 清單（名稱、一句話、路徑），讓角色需要時自己打開讀。
    做法同 Claude Code：只放清單、不放全文；不論哪個 agent 都一樣看得到。
    已整份放進提示的（人設、指派的 skill）不重複列。roles.json 的 skill_index: false 可關閉。"""
    if cfg.get("skill_index") is False:
        return ""
    try:
        from mbox import skills as _skills
        loaded = {sid for sid, _ in _skills.compose(cfg, cfg.get("_room"))}
        pf = cfg.get("persona_file") or ""
        import re as _re
        m = _re.match(r"^skills/([^/]+)/SKILL\.md$", pf)
        if m:
            loaded.add(m[1])
        d = _skills.skills_dir()
        rows = []
        for sid in _skills.skill_ids():
            if sid in loaded:
                continue
            f = d / sid / "SKILL.md"
            try:
                desc = _skills._description(f.read_text(encoding="utf-8", errors="replace")) or "（沒有說明）"
            except OSError:
                continue
            rows.append(f"- {sid}：{desc[:160]}（{f}）")
    except Exception:          # 清單壞掉不能讓角色叫不醒
        return ""
    if not rows:
        return ""
    more = len(rows) - SKILL_INDEX_MAX
    rows = rows[:SKILL_INDEX_MAX]
    if more > 0:
        rows.append(f"- …另有 {more} 個，見 {d}")
    return ("\n可用的 skill（需要時用檔案讀取工具打開 SKILL.md 全文再照做；與任務無關的不必讀）：\n"
            + "\n".join(rows) + "\n")


def _extra_skills(cfg: dict) -> str:
    """角色與群指派的 skill（SPEC-1.1 §3）：人設之後依序附上，重複只載一次。群號由 cfg["_room"] 帶入。"""
    try:
        from mbox import skills as _skills
        items = _skills.compose(cfg, cfg.get("_room"))
    except Exception:   # 設定壞掉不能讓角色叫不醒
        return ""
    parts = []
    for sid, f in items:
        try:
            parts.append(f"\n\n以下是指派給你的 skill「{sid}」：\n" + f.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return "".join(parts)


def _team_rules_line(cfg: dict) -> str:
    """（相容）只回路徑那一行。"""
    rel = cfg.get("team_rules") or _paths.setting("AAF_TEAM_RULES")
    if not rel:
        return ""
    return f"團隊教條：{_paths.resolve(rel)}（開工前讀）"


def _team_rules_block(cfg: dict) -> str:
    """團隊教條全文放進系統提示（不只給路徑：給路徑時角色常常沒去讀）。
    超過 TEAM_RULES_MAX 只放前段，並附路徑請角色讀完；讀不到檔案就退回只給路徑。"""
    rel = cfg.get("team_rules") or _paths.setting("AAF_TEAM_RULES")
    if not rel:
        return ""
    f = _paths.resolve(rel)
    try:
        text = f.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _team_rules_line(cfg)
    if len(text) > TEAM_RULES_MAX:
        return (f"\n以下是團隊教條（前 {TEAM_RULES_MAX} 字；全文 {len(text)} 字在 {f}，開工前讀完）：\n"
                + text[:TEAM_RULES_MAX] + "\n…（截斷）\n")
    return f"\n以下是團隊教條（{f}），一律遵守：\n" + text + "\n"


# ---------------- 互動畫面的通用活動判斷（driver 可覆寫 screen_activity） ----------------
def generic_screen_activity(tail: str) -> str | None:
    import re
    if re.search(r'Would you like to (?:run|proceed)|Do you want to proceed|Allow this (?:command|tool)', tail, re.I):
        return 'waiting'
    if re.search(r'Retrying in \d+|Reconnecting\.\.\.', tail, re.I):
        return 'retrying'
    if (re.search(r'^\s*[•◦✢✳✶✻✽·*].*(?:esc to interrupt|esc to cancel)', tail, re.M | re.I)
            or re.search(r'^\s*[✢✳✶✻✽·*]\s+[^\W\d_]+(?:…|\.{3})\s+\(\d+[hms]', tail, re.M)):
        return 'working'
    return None
