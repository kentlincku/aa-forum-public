"""Hermes 內部資料（只住在 hermes driver）：~/.hermes/state.db、config.yaml、模型目錄快取、active_sessions。

由 AA Forum member_health 搬來（S4）：AA Forum 與 dispatcher 不再直接讀這些檔案，改經 hermes driver 的 usage()／room_status()。
"""
import json
import os
import re
import subprocess
import time
from pathlib import Path

from drivers.base import process_start
from mbox.overrides import load_override


def hermes_home():
    """假設 AA Forum 與 dispatcher 使用同一個 HERMES_HOME（角色繼承 dispatcher 環境，adapters env()）。"""
    return Path(os.environ.get('HERMES_HOME') or Path.home() / '.hermes')


def hermes_model_window(home):
    """~/.hermes/config.yaml 頂層 model: 區塊的 dict(default, context_length, provider, base_url)；讀不到的鍵為 None。
    只解析這幾個純量、只支援 block style（`model:` 下縮排），不引入 yaml 依賴；flow style 讀不到即視為未設定。"""
    out = dict(default=None, context_length=None, provider=None, base_url=None)
    try:
        text = (home / 'config.yaml').read_text()
    except OSError:
        return out
    block = re.search(r'^model:[ \t]*\n((?:[ \t]+[^\n]*\n?)*)', text, re.M)
    if not block:
        return out
    for key in ('default', 'provider', 'base_url'):
        m = re.search(rf'^[ \t]+{key}:[ \t]*["\']?([^"\'\s#]+)', block[1], re.M)
        out[key] = m[1] if m else None
    window = re.search(r'^[ \t]+context_length:[ \t]*(\d+)[ \t]*(?:#.*)?$', block[1], re.M)
    out['context_length'] = int(window[1]) if window else None
    return out


try:  # 與 Hermes 相同的 route 正規化（hermes-agent hermes_cli/route_identity.py）
    from hermes_cli.route_identity import normalize_route_base_url
except Exception:  # AA Forum 環境通常 import 不到 hermes-agent：照抄 route_identity.normalize_route_base_url（2026-10-05 版）
    from urllib.parse import urlsplit, urlunsplit

    def normalize_route_base_url(base_url):
        raw = str(base_url or '')
        if not raw:
            return ''
        if any(ord(c) <= 0x20 for c in raw):
            return raw
        had_query = '?' in raw.split('#', 1)[0]
        try:
            parsed = urlsplit(raw)
            hostname = parsed.hostname
            if not parsed.scheme or not hostname:
                return raw
            scheme = parsed.scheme.lower()
            if '%' in hostname:
                address, zone = hostname.split('%', 1)
                host = f'{address.lower()}%{zone}'
            else:
                host = hostname.lower()
            port = parsed.port
        except (TypeError, ValueError):
            return raw
        route_host = parsed.netloc.rsplit('@', 1)[-1]
        if route_host.startswith('[') or ':' in host:
            host = f'[{host}]'
        if port is not None and (scheme, port) not in {('http', 80), ('https', 443)}:
            host = f'{host}:{port}'
        if '@' in parsed.netloc:
            host = f"{parsed.netloc.rsplit('@', 1)[0]}@{host}"
        path = parsed.path
        if path.endswith('/') and not had_query:
            path = path[:-1]
        normalized = urlunsplit((scheme, host, path, parsed.query, ''))
        if had_query and not parsed.query:
            normalized += '?'
        return normalized


def config_pin_mismatch(cfg, model, provider, base_url):
    """config.yaml model.context_length 是否「不」描述此 session 的 route；回不適用原因或 None（＝適用）。
    對齊 Hermes agent_init._scope_context_length_to_default_runtime／_context_route_mismatch：
    - 模型須等於 config 預設；
    - config 有 base_url → 只比正規化後的 route（不比 provider，同 agent_init.py:126-127）；
    - config 沒有 base_url → 才比 provider。
    與 Hermes 的差異（AA Forum venv import 不到 hermes_cli）：
    - 模型為字串全等，未經 normalize_model_for_provider（例如 vendor 前綴、別名不會被視為相同）；
    - 無 base_url 時 provider 只做 strip/lower，不做別名正規化，也不展開 provider 預設 route 清單。"""
    if model != cfg['default']:
        return f'模型不同（config 預設 {cfg["default"]}）'
    if cfg['base_url']:
        want, got = normalize_route_base_url(cfg['base_url']), normalize_route_base_url(base_url)
        if want != got:
            return f'route 不同（session {got or "未記錄"}，config {want}）'
        return None
    if cfg['provider'] and (provider or '').strip().lower() != cfg['provider'].strip().lower():
        return f'provider 不同（session {provider or "未知"}，config {cfg["provider"]}）'
    return None


# Hermes provider id → models.dev 目錄 id（同 hermes-agent agent/models_dev.py PROVIDER_TO_MODELS_DEV 的常用子集）
MODELS_DEV_PROVIDER = {'copilot': 'github-copilot', 'gemini': 'google', 'openai-codex': 'openai',
                       'openai-api': 'openai', 'kimi': 'kimi-for-coding', 'xai-oauth': 'xai'}


def hermes_context_window(home, model, provider, base_url, cfg_window=None, cfg_model=None):
    """依序找 model 的 context 上限，回 (window, 來源) 或 (None, 原因)：
    1. roles.json 的 context_length——僅當 session 模型＝roles.json 的 model（換過模型的舊上限不可沿用）
       roles.json 只寫 context_length 未寫 model 時無從比對，此值忽略（往下查）。
    2. config.yaml model.context_length（使用者釘選值）——僅當 session 的 model／provider／base_url 都等於
       config 預設 route（同 Hermes 的 pin scoping）；不適用時原因附在後續來源文字
    3. context_length_cache.yaml「model@base_url」（Hermes 實測快取）
    4. models_dev_cache.json 該 provider 目錄的 limit.context"""
    if type(cfg_window) is int and cfg_window > 0 and cfg_model and model == cfg_model:
        return cfg_window, 'roles 設定 context_length'
    cfg = hermes_model_window(home)
    pin_note = ''
    if cfg['context_length']:
        why_not = config_pin_mismatch(cfg, model, provider, base_url)
        if why_not is None:
            return cfg['context_length'], 'config.yaml 預設模型 context_length'
        if model == cfg['default']:
            pin_note = f'；config pin 不適用：{why_not}'
    try:
        cache = (home / 'context_length_cache.yaml').read_text()
        for key, value in re.findall(r'^[ \t]+(\S+):[ \t]*(\d+)[ \t]*$', cache, re.M):
            name, _, url = key.partition('@')
            if normalize_route_base_url(url) == normalize_route_base_url(base_url) and (name == model or name.split('/')[-1] == model):
                return int(value), 'Hermes context_length 快取' + pin_note
    except OSError:
        pass
    try:
        catalog = json.loads((home / 'models_dev_cache.json').read_text())
        entry = catalog[MODELS_DEV_PROVIDER.get(provider, provider)]['models'][model]
        context = entry['limit']['context']
        if type(context) is int and context > 0:
            return context, f'models.dev 目錄（{provider}）' + pin_note
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None, f'查不到模型 {model}（{provider or "provider 未知"}）的 context 上限' + pin_note


def hermes_context(state_dir, home=None, cfg_window=None, cfg_model=None, effective=None):
    """Hermes headless 的 context 已用 %：session.hermes → state.db sessions 列。
    已用 = 持久化 _usage_anchor 的 prompt_tokens + completion_tokens（最後一次 API 回應的真實用量）；
    分母 = 該 session 實際使用模型的 context 上限（hermes_context_window）。
    成功回 {percent, model, measured_at, source}；否則回 {reason}（顯示「未知」並附原因），不估算。"""
    home = home or hermes_home()
    try:
        sid = (state_dir / 'session.hermes').read_text().strip()
    except OSError:
        return dict(reason='尚無 Hermes session')
    return hermes_session_context(sid, home, cfg_window, cfg_model, effective)


def hermes_session_context(sid, home=None, cfg_window=None, cfg_model=None, effective=None):
    """依 state.db session id 算血量（headless 與 tmux 房共用）；規則見 hermes_context。
    effective＝(model, provider, 層名)：實際生效的模型（#46）。--resume 接續時 Hermes 不改寫 sessions.model
    （只有互動 /model 會呼叫 update_session_model），所以優先用 effective，sessions.model 只當最後備援。
    provider 與 session 的 billing_provider 不同時，route（base_url）視為未知，config pin 不適用（#33 規則）。"""
    home = home or hermes_home()
    db_path = home / 'state.db'
    if not sid or not db_path.exists():
        return dict(reason='尚無 Hermes session' if not sid else 'Hermes state.db 不存在')
    import sqlite3
    try:
        db = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=1)
        try:
            row = db.execute('SELECT model, model_config, last_activity_at, billing_provider, billing_base_url '
                             'FROM sessions WHERE id=?', (sid,)).fetchone()
        finally:
            db.close()
    except sqlite3.Error:
        return dict(reason='Hermes state.db 暫不可讀')
    if not row:
        return dict(reason='state.db 查無本角色 session')
    model, config, at, provider, base_url = row
    try:
        anchor = json.loads(config or '{}').get('_usage_anchor')
        used = int(anchor['prompt_tokens']) + int(anchor.get('completion_tokens') or 0)
    except (ValueError, TypeError, KeyError, AttributeError):
        return dict(reason='session 尚無用量紀錄')
    layer = 'state.db sessions.model'
    if effective and effective[0]:
        eff_model, eff_provider, layer = effective
        if eff_provider and eff_provider != provider:
            base_url = None  # 換了 provider：session 記的 route 不再適用
            provider = eff_provider
        if eff_model != model:
            layer += f'；state.db 未更新（仍記 {model or "無"}）'
        model = eff_model
    if not model:
        return dict(reason='session 未記錄模型')
    window, why = hermes_context_window(home, model, provider, base_url, cfg_window, cfg_model)
    if not window:
        return dict(reason=f'{why}（模型取自 {layer}）')
    # 越界：錨點可能早於壓縮或來自換模型前的長對話；此時分母不可信，寧可顯示未知。
    if not 0 < used <= window:
        return dict(reason=f'用量 {used} 超出 {model} 上限 {window}（{why}）')
    return dict(percent=round(100 * used / window), model=model, used=used, window=window, provider=provider,
                measured_at=at if isinstance(at, (int, float)) else None,
                source=f'Hermes state.db 最後回應用量 {used}/{window} tokens（模型取自 {layer}；上限來源：{why}）')


def hermes_session_for_pid(pid, home=None, start_time=None):
    """tmux 房內互動 Hermes 的 session id：讀 Hermes 的 runtime/active_sessions.json（CLI 啟動時登記
    pid、process_start_time、metadata.live_session_id；見 hermes-agent cli.py _claim_active_session）。
    須 pid 相符且登記的 process_start_time 與 ps 啟動時間相差 < 2 秒（防 PID 重用）；
    同 pid 有多筆不同 session 時視為不明。回 (sid, None) 或 (None, 原因)。"""
    home = home or hermes_home()
    try:
        entries = json.loads((home / 'runtime' / 'active_sessions.json').read_text()).get('entries') or []
    except (OSError, ValueError, AttributeError):
        return None, 'Hermes active_sessions.json 不可讀'
    if start_time is None:
        start_time = process_start_epoch(pid)
    sids = set()
    for entry in entries:
        if not isinstance(entry, dict) or entry.get('pid') != pid:
            continue
        registered = entry.get('process_start_time')
        if start_time is None or not isinstance(registered, (int, float)) or abs(registered - start_time) >= 2:
            continue
        sid = (entry.get('metadata') or {}).get('live_session_id') or entry.get('session_id')
        if isinstance(sid, str) and sid:
            sids.add(sid)
    if len(sids) == 1:
        return sids.pop(), None
    return None, ('active_sessions 無此程序的登記' if not sids else f'active_sessions 同程序有 {len(sids)} 個 session')


def process_start_epoch(pid):
    """ps lstart（秒精度）→ epoch；與 AA Forum 共用 drivers.base.process_start 的查詢；失敗回 None。"""
    try:
        return time.mktime(time.strptime(process_start(pid, c_locale=True), '%a %b %d %H:%M:%S %Y'))
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


# 壓縮接續邊：條件照抄 hermes-agent hermes_state_sessions.py:1155-1162 compression_parent_edge
# （_delegate_from 用 _sql_json_extract 的 json_valid 防呆寫法，hermes_state_common.py:82；_branched_from 原樣）。
# /new、/branch、subagent／delegate、tool 子 session 都不是同一段對話的接續，不可追。
# 另加 _reset_from（reset fork，對齊 hermes_state_compression.py:751-757 #114271 的 include_reset）。
_COMPRESSION_CHILD_SQL = """
    SELECT child.id FROM sessions AS child JOIN sessions AS parent ON child.parent_session_id = parent.id
    WHERE parent.id = ?
      AND parent.end_reason = 'compression'
      AND json_extract(COALESCE(child.model_config, '{}'), '$._branched_from') IS NULL
      AND json_extract(CASE WHEN json_valid(child.model_config) THEN child.model_config ELSE json_object() END,
                       '$._delegate_from') IS NULL
      AND json_extract(CASE WHEN json_valid(child.model_config) THEN child.model_config ELSE json_object() END,
                       '$._reset_from') IS NULL
      AND COALESCE(child.source, '') != 'tool'
    ORDER BY child.started_at DESC LIMIT 1
"""
TIP_MAX_DEPTH = 64


def hermes_session_tip(sid, home=None):
    """沿 Hermes 的「壓縮接續」邊走到最新子孫（同層多個時取 started_at 最大者），防環、深度上限 TIP_MAX_DEPTH。
    Hermes 壓縮時把 agent.session_id 換成 child（hermes-agent agent/conversation_compression.py:1695），
    但 CLI 的 active_sessions 只在 claim 時寫一次 live_session_id（cli.py:937），因此需自行追到鏈尾。
    回 (tip_sid, 層數)；讀不到 state.db 時原樣回 (sid, 0)。

    與 Hermes 其他實作的已知差異（lead #1052 判定可接受）：
    1. fork 標記只要存在就排除（照 hermes_state_sessions.py:1155 SQL）；Hermes _is_explicit_fork_child_row
       （hermes_state_messages.py:1996）只在標記指向 child 自己的 parent 時才排除。影響：delegate／branch 出來的
       session 再被壓縮時會漏追（compression 會把 model_config 複製給接續 session），只會少追、不會誤追。
    2. 同層多個壓縮 child：取 started_at 最新；Hermes get_compression_lineage 取最早。此情況本身屬異常。"""
    home = home or hermes_home()
    import sqlite3
    try:
        db = sqlite3.connect(f'file:{home / "state.db"}?mode=ro', uri=True, timeout=1)
    except sqlite3.Error:
        return sid, 0
    depth, seen = 0, {sid}
    try:
        while depth < TIP_MAX_DEPTH:
            row = db.execute(_COMPRESSION_CHILD_SQL, (sid,)).fetchone()
            if not row or row[0] in seen:
                break
            sid = row[0]
            seen.add(sid)
            depth += 1
    except sqlite3.Error:
        pass
    finally:
        db.close()
    return sid, depth


# Hermes CLI 狀態列：「☤ claude-opus-5.5 │ ~102K/1M │ [█░░…] ~10% │ …」
# 依 2026-10-05 Hermes 狀態列格式實測（tmux 房 capture-pane）；格式改版時此 regex 需跟著更新。
HERMES_BAR = re.compile(r'^\s*☤\s+(\S+)\s+│\s+~?([\d.]+[KkMm]?)/([\d.]+[KkMm]?)\s+│[^\n│]*?~?(\d{1,3})%\s*│', re.M)


def hermes_status_bar(screen):
    """解析畫面上最後一條 Hermes 狀態列，回 dict(model, percent, used, window) 或 None。"""
    bars = list(HERMES_BAR.finditer(screen))
    if not bars:
        return None
    m = bars[-1]
    percent = int(m[4])
    if not 0 <= percent <= 100:
        return None
    return dict(model=m[1], percent=percent, used=m[2], window=m[3])


INIT_SCAN_LINES = 50  # Hermes stream-json 的 system/init 事件在輸出開頭，掃前 50 行足夠


def last_init_model(state_dir, sid):
    """最近一輪 headless 輸出（prev_out.txt）裡 Hermes init 事件的 model；session_id 須相符。"""
    try:
        with (state_dir / 'prev_out.txt').open(encoding='utf-8', errors='replace') as f:
            for _, line in zip(range(INIT_SCAN_LINES), f):
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if (isinstance(ev, dict) and ev.get('type') == 'system' and ev.get('subtype') == 'init'
                        and ev.get('session_id') == sid and isinstance(ev.get('model'), str) and ev['model']):
                    return ev['model']
    except OSError:
        pass
    return None


def effective_model(agent, cfg, state_dir, mbox_home):
    """headless Hermes 角色「產生目前用量的那一輪」所用的 (model, provider, 層名)；都沒有回 None（交給 state.db）。
    分子（_usage_anchor）是最近一輪的用量，分母必須對同一輪的模型（reviewer #48 SHOULD），所以：
      1. 最近一輪 init（session_id 相符）——實際跑過的模型；若 override 與它不同，層名註明「override X 下一輪生效」。
      2. 還沒有相符的 init 時：AA Forum override（與 driver 同一份解析）→ roles.json model／provider。
      3. 都沒有 → state.db sessions.model。"""
    over = load_override(agent, mbox_home)
    try:
        sid = (state_dir / 'session.hermes').read_text().strip()
    except OSError:
        sid = ''
    init = last_init_model(state_dir, sid) if sid else None
    if init:
        layer = '最近一輪 init'
        if over.get('model') and over['model'] != init:
            layer += f'；override {over["model"]} 下一輪生效，目前仍為 {init}'
        provider = over.get('provider') if over.get('model') == init else cfg.get('provider') if cfg.get('model') == init else None
        return init, provider, layer
    if over.get('model'):
        return over['model'], over.get('provider') or cfg.get('provider'), 'AA Forum override（尚無本 session 的 init）'
    if cfg.get('model'):
        return cfg['model'], cfg.get('provider'), 'roles.json'
    return None


# Hermes 安裝器產生的 launcher：`python3 -I -c '<script>' …`，comm 是 python，前兩個 token 是 python3 與 -I。
# 依據（~/.hermes/hermes-agent 安裝器寫入的 launcher 內容，2026-10-05 實測 ps）：腳本內同時有
# `sys.path.insert(0, '<…>/hermes-agent')` 與 `from hermes_cli.main import main`；也接受 `-m hermes_cli.main`。
_HERMES_LAUNCHER = re.compile(r"sys\.path\.insert\(0,\s*['\"][^'\"]*/hermes-agent['\"]\)")


def is_hermes_launcher(comm: str, args: str) -> bool:
    """macOS ps 的 comm 欄會截成 16 字（長路徑 python 變成 `/<家目錄>/xxxx`，basename 無意義），
    所以 comm 或 args 第一個 token 任一為 python 即可。"""
    first = os.path.basename(args.split(None, 1)[0]) if args.strip() else ''
    if not any(re.match(r'python(\d+(\.\d+)?)?$', name) for name in (os.path.basename(comm or ''), first)):
        return False
    if re.search(r'(?:^|\s)-m\s+hermes_cli\.main(?:\s|$)', args):
        return True
    return bool(_HERMES_LAUNCHER.search(args)) and 'from hermes_cli.main import main' in args
