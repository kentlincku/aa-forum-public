"""成員血量（v2 S4）：本使用者唯讀遙測；絕不回傳終端內容。

核心不認識任何 agent：
  - headless 角色：讀核心記錄的最近一輪 TurnResult（經 broker /v1/turns）＋ driver 的 usage()。
  - tmux 房：以各 driver 的 matches_process 辨識前景程序，再交給該 driver 的 room_usage／screen_model／
    screen_activity。AA Forum 本身不讀任何 agent 的內部檔案（資料庫、rollout、session 檔都只在 driver 內讀）。
"""
import re
import subprocess
import sys
import time

import runtime as rt

if str(rt.ROOT) not in sys.path:
    sys.path.insert(0, str(rt.ROOT))

import drivers  # noqa: E402

_cache = {}
NOT_PROVIDED = '此 agent 未提供'


def engine_label(name):
    cls = drivers.DRIVERS.get(name)
    return getattr(cls, 'label', None) or name or '未知'


def _apply_usage(result, usage):
    """把 driver 的 usage（契約 v1）套到血量結果；取不到就附原因。"""
    if not usage:
        result['percent_reason'] = NOT_PROVIDED
        result['source'] += '；血量未知：' + NOT_PROVIDED
        return result
    if usage.get('model'):
        result['model'] = usage['model']
    if usage.get('percent') is not None:
        result.update(percent=usage['percent'], measured_at=usage.get('measured_at'))
        result['source'] += '；' + usage.get('source', '')
    else:
        why = usage.get('reason') or usage.get('source') or NOT_PROVIDED
        result['percent_reason'] = why
        result['source'] += '；血量未知：' + why
    if usage.get('context_used') is not None:
        result['context_used'] = usage['context_used']
    return result


def headless_status(agent, result):
    """沒有 tmux 房的角色：狀態取 driver.status()，用量取核心 TurnResult＋driver.usage()。"""
    cfg = rt.load_roles().get(agent, {})
    ad = drivers.make(agent, cfg, rt.MBOX_HOME)
    describe = getattr(ad, 'engine_desc', None)
    result['engine'] = describe() if describe else engine_label(ad.name) + '（headless）'
    running = ad.status() == 'busy'
    try:
        unread = rt.unread_count(agent)
    except Exception:
        unread = None
    result.update(activity='working' if running else 'idle',
                  activity_source=f'dispatcher 狀態；mbox 未讀 {unread if unread is not None else "未知"}',
                  status='no_visible_error',
                  source=getattr(ad, 'run_desc', 'headless：每輪由 dispatcher 啟動，無常駐終端'))
    try:
        turn = rt.last_turn(agent)
    except Exception:
        turn = None
    if turn:
        result['last_turn'] = {k: turn.get(k) for k in ('ok', 'exit', 'model_used', 'finished_at', 'duration_ms')}
        if turn.get('model_used'):
            result['model'] = turn['model_used']
        if not turn.get('ok') and turn.get('error') and not running:
            msg = str(turn['error'])[:200]
            result['status'] = 'rate_limit' if re.search(r'429|rate.?limit', msg, re.I) else 'api_error'
            result['source'] += '；上一輪錯誤：' + msg
    # 用量的「模型」必須是產生該用量的那一輪：取最近一輪「成功」的（失敗輪沒有新用量）
    ok_turn = turn if turn and turn.get('ok') else None
    if turn and not turn.get('ok'):
        try:
            ok_turn = rt.last_turn(agent, ok_only=True)
        except Exception:
            ok_turn = None
    alert = rt.MBOX_HOME / 'roles' / agent / 'model_alert.json'
    if alert.exists():
        try:
            a = __import__('json').loads(alert.read_text())
            result['status'] = 'api_error'
            result['source'] += f"；⚠ 模型 {a.get('model')} 不能用" + ("，已自動退回" if a.get('rolled_back') else "") + f"：{a.get('error','')[:160]}"
        except ValueError:
            pass
    try:
        usage = ad.usage(ok_turn)
    except Exception as exc:
        usage = dict(source='', reason=f'driver usage() 失敗：{type(exc).__name__}')
    return _apply_usage(result, usage)


def inspect_member(agent, owner=None):
    now = time.time()
    result = dict(agent=agent, engine='未知', model=None, percent=None, measured_at=None,
                  observed_at=now, status='unknown', activity='unknown',
                  source='無可靠讀數', activity_source='無可靠訊號')
    if agent not in rt.agent_roles():
        return result
    key = agent
    if key in _cache and now - _cache[key]['observed_at'] < 2:
        return dict(_cache[key])
    cfg = rt.load_roles().get(agent, {})
    if not rt.session_alive(agent) and rt.is_headless(cfg):
        result = headless_status(agent, result)
        _cache[key] = dict(result)
        return result
    try:
        command = [rt.tmux_bin()]
        panes = subprocess.run(command + ['list-panes', '-s', '-t', '=' + rt.tmux_session(agent),
                               '-F', '#{pane_id}|#{pane_in_mode}|#{pane_current_command}|#{pane_pid}'],
                               capture_output=True, text=True, timeout=2)
        if panes.returncode:
            result.update(status='offline', source='本帳號預設 tmux 無可讀房間')
            return result
        rows = panes.stdout.strip().splitlines()
        if len(rows) != 1:
            result['source'] = '多視窗或多窗格，無法唯一識別'
            return result
        pane, mode, _, pane_pid = rows[0].split('|')
        if mode != '0':
            result['source'] = '正在捲動歷史，暫不推定'
            return result
        # 只接受本 UID、同房祖先鏈、同前景程序群組中的唯一特務；引擎由各 driver 自己辨識。
        full = rt.process_table()
        processes = {pid: (v[0], v[1], v[2], rt.detect_engine(v[3], v[4]) or v[3]) for pid, v in full.items()}
        root_pid = int(pane_pid)
        group = processes.get(root_pid, (0, 0, -1, ''))[2]
        candidates = []
        for pid, (_, pgid, _, name) in sorted(processes.items()):
            if pgid != group or group <= 0 or name not in drivers.DRIVERS:
                continue
            if any(c[1] == name for c in candidates):
                continue  # 同引擎包裝程序與子程序只算一個
            ancestor, visited = pid, set()
            while ancestor in processes and ancestor not in visited:
                if ancestor == root_pid:
                    candidates.append((pid, name))
                    break
                visited.add(ancestor)
                ancestor = processes[ancestor][0]
        if len(candidates) != 1:
            root_info = processes.get(root_pid, (0, 0, -1, ''))
            if (not candidates
                    and root_info[3].lstrip('-') in ('bash', 'zsh', 'sh', 'fish')
                    and root_info[1] == root_info[2]
                    and not any(info[0] == root_pid for info in processes.values())):
                result.update(status='not_started', engine='尚未啟動', source='只有 tmux／shell，尚無特務可量測')
            else:
                result['source'] = '無法唯一確認本房前景特務程序'
            return result
        agent_pid, foreground = candidates[0]
        capture = subprocess.run(command + ['capture-pane', '-p', '-t', pane, '-S', '-40'],
                                 capture_output=True, text=True, timeout=2)
        if capture.returncode:
            return result
        screen = capture.stdout
        tail = '\n'.join(screen.splitlines()[-18:])
        ad = drivers.DRIVERS[foreground](agent, dict(cfg, driver=foreground), rt.MBOX_HOME)
        result['engine'] = engine_label(foreground)
        model = ad.screen_model(screen)
        if model:
            result['model'] = model
        usage = ad.room_usage(agent_pid, tail)
        if usage and usage.get('percent') is not None:
            result.update(percent=usage['percent'], measured_at=usage.get('measured_at'), source=usage['source'])
            if usage.get('model'):
                result['model'] = usage['model']
        elif usage and usage.get('reason'):
            result['percent_reason'] = usage['reason']
        activity = ad.screen_activity(screen, tail)
        if activity:
            result['activity'] = activity
            result['activity_source'] = '終端提示推定；不含背景特務'
        errors = re.findall(r'^\s*(?:[!✖×●⎿]\s*)?(API Error:[^\n]*|Error:[^\n]*|[^\n]*Retrying in \d+[^\n]*)$',
                            tail, re.M | re.I)
        result['status'] = 'no_visible_error'
        if errors:
            last = errors[-1]
            result['status'] = ('rate_limit' if re.search(r'429|rate.?limit', last, re.I) else
                                'overload' if re.search(r'529|overload', last, re.I) else 'api_error')
        limits = re.findall(r"^[ \t]*(?:⎿[ \t]*)?You['’]ve hit your session limit[ \t]*[·•][ \t]*resets[ \t]+([^\n]+)$",
                            tail, re.M | re.I)
        if limits:
            result.update(status='quota_exhausted', activity='quota_wait',
                          activity_source='畫面顯示額度用盡，等待重置', reset_hint=limits[-1].strip()[:120])
    except (OSError, ValueError, subprocess.TimeoutExpired):
        result['source'] = '讀取失敗或逾時'
    finally:
        _cache[key] = dict(result)
    return result
