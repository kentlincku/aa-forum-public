"""自我檢查與告警（SPEC-simplify-deploy P4、D4、D5）。

  python -m mbox.doctor            檢查一次並印出結果（bin/aaf doctor）
  python -m mbox.doctor --json

檢查分兩級：
  便宜檢查（dispatcher 每輪）：broker／AA Forum port、dispatcher heartbeat、var 可寫、tokens 齊全
  driver 檢查（每 10 分鐘）：roles.json 用到的每個 driver 執行 check()（例：`hermes --version`）

告警：只在狀態「正常 → 異常」時發一次，「異常 → 正常」時發一則已恢復（不重複洗版）。
送達：mbox 給 human 角色＋AA Forum 所有使用中的群（/api/system/announce，以「系統」身分貼文，不叫醒角色）。
broker 或 AA Forum 本身掛掉時送不出去，先留在 var/alerts/queue.jsonl，下次送得出去時補發。
supervisor 也把告警寫進同一個佇列。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import sys as _sys
_sys.path.insert(0, str(ROOT)) if str(ROOT) not in _sys.path else None
from mbox import paths as _paths  # noqa: E402
INST = _paths.instance()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DRIVER_CHECK_EVERY = 600
HB_STALE = 120


def home() -> Path:
    return Path(_paths.var())


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def _ports() -> tuple[int, int]:
    def port_of(url_env, port_env, default):
        if os.environ.get(port_env):
            return int(os.environ[port_env])
        url = os.environ.get(url_env, "")
        try:
            return int(url.rsplit(":", 1)[1].split("/")[0]) if url.count(":") >= 2 else default
        except ValueError:
            return default
    return port_of("MBOX_URL", "MBOX_PORT", 8775), port_of("AAF_CHAT_URL", "AAF_SERVER_PORT", 8111)


def cheap_checks(roles: dict, check_heartbeat: bool = True) -> dict[str, tuple[bool, str]]:
    h = home()
    broker_port, zk_port = _ports()
    r: dict[str, tuple[bool, str]] = {}
    r["port:broker"] = (_port_open(broker_port), f"127.0.0.1:{broker_port}")
    r["port:server"] = (_port_open(zk_port), f"127.0.0.1:{zk_port}")
    try:
        probe = h / ".doctor-write-test"
        probe.write_text("ok")
        probe.unlink()
        r["var:writable"] = (True, str(h))
    except OSError as e:
        r["var:writable"] = (False, f"{h}：{type(e).__name__}")
    missing = [x for x in list(roles) + ["server"] if not (h / "tokens" / x).exists()]
    r["tokens"] = (not missing, "齊全" if not missing else "缺：" + "、".join(missing))
    if check_heartbeat:
        hb = h / "dispatcher.heartbeat"
        if hb.exists():
            age = time.time() - hb.stat().st_mtime
            r["dispatcher:heartbeat"] = (age <= HB_STALE, f"{int(age)} 秒前")
        else:
            r["dispatcher:heartbeat"] = (False, "沒有 heartbeat 檔")
    return r


def driver_checks(roles: dict) -> dict[str, tuple[bool, str]]:
    import drivers
    r: dict[str, tuple[bool, str]] = {}
    seen: dict[tuple, tuple[bool, str]] = {}
    for role, cfg in roles.items():
        if drivers.is_manual(cfg):
            continue
        name = drivers.resolve(cfg)
        key = (name, cfg.get("bin"), cfg.get("command"))
        if key not in seen:
            try:
                ad = drivers.make(role, cfg, home())
                seen[key] = ad.check()
            except (Exception, SystemExit) as e:
                seen[key] = (False, f"{type(e).__name__}: {e}")
        r[f"driver:{name}" + (f"({cfg.get('bin')})" if cfg.get("bin") else "")] = seen[key]
    return r


def auth_checks(roles: dict, auth: dict | None = None, ssh_probe: bool = True) -> dict[str, tuple[bool, str]]:
    """認證總檢（P3）：AA Forum 帳號、角色 token、agent CLI 登入、宣告的機密與 ssh 主機。都只讀、不印機密值。

    auth 取自 roles.json 頂層的 "auth"（訂製版宣告），格式：
      {"secrets": ["my-api-token", ...],
       "ssh": [{"name": "build", "dest": "user@host", "secret": "可選，金鑰不通時改用的機密名"}]}
    """
    import stat as _stat
    h = home()
    r: dict[str, tuple[bool, str]] = {}
    zk_state = Path(os.environ.get("AAF_SERVER_STATE") or h / "server")
    acc = zk_state / "accounts.json"
    try:
        n = len(json.loads(acc.read_text()).get("users") or {}) if acc.exists() else 0
    except (ValueError, AttributeError):
        n = 0
    from mbox import paths as _p
    if (_p.setting("AAF_AUTH") or "local").strip().lower() == "os":
        import pwd
        key = Path("/etc/ssh/ssh_host_ed25519_key.pub")
        py = _p.setting("AAF_AUTH_PYTHON") or "/usr/bin/python3"
        has_pm = __import__("subprocess").run([py, "-c", "import paramiko"], capture_output=True).returncode == 0
        ok = key.exists() and has_pm
        r["auth:AA Forum 帳號"] = (ok, f"主機系統帳號登入（{pwd.getpwuid(os.geteuid()).pw_name}）" if ok else
                         ("缺 " + "、".join(x for x, c in (("sshd 主機金鑰", key.exists()), (f"{py} 的 paramiko", has_pm)) if not c)))
    else:
        r["auth:AA Forum 帳號"] = (n > 0, f"{n} 個帳號" if n else f"尚未建立（開 AA Forum 網頁 /app/ 建立，或 bin/aaf account create）；{acc}")
    bad = []
    for x in list(roles) + ["server"]:
        f = h / "tokens" / x
        if f.exists() and _stat.S_IMODE(f.stat().st_mode) != 0o600:
            bad.append(f"{x}({oct(_stat.S_IMODE(f.stat().st_mode))})")
    r["auth:token 權限"] = (not bad, "全部 0600" if not bad else "應為 0600：" + "、".join(bad) + "（chmod 600 var/tokens/*）")
    import drivers
    seen: dict[str, tuple] = {}
    for role, cfg in roles.items():
        if drivers.is_manual(cfg):
            continue
        try:
            ad = drivers.make(role, cfg, h)
        except (Exception, SystemExit) as e:
            r[f"auth:登入 {role}"] = (False, f"{type(e).__name__}: {e}")
            continue
        key = f"{drivers.resolve(cfg)}/{cfg.get('acp_agent', '')}/{cfg.get('bin', '')}"
        if key not in seen:
            try:
                seen[key] = ad.auth_check()
            except Exception as e:
                seen[key] = (False, f"{type(e).__name__}: {e}")
        ok, d = seen[key]
        if ok is not None:
            r[f"auth:登入 {key.strip('/')}"] = (bool(ok), d)
    auth = auth or {}
    from mbox import secret_store
    for name in auth.get("secrets", []):
        try:
            src = secret_store.source(name)
            r[f"auth:機密 {name}"] = (bool(src), f"存在（{src}）" if src else f"缺少：bin/aaf secret set {name}")
        except secret_store.SecretError as e:
            r[f"auth:機密 {name}"] = (False, str(e))
    for host in auth.get("ssh", []):
        label = f"auth:ssh {host.get('name') or host.get('dest')}"
        if not ssh_probe:
            continue
        try:
            p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host["dest"], "true"],
                               capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL)
            key_ok = p.returncode == 0
        except (OSError, subprocess.TimeoutExpired, KeyError) as e:
            r[label] = (False, f"{type(e).__name__}")
            continue
        if not key_ok and ("timed out" in p.stderr or "No route" in p.stderr or "Connection refused" in p.stderr
                           or "Could not resolve" in p.stderr):
            r[label] = (False, "連不到主機（網路/VPN），無法判斷認證：" + p.stderr.strip().splitlines()[-1][:80])
            continue
        if key_ok:
            r[label] = (True, "金鑰登入可通")
        elif host.get("secret"):
            src = secret_store.source(host["secret"])
            r[label] = (bool(src), f"金鑰不通，改用密碼：機密 {host['secret']} " + ("存在" if src else "缺少"))
        else:
            r[label] = (False, "金鑰登入不通（ssh-copy-id 或宣告 secret）")
    return r


# ---------------- 告警狀態與送達 ----------------
def _state_file() -> Path:
    return home() / "alerts" / "doctor_state.json"


def _queue_file() -> Path:
    return home() / "alerts" / "queue.jsonl"


def _load_state() -> dict:
    try:
        return json.loads(_state_file().read_text())
    except (OSError, ValueError):
        return {}


def _save_state(st: dict):
    f = _state_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False))
    os.replace(tmp, f)


def enqueue(key: str, level: str, text: str):
    f = _queue_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"key": key, "level": level, "text": text, "at": time.time()}, ensure_ascii=False) + "\n")


FAIL_GRACE = 2   # 連續失敗幾次才算異常（避開服務重啟的幾秒空窗；driver 檢查一次即算）


def evaluate(results: dict[str, tuple[bool, str]], grace: int = FAIL_GRACE) -> list[tuple[str, str, str]]:
    """比對上次狀態；只在轉換時產生告警。回傳 [(key, level, text)] 並更新狀態檔。
    便宜檢查要連續失敗 grace 次才告警；driver:* 檢查每 10 分鐘才一次，失敗一次就告警。"""
    st = _load_state()
    out = []
    for key, (ok, detail) in results.items():
        prev = st.get(key, {})
        was_bad = prev.get("bad", False)
        fails = 0 if ok else prev.get("fails", 0) + 1
        need = 1 if key.startswith("driver:") else grace
        bad = was_bad if (not ok and fails < need) else (not ok)
        if bad and not was_bad:
            out.append((key, "problem", f"異常：{key}（{detail}）"))
        elif not bad and was_bad:
            out.append((key, "recovered", f"已恢復：{key}（{detail}）"))
        st[key] = {"bad": bad, "fails": fails, "detail": detail, "at": time.time()}
    _save_state(st)
    return out


def _zk_announce(text: str, key: str) -> int:
    """以 AA Forum 的 __system__ 憑證呼叫 /api/system/announce：以「系統」身分貼到所有使用中的群，
    不排通知、不叫醒角色。回傳貼出的群數；失敗丟例外。"""
    state = Path(os.environ.get("AAF_SERVER_STATE") or _paths.var() / "server")
    token = json.loads((state / "credentials.json").read_text()).get("__system__")
    if not token:
        raise RuntimeError("AA Forum 尚未產生 __system__ 憑證（AA Forum 需為 v2 版並啟動過）")
    _, zk_port = _ports()
    base = os.environ.get("AAF_CHAT_URL", f"http://127.0.0.1:{zk_port}").rstrip("/")
    safe = "".join(c if c.isalnum() or c in "_.:-" else "-" for c in key)[:100]
    req = urllib.request.Request(base + "/api/system/announce", method="POST",
                                 data=json.dumps({"body": "【系統】" + text, "key": safe}).encode(),
                                 headers={"Authorization": f"Bearer {token}", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return len(json.loads(r.read()).get("rooms", []))


def flush(store, roles: dict, log=print) -> int:
    """送出佇列中的告警：mbox 給 human＋AA Forum 各群。送不出去的留在佇列。回傳送出筆數。"""
    q = _queue_file()
    if not q.exists():
        return 0
    lines = [l for l in q.read_text(encoding="utf-8").splitlines() if l.strip()]
    if not lines:
        return 0
    humans = [r for r, c in roles.items() if c.get("rank") == "human"]
    keep, sent = [], 0
    for line in lines:
        try:
            a = json.loads(line)
        except ValueError:
            continue
        ident = f"{a.get('key')}-{a.get('level')}-{int(a.get('at', 0))}"
        ok = True
        try:
            for h in humans:
                store.send({"id": "mbox-dispatcher", "rank": "human", "runtime": "system"}, h,
                           f"[aaf doctor] {a['text']}", kind="system", idem_key=f"doctor-{ident}-{h}")
        except Exception as e:
            ok = False
            log(f"告警寄 mbox 失敗（保留重試）：{type(e).__name__}: {e}")
        if ok and not a.get("zk_done"):
            try:
                _zk_announce(a["text"], ident)
                a["zk_done"] = True
            except Exception as e:
                ok = False
                a["zk_done"] = False
                log(f"告警發 AA Forum 失敗（保留重試）：{type(e).__name__}: {e}")
        if ok:
            sent += 1
        else:
            keep.append(json.dumps(a, ensure_ascii=False))
    tmp = q.with_suffix(".tmp")
    tmp.write_text("".join(l + "\n" for l in keep), encoding="utf-8")
    os.replace(tmp, q)
    return sent


class Doctor:
    """dispatcher 每輪呼叫 tick()：便宜檢查每輪、driver 檢查每 10 分鐘；轉換才告警；送出佇列。"""

    def __init__(self, driver_every: float = DRIVER_CHECK_EVERY):
        self.driver_every = driver_every
        self.last_driver = 0.0

    def tick(self, store, roles: dict, log=print):
        results = cheap_checks(roles, check_heartbeat=False)  # 自己就是 dispatcher，不查自己的 heartbeat
        if time.time() - self.last_driver >= self.driver_every:
            results.update(driver_checks(roles))
            self.last_driver = time.time()
        for key, level, text in evaluate(results):
            log(f"doctor {level}: {text}")
            enqueue(key, level, text)
        flush(store, roles, log)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="aaf doctor")
    ap.add_argument("--roles", type=Path, default=Path(_paths.roles_file()))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-auth", action="store_true", help="略過認證總檢")
    ap.add_argument("--no-ssh", action="store_true", help="認證總檢不實連 ssh 主機")
    a = ap.parse_args(argv)
    doc = json.loads(a.roles.read_text())
    roles = doc["roles"]
    res = cheap_checks(roles)
    res.update(driver_checks(roles))
    if not a.no_auth:
        res.update(auth_checks(roles, doc.get("auth"), ssh_probe=not a.no_ssh))
    if a.json:
        print(json.dumps({k: {"ok": ok, "detail": d} for k, (ok, d) in res.items()}, ensure_ascii=False, indent=1))
    else:
        for k, (ok, d) in res.items():
            print(f"{'OK  ' if ok else 'FAIL'}  {k:<28} {d}")
        q = _queue_file()
        pending = len([l for l in q.read_text().splitlines() if l.strip()]) if q.exists() else 0
        if pending:
            print(f"待送告警：{pending} 則（var/alerts/queue.jsonl）")
    return 0 if all(ok for ok, _ in res.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
