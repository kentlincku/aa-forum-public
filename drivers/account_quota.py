"""Claude／Codex 的帳號額度（只在 driver 內讀；v2 S4 由 AA Forum 搬來）。不回傳憑證或內部帳號 ID。

Mac 移植：單一使用者，直接讀本人 HOME；執行檔另找 /opt/homebrew/bin。
Hermes／pi 沒有可讀的額度 API，不列入。
"""
import json
import math
import os
from pathlib import Path
import select
import shutil
import subprocess
import threading
import time

_lock = threading.Lock()
_cached = None
_checked = 0.0
_claude_email = None


def command(name, home):
    # 同一安全規則供兩個引擎使用：優先此擁有者安裝，不借其他人的 HOME。
    candidates = [home / '.local/bin' / name]
    versions = sorted((home / '.nvm/versions/node').glob('v*'),
                      key=lambda p: tuple(int(n) if n.isdigit() else 0 for n in p.name[1:].split('.')),
                      reverse=True)
    candidates.extend(p / 'bin' / name for p in versions)
    candidates.append(Path(shutil.which(name, path='/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin') or '/nonexistent'))
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    raise FileNotFoundError(name)


def window(label, used, resets, minutes, now):
    invalid_numbers = any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                          for v in (used, resets))
    if invalid_numbers:
        return None
    if not 0 <= used <= 100 or resets <= now:
        return None
    return dict(label=label, remaining_percent=100-used, resets_at=resets, window_minutes=minutes)


def codex_read(home):
    executable = command('codex', home)
    env = dict(HOME=str(home), CODEX_HOME=str(home / '.codex'), LANG='C.UTF-8',
               PATH=str(Path(executable).parent) + ':/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin')
    p = subprocess.Popen([executable, 'app-server'], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, cwd=home)
    try:
        p.stdin.write(b'{"id":1,"method":"initialize","params":{"clientInfo":{"name":"quota_panel","version":"1"}}}\n')
        p.stdin.flush()
        pending = b''
        results = {}
        deadline = time.monotonic() + 8  # 本機查詢最多八秒，不等待特務回合。
        while time.monotonic() < deadline:
            if not select.select([p.stdout], [], [], .2)[0]:
                continue
            chunk = os.read(p.stdout.fileno(), 65536)
            if not chunk:
                break
            pending += chunk
            if len(pending) > 1048576:  # 防禦性限制一 MiB，遠大於正常額度回應。
                raise ValueError('oversized response')
            while b'\n' in pending:
                line, pending = pending.split(b'\n', 1)
                data = json.loads(line)
                if data.get('id') in (1, 2, 3) and 'error' in data:
                    raise ValueError('quota unavailable')
                if data.get('id') == 1:
                    p.stdin.write(b'{"method":"initialized"}\n{"id":2,"method":"account/rateLimits/read"}\n{"id":3,"method":"account/read","params":{"refreshToken":false}}\n')
                    p.stdin.flush()
                if data.get('id') in (2, 3):
                    results[data['id']] = data.get('result') or {}
                if 2 in results and 3 in results:
                    account = results[3].get('account') or {}
                    results[2]['email'] = account.get('email')
                    return results[2]
        raise TimeoutError('quota timeout')
    finally:
        p.terminate()
        try:
            p.wait(timeout=1)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
        p.stdin.close()
        p.stdout.close()


def _iso(s):
    import datetime
    return datetime.datetime.fromisoformat(str(s).replace('Z', '+00:00')).timestamp()


def hermes_usage(provider, now):
    """Hermes 的 /usage（`hermes usage --provider X --json`）：讀本人已設定的登入，不碰憑證內容。"""
    exe = shutil.which('hermes', path=str(Path.home() / '.local/bin') + ':/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin')
    r = subprocess.run([exe, 'usage', '--provider', provider, '--json'], capture_output=True, text=True, timeout=20)
    d = json.loads(r.stdout)
    wins = []
    for w in d.get('windows') or []:
        try:
            resets = _iso(w['resets_at'])
        except (KeyError, ValueError, TypeError):
            continue
        label = {'Current session': '5 小時', 'Current week': '每週'}.get(w.get('label'), w.get('label') or '')
        x = window(label, w.get('used_percent'), resets, None, now)
        if x:
            wins.append(x)
    return dict(windows=wins, observed_at=_iso(d['fetched_at']) if d.get('fetched_at') else now, plan=d.get('plan'))


def copilot_quota(now):
    """GitHub Copilot（Hermes 預設 provider）：gh api /copilot_internal/user 的 quota_snapshots。"""
    gh = shutil.which('gh', path='/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin')
    out = dict(engine='GitHub Copilot', source='gh api copilot_internal/user（Hermes 用的 provider）',
               observed_at=None, windows=[])
    if not gh:
        out['error'] = '找不到 gh'
        return out
    r = subprocess.run([gh, 'api', '/copilot_internal/user'], capture_output=True, text=True, timeout=15)
    d = json.loads(r.stdout)
    reset = _iso(d['quota_reset_date'] + 'T00:00:00+00:00') if d.get('quota_reset_date') else None
    names = {'premium_interactions': '進階請求', 'chat': '對話', 'completions': '補全'}
    for key, snap in (d.get('quota_snapshots') or {}).items():
        if snap.get('unlimited'):
            out['windows'].append(dict(label=names.get(key, key), remaining_percent=100, resets_at=reset,
                                       window_minutes=None, unlimited=True))
            continue
        if not snap.get('entitlement'):
            continue      # 方案沒有這一項（例：個人方案無進階請求額度）
        pct = snap.get('percent_remaining')
        if isinstance(pct, (int, float)) and reset:
            out['windows'].append(dict(label=f"{names.get(key, key)}（{int(snap.get('remaining', 0))}/{int(snap['entitlement'])}）",
                                       remaining_percent=round(pct), resets_at=reset, window_minutes=None))
    out['observed_at'] = now
    out['plan'] = d.get('copilot_plan')
    return out


def read_quota():
    global _cached, _checked, _claude_email
    home = Path.home()
    now = time.time()
    claude = dict(engine='Claude', source='Anthropic OAuth 用量（hermes usage）', observed_at=None, windows=[])
    try:
        claude.update(hermes_usage('anthropic', now))
    except Exception:
        claude['windows'] = []
    with _lock:
        if _cached is None or time.monotonic() - _checked >= 60:  # 所有分頁共用每分鐘一次查詢。
            _checked = time.monotonic()
            _claude_email = None
            try:
                executable = command('claude', home)
                # 登入查詢最多三秒，避免阻塞額度；Email 限長防異常資料撐大畫面。
                status = subprocess.run([executable, 'auth', 'status', '--json'],
                                        capture_output=True, text=True, timeout=3, check=True,
                                        cwd=home, env=dict(HOME=str(home), LANG='C.UTF-8',
                                            CLAUDE_CONFIG_DIR=str(home / '.claude'),
                                            PATH=str(Path(executable).parent)+':/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin'))
                login = json.loads(status.stdout)
                if login.get('loggedIn') is True:
                    email = login.get('email')
                    if isinstance(email, str):
                        _claude_email = email[:254]  # 顯示上限 254 字元，不作信箱有效性判定。
            except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
                pass
            previous = _cached
            _cached = dict(engine='Codex', source='此擁有者 Codex 登入', observed_at=None, windows=[])
            try:
                raw = codex_read(home)
                email = raw.get('email')
                if isinstance(email, str):
                    _cached['email'] = email[:254]  # 與 Claude 相同的顯示長度上限。
                observed = time.time()
                buckets = raw.get('rateLimitsByLimitId') or {'codex': raw.get('rateLimits')}
                for key, bucket in buckets.items():
                    if not isinstance(bucket, dict):
                        continue
                    for field in ('primary', 'secondary'):
                        item = bucket.get(field) or {}
                        minutes = item.get('windowDurationMins')
                        invalid_duration = isinstance(minutes, bool) or not isinstance(minutes, int) or minutes <= 0
                        if invalid_duration:
                            continue
                        if minutes % 1440 == 0:
                            duration = f'{minutes // 1440} 天'
                        elif minutes % 60 == 0:
                            duration = f'{minutes // 60} 小時'
                        else:
                            duration = f'{minutes} 分鐘'
                        label = str(bucket.get('limitName') or key)[:80] + ' · ' + duration
                        w = window(label, item.get('usedPercent'), item.get('resetsAt'), minutes, observed)
                        if w:
                            _cached['windows'].append(w)
                _cached['observed_at'] = observed
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                _cached = dict(previous or _cached)
                # 不回傳例外原文（可能含憑證）；舊帳號僅為上次快照的帳號。
                if isinstance(exc, TimeoutError):
                    _cached['error'] = '查詢逾時'
                elif isinstance(exc, FileNotFoundError):
                    _cached['error'] = '未找到 Codex 執行檔'
                else:
                    _cached['error'] = '查詢失敗'
        codex = dict(_cached)
        claude['email'] = _claude_email
        codex['windows'] = [w for w in _cached['windows'] if w['resets_at'] > time.time()]
    for item in (claude, codex):
        # 超過五分鐘沒有觀測，明示上次讀數，不當作即時值。
        item['stale'] = bool(item.get('error')) or item['observed_at'] is None or time.time() - item['observed_at'] > 300
    return {'accounts': [claude, codex]}


def engine_quota(label):
    return next((a for a in read_quota()['accounts'] if a['engine'] == label), None)
