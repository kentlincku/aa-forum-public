"""AA Forum ↔ 角色執行環境的共用層（Mac 移植·單一使用者）。

取代原版散落在 app.py / agent_control.py / member_health.py 的 Linux 假設：
  - 角色名單：讀 deploy/roles.json（七角色），不再是 <role>-<owner> 寫死表
  - tmux：用本使用者預設 server（不寫死 /tmp/tmux-UID/default，macOS 在 $TMPDIR）
  - 程序樹：只用 ps（macOS 沒有 /proc）
  - 通知：寫入 mbox 信箱並交 dispatcher 叫醒，不再 tmux 打字
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # repo root
sys.path.insert(0, str(ROOT))

INST = Path(os.path.expanduser(os.environ.get('AAF_HOME') or str(ROOT)))   # 實例根（訂製版）；ROOT＝公版程式
ROLES_FILE = Path(os.environ.get('MBOX_ROLES') or INST / 'deploy' / 'roles.json')
MBOX_HOME = Path(os.environ.get('MBOX_HOME') or INST / 'var')


def setting(key, default=None):
    """環境變數優先，其次實例 .aaf.env（mbox.paths.setting）。"""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from mbox import paths
    return paths.setting(key, default)

HUMAN_FALLBACK = 'owner'   # roles.json 沒有 rank=human 時才用；正常部署一律由 roles.json 決定


def is_headless(cfg: dict) -> bool:
    """依 driver 的叫醒等級判斷（相容舊 adapter 欄位）。"""
    import sys
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import drivers
    return drivers.wake_level(cfg) == 'headless'


def load_roles() -> dict:
    return json.loads(ROLES_FILE.read_text())['roles']


def agent_roles() -> list[str]:
    """非人類角色，依 roles.json 的順序。"""
    return [r for r, c in load_roles().items() if c.get('rank') != 'human']


def human_ids() -> list[str]:
    """roles.json 中所有 rank=human 的角色（依檔案順序）。"""
    return [r for r, c in load_roles().items() if c.get('rank') == 'human']


def human_id() -> str:
    """主要使用者：AA Forum 的 Owner 帳號對應到它（第一個 rank=human）。"""
    ids = human_ids()
    return ids[0] if ids else HUMAN_FALLBACK


def role_label(role: str) -> str:
    """顯示名：roles.json 角色的 label（例「組長」、「Lead」）＋ id；沒設 label 就只顯示 id。"""
    try:
        label = (load_roles().get(role) or {}).get('label')
    except (OSError, ValueError, KeyError):
        label = None
    if not label or label.lower() == role.lower():
        return label or role          # 「Lead lead」重複就只顯示一次
    return f'{label} {role}'


def tmux_bin() -> str:
    return shutil.which('tmux') or '/opt/homebrew/bin/tmux'


def tmux_session(role: str) -> str:
    c = load_roles().get(role, {})
    return c.get('tmux_session', f"{os.environ.get('AAF_TMUX_PREFIX', 'civ-')}{role}")


def tmux(*args, timeout=5):
    return subprocess.run([tmux_bin(), *args], capture_output=True, text=True, timeout=timeout)


def session_alive(role: str) -> bool:
    try:
        return tmux('has-session', '-t', '=' + tmux_session(role), timeout=2).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def process_table() -> dict[int, tuple]:
    """pid -> (ppid, pgid, tpgid, comm, args)。macOS 與 Linux 的 ps 都支援這組欄位。"""
    out = subprocess.run(['ps', '-u', str(os.geteuid()), '-o', 'pid=,ppid=,pgid=,tpgid=,comm=,args='],
                         capture_output=True, text=True, timeout=3).stdout
    table = {}
    for line in out.splitlines():
        f = line.split(None, 5)
        if len(f) >= 5:
            comm = os.path.basename(f[4])
            table[int(f[0])] = (int(f[1]), int(f[2]), int(f[3]), comm, f[5] if len(f) > 5 else '')
    return table


def process_start(pid: int, c_locale: bool = False) -> str:
    """程序啟動時間戳（替代 /proc/<pid>/stat 第 22 欄），防 PID 重用。
    c_locale=True 時固定 LC_ALL=C 以便解析成 epoch；預設回傳原字串。"""
    env = {**os.environ, 'LC_ALL': 'C'} if c_locale else None
    r = subprocess.run(['ps', '-o', 'lstart=', '-p', str(pid)], capture_output=True, text=True, timeout=2, env=env)
    return r.stdout.strip()


def _drivers():
    import sys
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import drivers
    return drivers


def interactive_engines() -> list[str]:
    """有互動模式（能力 interactive_room 且有執行檔）的 driver 名稱。"""
    d = _drivers()
    return [n for n, c in d.DRIVERS.items() if getattr(c, 'bin_default', None)
            and 'interactive_room' in c.manifest().get('capabilities', [])]


def detect_engine(comm: str, args: str) -> str | None:
    """由各 driver 的 matches_process 判斷 tmux 房內前景程序是哪個 agent（核心不寫死名稱）。"""
    d = _drivers()
    for name in interactive_engines():
        if d.DRIVERS[name].matches_process(comm, args):
            return name
    return None


def known_engines() -> dict[str, str | None]:
    """可用的互動引擎與其執行檔（由 driver.binary() 尋找）。"""
    d = _drivers()
    return {n: d.DRIVERS[n].find_binary(d.DRIVERS[n].bin_default) for n in interactive_engines()}


# ---------------- mbox broker API（不開啟或寫入 mbox 資料庫） ----------------
def broker_api(path: str, body: dict | None = None):
    """Use the provisioned system token; HTTP failures propagate to outbox retry."""
    token_file = Path(os.environ.get('MBOX_SYSTEM_TOKEN_FILE', MBOX_HOME / 'tokens' / 'server'))
    token = token_file.read_text().strip()
    url = os.environ.get('MBOX_URL', 'http://127.0.0.1:8775').rstrip('/')
    request = urllib.request.Request(url + path,
        data=None if body is None else json.dumps(body, ensure_ascii=False).encode(),
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=float(os.environ.get('MBOX_API_TIMEOUT', '5'))) as response:
        return json.load(response)


def notify_role(role: str, text: str, idem_key: str, kind: str = 'chat',
                priority: str = 'must', priority_reason: str = 'control→must', **source) -> str:
    """Send via broker: queued for new notifications, duplicate for accepted retries.

    Both mean acceptance, not reading. Transport failures propagate; chat outbox
    keeps the pending row and retries with the same idempotency key.
    """
    if role not in agent_roles():
        raise ValueError('recipient outside roster')
    result = broker_api('/v1/notifications', dict(to=role, body=text, kind=kind, idem_key=idem_key,
                        priority=priority, priority_reason=priority_reason, **source))
    return 'duplicate' if result.get('duplicate') else 'queued'


def notification_priority(author: str, recipient: str, mentions: list, kind: str = 'chat') -> tuple[str, str]:
    """Classify once when the chat notification is queued, never in dispatcher."""
    if author in ('Owner', human_id()):
        return 'must', 'sender=owner'
    if kind == 'task':
        return 'must', 'task'
    if recipient in mentions:
        return 'must', f'mention=@{recipient}'
    return 'digest', 'default→digest'


def recent_runs(limit: int = 50) -> list[dict]:
    return broker_api(f'/v1/runs?limit={max(0,min(limit,500))}')


def last_turn(role: str, ok_only: bool = False) -> dict | None:
    """核心記錄的最近一輪 TurnResult（契約 v1）；ok_only＝最近一輪成功的。取不到回 None。"""
    from urllib.parse import quote
    rows = broker_api(f'/v1/turns?role={quote(role)}&limit=1' + ('&ok=1' if ok_only else ''))
    return rows[0] if rows else None


def unread_count(role: str) -> int:
    return next((a['unread'] for a in broker_api('/v1/agents') if a['id'] == role), 0)
