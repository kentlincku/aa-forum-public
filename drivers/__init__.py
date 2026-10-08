"""driver 註冊表。核心只透過這裡取得 driver；roles.json 以 `driver` 欄位指定（舊的 `adapter` 欄位仍可讀，見 resolve）。

舊名對照（相容）：claude-headless→claude、codex-headless→codex、hermes-headless→hermes、
pi-headless→pi、command→command、tmux→tmux、hook→hook、manual→manual。
"""
from __future__ import annotations

import json
import subprocess  # noqa: F401  （測試透過 drivers.subprocess 攔截 Popen）
from pathlib import Path

from drivers.base import (ROOT, PREVIEW_LIMIT, Adapter, Headless, Hooked, Tmux,  # noqa: F401
                          MODEL_ID_RE, effective_cfg, load_override, override_path,
                          render_preview, system_prompt, tmux_room_exists, wake_prompt)
from drivers.claude import ClaudeHeadless
from drivers.codex import CodexHeadless
from drivers.command import CommandHeadless
from drivers.external import ExternalHeadless
from drivers.acp import AcpDriver
from drivers.hermes import HermesHeadless
from drivers.pi import PiHeadless

DRIVERS: dict[str, type[Adapter]] = {
    "claude": ClaudeHeadless, "codex": CodexHeadless, "hermes": HermesHeadless, "pi": PiHeadless,
    "command": CommandHeadless, "external": ExternalHeadless, "acp": AcpDriver, "tmux": Tmux, "hook": Hooked, "manual": Adapter,
}
LEGACY = {"claude-headless": "claude", "codex-headless": "codex", "hermes-headless": "hermes",
          "pi-headless": "pi"}
ADAPTERS = {**{k: DRIVERS[v] for k, v in LEGACY.items()},
            "command": CommandHeadless, "tmux": Tmux, "hook": Hooked, "manual": Adapter}

_warned: set[str] = set()


def resolve(cfg: dict) -> str:
    """回傳 driver 名稱。優先 cfg.driver；否則由舊欄位 adapter 對照（標 deprecated）。"""
    if cfg.get("driver"):
        return cfg["driver"]
    kind = cfg.get("adapter", "manual")
    name = LEGACY.get(kind, kind)
    if kind in LEGACY and kind not in _warned:
        _warned.add(kind)
        import sys
        print(f"[drivers] roles.json 欄位 adapter={kind} 已 deprecated，請改 driver={name}", file=sys.stderr)
    return name


def is_manual(cfg: dict) -> bool:
    return resolve(cfg) == "manual"


def wake_level(cfg: dict) -> str:
    cls = DRIVERS.get(resolve(cfg))
    return getattr(cls, "level", "manual") if cls else "manual"


def make(role: str, cfg: dict, home: Path, lane=None) -> Adapter:
    """lane：每群獨立工作階段（SPEC-1.1 §2）。None／"default"＝角色的預設工作階段；群號＝該群專用。
    只有支援的 driver（lanes=True）會用到；其他 driver 一律單一工作階段。"""
    cfg = effective_cfg(role, cfg, home)
    name = resolve(cfg)
    if name not in DRIVERS:
        raise SystemExit(f"{role}: 不認得的 driver {name}（可用：{', '.join(DRIVERS)}）")
    cls = DRIVERS[name]
    if lane not in (None, "default") and supports_lanes(cfg):
        from mbox.overrides import room_cfg
        cfg = room_cfg(role, cfg, lane, home)       # 本群覆寫（SPEC-1.1 §6）；只換 agent／模型，driver 不變
        return cls(role, cfg, home, lane=int(lane))
    return cls(role, cfg, home)


def supports_lanes(cfg: dict) -> bool:
    """這個角色是否每群一個工作階段：driver 支援，且 roles.json 沒有 room_sessions: false。"""
    cls = DRIVERS.get(resolve(cfg))
    return bool(getattr(cls, "lanes", False)) and cfg.get("room_sessions", True) is not False


def manifests() -> dict[str, dict]:
    return {n: c.manifest() for n, c in DRIVERS.items()}


def check_model_override(cfg: dict, model: str, provider: str = ""):
    """切換模型前實測（目前只有 ACP 角色支援）；回 None 代表不檢查。"""
    if cfg.get("driver") != "acp":
        return None
    from drivers import acp_catalog
    agent = cfg.get("acp_agent", "hermes")
    mid = f"{provider}:{model}" if provider and agent == "hermes" and ":" not in model else model
    return acp_catalog.check_model(agent, mid)
