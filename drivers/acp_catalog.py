"""ACP agent 目錄與安裝（S8）：網頁「加入 agent」用。

- 內建清單：每個 agent 的 ACP 指令從哪來、怎麼裝。轉接器一律裝在 v2 的 vendor/acp（版本固定、不動全域）。
- 自訂：填任意 npm 套件名稱（可帶版本），裝到 vendor/acp，取它的 bin 當 ACP 指令。
- 安裝只在使用者確認後執行；輸出逐行寫 var/acp-install/<job>.log，網頁輪詢顯示。
"""
from __future__ import annotations

import json
import tempfile
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from drivers.base import ROOT, Adapter

VENDOR = ROOT / "vendor" / "acp"


def _work():
    from mbox import paths as _p
    w = _p.instance() / "work"
    w.mkdir(parents=True, exist_ok=True)
    return w
VENDOR_BIN = VENDOR / "node_modules" / ".bin"
NPM_NAME_RE = r"(@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]*"
NPM_SPEC_RE = re.compile(rf"^{NPM_NAME_RE}(@[0-9A-Za-z.\-+^~]+)?$")

# 內建 agent。cmd：ACP 指令（vendor bin 或 PATH 上的執行檔）；npm：要裝到 vendor/acp 的套件。
#   needs：agent 本體（轉接器只是橋，agent 本體與登入要另外準備）
CATALOG: dict[str, dict] = {
    "hermes": dict(label="Hermes", acp="hermes-acp", source="path", npm=None,
                   needs="Hermes Agent 本體（內建 hermes-acp）", login="hermes 已設定 provider"),
    "claude": dict(label="Claude Code", acp="claude-agent-acp", source="vendor",
                   npm="@agentclientprotocol/claude-agent-acp@0.86.0",
                   needs="Claude Code（claude）已登入", login="claude 登入"),
    "codex": dict(label="Codex", acp="codex-acp", source="vendor", npm="@zed-industries/codex-acp@0.16.0",
                  needs="Codex CLI 已登入（codex login）", login="codex login"),
    "pi": dict(label="pi", acp="pi-acp", source="vendor", npm="pi-acp@0.0.34",
               needs="pi coding agent（pi）與其模型設定", login="pi 的 provider 設定"),
    "agy": dict(label="Antigravity CLI（agy）", acp="agy", source="shim", npm=None,
                needs="agy 本體已登入（agy 沒有 ACP 模式，由 v2 內建轉接 drivers/agy_acp.py）", login="agy 登入"),
    "gemini": dict(label="Gemini CLI", acp="gemini", args=["--acp"], source="vendor",
                   npm="@google/gemini-cli@0.62.0",
                   needs="Gemini 登入（第一次執行 gemini 互動登入，或設 GEMINI_API_KEY）", login="gemini 登入"),
}


def _custom_file() -> Path:
    return VENDOR / "custom.json"


def custom_agents() -> dict[str, dict]:
    try:
        return json.loads(_custom_file().read_text())
    except (OSError, ValueError):
        return {}


DEFAULT_AGENT = "hermes"   # roles.json 沒寫 acp_agent 時 acp driver 用的 agent


def all_agents() -> dict[str, dict]:
    return {**CATALOG, **{k: dict(v, custom=True) for k, v in custom_agents().items()}}


def resolve_cmd(agent: str) -> list[str] | None:
    """agent id → ACP 指令；找不到（未安裝）回 None。"""
    spec = all_agents().get(agent)
    if not spec:
        return None
    exe = None
    if spec.get("source") == "shim":
        import sys as _sys
        return ([_sys.executable, str(ROOT / "drivers" / "agy_acp.py")]
                if Adapter.find_binary(spec["acp"]) else None)
    if spec.get("source") == "path":
        exe = Adapter.find_binary(spec["acp"])
    vb = VENDOR_BIN / spec["acp"]
    if not exe and vb.exists():
        exe = str(vb)
    if not exe and spec.get("source") != "vendor":
        exe = Adapter.find_binary(spec["acp"])
    if not exe and agent == "pi":
        exe = Adapter.find_binary("pi-acp")          # 全域安裝的 pi-acp；保留相容
    return [exe, *spec.get("args", [])] if exe else None


def login_status(aid: str) -> tuple[bool | None, str]:
    """agent 本體的登入狀態（只讀；不印帳號細節）。None＝無法判斷。
    「ACP 轉接器裝好了」不等於「登入了」：這裡實際問 agent 本體。"""
    import drivers as _d
    from pathlib import Path as _P
    if aid == "pi":
        f = _P.home() / ".pi" / "agent" / "models.json"
        return (True, "已設定模型（~/.pi/agent/models.json）") if f.exists() else (False, "未設定模型")
    sub = _d.DRIVERS.get(aid)
    if not sub or not getattr(sub, "auth_cmd", None):
        return None, "無法判斷"
    try:
        tmp = _P(tempfile.gettempdir()) / "civ-login-probe"
        return sub(f"_probe_{aid}", {"workdir": str(tmp / "work")}, tmp).auth_check(timeout=15)
    except Exception as e:
        return None, f"檢查失敗：{type(e).__name__}"


def status() -> list[dict]:
    out = []
    for aid, spec in all_agents().items():
        cmd = resolve_cmd(aid)
        logged, why = login_status(aid) if cmd else (None, "")
        out.append(dict(id=aid, label=spec["label"], installed=bool(cmd), cmd=cmd, npm=spec.get("npm"),
                        needs=spec.get("needs"), custom=bool(spec.get("custom")), logged_in=logged, login_detail=why,
                        install_cmd=install_command(aid) if spec.get("npm") else None))
    return out


def install_command(agent: str, npm: str | None = None) -> list[str]:
    spec = all_agents().get(agent) or {}
    pkg = npm or spec.get("npm")
    if not pkg or not NPM_SPEC_RE.match(pkg):
        raise ValueError("npm 套件名稱不合法")
    npm_bin = shutil.which("npm") or "npm"
    return [npm_bin, "install", "--no-audit", "--no-fund", "--save-exact", pkg]


def plan_custom(npm_spec: str) -> dict:
    """自訂：查 npm 套件的 bin，回傳安裝計畫（不安裝）。"""
    npm_spec = npm_spec.strip()
    if not NPM_SPEC_RE.match(npm_spec):
        raise ValueError("npm 套件名稱不合法（例：some-acp 或 @scope/pkg@1.2.3）")
    r = subprocess.run([shutil.which("npm") or "npm", "view", npm_spec, "name", "version", "bin", "--json"],
                       capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise ValueError(f"npm 查不到 {npm_spec}：{(r.stderr or r.stdout).strip()[-200:]}")
    info = json.loads(r.stdout or "{}")
    if isinstance(info, list):
        info = info[-1]
    bins = info.get("bin") or {}
    if isinstance(bins, str):
        bins = {info["name"].split("/")[-1]: bins}
    if not bins:
        raise ValueError(f"{npm_spec} 沒有可執行檔（bin），不是 ACP 轉接器")
    name, version = info["name"], info["version"]
    aid = re.sub(r"[^a-z0-9-]", "-", name.split("/")[-1].lower()).strip("-")[:24] or "custom"
    pick = next((b for b in bins if "acp" in b), next(iter(bins)))
    return dict(id=aid, label=name, npm=f"{name}@{version}", acp=pick, bins=list(bins),
                install_cmd=install_command(aid, f"{name}@{version}"))


# ---------------- 安裝工作 ----------------
_jobs: dict[str, dict] = {}
_lock = threading.Lock()


def _job_dir() -> Path:
    from mbox import paths as _p
    d = _p.var() / "acp-install"
    d.mkdir(parents=True, exist_ok=True)
    return d


def start_install(agent: str, custom: dict | None = None) -> dict:
    """確認後才呼叫。背景跑 npm install，回 job id。"""
    if custom:
        plan = custom
        cmd = install_command(plan["id"], plan["npm"])
    else:
        if agent not in CATALOG or not CATALOG[agent].get("npm"):
            raise ValueError(f"{agent} 不需要（或不能）由系統安裝")
        cmd = install_command(agent)
        plan = None
    with _lock:
        if any(j["state"] == "running" for j in _jobs.values()):
            raise ValueError("另一個安裝正在進行，請稍候")
        jid = uuid.uuid4().hex[:10]
        log = _job_dir() / f"{jid}.log"
        log.write_text("")
        job = dict(id=jid, agent=(plan or {}).get("id", agent), cmd=cmd, state="running", started=time.time(),
                   log=str(log), exit=None)
        _jobs[jid] = job
    VENDOR.mkdir(parents=True, exist_ok=True)
    if not (VENDOR / "package.json").exists():
        (VENDOR / "package.json").write_text(json.dumps({"name": "aaf-acp-adapters", "private": True,
                                                        "dependencies": {}}, indent=2))

    def run():
        with open(log, "a", encoding="utf-8") as f:
            f.write("$ " + " ".join(cmd) + f"\n（目錄：{VENDOR}）\n")
            f.flush()
            try:
                p = subprocess.Popen(cmd, cwd=VENDOR, stdout=f, stderr=subprocess.STDOUT, text=True)
                code = p.wait(timeout=900)
            except Exception as e:
                f.write(f"\n執行失敗：{type(e).__name__}: {e}\n")
                code = -1
            if code == 0 and plan:
                cur = custom_agents()
                cur[plan["id"]] = dict(label=plan["label"], acp=plan["acp"], source="vendor", npm=plan["npm"],
                                       needs="自訂 ACP 轉接器（agent 本體與登入請自行確認）")
                _custom_file().write_text(json.dumps(cur, ensure_ascii=False, indent=1))
            ok = code == 0 and resolve_cmd(job["agent"]) is not None
            f.write(f"\n結束：exit {code}；" + ("ACP 指令已就緒：" + " ".join(resolve_cmd(job["agent"]) or [])
                                              if ok else "安裝後仍找不到 ACP 指令") + "\n")
        job.update(state="done" if ok else "failed", exit=code, finished=time.time())

    threading.Thread(target=run, daemon=True).start()
    return job


def job_status(jid: str, offset: int = 0) -> dict:
    job = _jobs.get(jid)
    if not job:
        raise KeyError(jid)
    text = Path(job["log"]).read_text(encoding="utf-8", errors="replace")
    return dict(job, output=text[offset:], offset=len(text))


def probe(agent: str, timeout: float = 90) -> dict:
    """連線檢查：initialize＋session/new（不送提示，不花 token）。"""
    import queue
    cmd = resolve_cmd(agent)
    if not cmd:
        return dict(ok=False, error="尚未安裝")
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         bufsize=1, start_new_session=True, cwd=str(_work()))
    q: "queue.Queue[dict]" = queue.Queue()

    def rd():
        for line in p.stdout:
            try:
                q.put(json.loads(line))
            except ValueError:
                pass
    threading.Thread(target=rd, daemon=True).start()

    def call(i, method, params):
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params}) + "\n")
        p.stdin.flush()
        end = time.time() + timeout
        while time.time() < end:
            try:
                m = q.get(timeout=0.5)
            except queue.Empty:
                if p.poll() is not None:
                    raise RuntimeError(f"程序結束 exit {p.returncode}")
                continue
            if m.get("id") == i:
                if "error" in m:
                    raise RuntimeError(json.dumps(m["error"], ensure_ascii=False)[:300])
                return m.get("result") or {}
            if "id" in m and "method" in m:
                p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32601, "message": "probe"}}) + "\n")
                p.stdin.flush()
        raise TimeoutError(method)

    try:
        init = call(1, "initialize", {"protocolVersion": 1, "clientCapabilities": {
            "fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}})
        new = call(2, "session/new", {"cwd": str(_work()), "mcpServers": []})
        models = new.get("models") or {}
        opt = next((o for o in new.get("configOptions") or [] if o.get("category") == "model"), {})
        listed = [dict(id=m.get("modelId"), name=m.get("name") or m.get("modelId"))
                  for m in models.get("availableModels") or [] if m.get("modelId")]
        listed += [dict(id=o.get("value"), name=o.get("name") or o.get("value"))
                   for o in opt.get("options") or [] if o.get("value")]
        return dict(ok=True, agent_info=init.get("agentInfo"),
                    auth_methods=[a.get("id") for a in init.get("authMethods", [])],
                    model=models.get("currentModelId") or opt.get("currentValue"), models=listed)
    except Exception as e:
        err = ""
        try:
            p.kill()
            err = p.stderr.read()[-300:]
        except Exception:
            pass
        msg = f"{type(e).__name__}: {e}"
        if re.search(r"auth|login|unauthor|credential|api key", msg + err, re.I):
            msg += "（看起來需要登入：請先在終端完成該 agent 的登入）"
        return dict(ok=False, error=msg, stderr=err.strip()[-300:])
    finally:
        import signal
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass


# ---------------- 帳號額度（每個 agent 一筆） ----------------
def quotas(agents_in_use: set[str] | None = None) -> list[dict]:
    """已安裝的 agent 各回一筆額度；沒有額度概念或查不到的，回原因（網頁照樣列出，不會消失）。"""
    import time as _t
    from drivers import account_quota as aq
    now = _t.time()
    out = []
    for a in status():
        if not a["installed"]:
            continue
        aid = a["id"]
        item = dict(engine=a["label"], agent=aid, in_use=bool(agents_in_use and aid in agents_in_use),
                    windows=[], observed_at=None)
        try:
            if aid == "hermes":
                q = aq.copilot_quota(now)
                q["engine"] = "Hermes · " + q["engine"]
                item.update(q)
            elif aid == "claude":
                item.update(aq.hermes_usage("anthropic", now), source="Anthropic OAuth 用量")
            elif aid == "codex":
                raw = [x for x in aq.read_quota()["accounts"] if x["engine"] == "Codex"]
                if raw:
                    item.update({k: v for k, v in raw[0].items() if k != "engine"})
            elif aid == "pi":
                item.update(note="使用本機模型（omlx），沒有帳號額度")
            elif aid in ("gemini", "agy"):
                item.update(note="Google 未提供可讀的額度查詢；用量看每輪 token")
            else:
                item.update(note="此 agent 未提供額度")
        except Exception as e:
            item.update(error=f"查詢失敗：{type(e).__name__}", stale=True)
        if not item["windows"] and not item.get("note") and not item.get("error"):
            item["note"] = "目前沒有可顯示的額度"
        out.append(item)
    return out


# ---------------- 各 agent 的模型清單（建立群組／角色設定用） ----------------
_model_cache: dict[str, tuple[float, dict]] = {}
CODEX_MODELS = ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex", "gpt-6-sol", "gpt-6.1-sol"]


def agent_models(agent: str, ttl: float = 1800) -> dict:
    """{default, models:[{id,name}], source}；ACP probe 結果快取 30 分鐘。"""
    import time as _t
    hit = _model_cache.get(agent)
    if hit and _t.time() - hit[0] < ttl:
        return hit[1]
    if agent == "codex":
        # codex-acp 不回模型清單：用 roles 曾用過＋常見清單（自訂欄位仍可填）
        res = dict(default=None, models=[dict(id=m, name=m) for m in CODEX_MODELS], source="常用清單（codex-acp 不回報）")
    else:
        r = probe(agent, timeout=120)
        uniq = list({m["id"]: m for m in (r.get("models") or []) if isinstance(m, dict) and m.get("id")}.values())
        res = (dict(default=r.get("model"), models=uniq, source="ACP session/new")
               if r.get("ok") else dict(default=None, models=[], source="讀取失敗：" + r.get("error", "")[:120]))
    _model_cache[agent] = (_t.time(), res)
    return res


def model_switch(session: dict, want: str) -> tuple[str, dict, str] | None:
    """怎麼把 session 切到 want 模型（ACP 兩種回報方式擇一，config option 優先）。
    回 (method, params_without_sessionId, target)；不需切換回 None；清單裡沒有這個模型丟 ValueError。
    - configOptions 裡 category=model（pi-acp 等；它們不支援 session/set_model）
    - models.availableModels → session/set_model
    - 都沒回報（codex-acp：模型由啟動參數決定）→ None"""
    def match(ids):
        return next((i for i in ids if i == want or i.split(":", 1)[-1] == want or i.split("/", 1)[-1] == want), None)
    opt = next((o for o in session.get("configOptions") or [] if o.get("category") == "model"), None)
    if opt:
        vals = [o.get("value") for o in opt.get("options", []) if o.get("value")]
        target = match(vals)
        if not target:
            raise ValueError(f"這個 agent 沒有模型 {want}（可用：{', '.join(vals[:8])}）")
        if target == opt.get("currentValue"):
            return None
        return "session/set_config_option", {"configId": opt["id"], "value": target}, target
    models = session.get("models") or {}
    ids = [m.get("modelId") for m in models.get("availableModels") or [] if m.get("modelId")]
    if not ids:
        return None
    target = match(ids)
    if not target:
        raise ValueError(f"這個 agent 沒有模型 {want}（可用：{', '.join(ids[:8])}）")
    if target == models.get("currentModelId"):
        return None
    return "session/set_model", {"modelId": target}, target


def check_model(agent: str, model: str, timeout: float = 120) -> dict:
    """切換模型前實測：開暫時 session、set_model、送一句極短提示。回 {ok, error?}（約一次極小呼叫的 token）。"""
    import queue, signal, subprocess, threading, time as _t
    cmd = resolve_cmd(agent)
    if not cmd:
        return dict(ok=False, error="尚未安裝")
    if agent == "codex":
        cmd = cmd + ["-c", f'model="{model}"']
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                         bufsize=1, start_new_session=True, cwd=str(_work()))
    q: "queue.Queue[dict]" = queue.Queue()
    text = []
    def rd():
        for line in p.stdout:
            try:
                q.put(json.loads(line))
            except ValueError:
                pass
    threading.Thread(target=rd, daemon=True).start()
    def call(i, method, params):
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params}) + "\n"); p.stdin.flush()
        end = _t.time() + timeout
        while _t.time() < end:
            try:
                m = q.get(timeout=0.5)
            except queue.Empty:
                if p.poll() is not None:
                    raise RuntimeError("agent 結束")
                continue
            if m.get("method") == "session/update":
                u = m["params"].get("update", {})
                if u.get("sessionUpdate") == "agent_message_chunk":
                    text.append((u.get("content") or {}).get("text") or "")
                continue
            if "method" in m and "id" in m:
                p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32601, "message": "no"}}) + "\n"); p.stdin.flush()
                continue
            if m.get("id") == i:
                if "error" in m:
                    raise RuntimeError(json.dumps(m["error"], ensure_ascii=False)[:300])
                return m.get("result") or {}
        raise TimeoutError(method)
    try:
        call(1, "initialize", {"protocolVersion": 1, "clientCapabilities": {}})
        new = call(2, "session/new", {"cwd": str(_work()), "mcpServers": []})
        sid = new["sessionId"]
        target = model
        if agent != "codex":
            sw = model_switch(new, model)          # 清單裡沒有 → ValueError，下方回 ok=False
            if sw:
                method, params, target = sw
                call(3, method, {"sessionId": sid, **params})
        res = call(4, "session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "只回覆 OK 兩個字。"}]})
        from drivers.acp_host import _provider_error
        err = _provider_error("".join(text), res.get("usage") or {})
        if err or res.get("stopReason") not in (None, "end_turn"):
            return dict(ok=False, error=err or f"stopReason={res.get('stopReason')}")
        return dict(ok=True, model=target)
    except Exception as e:
        return dict(ok=False, error=f"{type(e).__name__}: {e}")
    finally:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
