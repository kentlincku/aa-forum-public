"""公版／實例的路徑（P5）。

CORE      公版程式所在（本 repo）：mbox/、server/、drivers/、contract/、bin/、vendor/。
INSTANCE  一套部署的根目錄（$AAF_HOME；沒設就是 CORE，與 v2 單一 repo 相容）。
          訂製版 repo 就是一個 INSTANCE，底下放：
            deploy/roles.json   角色與 auth 宣告
            skills/  rules/  hooks/  reminders/   內容
            var/  work/  outputs/                  執行期狀態（不進 git）
            .aaf.env                          選用：port、tmux 前綴等環境設定（bin/aaf 啟動時載入）
"""
from __future__ import annotations

import os
from pathlib import Path

CORE = Path(__file__).resolve().parent.parent


def instance() -> Path:
    return Path(os.path.expanduser(os.environ.get("AAF_HOME") or str(CORE)))


def var() -> Path:
    return Path(os.environ.get("MBOX_HOME") or instance() / "var")


def roles_file() -> Path:
    return Path(os.environ.get("MBOX_ROLES") or instance() / "deploy" / "roles.json")


def resolve(p: str | os.PathLike) -> Path:
    """roles.json 裡的相對路徑（persona_file 等）：先找 INSTANCE，再找 CORE。"""
    pp = Path(os.path.expanduser(str(p)))
    if pp.is_absolute():
        return pp
    for base in (instance(), CORE):
        if (base / pp).exists():
            return base / pp
    return instance() / pp


_ENV_KEYS = ("AAF_", "MBOX_PORT", "MBOX_URL")


def setting(key: str, default: str | None = None) -> str | None:
    """環境變數優先；沒有就讀實例的 .aaf.env（只接受白名單 KEY=VALUE）。
    讓不經 bin/* 啟動的 Python 入口（測試、工具）也拿到同一份實例設定。"""
    if os.environ.get(key):
        return os.environ[key]
    f = instance() / ".aaf.env"
    if key.startswith(_ENV_KEYS) and f.is_file():
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if k.strip() == key:
                    return v.strip()
    return default


def tmux_prefix() -> str:
    return setting("AAF_TMUX_PREFIX", "civ-")
