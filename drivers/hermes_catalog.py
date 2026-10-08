"""Hermes 的模型目錄（任務 #27；v2 S5 由 AA Forum 移入 hermes driver）：只讀本機 Hermes 檔案，不連網、不讀憑證。

來源：
  1. ~/.hermes/config.yaml 頂層 model:（default + provider）→ 目前預設
  2. ~/.hermes/config.yaml providers:<name>（自訂端點）→ 只取 default_model；api_key 等欄位不讀
  3. ~/.hermes/models_dev_cache.json → 上述「本機已設定」provider 對應目錄中 tool_call=true 的模型（含 context 上限）
沒有設定的 provider 不列，避免選了之後才發現沒有登入。
"""
import json
import re

from drivers.base import MODEL_ID_RE  # 與 override 讀寫端同一條規則，不另抄一份
from drivers.hermes_state import MODELS_DEV_PROVIDER, hermes_home


def _model_block(text):
    """頂層 model: 區塊的 default / provider（block style；讀不到回空 dict）。"""
    block = re.search(r'^model:[ \t]*\n((?:[ \t]+[^\n]*\n?)*)', text, re.M)
    out = {}
    if block:
        for key in ('default', 'provider'):
            m = re.search(rf'^[ \t]+{key}:[ \t]*["\']?([^"\'\s#]+)', block[1], re.M)
            if m:
                out[key] = m[1]
    return out


def _custom_providers(text):
    """providers: 底下第一層名稱與其 default_model；只讀這兩個欄位。"""
    block = re.search(r'^providers:[ \t]*\n((?:[ \t]+[^\n]*\n?)*)', text, re.M)
    out = {}
    if not block:
        return out
    current = None
    for line in block[1].splitlines():
        top = re.fullmatch(r'  ([A-Za-z0-9._-]+):[ \t]*', line)
        if top:
            current = top[1]
            out[current] = None
            continue
        if re.match(r'  \S', line):
            current = None  # 其他第一層鍵（名稱不合法等）：其下欄位不歸給前一個 provider
            continue
        dm = re.fullmatch(r'[ \t]{4,}default_model:[ \t]*["\']?([^"\'\s#]+)["\']?[ \t]*', line)
        if current and dm:
            out[current] = dm[1]
    return out


def _valid(value):
    return isinstance(value, str) and re.fullmatch(MODEL_ID_RE, value) is not None


def model_catalog(home=None):
    """回 {providers:[{id, label, source, models:[{id, name, context}]}], default:{provider, model}, warnings:[...]}。
    整份讀不到時 providers 為空，warnings 說明原因（前端退回文字框）。"""
    home = home or hermes_home()
    warnings = []
    try:
        text = (home / 'config.yaml').read_text()
    except OSError:
        return dict(providers=[], default={}, warnings=['讀不到 ~/.hermes/config.yaml'])
    default = {k: v for k, v in _model_block(text).items() if _valid(v)}
    providers = {}

    def add(pid, source):
        if _valid(pid) and pid not in providers:
            providers[pid] = dict(id=pid, label=pid, source=source, models=[])
        return providers.get(pid)

    if default.get('provider'):
        add(default['provider'], 'config.yaml model.provider')
    for pid, dm in _custom_providers(text).items():
        entry = add(pid, 'config.yaml providers（自訂端點）')
        if entry and _valid(dm):
            entry['models'].append(dict(id=dm, name=dm, context=None))
    try:
        catalog = json.loads((home / 'models_dev_cache.json').read_text())
        if not isinstance(catalog, dict):
            raise ValueError('不是物件')
    except (OSError, ValueError) as exc:
        catalog = {}
        warnings.append(f'models.dev 快取不可用（{type(exc).__name__}），只列 config 內的模型')
    for pid, entry in providers.items():
        try:
            models = catalog[MODELS_DEV_PROVIDER.get(pid, pid)]['models']
            if not isinstance(models, dict):
                continue
        except (KeyError, TypeError):
            continue
        entry['label'] = catalog[MODELS_DEV_PROVIDER.get(pid, pid)].get('name') or pid
        seen = {m['id'] for m in entry['models']}
        for mid, info in sorted(models.items()):
            if not isinstance(info, dict) or info.get('tool_call') is not True or not _valid(mid) or mid in seen:
                continue  # 角色要用工具，tool_call 不可用的模型不列
            context = (info.get('limit') or {}).get('context') if isinstance(info.get('limit'), dict) else None
            entry['models'].append(dict(id=mid, name=str(info.get('name') or mid)[:100],
                                        context=context if type(context) is int and context > 0 else None))
    # 目前預設模型一定在清單中（即使目錄沒有它）
    if default.get('provider') and default.get('default'):
        entry = providers[default['provider']]
        if not any(m['id'] == default['default'] for m in entry['models']):
            entry['models'].insert(0, dict(id=default['default'], name=default['default'], context=None))
    if not providers:
        warnings.append('config.yaml 沒有可辨識的 provider')
    return dict(providers=list(providers.values()),
                default=dict(provider=default.get('provider'), model=default.get('default')),
                warnings=warnings)
