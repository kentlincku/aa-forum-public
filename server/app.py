"""AA Forum（Mac 移植·單一使用者）。資料庫與 outbox 同交易提交；投遞 at-least-once。

與原始 AA Forum 的差異：
  - 單一實例：設定有預設值（state 在 <instance>/var/server、port 8111、擁有者＝roles.json 的 human）
  - 角色名單讀 deploy/roles.json，角色 id 即 mbox 角色名（lead、builder…），不再是 <role>-<owner>
  - 通知改寫入 mbox 信箱（含留言全文），由 dispatcher 叫醒；不再經 tmux 打字，
    也因此沒有 2048 bytes／不得換行／分段的限制
  - 拿掉 SSH 登入與 HTTPS gateway；保留帳號＋密碼登入與通行碼備援
"""
import asyncio
import base64
import binascii
import contextlib
from datetime import datetime
import hashlib
import html
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import HTMLResponse, StreamingResponse, Response, RedirectResponse, JSONResponse
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, Field
from typing import Optional
from member_health import inspect_member
import runtime as rt
from fastapi.responses import FileResponse

ROOT = Path(__file__).resolve().parent
STATE = Path(os.environ.get('AAF_SERVER_STATE') or rt.MBOX_HOME / 'server')
STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
DB = STATE / 'chat.sqlite3'
CREDS = STATE / 'credentials.json'
PORT = int(os.environ.get('AAF_SERVER_PORT', 8111))
INSTANCE = os.environ.get('AAF_OWNER') or rt.human_id()
if not re.fullmatch(r'[a-z][a-z0-9_-]{0,39}', INSTANCE):
    raise ValueError('invalid AAF_OWNER')
# 由服務帳號與已驗證的實例名定位；不接受瀏覽器提供任意資料夾。
REMINDER_LIBRARY = Path(os.environ.get('AAF_REMINDER_LIBRARY', rt.INST / 'reminders'))
# 內建範本：本程式目錄下的 template_*.md，唯讀。
# 列在本帳號存檔旁邊；同名時本帳號自己存的那份優先。
REMINDER_TEMPLATES = ROOT


def reminder_template_names():
    return sorted(p.name for p in REMINDER_TEMPLATES.glob('template_*.md') if p.is_file() and not p.is_symlink())


UPLOAD_URL = os.environ.get('AAF_UPLOAD_URL', '')
TAILNET_HOST = os.environ.get('AAF_TAILNET_HOST', '')
PASSCODE_FILE = STATE / 'login_passcode.txt'
ACCOUNTS_FILE = STATE / 'accounts.json'   # 帳號→scrypt 雜湊；不存明文
TAILNET_IP = os.environ.get('AAF_TAILNET_IP', '')
TAILNET_CIDR = ipaddress.ip_network('100.64.0.0/10')  # Tailscale 私網位址範圍
ALLOWED_SOURCE_CIDRS = tuple(ipaddress.ip_network(value.strip()) for value in
                            os.environ.get('AAF_ALLOWED_SOURCE_CIDRS', '').split(',') if value.strip())
if any(network.prefixlen == 0 for network in ALLOWED_SOURCE_CIDRS):
    raise ValueError('unrestricted source network is not allowed')
LOCAL_SOURCES = {'127.0.0.1', '::1', 'testclient', 'testserver'}


def source_allowed(request) -> bool:
    """限制傳輸來源；來源限制不取代每個實例的登入驗證。"""
    client = request.client
    src = client.host if client else ''
    if src in LOCAL_SOURCES:
        return True
    try:
        address = ipaddress.ip_address(src)
        return address in TAILNET_CIDR or any(address in network for network in ALLOWED_SOURCE_CIDRS)
    except ValueError:
        return False
CREDENTIAL_LOCK = threading.Lock()
ROOM_DELIVERY_LOCK = threading.RLock()
MAX_IMAGE_BYTES = 5 * 1024 * 1024
def _limit_mb(key: str, default: int) -> int | None:
    """上限（MB）：環境變數或實例 .aaf.env；0 或負數＝不設上限（回 None）。"""
    from mbox import paths as _p
    try:
        mb = int(_p.setting(key, str(default)))
    except (TypeError, ValueError):
        mb = default
    return None if mb <= 0 else mb * 1024 * 1024


MAX_FILE_BYTES = _limit_mb('AAF_CHAT_FILE_MB', 100)       # 群聊附檔單檔上限；None＝不限
MAX_IMAGE_PIXELS = 20_000_000


def connect():
    db = sqlite3.connect(DB, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    return db


def credentials(agent=None):
    with CREDENTIAL_LOCK:
        data = json.loads(CREDS.read_text()) if CREDS.exists() else {}
        changed = False
        for name in ['Owner', '__system__'] + ([agent] if agent else []):
            if name not in data:
                data[name] = secrets.token_urlsafe(32)
                changed = True
        if changed:
            temp = CREDS.with_suffix('.tmp')
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as f:
                json.dump(data, f)
            os.replace(temp, CREDS)
        return data


def login_passcode():
    # 通行碼與 API 憑證分開保存，不進通知或前端原始碼。
    with CREDENTIAL_LOCK:
        if not PASSCODE_FILE.exists():
            fd = os.open(PASSCODE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as f:
                f.write(secrets.token_urlsafe(9))
        return PASSCODE_FILE.read_text().strip()


# ── 帳號＋密碼登入（2026-09-10·Dr. Tsai 令「輸入通行碼太麻煩，改帳密」）──
# 通行碼保留兩個用途：① 備援登入（免鎖死）② 建立／重設帳號的授權（拿得到通行碼的人＝管理者）。
# 密碼只以 scrypt 加鹽雜湊落檔（accounts.json·0600），程式任何地方不記錄明文。
ACCOUNT_NAME_RE = re.compile(r'[A-Za-z][A-Za-z0-9_.-]{1,31}')
_SCRYPT = dict(n=2**14, r=8, p=1, dklen=32)


def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT).hex()


def _read_accounts_unlocked():
    if not ACCOUNTS_FILE.exists():
        return {}
    try:
        data = json.loads(ACCOUNTS_FILE.read_text())
    except json.JSONDecodeError:
        return {}
    users = data.get('users') if isinstance(data, dict) else None
    return users if isinstance(users, dict) else {}


def list_accounts():
    with CREDENTIAL_LOCK:
        return sorted(_read_accounts_unlocked())


def save_account(username: str, password: str):
    # 一個 OS 擁有者一個資料實例；不可把另一個登入名授予本實例 Owner。
    if username != INSTANCE:
        raise HTTPException(403, '帳號不屬於此 AA Forum，請使用該帳號的專用入口')
    salt = secrets.token_bytes(16)
    digest = _hash_password(password, salt)
    with CREDENTIAL_LOCK:
        users = _read_accounts_unlocked()
        users[username] = {'salt': salt.hex(), 'hash': digest, 'updated': int(time.time())}
        temp = ACCOUNTS_FILE.with_suffix('.tmp')
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump({'users': users}, f)
        os.replace(temp, ACCOUNTS_FILE)


AUTH_MODE = (rt.setting('AAF_AUTH') or 'local').strip().lower()   # local＝AA Forum 自己的帳號；os＝本機 OS 帳號密碼
if AUTH_MODE not in ('local', 'os'):
    raise ValueError('AAF_AUTH 只能是 local 或 os')
OS_AUTH_LOCK = threading.Lock()
OS_AUTH_ATTEMPTS: list[float] = []


def os_user() -> str:
    import pwd
    return pwd.getpwuid(os.geteuid()).pw_name


def verify_os_account(username: str, password: str) -> bool:
    """AAF_AUTH=os：帳號必須是執行本服務的 OS 帳號，密碼交給本機 sshd 驗證（server/os_auth.py）。
    五分鐘最多 20 次、序列化，避免把 sshd 連線耗盡。"""
    if username != os_user():
        return False
    with OS_AUTH_LOCK:
        now = time.monotonic()
        OS_AUTH_ATTEMPTS[:] = [t for t in OS_AUTH_ATTEMPTS if now - t < 300]
        if len(OS_AUTH_ATTEMPTS) >= 20:
            raise HTTPException(429, '登入嘗試過多，請五分鐘後再試')
        OS_AUTH_ATTEMPTS.append(now)
        py = rt.setting('AAF_AUTH_PYTHON') or '/usr/bin/python3'
        try:
            r = subprocess.run([py, '-I', str(ROOT / 'os_auth.py')], text=True, capture_output=True, timeout=20,
                               input=json.dumps({'username': username, 'password': password}))
        except (OSError, subprocess.TimeoutExpired):
            raise HTTPException(503, '主機帳密驗證暫時無法使用，請稍後再試')
        if r.returncode == 3:
            raise HTTPException(503, '主機帳密驗證未就緒（缺 paramiko 或 sshd 主機金鑰）')
        return r.returncode == 0


def verify_account(username: str, password: str) -> bool:
    if AUTH_MODE == 'os':
        return verify_os_account(username, password)
    if username != INSTANCE:
        _hash_password(password, b'\x00' * 16)
        return False
    with CREDENTIAL_LOCK:
        record = _read_accounts_unlocked().get(username)
    if not record:
        _hash_password(password, b'\x00' * 16)   # 帳號不存在也花同樣時間，回應時間猜不出帳號
        return False
    try:
        expected = record['hash']; salt = bytes.fromhex(record['salt'])
    except (KeyError, TypeError, ValueError):
        return False
    return secrets.compare_digest(_hash_password(password, salt), expected)


def init():
    credentials()
    # 升級隔離邊界時撤銷舊的共用真人 token；保留助手憑證與帳密。
    # 標記寫在同份憑證 JSON，原子取代避免重啟再次輪替。
    with CREDENTIAL_LOCK:
        tokens = json.loads(CREDS.read_text())
        if tokens.get('_owner_isolation_v1') != INSTANCE:
            tokens['Owner'] = secrets.token_urlsafe(32)
            tokens['_owner_isolation_v1'] = INSTANCE
            temporary = CREDS.with_suffix('.isolation.tmp')
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as target:
                json.dump(tokens, target)
            os.replace(temporary, CREDS)
    login_passcode()
    with contextlib.closing(connect()) as db, db:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript('''
        CREATE TABLE IF NOT EXISTS agents(id TEXT PRIMARY KEY, label TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS folders(name TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS rooms(id INTEGER PRIMARY KEY, name TEXT NOT NULL,
            notify INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS members(room INTEGER REFERENCES rooms(id),
            agent TEXT REFERENCES agents(id), last_read INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(room,agent));
        CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT,
            room INTEGER REFERENCES rooms(id), author TEXT REFERENCES agents(id),
            body TEXT NOT NULL, mentions TEXT NOT NULL, reply_to INTEGER REFERENCES messages(id),
            created REAL NOT NULL, client_id TEXT NOT NULL, UNIQUE(author,client_id));
        CREATE TABLE IF NOT EXISTS outbox(message INTEGER REFERENCES messages(id),
            recipient TEXT REFERENCES agents(id), status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL,
            detail TEXT NOT NULL DEFAULT '', PRIMARY KEY(message,recipient));
        CREATE TABLE IF NOT EXISTS images(message INTEGER PRIMARY KEY REFERENCES messages(id),
            mime TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
            digest TEXT NOT NULL, content BLOB NOT NULL);
        CREATE TABLE IF NOT EXISTS files(message INTEGER PRIMARY KEY REFERENCES messages(id),
            name TEXT NOT NULL, size INTEGER NOT NULL,
            digest TEXT NOT NULL, content BLOB NOT NULL);
        CREATE INDEX IF NOT EXISTS message_room ON messages(room,id);
        CREATE INDEX IF NOT EXISTS outbox_due ON outbox(status,next_try);
        CREATE TABLE IF NOT EXISTS invitations(room INTEGER REFERENCES rooms(id),
            agent TEXT REFERENCES agents(id), invited_by TEXT REFERENCES agents(id),
            created REAL NOT NULL, PRIMARY KEY(room,agent));
        CREATE TABLE IF NOT EXISTS likes(message INTEGER REFERENCES messages(id),
            agent TEXT REFERENCES agents(id), created REAL NOT NULL,
            PRIMARY KEY(message,agent));
        CREATE TABLE IF NOT EXISTS read_receipts(message INTEGER REFERENCES messages(id),
            agent TEXT REFERENCES agents(id), created REAL NOT NULL,
            PRIMARY KEY(message,agent));
        CREATE TABLE IF NOT EXISTS room_changes(id INTEGER PRIMARY KEY,
            room INTEGER REFERENCES rooms(id), actor TEXT REFERENCES agents(id),
            before_name TEXT NOT NULL, after_name TEXT NOT NULL,
            before_state TEXT NOT NULL, after_state TEXT NOT NULL, created REAL NOT NULL);
        INSERT OR IGNORE INTO agents(id,label) VALUES('Owner','使用者');
        CREATE TABLE IF NOT EXISTS system_announcements(message INTEGER PRIMARY KEY REFERENCES messages(id));
        INSERT OR IGNORE INTO agents(id,label) VALUES('__reminder__','機械提醒');
        INSERT OR IGNORE INTO agents(id,label) VALUES('__system__','系統');
        CREATE TABLE IF NOT EXISTS reminders(room INTEGER PRIMARY KEY REFERENCES rooms(id),
            body TEXT NOT NULL, minutes INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
            next_due REAL NOT NULL, updated_by TEXT REFERENCES agents(id), updated REAL NOT NULL);
        ''')
        db.execute('''CREATE TABLE IF NOT EXISTS notification_batches(
            id TEXT PRIMARY KEY, payload TEXT NOT NULL, reminder INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', created REAL NOT NULL)''')
        # 只更新顯示名稱；Owner 身分與歷史留言的作者鍵不變，舊實例也適用。
        db.execute("UPDATE agents SET label=? WHERE id='Owner'", (INSTANCE,))
        # 舊邀請保留原紀錄；空理由表示升版前未記，不替歷史補造理由。
        if 'reason' not in {r['name'] for r in db.execute('PRAGMA table_info(invitations)')}:
            db.execute("ALTER TABLE invitations ADD COLUMN reason TEXT NOT NULL DEFAULT ''")
        if 'active' not in {r['name'] for r in db.execute('PRAGMA table_info(agents)')}:
            db.execute("ALTER TABLE agents ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
        if 'state' not in {r['name'] for r in db.execute('PRAGMA table_info(rooms)')}:
            db.execute("ALTER TABLE rooms ADD COLUMN state TEXT NOT NULL DEFAULT 'active'")
        if 'reason' not in {r['name'] for r in db.execute('PRAGMA table_info(room_changes)')}:
            db.execute("ALTER TABLE room_changes ADD COLUMN reason TEXT NOT NULL DEFAULT ''")
        if 'folder' not in {r['name'] for r in db.execute('PRAGMA table_info(rooms)')}:
            db.execute("ALTER TABLE rooms ADD COLUMN folder TEXT NOT NULL DEFAULT ''")
        if 'pinned' not in {r['name'] for r in db.execute('PRAGMA table_info(rooms)')}:
            db.execute('ALTER TABLE rooms ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0')
        db.execute('CREATE TABLE IF NOT EXISTS room_approval(room INTEGER PRIMARY KEY REFERENCES rooms(id), '
                   'enabled INTEGER NOT NULL, until TEXT, actor TEXT NOT NULL, changed REAL NOT NULL)')
        for table, fields in {
            'outbox': {'batch_id': 'TEXT', 'priority': "TEXT NOT NULL DEFAULT 'must'", 'priority_reason': "TEXT NOT NULL DEFAULT 'legacy→must'"},
            'messages': {'notification_kind': "TEXT NOT NULL DEFAULT 'chat'"},
        }.items():
            known = {r['name'] for r in db.execute(f'PRAGMA table_info({table})')}
            for field, definition in fields.items():
                if field not in known:
                    db.execute(f'ALTER TABLE {table} ADD COLUMN {field} {definition}')
        if 'reminder_part' not in {r['name'] for r in db.execute('PRAGMA table_info(outbox)')}:
            db.execute('ALTER TABLE outbox ADD COLUMN reminder_part INTEGER NOT NULL DEFAULT 0')
        # 舊週期不擅改頻率，停用並取消待送；支援的週期向後對齊原定時間。
        db.execute('UPDATE reminders SET enabled=0 WHERE minutes NOT IN (10,15,30)')
        db.execute("""UPDATE outbox SET status='cancelled',detail='unsupported reminder period'
            WHERE status='pending' AND message IN
            (SELECT m.id FROM messages m JOIN reminders r ON r.room=m.room
             WHERE m.author='__reminder__' AND r.minutes NOT IN (10,15,30))""")
        for reminder in db.execute('SELECT room,minutes,next_due FROM reminders WHERE enabled=1').fetchall():
            step = reminder['minutes'] * 60
            due = reminder['next_due']
            if due % step:
                db.execute('UPDATE reminders SET next_due=? WHERE room=?',
                           ((due // step + 1) * step, reminder['room']))
        # 舊凍結群一併停用；解凍不能復活舊排程或舊投遞。
        db.execute("UPDATE reminders SET enabled=0 WHERE room IN (SELECT id FROM rooms WHERE state='frozen')")
        db.execute("""UPDATE outbox SET status='cancelled',detail='room frozen'
            WHERE status IN ('pending','disabled') AND message IN
            (SELECT m.id FROM messages m JOIN rooms r ON r.id=m.room
             WHERE r.state='frozen' AND m.author='__reminder__')""")
    refresh_agents()


def refresh_agents():
    # 名單只來自 deploy/roles.json（部署者設定），不從 tmux 探索。
    live = set(rt.agent_roles())
    if not live:
        raise ValueError('roles.json 沒有任何角色')
    with contextlib.closing(connect()) as db, db:
        stored = {row[0] for row in db.execute('SELECT id FROM agents')}
        # roles.json 移除的角色：保留身分與歷史，只標為停用（D9）；加回時恢復。
        retired = stored - live - {'Owner', '__reminder__', '__system__'}
        for agent in retired:
            db.execute('UPDATE agents SET active=0 WHERE id=?', (agent,))
        for agent in sorted(live):
            db.execute('INSERT OR IGNORE INTO agents(id,label) VALUES(?,?)', (agent, rt.role_label(agent)))
            db.execute('UPDATE agents SET label=?,active=1 WHERE id=?', (rt.role_label(agent), agent))
            credentials(agent)
    return live


def session_alive(agent: str) -> bool:
    # 線上＝tmux 房在，或角色設定為 headless（由 dispatcher 按需啟動，不需常駐房）。
    cfg = rt.load_roles().get(agent, {})
    return rt.session_alive(agent) or rt.is_headless(cfg)


def member(db, room, who, write=False):
    if not db.execute('SELECT 1 FROM members WHERE room=? AND agent=?', (room, who)).fetchone():
        raise HTTPException(403, '不是此群成員')
    state = db.execute('SELECT state FROM rooms WHERE id=?',(room,)).fetchone()['state']
    if state=='deleted' and who!='Owner':
        raise HTTPException(404,'群組已移至垃圾桶')
    if write and state!='active':
        raise HTTPException(409,'群組已凍結、封存或移至垃圾桶，請先恢復')


def identity(request):
    token = request.headers.get('authorization', '').removeprefix('Bearer ')
    for who, value in credentials().items():
        if who.startswith('_'):
            continue
        if secrets.compare_digest(value.encode(), token.encode()):
            return who
    raise HTTPException(401, '請從本機首頁登入，或使用 CLI 憑證')


def find_mentions(body, agents):
    # Quoted text and fenced/inline code do not create notifications.
    plain = re.sub(r'```[\s\S]*?```', '', body)
    plain = '\n'.join(line for line in plain.splitlines() if not line.lstrip().startswith('>'))
    plain = re.sub(r'`[^`]*`', '', plain)
    names = {a['id']: a['id'] for a in agents}
    names.update({a['label']: a['id'] for a in agents})
    hits = re.findall(r'(?<![\w@])@([\w-]+)', plain)
    if 'all' in hits:
        return sorted({a['id'] for a in agents})
    return sorted({names[h] for h in hits if h in names})


class RoomInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    members: list[str] = Field(min_length=1, max_length=40)


class FileInput(BaseModel):
    name: str = Field(min_length=1, max_length=240)
    content: str = Field(max_length=4 * ((MAX_FILE_BYTES + 2) // 3)) if MAX_FILE_BYTES else Field()


class PostInput(BaseModel):
    notification_kind: str = Field(default='chat', pattern='^(chat|task)$')
    file: FileInput | None = None
    body: str = Field(default='', max_length=16000)
    reply_to: int | None = None
    client_id: str = Field(min_length=1, max_length=80)
    image: str | None = Field(default=None, max_length=4 * ((MAX_IMAGE_BYTES + 2) // 3))


class ReadInput(BaseModel):
    through: int = Field(ge=0)


class ConfirmReadInput(BaseModel):
    messages: list[int] = Field(min_length=1, max_length=500)


class ManageRoomInput(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    action: str | None = None
    folder: str | None = Field(default=None, max_length=40)
    pinned: bool | None = None


class FolderInput(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    original: str | None = Field(default=None, min_length=1, max_length=40)
    delete: bool = False


class NotifyInput(BaseModel):
    enabled: bool


class InviteInput(BaseModel):
    agent: str = Field(min_length=1, max_length=40)
    reason: str = Field(min_length=1, max_length=500)


class LikeInput(BaseModel):
    liked: bool


class FreezeInput(BaseModel):
    frozen: bool
    reason: str = Field(min_length=1, max_length=500)



class ReminderInput(BaseModel):
    body: str = Field(max_length=12000)
    minutes: int = Field(ge=10, le=30, strict=True)
    enabled: bool
    expected: dict | None = None


def reminder_statistics(db, room, people, start, end):
    """固定半開區間；只讀儲存的提及名單，不重解析文字或重複算同則 @。"""
    counts = {p['id']: [0, 0] for p in people if p['id'] != '__reminder__'}
    labels = {p['id']: p['label'] for p in people}
    legacy_notice = False
    rows = db.execute('''SELECT m.author,m.mentions,m.client_id,a.label FROM messages m
        JOIN agents a ON a.id=m.author WHERE m.room=? AND m.created>=? AND m.created<?
        AND m.author!='__reminder__'
        AND NOT EXISTS (SELECT 1 FROM system_announcements s WHERE s.message=m.id)''', (room, start, end))
    for row in rows:
        # 舊版沒有可信公告分類；相似 key 只觸發揭露，不拿來排除一般留言。
        if row['client_id'].startswith(('invite-', 'remove-', 'freeze-')):
            legacy_notice = True
        author = row['author']
        counts.setdefault(author, [0, 0])  # 保留區間內已離群者的發言。
        labels[author] = row['label']
        counts[author][0] += 1
        if 'Owner' in json.loads(row['mentions']):
            counts[author][1] += 1
    start_label = datetime.fromtimestamp(start).astimezone().strftime('%Y-%m-%d %H:%M %z')
    end_label = datetime.fromtimestamp(end).astimezone().strftime('%Y-%m-%d %H:%M %z')
    lines = [f'統計區間：{start_label} 至 {end_label}（不含終點）',
             f'成員｜發言則數｜提及 {INSTANCE} 的留言則數（含 @all）']
    for author, (spoken, mentioned) in sorted(counts.items()):
        lines.append(f'{labels[author]}｜{spoken}｜{mentioned}')
    if legacy_notice:
        lines.append('註：本區間有未分類的公告格式重試鍵；升版前系統公告可能計入發言數。')
    return '\n'.join(lines)


def queue_notification(db, mid, recipient, status, next_try):
    if db.execute('SELECT 1 FROM agents WHERE id=? AND active=0', (recipient,)).fetchone():
        return  # 已停用的角色不排通知
    message = db.execute('SELECT author,mentions,notification_kind FROM messages WHERE id=?', (mid,)).fetchone()
    priority, reason = rt.notification_priority(message['author'], recipient,
                                                json.loads(message['mentions']), message['notification_kind'])
    db.execute('''INSERT INTO outbox(message,recipient,status,next_try,priority,priority_reason)
                  VALUES(?,?,?,?,?,?)''', (mid,recipient,status,next_try,priority,reason))


def emit_reminder(db, room_id, rules, now, next_due, period, *, initial=False):
    """呼叫者持有同一寫入交易；留言與通知排隊一起提交。"""
    room = db.execute('SELECT notify FROM rooms WHERE id=?', (room_id,)).fetchone()
    people = [dict(r) for r in db.execute('SELECT a.* FROM agents a JOIN members m ON a.id=m.agent WHERE m.room=?', (room_id,))]
    next_label = datetime.fromtimestamp(next_due).astimezone().strftime('%Y-%m-%d %H:%M:%S %z')
    body = ('【機械提醒】\n' + rules + '\n\n'
            + ('啟用即時提醒；首則不計完整區間統計。\n' if initial else '')
            + f'下一次預定發送：{next_label}')
    if not initial:
        end = next_due - period
        body += '\n\n' + reminder_statistics(db, room_id, people, end - period, end)
    key = 'reminder-' + secrets.token_hex(16)
    mid = db.execute('INSERT INTO messages(room,author,body,mentions,created,client_id) VALUES(?,?,?,?,?,?)',
                     (room_id, '__reminder__', body, json.dumps(find_mentions(rules, people)), now, key)).lastrowid
    for person in people:
        if person['id'] not in ('Owner', '__reminder__'):
            queue_notification(db, mid, person['id'], 'pending' if room['notify'] else 'disabled', now + 2)


def run_reminders():
    """排程與留言同交易；重啟後最多補一則，不補發整段離線期間。"""
    now = time.time()
    with contextlib.closing(connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        # 每輪最多100群，避免持有SQLite寫鎖太久；其餘下一輪再處理。
        due = db.execute('SELECT * FROM reminders WHERE enabled=1 AND next_due<=? LIMIT 100', (now,)).fetchall()
        for item in due:
            room = db.execute('SELECT state,notify FROM rooms WHERE id=?', (item['room'],)).fetchone()
            owner_present = db.execute('SELECT 1 FROM members WHERE room=? AND agent=?',
                                       (item['room'], item['updated_by'])).fetchone()
            if not owner_present:
                db.execute('UPDATE reminders SET enabled=0 WHERE room=?', (item['room'],))
                continue
            # 10／15／30均整除小時；worker延遲不累積，離線只補最近完整區間。
            minutes = item['minutes']
            if minutes not in (10, 15, 30):
                db.execute('UPDATE reminders SET enabled=0 WHERE room=?', (item['room'],))
                continue
            period = minutes * 60
            end = (now // period) * period
            db.execute('UPDATE reminders SET next_due=? WHERE room=?',
                       (end + period, item['room']))
            if room['state'] != 'active':
                continue
            emit_reminder(db, item['room'], item['body'], now, end + period, period)


def deliver_once():
    # Serialize freeze with the complete delivery attempt, not only its database read.
    with ROOM_DELIVERY_LOCK:
        # Drain accepted retries and newly queued batches separately. A failed
        # attempt stops this bounded pass so the backoff schedule remains intact.
        for _ in range(20):
            if not _deliver_once():
                break


MAX_NOTIFY_BODY = 4000   # 每則留言附進通知的字數上限；超過附截斷提示，全文用 cli read


def _deliver_once():
    with contextlib.closing(connect()) as db:
        row = db.execute('''SELECT o.recipient,m.room,m.author,r.name,o.priority,o.batch_id FROM outbox o
            JOIN messages m ON m.id=o.message JOIN rooms r ON r.id=m.room
            WHERE o.status='pending' AND o.next_try<=? AND r.notify=1 AND r.state='active'
            AND EXISTS (SELECT 1 FROM members b WHERE b.room=m.room AND b.agent=o.recipient)
            ORDER BY (o.priority='digest'), o.next_try, o.message LIMIT 1''', (time.time(),)).fetchone()
        if not row:
            return
        recipient, room, author, room_name, priority, batch_id = row
        if batch_id:
            # Membership, text, classification and idempotency key were frozen
            # before the first HTTP attempt; new rows cannot join this retry.
            return _attempt_notification_batch(batch_id)
        reminder = author == '__reminder__'
        pending = [dict(r) for r in db.execute('''SELECT o.message,o.attempts,o.priority_reason,m.mentions,m.body,m.author,
            m.reply_to,a.label,f.name AS file_name,f.size AS file_size,i.mime AS image_mime FROM outbox o
            JOIN messages m ON m.id=o.message JOIN agents a ON a.id=m.author
            LEFT JOIN files f ON f.message=m.id LEFT JOIN images i ON i.message=m.id
            WHERE m.room=? AND o.recipient=? AND (m.author='__reminder__')=? AND o.status='pending'
            AND o.next_try<=? AND o.priority=? AND o.batch_id IS NULL ORDER BY o.message''' + (' LIMIT 1' if reminder else ''),
            (room, recipient, reminder, time.time(), priority))]
    if not pending:
        return
    ids = [r['message'] for r in pending]
    tagged = any(recipient in json.loads(r['mentions']) for r in pending)
    cli = 'aaf-chat'
    # 信箱沒有長度與換行限制：直接附上留言全文（各則上限 MAX_NOTIFY_BODY 字），省掉「先叫醒再讀」那一輪。
    parts = []
    for r in pending:
        body = r['body'] if len(r['body']) <= MAX_NOTIFY_BODY else r['body'][:MAX_NOTIFY_BODY] + '\n…（截斷；全文用 read 指令）'
        head = f'#{r["message"]} {r["label"]}' + (f'（回覆 #{r["reply_to"]}）' if r['reply_to'] else '')
        att = r.get('file_name') or (f'image-{r["message"]}.' + r['image_mime'].split('/')[-1] if r.get('image_mime') else None)
        if att:
            # 附件只存在 AA Forum；寫明怎麼取，不然 agent 看不到
            body += (f'\n📎 附件 {att}' + (f'（{r["file_size"]} bytes）' if r.get('file_size') else '')
                     + f'：{cli} download {room} {r["message"]} --out {att}')
        parts.append(head + '\n' + body)
    if reminder:
        text = (f'此為「{room_name}」群組的機械提醒（AA Forum · 群 #{room} · 訊息 #{ids[0]}）。無須回覆收到。\n\n'
                + pending[0]['body'] + f'\n\n指令與常設授權入口：{cli} --help')
    else:
        text = (f'AA Forum「{room_name}」群 #{room} 有新留言（訊息 {min(ids)}–{max(ids)}）。'
                f'{"有人 @ 你，請讀後回應。" if tagged else "有新證據或異議才補充，無須回覆收到。"}\n\n'
                + '\n\n'.join(parts)
                + f'\n\n回到群內回覆：{cli} post {room} --reply <訊息編號> --file -\n'
                  f'讀完逐則確認：{cli} confirm-read {room} <編號...>\n'
                  f'指令與常設授權入口：{cli} --help')
    batch_id = 'zk-batch-' + secrets.token_hex(16)
    payload = dict(role=recipient, text=text, idem_key=batch_id, priority=priority,
                   priority_reason=';'.join(dict.fromkeys(r['priority_reason'] for r in pending)),
                   source_owner=any(r['author'] == 'Owner' for r in pending),
                   source_room=room, source_messages=ids)
    # Claim all rows and persist the immutable request together, before any HTTP.
    # Another process can race our read; a partial claim is rolled back completely.
    with contextlib.closing(connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        claimed = 0
        for mid in ids:
            claimed += db.execute("""UPDATE outbox SET batch_id=?
                WHERE message=? AND recipient=? AND status='pending' AND batch_id IS NULL""",
                (batch_id,mid,recipient)).rowcount
        if claimed != len(ids):
            db.rollback()
            return False
        db.execute('INSERT INTO notification_batches(id,payload,reminder,created) VALUES(?,?,?,?)',
                   (batch_id,json.dumps(payload,ensure_ascii=False),int(reminder),time.time()))
    return _attempt_notification_batch(batch_id)


def _attempt_notification_batch(batch_id):
    with contextlib.closing(connect()) as db:
        batch = db.execute('SELECT payload,reminder FROM notification_batches WHERE id=?', (batch_id,)).fetchone()
    if batch is None:
        raise RuntimeError('notification batch missing')
    payload = json.loads(batch['payload'])
    try:
        status = rt.notify_role(**payload)
        detail = f'status={status} mbox'
    except Exception as exc:
        status, detail = 'pending', type(exc).__name__ + ': ' + str(exc)[:200]
    accepted = status in ('queued', 'duplicate')
    status = 'queued' if accepted else 'pending'
    with contextlib.closing(connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        # Update only the claimed members. New rows have no batch_id yet.
        pending = db.execute("SELECT message,recipient,attempts FROM outbox WHERE batch_id=? AND status='pending'",
                             (batch_id,)).fetchall()
        for entry in pending:
            attempts = entry['attempts'] + 1
            db.execute("""UPDATE outbox SET status=?,attempts=?,next_try=?,detail=?,reminder_part=?
                WHERE message=? AND recipient=? AND batch_id=? AND status='pending'""",
                (status,0 if accepted else attempts,
                 time.time() + (2 if accepted else min(300,5*2**min(attempts,6))),detail,
                 1 if accepted and batch['reminder'] else 0,entry['message'],entry['recipient'],batch_id))
        db.execute('UPDATE notification_batches SET status=? WHERE id=?', (status,batch_id))
    return accepted


# ---------------- 群組範圍（v2 S10）：名單同步到 broker、角色之間的 mbox 往來同步回群 ----------------
MIRROR_BODY = 600
_KIND_LABEL = {'task': '派工', 'result': '回報', 'chat': '私訊', 'system': '系統'}


def sync_room_members():
    """把每個使用中群的成員（停用者除外）告訴 broker，讓 mbox 擋下群外的人。"""
    with contextlib.closing(connect()) as db:
        rooms = {}
        for room, agent in db.execute("""SELECT m.room, m.agent FROM members m JOIN rooms r ON r.id=m.room
                JOIN agents a ON a.id=m.agent WHERE r.state='active' AND COALESCE(a.active,1)=1"""):
            rooms.setdefault(str(room), []).append(rt.human_id() if agent == 'Owner' else agent)
    try:
        rt.broker_api('/v1/room-members', {'rooms': rooms})
    except Exception as exc:
        print('room-members sync retry: ' + type(exc).__name__, file=sys.stderr, flush=True)


_READ_SINCE = [0.0]


def sync_read_progress():
    """角色處理完（ack）AA Forum 通知 = 讀過那批留言：推進它在群裡的已讀位置，outbox 標 read。
    （以前只有角色自己下 aaf-chat read 才會推進，角色多半不下 → 群裡一直顯示未讀。）"""
    try:
        q = '/v1/notifications/acked?since=' + str(_READ_SINCE[0])
        rows = rt.broker_api(q)
    except Exception:
        return
    if not rows:
        return
    with contextlib.closing(connect()) as db, db:
        for r in rows:
            try:
                ids = [int(x) for x in json.loads(r['source_messages'] or '[]')]
            except (ValueError, TypeError):
                ids = []
            if not ids:
                continue
            room, top = int(r['source_room']), max(ids)
            db.execute('UPDATE members SET last_read=MAX(last_read,?) WHERE room=? AND agent=?', (top, room, r['recipient']))
            db.execute("""UPDATE outbox SET status='read' WHERE recipient=? AND status IN ('queued','fetched')
                AND message IN (SELECT id FROM messages WHERE room=? AND id<=?)""", (r['recipient'], room, top))
    _READ_SINCE[0] = max(float(r['updated_at']) for r in rows)


def _notice_room(db):
    """「通知」群：角色寫給使用者、跟任何群無關的信放這裡（只有 Owner，不叫醒任何角色）。"""
    r = db.execute("SELECT id FROM rooms WHERE name='通知' AND state='active'").fetchone()
    if r:
        return r[0]
    rid = db.execute("INSERT INTO rooms(name,created) VALUES('通知',?)", (time.time(),)).lastrowid
    db.execute("INSERT INTO members(room,agent) VALUES(?, 'Owner')", (rid,))
    return rid


def mirror_once():
    """角色之間跟某群有關的派工／回報／私訊，以精簡格式貼回該群（系統身分，不叫醒任何人）。"""
    try:
        pending = rt.broker_api('/v1/room-mirror')
    except Exception:
        return
    done, failed = [], []
    with contextlib.closing(connect()) as db, db:
        for m in pending:
            if m['room'] == 0:
                m['room'] = _notice_room(db)
            if not db.execute("SELECT 1 FROM rooms WHERE id=? AND state='active'", (m['room'],)).fetchone():
                done.append(m['id']); continue
            cid = f"mbox-{m['id']}"
            if db.execute("SELECT 1 FROM messages WHERE client_id=? AND author='__system__'", (cid,)).fetchone():
                done.append(m['id']); continue
            body = m['body'].strip()
            if len(body) > MIRROR_BODY:
                body = body[:MIRROR_BODY].rstrip() + f'…（全文 mbox #{m["id"]}）'
            label = _KIND_LABEL.get(m['kind'], m['kind'])
            text = f"↪ {label}｜{m['sender']} → {m['recipient']}（mbox #{m['id']}）\n{body}"
            try:
                mid = db.execute('INSERT INTO messages(room,author,body,mentions,created,client_id) VALUES(?,?,?,?,?,?)',
                                 (m['room'], '__system__', text, '[]', time.time(), cid)).lastrowid
                db.execute('INSERT INTO system_announcements(message) VALUES(?)', (mid,))
                done.append(m['id'])
            except sqlite3.Error:
                failed.append(m['id'])
    if done or failed:
        try:
            rt.broker_api('/v1/room-mirror', {'done': done, 'failed': failed})
        except Exception:
            pass


def transcript_root() -> Path:
    return Path(os.environ.get('AAF_TRANSCRIPT_DIR') or rt.INST / 'outputs' / 'transcripts')


def export_transcripts(rooms=None, since=None) -> dict:
    import transcript
    with contextlib.closing(connect()) as db:
        return transcript.export(db, transcript_root(), rt.setting('AAF_TZ'), rooms=rooms, since=since)


def _transcript_minutes() -> float:
    try:
        return float(rt.setting('AAF_TRANSCRIPT_MINUTES') or 120)
    except ValueError:
        return 120


@asynccontextmanager
async def lifespan(app):
    init()
    import transcript
    sched = transcript.Scheduler(_transcript_minutes())
    async def worker():
        tick = 0
        while True:
            try:
                await asyncio.to_thread(run_reminders)
                await asyncio.to_thread(deliver_once)
                if tick % 5 == 0:
                    await asyncio.to_thread(sync_room_members)
                await asyncio.to_thread(mirror_once)
                await asyncio.to_thread(sync_read_progress)
                if sched.due():
                    await asyncio.to_thread(export_transcripts)
                tick += 1
            except Exception as exc:
                # Leave pending rows intact so a transient database error cannot kill delivery.
                print('notification worker retry: '+type(exc).__name__, file=sys.stderr, flush=True)
            await asyncio.sleep(2)
    task = asyncio.create_task(worker())
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


app = FastAPI(lifespan=lifespan)

import agent_admin  # noqa: E402  S8：網頁加入 agent（安裝 ACP、新增角色）
agent_admin._identity = lambda request: identity(request)
app.include_router(agent_admin.router)
import skill_admin  # noqa: E402
skill_admin._identity = lambda request: identity(request)
skill_admin._is_human = lambda who: is_human(who)
app.include_router(skill_admin.router)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    if request.url.path in {'/login', '/setup'}:
        # 預設 422 帶 input；帳密欄位不可回顯到錯誤回應。
        return JSONResponse({'detail': '帳密格式不符，請檢查後再試'}, status_code=422)
    return await request_validation_exception_handler(request, exc)


@app.middleware('http')
async def local_boundary(request: Request, call_next):
    if not source_allowed(request):
        return HTMLResponse('僅接受本機或私網來源', status_code=403)
    host = request.headers.get('host', '')
    host_only = host.rsplit(':', 1)[0]  # 去 port(手機直連帶 :PORT)
    # Host／Origin 是跨來源請求的防線，不能代替登入。
    allowed_hosts = {'127.0.0.1', 'localhost', 'testserver', TAILNET_HOST, TAILNET_IP}
    origin = request.headers.get('origin')
    allowed_origins = {f'http://127.0.0.1:{PORT}', f'http://localhost:{PORT}',
                       f'http://{TAILNET_HOST}:{PORT}', f'https://{TAILNET_HOST}',
                       f'http://{TAILNET_IP}:{PORT}'}
    if host_only not in allowed_hosts or (origin and origin not in allowed_origins):
        return HTMLResponse('僅接受本機或私網同源請求', status_code=403)
    response = await call_next(request)
    # 預設一律 no-store；唯一例外是新版前端帶雜湊檔名的建置產物（公開程式碼、非使用者資料）。
    if not (request.url.path.startswith('/app/assets/') and response.status_code == 200):
        response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@app.get('/')
def root():
    return RedirectResponse('/app/', status_code=307)


# 舊頁面網址導到新介面的對應頁（書籤不會壞）
_LEGACY = {'/control': '/app/dashboard', '/agents': '/app/agents', '/skills': '/app/skills', '/login': '/app/'}
for _old, _new in _LEGACY.items():
    app.add_api_route(_old, (lambda target: (lambda: RedirectResponse(target, status_code=308)))(_new), methods=['GET'], include_in_schema=False)


@app.get('/api/config')
def configuration(request: Request):
    identity(request)
    # 傳檔站已內建在同一支 AA Forum 的 /files/（2026-10-01），同源、沿用這次登入。
    import files_portal
    return {'owner': INSTANCE, 'admin_id': 'Owner',
            'upload_url': '/files/',
            'chat_file_bytes': MAX_FILE_BYTES,             # null＝不限
            'upload_bytes': files_portal.UPLOAD_BYTES,
            'version': (rt.ROOT / 'VERSION').read_text().strip() if (rt.ROOT / 'VERSION').exists() else None}


class LoginInput(BaseModel):
    # 兩種登入：帳號＋密碼（常態）／通行碼（備援）。兩組都給時先看帳密。
    username: Optional[str] = Field(default=None, min_length=1, max_length=64)
    password: Optional[str] = Field(default=None, min_length=1, max_length=200)
    passcode: Optional[str] = Field(default=None, min_length=1, max_length=200)


class SetupInput(BaseModel):
    passcode: Optional[str] = Field(default=None, min_length=1, max_length=200)   # 尚無任何帳號時可不填
    username: str = Field(min_length=2, max_length=32)
    password: str = Field(min_length=8, max_length=200)


@app.post('/login')
def login(body: LoginInput, request: Request):
    if body.username is not None and body.password is not None:
        if verify_account(body.username, body.password):
            return {'token': credentials()['Owner'], 'user': body.username, 'owner': INSTANCE}
        raise HTTPException(401, '帳號或密碼錯誤' + ('（請用這台主機的系統帳號密碼）' if AUTH_MODE == 'os' else ''))
    if body.passcode is not None:
        if secrets.compare_digest(body.passcode.encode(), login_passcode().encode()):
            return {'token': credentials()['Owner']}
        raise HTTPException(401, '通行碼錯誤')
    raise HTTPException(400, '請填帳號與密碼，或通行碼')


@app.get('/api/setup-state')
def setup_state():
    # 公開：只回「有沒有帳號」，登入頁靠它決定要不要顯示通行碼欄。不回名字、不回任何雜湊。
    if AUTH_MODE == 'os':
        return {'has_accounts': True, 'auth': 'os'}
    return {'has_accounts': bool(list_accounts())}


@app.post('/setup')
def setup(body: SetupInput):
    if AUTH_MODE == 'os':
        raise HTTPException(403, '已改用主機系統帳密登入，不需要在 AA Forum 建立或重設帳號')
    # 建立或重設帳號。已有帳號 ⇒ 以通行碼為授權（同一個實例的管理者憑證）；
    # 🔴 尚無任何帳號 ⇒ 免通行碼——窗口只到第一個帳號建好，
    #    且本路由同受 local_boundary 中介層的來源限制（本機／Tailscale／允許網段），不對公網開。
    if list_accounts():
        if body.passcode is None or not secrets.compare_digest(body.passcode.encode(), login_passcode().encode()):
            raise HTTPException(401, '通行碼錯誤')
    if not ACCOUNT_NAME_RE.fullmatch(body.username):
        raise HTTPException(400, '帳號：字母開頭，英數、底線、點、連字號，2 到 32 字')
    save_account(body.username, body.password)
    return {'ok': True, 'user': body.username}


@app.get('/api/accounts')
def accounts_list(request: Request):
    # 只列帳號名，不含任何雜湊或鹽；只有登入者可看。
    identity(request)
    return list_accounts()


@app.get('/api/agents')
def agents(request: Request):
    identity(request)
    live = refresh_agents()
    roles = rt.load_roles()
    with contextlib.closing(connect()) as db:
        return [dict(r, online=(r['id'] in live and bool(r['active']) and session_alive(r['id'])),
                     acp_agent=roles.get(r['id'], {}).get('acp_agent'), model=roles.get(r['id'], {}).get('model'))
                for r in db.execute("SELECT * FROM agents WHERE id NOT IN ('__reminder__','__system__') ORDER BY id")]


class RoomEngine(BaseModel):
    agent: str = Field(default='', max_length=100)
    model: str = Field(default='', max_length=100)


def _room_engine_view(role: str, cfg: dict, room: int) -> dict:
    sys.path.insert(0, str(rt.ROOT))
    import drivers as _drv
    from mbox.overrides import effective_cfg, room_cfg
    base = effective_cfg(role, cfg, rt.MBOX_HOME)
    lanes = _drv.supports_lanes(base) if cfg.get('driver') != 'manual' else False
    eff = room_cfg(role, base, room, rt.MBOX_HOME) if lanes else base
    return dict(role=role, per_room=lanes, overridden=bool(eff.get('_room_override')),
                acp_agent=eff.get('acp_agent'), model=eff.get('model'),
                default_agent=base.get('acp_agent'), default_model=base.get('model'))


@app.get('/api/rooms/{room}/engines')
def room_engines(room: int, request: Request):
    """本群每個角色實際使用的 agent／模型（SPEC-1.1 §6）。"""
    who = identity(request)
    roles = rt.load_roles()
    with contextlib.closing(connect()) as db:
        member(db, room, who)
        ids = [r['agent'] for r in db.execute('SELECT agent FROM members WHERE room=?', (room,))]
    return [_room_engine_view(i, roles[i], room) for i in ids if i in roles and roles[i].get('driver') != 'manual'
            and roles[i].get('rank') != 'human']


@app.post('/api/rooms/{room}/agents/{role}/engine')
def set_room_engine(room: int, role: str, data: RoomEngine, request: Request):
    """只在這個群換 agent／模型；兩者都空＝恢復預設。只限擁有者。"""
    who = identity(request)
    if who != 'Owner':
        raise HTTPException(403, '只有擁有者可以切換')
    roles = rt.load_roles()
    cfg = roles.get(role)
    with contextlib.closing(connect()) as db:
        member(db, room, who)
        if not cfg or not db.execute('SELECT 1 FROM members WHERE room=? AND agent=?', (room, role)).fetchone():
            raise HTTPException(404, '這個角色不在本群')
    view = _room_engine_view(role, cfg, room)
    if not view['per_room']:
        raise HTTPException(409, '這個角色不支援每群各自的 session，只能在 Agents 頁全域切換。')
    sys.path.insert(0, str(rt.ROOT))
    from drivers import acp_catalog
    from mbox.overrides import OverrideError, set_room_override
    agent, model = data.agent.strip(), data.model.strip()
    if agent and (agent not in acp_catalog.all_agents() or not acp_catalog.resolve_cmd(agent)):
        raise HTTPException(400, f'{agent} 尚未安裝 ACP')
    if model:
        chk = acp_catalog.check_model(agent or view['default_agent'] or acp_catalog.DEFAULT_AGENT, model)
        if not chk.get('ok'):
            raise HTTPException(400, f'{role}：模型 {model} 實測失敗，未切換：{chk.get("error", "")[:300]}')
    try:
        state = set_room_override(role, room, rt.MBOX_HOME, acp_agent=agent if agent != view['default_agent'] else '',
                                  model=model)
    except OverrideError as exc:
        raise HTTPException(400, str(exc))
    return dict(ok=True, state=state, **_room_engine_view(role, cfg, room))


@app.get('/api/rooms/{room}/reminder')
def get_reminder(room: int, request: Request):
    who = identity(request)
    with contextlib.closing(connect()) as db:
        member(db, room, who)
        row = db.execute('SELECT * FROM reminders WHERE room=?', (room,)).fetchone()
        return dict(row) if row else {'body': '', 'minutes': 30, 'enabled': False, 'next_due': None}


class ReminderFileInput(BaseModel):
    # UTF-8 檔名限制180 bytes，保留餘裕低於常見檔案系統255-byte上限。
    name: str = Field(min_length=1, max_length=180)
    body: str = Field(max_length=12000)


@app.get('/api/rooms/{room}/reminder-files')
def reminder_files(room: int, request: Request, name: str = ''):
    who = identity(request)
    with contextlib.closing(connect()) as db:
        member(db, room, who)
    if not name:
        files = []
        if REMINDER_LIBRARY.exists():
            for path in REMINDER_LIBRARY.glob('*.md'):
                valid_name = re.fullmatch(r'[\w][\w .-]*\.md', path.name) and len(path.name.encode('utf-8')) <= 180
                if valid_name and path.is_file() and not path.is_symlink():
                    files.append(path.name)
        files += [n for n in reminder_template_names() if n not in files]
        files.sort()
        return {'directory': str(REMINDER_LIBRARY), 'files': files}
    # 只收單一檔名，禁止路徑與隱藏檔；不把網頁變成任意本機檔案入口。
    if not re.fullmatch(r'[\w][\w .-]*\.md', name) or len(name.encode('utf-8')) > 180:
        raise HTTPException(422, '請使用一般 .md 檔名，不可包含路徑')
    src_path = REMINDER_LIBRARY / name
    if not os.path.lexists(src_path) and name in reminder_template_names():
        src_path = REMINDER_TEMPLATES / name
    try:
        fd = os.open(src_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise HTTPException(422, '只能載入一般文字檔')
            raw = source.read(48001)  # 對應提醒12000字的UTF-8大小上限。
        if len(raw) > 48000:
            raise HTTPException(422, '檔案超過48 KB')
        body = raw.decode('utf-8')
        if len(body) > 12000:
            raise HTTPException(422, '提醒最多12000字')
    except FileNotFoundError:
        raise HTTPException(404, '找不到這個檔案')
    except (OSError, UnicodeError):
        raise HTTPException(422, '無法讀取此 UTF-8 Markdown 檔案')
    return {'name': name, 'body': body}


@app.post('/api/rooms/{room}/reminder-files')
def save_reminder_file(room: int, data: ReminderFileInput, request: Request):
    who = identity(request)
    with contextlib.closing(connect()) as db:
        member(db, room, who)
    name = data.name.strip()
    if not name.endswith('.md'):
        name += '.md'
    if not re.fullmatch(r'[\w][\w .-]*\.md', name) or len(name.encode('utf-8')) > 180:
        raise HTTPException(422, '請使用一般 .md 檔名，不可包含路徑')
    try:
        REMINDER_LIBRARY.mkdir(parents=True, exist_ok=True)
        # 排他建立防止同名覆寫，也不追隨既有 symlink；另存新檔保留舊規範。
        with (REMINDER_LIBRARY / name).open('x', encoding='utf-8') as target:
            target.write(data.body)
    except FileExistsError:
        raise HTTPException(409, '同名檔案已存在，請修改檔名另存')
    except OSError:
        raise HTTPException(500, '無法存檔，請檢查資料夾權限與磁碟空間')
    return {'name': name, 'directory': str(REMINDER_LIBRARY)}


@app.post('/api/rooms/{room}/reminder')
def set_reminder(room: int, data: ReminderInput, request: Request):
    who = identity(request)
    if data.minutes not in (10, 15, 30):
        raise HTTPException(422, '週期只可選10、15或30分鐘')
    if not data.body.strip():
        raise HTTPException(422, '請輸入提醒文字')
    now = time.time()
    with ROOM_DELIVERY_LOCK, contextlib.closing(connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        member(db, room, who, write=True)
        previous = db.execute('SELECT * FROM reminders WHERE room=?', (room,)).fetchone()
        # set-section 讀取後若有人改設定，拒寫，不能以舊全文蓋掉別節的新內容。
        if data.expected is not None and (previous is None or
                {k:previous[k] for k in ('body','minutes','enabled')} != data.expected):
            raise HTTPException(409, '提醒設定已被修改，請重新讀取後再更新指定節')
        # 相同設定的網路重試不得撤掉剛排入的首則通知，也不重算下次時間。
        if previous and (previous['body'], previous['minutes'], bool(previous['enabled'])) == (data.body, data.minutes, data.enabled):
            return {'ok': True, 'next_due': previous['next_due'], 'sent_initial': False}
        initial = data.enabled and (previous is None or not previous['enabled'])
        period = data.minutes * 60
        next_due = (now // period + 1) * period
        db.execute('INSERT INTO reminders VALUES(?,?,?,?,?,?,?) ON CONFLICT(room) DO UPDATE SET body=excluded.body, minutes=excluded.minutes, enabled=excluded.enabled, next_due=excluded.next_due, updated_by=excluded.updated_by, updated=excluded.updated',
                   (room, data.body, data.minutes, data.enabled,
                    next_due, who, now))
        # 變更或停用後，尚未投遞的舊提醒不再送進助手房；已送達無法撤回。
        db.execute("UPDATE outbox SET status='cancelled' WHERE status='pending' AND message IN (SELECT id FROM messages WHERE room=? AND author='__reminder__')", (room,))
        if initial:
            emit_reminder(db, room, data.body, now, next_due, period, initial=True)
    return {'ok': True, 'next_due': next_due, 'sent_initial': bool(initial)}


@app.post('/api/rooms/{room}/reminder/stop')
def stop_reminder(room: int, request: Request):
    who = identity(request)
    with ROOM_DELIVERY_LOCK, contextlib.closing(connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        member(db, room, who)  # 唯讀群仍允許停止排程。
        db.execute('UPDATE reminders SET enabled=0,updated_by=?,updated=? WHERE room=?', (who, time.time(), room))
        db.execute("UPDATE outbox SET status='cancelled' WHERE status='pending' AND message IN (SELECT id FROM messages WHERE room=? AND author='__reminder__')", (room,))
    return {'ok': True}


@app.get('/api/rooms')
def rooms(request: Request):
    who = identity(request)
    with contextlib.closing(connect()) as db:
        rows = [dict(r) for r in db.execute('''SELECT r.*,b.last_read,
            (SELECT COALESCE(MAX(m.id),0) FROM messages m WHERE m.room=r.id AND m.author!='Owner') AS latest_other,
            (SELECT COUNT(*) FROM messages m WHERE m.room=r.id AND m.id>b.last_read AND m.author!=?) AS unread
            FROM rooms r JOIN members b ON r.id=b.room WHERE b.agent=?
            AND (r.state!='deleted' OR b.agent='Owner') ORDER BY r.id DESC''', (who,who))]
        for r in rows:
            ap = approval_state(db, r['id'])
            r['auto_approve'] = {'on': ap['on'], 'until': ap['until']}
        return rows


@app.get('/api/folders')
def folders(request: Request):
    who = identity(request)
    with contextlib.closing(connect()) as db:
        if who == 'Owner':
            return [r[0] for r in db.execute('SELECT name FROM folders ORDER BY name')]
        return [r[0] for r in db.execute('''SELECT DISTINCT r.folder FROM rooms r
            JOIN members b ON b.room=r.id WHERE b.agent=? AND r.state!='deleted'
            AND r.folder!='' ORDER BY r.folder''', (who,))]


@app.post('/api/folders')
def manage_folder(data: FolderInput, request: Request):
    if identity(request) != 'Owner':
        raise HTTPException(403, '資料夾管理由使用者操作')
    name = data.name.strip()
    if not name or (data.delete and data.original is not None):
        raise HTTPException(422, '請指定資料夾名稱及一項操作')
    with contextlib.closing(connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        original = data.original.strip() if data.original is not None else name
        if data.delete or data.original is not None:
            if not db.execute('SELECT 1 FROM folders WHERE name=?', (original,)).fetchone():
                raise HTTPException(404, '資料夾不存在')
        if data.delete:
            db.execute("UPDATE rooms SET folder='' WHERE folder=?", (name,))
            db.execute('DELETE FROM folders WHERE name=?', (name,))
        elif data.original is None or name != original:
            if db.execute('SELECT 1 FROM folders WHERE name=?', (name,)).fetchone():
                raise HTTPException(409, '已有同名資料夾')
            db.execute('INSERT INTO folders(name) VALUES(?)', (name,))
            if data.original is not None:
                db.execute('UPDATE rooms SET folder=? WHERE folder=?', (name, original))
                db.execute('DELETE FROM folders WHERE name=?', (original,))
    return {'ok': True}


@app.get('/api/search')
def search(request: Request, q: str = '', before: int = 0):
    who = identity(request)
    q = q.strip()
    if not q or len(q) > 200:
        raise HTTPException(422, '請輸入 1 至 200 字的搜尋文字')
    # Search literal text, not SQL wildcards. Return 50 previews plus one pagination sentinel.
    # Membership and deleted-room visibility must match the message/location endpoints.
    pattern = '%' + q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
    with contextlib.closing(connect()) as db:
        rows = [dict(r) for r in db.execute('''SELECT m.id,m.room,m.author,
            substr(m.body,1,500) AS body,r.name AS room_name FROM messages m
            JOIN rooms r ON r.id=m.room JOIN members b ON b.room=m.room
            WHERE b.agent=? AND (r.state!='deleted' OR b.agent='Owner')
            AND m.body LIKE ? ESCAPE '\\' AND (?=0 OR m.id<?)
            ORDER BY m.id DESC LIMIT 51''', (who, pattern, before, before))]
    return {'messages': rows[:50], 'has_more': len(rows) > 50}


@app.post('/api/rooms/{room}/manage')
def manage_room(room: int, data: ManageRoomInput, request: Request):
    who = identity(request)
    if who!='Owner':
        raise HTTPException(403,'群組管理由使用者操作')
    if sum(value is not None for value in (data.name, data.action, data.folder, data.pinned)) != 1:
        raise HTTPException(422,'請指定改名或一項管理操作')
    if data.action is not None and data.action not in ('archive','delete','restore'):
        raise HTTPException(422,'不支援的群組操作')
    if data.name is not None and not data.name.strip():
        raise HTTPException(422,'群名不可空白')
    with contextlib.closing(connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        member(db,room,who)
        if data.pinned is not None:
            # 釘選只改排序標記，保留資料夾、通知及唯讀狀態；不設數量上限。
            db.execute('UPDATE rooms SET pinned=? WHERE id=?', (int(data.pinned), room))
            return {'ok': True, 'pinned': data.pinned}
        if data.folder is not None:
            folder = data.folder.strip()
            if folder and not db.execute('SELECT 1 FROM folders WHERE name=?', (folder,)).fetchone():
                raise HTTPException(404, '資料夾不存在，請重新整理')
            db.execute('UPDATE rooms SET folder=? WHERE id=?', (folder, room))
            return {'ok': True, 'folder': folder}
        old = db.execute('SELECT * FROM rooms WHERE id=?',(room,)).fetchone()
        name = data.name.strip() if data.name is not None else old['name']
        state = old['state']
        if data.action=='delete':
            state='deleted'
        elif data.action=='archive':
            if state=='deleted':
                raise HTTPException(409,'垃圾桶群組請先恢復')
            state='archived'
        elif data.action=='restore':
            state='active'
        if name==old['name'] and state==old['state']:
            return {'ok':True,'state':state}
        db.execute('UPDATE rooms SET name=?,state=?,notify=CASE WHEN ?!=\'active\' THEN 0 ELSE notify END WHERE id=?',
                   (name,state,state,room))
        if state!='active':
            db.execute("UPDATE outbox SET status='cancelled' WHERE status='pending' AND message IN (SELECT id FROM messages WHERE room=?)",(room,))
        db.execute('INSERT INTO room_changes(room,actor,before_name,after_name,before_state,after_state,created) VALUES(?,?,?,?,?,?,?)',
                   (room,who,old['name'],name,old['state'],state,time.time()))
    return {'ok':True,'state':state}


@app.post('/api/rooms/{room}/freeze')
def freeze_room(room: int, data: FreezeInput, request: Request):
    who = identity(request)
    reason = data.reason.strip()
    if not reason:
        raise HTTPException(422, '請填寫凍結或重啟理由')
    with ROOM_DELIVERY_LOCK, contextlib.closing(connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        member(db, room, who)
        old = db.execute('SELECT * FROM rooms WHERE id=?', (room,)).fetchone()
        if old['state'] not in ('active', 'frozen'):
            raise HTTPException(409, '只能凍結正常群組或重啟凍結群組')
        state = 'frozen' if data.frozen else 'active'
        # 重複凍結也停用；解凍只改群狀態，不恢復舊提醒排程。
        if data.frozen:
            db.execute('UPDATE reminders SET enabled=0 WHERE room=?', (room,))
        if old['state'] == state:
            return {'ok':True, 'state':state, 'changed':False}
        now = time.time()
        db.execute('UPDATE rooms SET state=? WHERE id=?', (state,room))
        if data.frozen:
            db.execute("""UPDATE outbox SET status='cancelled',detail='room frozen'
                WHERE status IN ('pending','disabled') AND message IN
                (SELECT id FROM messages WHERE room=?)""", (room,))
        db.execute('INSERT INTO room_changes(room,actor,before_name,after_name,before_state,after_state,created,reason) VALUES(?,?,?,?,?,?,?,?)',
                   (room,who,old['name'],old['name'],old['state'],state,now,reason))
        body = f'群組狀態：{who} 已將本群{"凍結為唯讀" if data.frozen else "重啟討論"}。\n理由：{reason}'
        mid = db.execute('INSERT INTO messages(room,author,body,mentions,created,client_id) VALUES(?,?,?,?,?,?)',
                         (room,who,body,'[]',now,'freeze-'+secrets.token_hex(16))).lastrowid
        db.execute('INSERT INTO system_announcements(message) VALUES(?)', (mid,))
        return {'ok':True, 'state':state, 'changed':True}


class ApprovalInput(BaseModel):
    on: bool
    until: str | None = Field(default=None, pattern=r'^\d{4}-\d{2}-\d{2}$')
    reason: str = Field(default='', max_length=500)


def _today() -> str:
    from transcript import _tz
    return datetime.now(_tz(rt.setting('AAF_TZ'))).strftime('%Y-%m-%d')


def approval_state(db, room: int) -> dict:
    """群的自動核准狀態；until 含當日（實例時區），過期視為關。"""
    r = db.execute('SELECT * FROM room_approval WHERE room=?', (room,)).fetchone()
    if not r:
        return {'room': room, 'on': False, 'until': None, 'by': None, 'at': None, 'expired': False}
    expired = bool(r['enabled'] and r['until'] and r['until'] < _today())
    return {'room': room, 'on': bool(r['enabled']) and not expired, 'until': r['until'], 'by': r['actor'],
            'at': r['changed'], 'expired': expired}


def is_human(who: str) -> bool:
    return who == 'Owner' or who in rt.human_ids()


@app.get('/api/rooms/{room}/approval')
def get_approval(room: int, request: Request):
    """群是否自動核准。實例的核准關卡（build／上板工具等）查這裡；公版不決定哪些動作需要核准。"""
    who = identity(request)
    with contextlib.closing(connect()) as db:
        member(db, room, who)
        return approval_state(db, room)


@app.post('/api/rooms/{room}/approval')
def set_approval(room: int, data: ApprovalInput, request: Request):
    who = identity(request)
    if not is_human(who):
        raise HTTPException(403, '自動核准只能由使用者開關')
    if data.on and data.until and data.until < _today():
        raise HTTPException(422, '到期日不能早於今天')
    with ROOM_DELIVERY_LOCK, contextlib.closing(connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        member(db, room, who, write=True)
        old = approval_state(db, room)
        until = data.until if data.on else None
        if old['on'] == data.on and (not data.on or old['until'] == until):
            return {**old, 'changed': False}
        now = time.time()
        db.execute('INSERT INTO room_approval(room,enabled,until,actor,changed) VALUES(?,?,?,?,?) '
                   'ON CONFLICT(room) DO UPDATE SET enabled=excluded.enabled, until=excluded.until, '
                   'actor=excluded.actor, changed=excluded.changed', (room, int(data.on), until, who, now))
        label = INSTANCE if who == 'Owner' else who
        if data.on:
            span = f'到 {until}（含當日）' if until else '不設到期，手動關閉為止'
            body = f'自動核准：{label} 已開啟本群自動核准（{span}）。\n本群派出的工作，核准關卡會直接放行。'
        else:
            body = f'自動核准：{label} 已關閉本群自動核准。之後的工作回到逐項核准。'
        if data.reason.strip():
            body += f'\n理由：{data.reason.strip()}'
        mid = db.execute('INSERT INTO messages(room,author,body,mentions,created,client_id) VALUES(?,?,?,?,?,?)',
                         (room, who, body, '[]', now, 'approval-' + secrets.token_hex(16))).lastrowid
        db.execute('INSERT INTO system_announcements(message) VALUES(?)', (mid,))
        return {**approval_state(db, room), 'changed': True, 'message': mid}


@app.get('/api/rooms/{room}/search')
def room_search(room: int, request: Request, q: str = '', mentioned: bool = False, before: int = 0):
    who = identity(request)
    q = q.strip()
    if len(q) > 80 or (not mentioned and not q) or before < 0:
        raise HTTPException(422, '請輸入1–80字關鍵字；通知模式可不填關鍵字')
    with contextlib.closing(connect()) as db:
        member(db, room, who)
        clauses = ['m.room=?']
        args = [room]
        if q:
            escaped = q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            clauses.append("m.body LIKE ? ESCAPE '\\'")
            args.append('%' + escaped + '%')
        if mentioned:
            # 以已解析身分比對，不把內文裡的相似名字或程式碼當成點名。
            clauses.append('EXISTS (SELECT 1 FROM json_each(m.mentions) WHERE value=?)')
            args.append(who)
        if before:
            clauses.append('m.id<?')
            args.append(before)
        # 每頁50筆，額外取一筆判斷是否還有舊結果；不限制整體歷史筆數。
        rows = db.execute('SELECT m.id,m.author,m.body,m.created FROM messages m WHERE ' +
                          ' AND '.join(clauses) + ' ORDER BY m.id DESC LIMIT 51', args).fetchall()
        results = []
        for row in rows[:50]:
            # 保留命中前40字、每項最多200字，避免結果清單被長文撐滿。
            start = max(0, row['body'].lower().find(q.lower()) - 40) if q else 0
            snippet = row['body'][start:start + 200]
            results.append({'id': row['id'], 'author': row['author'], 'created': row['created'],
                            'snippet': ('…' if start else '') + snippet + ('…' if start + 200 < len(row['body']) else '')})
    return {'results': results, 'has_more': len(rows) > 50,
            'next_before': results[-1]['id'] if results else None}


@app.get('/api/messages/{message}/location')
def message_location(message: int, request: Request):
    who = identity(request)
    with contextlib.closing(connect()) as db:
        row = db.execute('''SELECT m.room FROM messages m JOIN members b ON b.room=m.room
            JOIN rooms r ON r.id=m.room WHERE m.id=? AND b.agent=?
            AND (r.state!='deleted' OR b.agent='Owner')''', (message,who)).fetchone()
        if not row:
            raise HTTPException(404, '找不到留言或沒有存取權限')
        return {'room':row['room'], 'id':message}


@app.post('/api/rooms')
def create_room(data: RoomInput, request: Request):
    if identity(request) != 'Owner':
        raise HTTPException(403, '建群由使用者操作')
    name = data.name.strip()
    if not name:
        raise HTTPException(422, '群名不可空白')
    with contextlib.closing(connect()) as db, db:
        selected = sorted(set(data.members + ['Owner']))
        valid = {r[0] for r in db.execute("SELECT id FROM agents WHERE id NOT IN ('__reminder__','__system__')")}
        if not set(selected) <= valid:
            raise HTTPException(422, '成員不存在，請重新整理名單')
        room = db.execute('INSERT INTO rooms(name,notify,created) VALUES(?,1,?)', (name,time.time())).lastrowid
        db.executemany('INSERT INTO members(room,agent) VALUES(?,?)', [(room,a) for a in selected])
        return {'id':room}


@app.get('/api/account-quota')
def account_quota(request: Request):
    if identity(request) != 'Owner':
        raise HTTPException(403, '額度與登入帳號僅供擁有者查看')
    from drivers import acp_catalog
    in_use = {c.get('acp_agent') for c in rt.load_roles().values() if c.get('acp_agent')}
    return {'accounts': acp_catalog.quotas(in_use)}


@app.get('/api/model-catalog')
def model_catalog_api(request: Request):
    if identity(request) != 'Owner':
        raise HTTPException(403, '模型清單僅供擁有者查看')
    import drivers
    # 依「有 model_catalog 能力的 driver」合併；目前只有 hermes 提供，其他 agent 不顯示目錄（可自訂輸入）
    for cls in drivers.DRIVERS.values():
        if 'model_catalog' in cls.manifest().get('capabilities', []):
            data = cls.model_catalog()
            if data:
                return data
    return dict(providers=[], default={}, warnings=['沒有 driver 提供模型目錄'])


def _session_rows(role: str) -> list[dict]:
    """角色的工作階段（SPEC-1.1 §2）：預設＋各群。只讀狀態檔，不碰主機。"""
    import json as _json
    import drivers.acp as _acp
    home = rt.MBOX_HOME
    rows = []
    for r, lane, d in _acp._host_dirs(home, role):
        def _read(name):
            f = d / name
            try:
                return f.read_text().strip()
            except OSError:
                return None
        pid = _read('acp_host.pid')
        alive = False
        if pid and pid.isdigit():
            try:
                os.kill(int(pid), 0)
                alive = True
            except OSError:
                pass
        usage = None
        try:
            u = _json.loads((d / 'acp_usage.json').read_text())
            if isinstance(u.get('used'), int) and isinstance(u.get('size'), int) and u['size']:
                usage = round(100 * u['used'] / u['size'])
        except (OSError, ValueError):
            pass
        times = [f.stat().st_mtime for f in (d / 'turns.log', d / 'acp_host.spec.json') if f.exists()]
        state = ('busy' if (d / 'acp_pending').exists() and alive else 'open' if alive
                 else 'parked' if (d / 'acp_host.parked').exists() else 'closed')
        rows.append(dict(room=lane, state=state, session=_read('acp_session'), context_percent=usage,
                         last_activity=max(times) if times else None,
                         recall_pending=(d / 'recall_pending').exists()))
    return rows


@app.get('/api/agents/{role}/sessions')
def agent_sessions(role: str, request: Request):
    """控制台：角色在各群的工作階段。"""
    if not is_human(identity(request)):
        raise HTTPException(403, '只有使用者可查看')
    if role not in rt.load_roles():
        raise HTTPException(404, '沒有這個角色')
    with contextlib.closing(connect()) as db:
        names = {r['id']: r['name'] for r in db.execute('SELECT id,name FROM rooms')}
    rows = _session_rows(role)
    for r in rows:
        r['room_name'] = names.get(r['room']) if r['room'] is not None else '（不屬於任何群）'
    return {'role': role, 'sessions': rows}


@app.post('/api/agents/{role}/sessions/{room}/close')
def close_room_session(role: str, room: int, request: Request):
    """關閉某群的工作階段（不寫交棒檔、不接回）：下次這群有信時開全新 session。"""
    if not is_human(identity(request)):
        raise HTTPException(403, '只有使用者可操作')
    cfg = rt.load_roles().get(role)
    if cfg is None:
        raise HTTPException(404, '沒有這個角色')
    import drivers as _drivers
    if not _drivers.supports_lanes(cfg):
        raise HTTPException(409, '這個角色不是每群一個工作階段')
    d = rt.MBOX_HOME / 'roles' / role / 'rooms' / str(room)
    if not d.is_dir():
        raise HTTPException(404, '這個群沒有工作階段')
    ad = _drivers.make(role, cfg, rt.MBOX_HOME, lane=room)
    if ad.health() == 'busy':
        raise HTTPException(409, '這個工作階段正在跑，等它跑完再關')
    ad.shutdown()
    (d / 'acp_host.parked').write_text(str(time.time()))
    (d / 'acp_session').unlink(missing_ok=True)
    (d / 'recall_pending').unlink(missing_ok=True)
    # 主機開啟時讀 acp_session 決定 load 或 new：檔案刪掉就是下次開新 session（核心的對照表下一輪會被新 id 覆蓋）
    return {'ok': True, 'role': role, 'room': room}


@app.get('/api/runs')
def recent_runs(request: Request, limit: int = 50):
    if identity(request) != 'Owner':
        raise HTTPException(403, '只有擁有者可查看執行記錄')
    try:
        return {'runs': rt.recent_runs(max(0,min(limit,500)))}
    except Exception as exc:
        raise HTTPException(503, f'執行記錄暫不可用（{type(exc).__name__}）') from exc


@app.get('/api/rooms/{room}/health')
def room_health(room: int, request: Request):
    who = identity(request)
    allowed = refresh_agents()
    with contextlib.closing(connect()) as db:
        member(db, room, who)
        agents = [dict(row) for row in db.execute(
            'SELECT a.id,a.label FROM agents a JOIN members m ON m.agent=a.id WHERE m.room=?', (room,))
                  if row['id'] in allowed]
    return {'members': [dict(inspect_member(a['id'], INSTANCE), label=a['label']) for a in agents]}


@app.get('/api/rooms/{room}/messages')
def messages(room: int, request: Request, after: int = 0):
    who = identity(request)
    with contextlib.closing(connect()) as db:
        member(db, room, who)
        rows = [dict(r) for r in db.execute('SELECT * FROM messages WHERE room=? AND id>? ORDER BY id LIMIT 500', (room,after))]
        people = [dict(r) for r in db.execute('''SELECT a.*,b.last_read FROM members b
            JOIN agents a ON a.id=b.agent WHERE b.room=?''', (room,))]
        # Fetch relations for this page in five queries, independent of page size.
        # Separate grouped queries avoid multiplying likes/readers/outbox via JOINs.
        relations = {key: {} for key in ('image', 'file', 'delivery', 'likes', 'read_by')}
        if rows:
            ids = [row['id'] for row in rows]
            slots = ','.join('?' for _ in ids)
            queries = {
                'image': f'SELECT message,mime,width,height FROM images WHERE message IN ({slots})',
                'file': f'SELECT message,name,size FROM files WHERE message IN ({slots})',
                'delivery': f'SELECT message,recipient,status,attempts,detail FROM outbox WHERE message IN ({slots}) ORDER BY message,recipient',
                'likes': f"""SELECT l.message,a.id,a.label FROM likes l JOIN agents a ON a.id=l.agent
                    WHERE l.message IN ({slots}) ORDER BY l.message,a.id""",
                'read_by': f"""SELECT r.message,a.id,a.label,r.created FROM read_receipts r
                    JOIN agents a ON a.id=r.agent WHERE r.message IN ({slots}) ORDER BY r.message,a.id""",
            }
            for key, query in queries.items():
                for related in db.execute(query, ids):
                    item = dict(related)
                    mid = item.pop('message')
                    relations[key].setdefault(mid, []).append(item)
        for row in rows:
            mid = row['id']
            row['mentions'] = json.loads(row['mentions'])
            for key in ('image', 'file'):
                items = relations[key].get(mid, [])
                row[key] = dict(items[0], url=f'/api/rooms/{room}/messages/{mid}/{key}') if items else None
            for key in ('delivery', 'likes', 'read_by'):
                row[key] = relations[key].get(mid, [])
        return {'messages':rows,'members':people, 'has_more':len(rows)==500}


@app.get('/api/rooms/{room}/messages/{message}/image')
def message_image(room: int, message: int, request: Request):
    who = identity(request)
    with contextlib.closing(connect()) as db:
        member(db, room, who)
        image = db.execute('''SELECT i.mime,i.content FROM images i JOIN messages m ON m.id=i.message
            WHERE m.room=? AND m.id=?''', (room,message)).fetchone()
        if not image:
            raise HTTPException(404, '圖片不屬於本群或不存在')
        return Response(image['content'], media_type=image['mime'],
                        headers={'Cache-Control':'private, no-store', 'X-Content-Type-Options':'nosniff'})


@app.get('/api/rooms/{room}/messages/{message}/file')
def message_file(room: int, message: int, request: Request):
    from urllib.parse import quote
    who = identity(request)
    with contextlib.closing(connect()) as db:
        member(db, room, who)
        file = db.execute('''SELECT f.name,f.content FROM files f JOIN messages m ON m.id=f.message
            WHERE m.room=? AND m.id=?''', (room,message)).fetchone()
        if not file:
            raise HTTPException(404, '附件不存在或不屬於本群')
        return Response(file['content'], media_type='application/octet-stream', headers={
            'Content-Disposition': "attachment; filename=download; filename*=UTF-8''" + quote(file['name'], safe=''),
            'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'private, no-store',
            'Content-Security-Policy': "sandbox; default-src 'none'"})


@app.post('/api/rooms/{room}/members')
def invite_member(room: int, data: InviteInput, request: Request):
    who = identity(request)
    reason = data.reason.strip()
    if not reason:
        raise HTTPException(422, '請填寫本群討論需要的邀請理由')
    with contextlib.closing(connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        member(db, room, who, write=True)
        if not db.execute("SELECT 1 FROM agents WHERE id=? AND id NOT IN ('__reminder__','__system__')", (data.agent,)).fetchone():
            raise HTTPException(422, '只能邀請既有助手，請重新整理名單')
        if db.execute('SELECT 1 FROM members WHERE room=? AND agent=?', (room,data.agent)).fetchone():
            return {'ok':True, 'added':False}
        # 與建群的上限一致：40 位助手加使用者；交易避免同時邀請超額。
        if db.execute('SELECT COUNT(*) FROM members WHERE room=?', (room,)).fetchone()[0] >= 41:
            raise HTTPException(422, '群組已達 41 人上限')
        # 起點不是確認已讀：只跳過入群前的自動收訊，不建立 read_receipts。
        # 同一交易內先取本群最新 ID，再寫入群告知；既有成員已在上方返回。
        start = db.execute('SELECT COALESCE(MAX(id),0) FROM messages WHERE room=?', (room,)).fetchone()[0]
        db.execute('INSERT INTO members(room,agent,last_read) VALUES(?,?,?)', (room,data.agent,start))
        now = time.time()
        db.execute('INSERT INTO invitations(room,agent,invited_by,created,reason) VALUES(?,?,?,?,?)',
                   (room,data.agent,who,now,reason))
        # 入群、稽核與群內告知一起提交；失敗全回滾，重試不會多一則公告。
        body = f'入群告知：{who} 邀請 @{data.agent} 加入本群。\n理由：{reason}\n新成員自本則起接收新訊息；完整歷史仍可主動查閱。'
        mid = db.execute('INSERT INTO messages(room,author,body,mentions,created,client_id) VALUES(?,?,?,?,?,?)',
                         (room,who,body,json.dumps([data.agent]),now,'invite-'+secrets.token_hex(16))).lastrowid
        db.execute('INSERT INTO system_announcements(message) VALUES(?)', (mid,))
        enabled = db.execute('SELECT notify FROM rooms WHERE id=?',(room,)).fetchone()[0]
        for person in db.execute('SELECT agent FROM members WHERE room=?',(room,)).fetchall():
            if person['agent'] not in (who, 'Owner'):
                queue_notification(db, mid,person['agent'],'pending' if enabled else 'disabled',now+2)
        # 新成員可讀完整歷史，但不補送歷史通知；後續留言沿用群通知設定。
        return {'ok':True, 'added':True, 'announcement':mid}


class AnnounceInput(BaseModel):
    body: str = Field(min_length=1, max_length=4000)
    key: str = Field(min_length=1, max_length=120, pattern=r'^[A-Za-z0-9_.:-]+$')


@app.post('/api/system/announce')
def system_announce(data: AnnounceInput, request: Request):
    """系統公告（doctor／supervisor 告警）：以「系統」身分貼到所有使用中的群；不排通知、不叫醒任何角色。
    只接受 credentials.json 的 __system__ 憑證（identity() 不認 _ 開頭的名字，一般 API 用不了它）。"""
    _system_auth(request)
    posted = []
    with contextlib.closing(connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        for (room,) in db.execute("SELECT id FROM rooms WHERE state='active'").fetchall():
            cid = f'sys-{data.key}-{room}'[:80]
            if db.execute("SELECT 1 FROM messages WHERE author='__system__' AND client_id=?", (cid,)).fetchone():
                continue
            mid = db.execute('INSERT INTO messages(room,author,body,mentions,created,client_id) VALUES(?,?,?,?,?,?)',
                             (room, '__system__', data.body, '[]', time.time(), cid)).lastrowid
            db.execute('INSERT INTO system_announcements(message) VALUES(?)', (mid,))
            posted.append(room)
    return {'ok': True, 'rooms': posted}


class RosterInput(BaseModel):
    added: list[str] = Field(default_factory=list, max_length=40)
    removed: list[str] = Field(default_factory=list, max_length=40)


def _system_auth(request):
    token = request.headers.get('authorization', '').removeprefix('Bearer ')
    expected = credentials().get('__system__', '')
    if not expected or not secrets.compare_digest(expected.encode(), token.encode()):
        raise HTTPException(401, '需要系統憑證')


@app.post('/api/system/roster')
def system_roster(data: RosterInput, request: Request):
    """roles.json 熱重載後由 dispatcher 呼叫（D8、D9）。冪等：重送不會重複入群或重複公告。
    新增：加入所有使用中的群（自入群起收新訊息）並公告；移除：標停用並公告（成員與歷史保留）。"""
    _system_auth(request)
    live = refresh_agents()
    joined, retired = [], []
    with contextlib.closing(connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        rooms = [r[0] for r in db.execute("SELECT id FROM rooms WHERE state='active'")]
        now = time.time()
        def announce(room, body, key):
            cid = f'sys-{key}-{room}'[:80]
            if db.execute("SELECT 1 FROM messages WHERE author='__system__' AND client_id=?", (cid,)).fetchone():
                return
            mid = db.execute('INSERT INTO messages(room,author,body,mentions,created,client_id) VALUES(?,?,?,?,?,?)',
                             (room, '__system__', body, '[]', now, cid)).lastrowid
            db.execute('INSERT INTO system_announcements(message) VALUES(?)', (mid,))
        for agent in data.added:
            if agent not in live:
                continue
            for room in rooms:
                if db.execute('SELECT 1 FROM members WHERE room=? AND agent=?', (room, agent)).fetchone():
                    continue
                start = db.execute('SELECT COALESCE(MAX(id),0) FROM messages WHERE room=?', (room,)).fetchone()[0]
                db.execute('INSERT INTO members(room,agent,last_read) VALUES(?,?,?)', (room, agent, start))
                announce(room, f'【系統】新角色 @{agent} 依 roles.json 加入本群；自本則起接收新訊息。', f'join-{agent}')
                joined.append([room, agent])
        for agent in data.removed:
            if agent in live:
                continue
            db.execute('UPDATE agents SET active=0 WHERE id=?', (agent,))
            for room in rooms:
                if db.execute('SELECT 1 FROM members WHERE room=? AND agent=?', (room, agent)).fetchone():
                    announce(room, f'【系統】角色 {agent} 已從 roles.json 移除，標為停用（歷史保留，不再通知）。', f'retire-{agent}')
                    retired.append([room, agent])
            db.execute("""UPDATE outbox SET status='cancelled',detail='recipient retired'
                          WHERE recipient=? AND status='pending'""", (agent,))
    return {'ok': True, 'joined': joined, 'retired': retired}


@app.post('/api/rooms/{room}/messages/{message}/like')
def like_message(room: int, message: int, data: LikeInput, request: Request):
    who = identity(request)
    with contextlib.closing(connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        member(db, room, who, write=True)
        if not db.execute('SELECT 1 FROM messages WHERE room=? AND id=?', (room,message)).fetchone():
            raise HTTPException(404, '留言不屬於本群或不存在')
        # 傳目標狀態，不用 toggle；網路重試不會把剛按的讚取消。
        if data.liked:
            db.execute('INSERT OR IGNORE INTO likes(message,agent,created) VALUES(?,?,?)',
                       (message,who,time.time()))
        else:
            db.execute('DELETE FROM likes WHERE message=? AND agent=?', (message,who))
    # 反應不寫 outbox、不更動已讀；既有畫面刷新會取到結果。
    return {'ok':True, 'liked':data.liked}


@app.post('/api/rooms/{room}/messages')
def post(room: int, data: PostInput, request: Request):
    who = identity(request)
    if not data.body.strip() and data.image is None and data.file is None:
        raise HTTPException(422, '請輸入留言、貼圖或選擇檔案')
    # 在解碼前查身分與群權限；不信任檔名、剪貼簿 MIME 或副檔名。
    with contextlib.closing(connect()) as db:
        member(db, room, who, write=True)
    from mbox import hooks as _hooks
    _ev = {'type': 'chat.post', 'sender': rt.human_id() if who == 'Owner' else who,
           'rank': 'human' if who == 'Owner' else None, 'to': None, 'room': room,
           'kind': data.notification_kind, 'body': data.body, 'reply_to': data.reply_to}
    _why = _hooks.run_pre(_ev)
    if _why:
        raise HTTPException(422, f'規則擋下：{_why}')
    attachment = None
    digest = None
    file_content = None
    file_digest = None
    if data.file is not None:
        name = data.file.name
        bad_path = name in ('.','..') or any(c in name for c in '/\\')
        bad_control = any(ord(c)<32 or ord(c)==127 for c in name)
        if not name.strip() or bad_path or bad_control:
            raise HTTPException(422, '檔名不可含路徑或控制字元')
        try:
            file_content = base64.b64decode(data.file.content, validate=True)
        except (ValueError, binascii.Error):
            raise HTTPException(422, '檔案內容編碼無效')
        if MAX_FILE_BYTES and len(file_content) > MAX_FILE_BYTES:
            raise HTTPException(413, f'檔案不可超過 {MAX_FILE_BYTES // 1048576} MB')
        file_digest = hashlib.sha256(file_content).hexdigest()
    if data.image is not None:
        try:
            raw = base64.b64decode(data.image, validate=True)
            if not raw or len(raw) > MAX_IMAGE_BYTES:
                raise ValueError('size')
            digest = hashlib.sha256(raw).hexdigest()
            with Image.open(io.BytesIO(raw)) as source:
                if source.format not in ('PNG','JPEG','WEBP') or source.width * source.height > MAX_IMAGE_PIXELS:
                    raise ValueError('format or pixels')
                source.load()
                # 只保存重新編碼的靜態像素，去掉 EXIF、尾隨內容及動畫。
                oriented = ImageOps.exif_transpose(source)
                clean = oriented.convert('RGBA' if 'A' in oriented.getbands() or 'transparency' in oriented.info else 'RGB')
                clean.info.clear()
                output = io.BytesIO()
                clean.save(output, format='PNG')
                content = output.getvalue()
                if len(content) > MAX_IMAGE_BYTES:
                    raise ValueError('normalized size')
                attachment = ('image/png', clean.width, clean.height, digest, content)
        except (ValueError, binascii.Error, OSError, UnidentifiedImageError, Image.DecompressionBombError):
            raise HTTPException(422, '圖片須為有效 PNG／JPEG／WebP，原檔與轉存後各限 5 MB、最多 2000 萬像素')
    with contextlib.closing(connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        member(db, room, who)
        member(db, room, who, write=True)
        old = db.execute('SELECT * FROM messages WHERE author=? AND client_id=?', (who,data.client_id)).fetchone()
        if old:
            old_image = db.execute('SELECT digest FROM images WHERE message=?', (old['id'],)).fetchone()
            old_file = db.execute('SELECT name,digest FROM files WHERE message=?', (old['id'],)).fetchone()
            expected_file = {'name':data.file.name, 'digest':file_digest} if data.file is not None else None
            if (dict(old_file) if old_file else None) != expected_file:
                raise HTTPException(409, '重試鍵已用於其他留言')
            if old['room']!=room or old['body']!=data.body or old['notification_kind']!=data.notification_kind or old['reply_to']!=data.reply_to or (old_image['digest'] if old_image else None)!=digest:
                raise HTTPException(409, '重試鍵已用於其他留言')
            return {'id':old['id']}
        if data.reply_to is not None and not db.execute('SELECT 1 FROM messages WHERE room=? AND id=?', (room,data.reply_to)).fetchone():
            raise HTTPException(422, '回覆對象必須是本群留言')
        people = [dict(r) for r in db.execute('SELECT a.* FROM agents a JOIN members b ON a.id=b.agent WHERE b.room=?', (room,))]
        mentions = find_mentions(data.body, people)
        mid = db.execute('INSERT INTO messages(room,author,body,mentions,reply_to,created,client_id) VALUES(?,?,?,?,?,?,?)',
                         (room,who,data.body,json.dumps(mentions),data.reply_to,time.time(),data.client_id)).lastrowid
        db.execute('UPDATE messages SET notification_kind=? WHERE id=?', (data.notification_kind,mid))
        if attachment:
            db.execute('INSERT INTO images(message,mime,width,height,digest,content) VALUES(?,?,?,?,?,?)', (mid,*attachment))
        if data.file is not None:
            db.execute('INSERT INTO files(message,name,size,digest,content) VALUES(?,?,?,?,?)',
                       (mid,data.file.name,len(file_content),file_digest,file_content))
        enabled = db.execute('SELECT notify FROM rooms WHERE id=?',(room,)).fetchone()[0]
        for a in people:
            if a['id'] not in (who, 'Owner'):
                queue_notification(db, mid,a['id'],'pending' if enabled else 'disabled',time.time()+2)
    _hooks.run_post({**_ev, 'message_id': mid})
    return {'id':mid}


@app.post('/api/rooms/{room}/read')
def mark_read(room: int, data: ReadInput, request: Request):
    # Legacy endpoint/name: fetch progress only, never an explicit read receipt.
    who = identity(request)
    with contextlib.closing(connect()) as db, db:
        member(db,room,who)
        if data.through and not db.execute('SELECT 1 FROM messages WHERE room=? AND id=?',(room,data.through)).fetchone():
            raise HTTPException(422,'讀取位置不屬於本群')
        db.execute('UPDATE members SET last_read=MAX(last_read,?) WHERE room=? AND agent=?',(data.through,room,who))
        # 先讀群不應取消尚待投遞的提醒全文（含尚未送完的分段）。
        db.execute('''UPDATE outbox SET status='fetched' WHERE recipient=? AND status!='read'
            AND NOT (status='pending' AND message IN (SELECT id FROM messages WHERE author='__reminder__')) AND message IN
            (SELECT id FROM messages WHERE room=? AND id<=?)''',(who,room,data.through))
    return {'ok':True}


@app.post('/api/rooms/{room}/confirm-read')
def confirm_read(room: int, data: ConfirmReadInput, request: Request):
    who = identity(request)
    ids = sorted(set(data.messages))
    with contextlib.closing(connect()) as db, db:
        member(db,room,who)
        # Validate the entire batch before writing; never infer an interval or backfill history.
        for mid in ids:
            if not db.execute('SELECT 1 FROM messages WHERE room=? AND id=?',(room,mid)).fetchone():
                raise HTTPException(422,'確認的訊息不屬於本群')
        db.executemany('INSERT OR IGNORE INTO read_receipts(message,agent,created) VALUES(?,?,?)',
                       [(mid,who,time.time()) for mid in ids])
    return {'ok':True,'confirmed':ids}


@app.post('/api/rooms/{room}/notify')
def notify(room: int, data: NotifyInput, request: Request):
    if identity(request)!='Owner':
        raise HTTPException(403,'由使用者設定通知')
    with contextlib.closing(connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        member(db,room,'Owner',write=True)
        db.execute('UPDATE rooms SET notify=? WHERE id=?',(int(data.enabled),room))
        if not data.enabled:
            db.execute("UPDATE outbox SET status='cancelled' WHERE status='pending' AND message IN (SELECT id FROM messages WHERE room=?)",(room,))
    return {'ok':True}


@app.get('/api/events')
async def events(request: Request):
    who = identity(request)
    async def stream():
        # Events only ask clients to refresh their authorized views; never broadcast bodies.
        while not await request.is_disconnected():
            yield 'event: refresh\ndata: {}\n\n'
            await asyncio.sleep(3)
    return StreamingResponse(stream(),media_type='text/event-stream')


# 新版前端（web/，React 建置輸出 web/dist）：/app/ 底下的靜態檔＋單頁應用 fallback。
# 沒有建置（web/dist 不存在）就回 404 說明，不影響舊頁面。
WEB_DIST = ROOT.parent / 'web' / 'dist'


@app.get('/app')
def web_app_root():
    return RedirectResponse('/app/', status_code=307)


@app.get('/app/{path:path}')
def web_app(path: str):
    if not (WEB_DIST / 'index.html').is_file():
        raise HTTPException(404, 'web UI not built: run `npm ci && npm run build` in web/')
    if path:
        target = (WEB_DIST / path).resolve()
        if target.is_relative_to(WEB_DIST.resolve()) and target.is_file():
            cache = 'public, max-age=31536000, immutable' if path.startswith('assets/') else 'no-cache'
            return FileResponse(target, headers={'Cache-Control': cache})
        if path.startswith('assets/') or '.' in path.rsplit('/', 1)[-1]:
            raise HTTPException(404, 'not found')
    return FileResponse(WEB_DIST / 'index.html', headers={'Cache-Control': 'no-cache'})


# 內建傳檔站（2026-10-01·原 8091 獨立服務併入）：只服務本人、沿用 AA Forum 登入。
import files_portal
FILES_ROUTER = files_portal.make_router(identity, INSTANCE, STATE)
app.include_router(FILES_ROUTER)


if __name__=='__main__':
    if len(sys.argv) >= 3 and sys.argv[1] == 'set-password':
        # 用法：.venv/bin/python server/app.py set-password <帳號>
        import getpass
        name = sys.argv[2]
        if not ACCOUNT_NAME_RE.fullmatch(name):
            sys.exit('帳號：字母開頭，英數、底線、點、連字號，2 到 32 字')
        pw1 = getpass.getpass('新密碼（至少 8 字）：'); pw2 = getpass.getpass('再輸入一次：')
        if pw1 != pw2:
            sys.exit('兩次不同，未改動')
        if len(pw1) < 8:
            sys.exit('少於 8 字，未改動')
        save_account(name, pw1)
        print('帳號已設定：' + name)
        sys.exit(0)
    import uvicorn
    FILES_ROUTER.start_indexer()
    uvicorn.run(app,host=os.environ.get('AAF_SERVER_HOST','127.0.0.1'),port=PORT,proxy_headers=False)
