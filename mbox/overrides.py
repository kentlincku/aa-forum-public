"""模型覆寫（核心保管）：AA Forum 寫入 var/roles/<role>/override.json，dispatcher 叫醒時疊到設定上傳給 driver。"""
from __future__ import annotations

import json
from pathlib import Path

MODEL_ID_RE = r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}'  # 模型／provider ID 規則（讀寫兩端共用）


def override_path(role: str, home: Path) -> Path:
    return home / "roles" / role / "override.json"


def load_override(role: str, home: Path) -> dict:
    """AA Forum 寫入的模型覆寫 {model, provider}；格式不合的值一律忽略（不讓壞檔把怪參數送進 argv）。"""
    import re
    try:
        data = json.loads(override_path(role, home).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    # 與寫入端同規則：須有合法 model；provider 若存在也須合法，否則整份不採用（不做部分套用）。
    values = {k: data[k] for k in ("model", "provider") if k in data}
    if "model" not in values or not all(isinstance(v, str) and re.fullmatch(MODEL_ID_RE, v)
                                        for v in values.values()):
        return {}
    return values


def effective_cfg(role: str, cfg: dict, home: Path) -> dict:
    """roles.json cfg 疊上 override；只覆蓋 model / provider，不碰 runtime / adapter。"""
    over = load_override(role, home)
    if not over:
        return cfg
    # 保留 roles.json 原值：用量的分母（roles.json context_length）只綁 roles.json 的 model
    return dict(cfg, **over, _roles_model=cfg.get("model"), _roles_provider=cfg.get("provider"))


class OverrideError(ValueError):
    pass


def override_info(role: str, cfg: dict, home: Path) -> dict:
    """目前的有效模型與來源（override 優先於 roles.json）。"""
    over = load_override(role, home)
    source = 'override' if over.get('model') else 'roles.json' if cfg.get('model') else 'default'
    return dict(model=over.get('model') or cfg.get('model'),
                provider=over.get('provider') or cfg.get('provider'), model_source=source)


def set_override(role: str, cfg: dict, home: Path, model: str, provider: str = '', check=None) -> str:
    """寫 var/roles/<role>/override.json；dispatcher 下一輪生效。model 空＝清除。
    check(cfg, model, provider) -> {ok, error}：實測新模型，不能用就不寫入。"""
    import os
    import re
    import uuid
    if cfg.get('driver') == 'manual' or cfg.get('rank') == 'human':
        raise OverrideError('這個角色不是 agent。')
    path = override_path(role, home)
    if not model:
        if provider:
            raise OverrideError('指定 provider 時須同時指定模型。')
        path.unlink(missing_ok=True)
        return 'cleared'
    for value in (model, provider):
        if value and not re.fullmatch(MODEL_ID_RE, value):
            raise OverrideError('模型／provider ID 只能含英文、數字、點、底線、冒號、斜線與連字號。')
    if check:
        chk = check(cfg, model, provider)
        if chk and not chk.get('ok'):
            raise OverrideError(f'模型 {model} 實測失敗，未切換：{chk.get("error", "")[:300]}')
    (home / 'roles' / role / 'model_alert.json').unlink(missing_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'override.{os.getpid()}.{uuid.uuid4().hex}.tmp')   # 並發寫入各用各的暫存檔
    tmp.write_text(json.dumps({'model': model, **({'provider': provider} if provider else {})}))
    os.replace(tmp, path)                                                     # 原子替換
    return 'set'


# ── 每群覆寫（SPEC-1.1 §6）：var/roles/<role>/rooms/<n>/override.json {acp_agent?, model?, provider?} ──

def room_override_path(role: str, room: int, home: Path) -> Path:
    return home / "roles" / role / "rooms" / str(int(room)) / "override.json"


def load_room_override(role: str, room, home: Path) -> dict:
    import re
    try:
        data = json.loads(room_override_path(role, int(room), home).read_text())
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for k in ("acp_agent", "model", "provider"):
        v = data.get(k)
        if isinstance(v, str) and re.fullmatch(MODEL_ID_RE, v):
            out[k] = v
    return out if (out.get("acp_agent") or out.get("model")) else {}


def room_cfg(role: str, cfg: dict, room, home: Path) -> dict:
    """角色設定疊上本群覆寫（在角色臨時覆寫之上）。換 agent 時一併換 runtime，且不沿用別的 agent 的模型。"""
    over = load_room_override(role, room, home)
    if not over:
        return cfg
    out = dict(cfg, _room_override=True)
    out.setdefault("_roles_model", cfg.get("_roles_model", cfg.get("model")))
    out.setdefault("_roles_provider", cfg.get("_roles_provider", cfg.get("provider")))
    if over.get("acp_agent") and over["acp_agent"] != cfg.get("acp_agent"):
        out.update(acp_agent=over["acp_agent"], runtime=f"acp-{over['acp_agent']}")
        out.pop("model", None)
        out.pop("provider", None)
    for k in ("model", "provider"):
        if over.get(k):
            out[k] = over[k]
    return out


def set_room_override(role: str, room: int, home: Path, acp_agent: str = "", model: str = "",
                      provider: str = "") -> str:
    import os
    import re
    import uuid
    path = room_override_path(role, room, home)
    if not acp_agent and not model:
        path.unlink(missing_ok=True)
        return "cleared"
    for v in (acp_agent, model, provider):
        if v and not re.fullmatch(MODEL_ID_RE, v):
            raise OverrideError("agent／模型／provider ID 只能含英文、數字、點、底線、冒號、斜線與連字號。")
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {k: v for k, v in (("acp_agent", acp_agent), ("model", model), ("provider", provider)) if v}
    tmp = path.with_name(f"override.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(body))
    os.replace(tmp, path)
    return "set"
