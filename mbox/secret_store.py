"""統一的機密讀取介面（P2 / D2）。

機密永遠不進 git，也不印到畫面。讀取順序（先找到先用）：
  1. 環境變數 AAF_SECRET_<NAME>（NAME 轉大寫、- 換成 _）
  2. 檔案 $AAF_SECRETS_DIR/<name>（預設 ~/.aaf/secrets/<name>），權限必須是 0600，否則拒讀
  3. macOS Keychain：generic password，service=<name>（`security find-generic-password -s <name> -w`）

CLI：
  python -m mbox.secret_store check <name> [...]   存在與否與來源，不印值；全部存在才 exit 0
  python -m mbox.secret_store set <name> [--keychain]  從隱藏輸入（或 stdin 管線）讀值，寫入 0600 檔（或 Keychain）
  python -m mbox.secret_store get <name>           印值到 stdout（給 shell 工具用；不要在對話或 log 中呼叫）
"""
from __future__ import annotations

import getpass
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class SecretError(Exception):
    pass


def _check_name(name: str) -> str:
    if not NAME_RE.match(name or ""):
        raise SecretError(f"不合法的機密名稱：{name!r}（只允許英數、_ . -，最長 64）")
    return name


def env_key(name: str) -> str:
    return "AAF_SECRET_" + name.upper().replace("-", "_").replace(".", "_")


def secrets_dir() -> Path:
    return Path(os.environ.get("AAF_SECRETS_DIR") or Path.home() / ".aaf" / "secrets")


def _keychain_enabled() -> bool:
    return sys.platform == "darwin" and os.environ.get("AAF_SECRETS_NO_KEYCHAIN") != "1"


def _from_file(name: str) -> str | None:
    f = secrets_dir() / name
    if not f.is_file():
        return None
    mode = stat.S_IMODE(f.stat().st_mode)
    if mode != 0o600:
        raise SecretError(f"{f} 權限是 {oct(mode)}，必須是 0600（chmod 600 {f}）；拒讀")
    return f.read_text(encoding="utf-8").rstrip("\n")


def _from_keychain(name: str) -> str | None:
    if not _keychain_enabled():
        return None
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", name, "-w"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.rstrip("\n") if r.returncode == 0 and r.stdout else None


def lookup(name: str) -> tuple[str | None, str | None]:
    """回傳 (值, 來源)；找不到回 (None, None)。來源為 env / file / keychain。"""
    _check_name(name)
    v = os.environ.get(env_key(name))
    if v:
        return v, "env"
    v = _from_file(name)
    if v:
        return v, "file"
    v = _from_keychain(name)
    if v:
        return v, "keychain"
    return None, None


def get(name: str) -> str:
    v, _ = lookup(name)
    if v is None:
        raise SecretError(f"找不到機密 {name}：請設環境變數 {env_key(name)}，"
                          f"或執行 bin/aaf secret set {name}")
    return v


def source(name: str) -> str | None:
    return lookup(name)[1]


def set_value(name: str, value: str, keychain: bool = False) -> str:
    _check_name(name)
    if not value:
        raise SecretError("值是空的，不寫入")
    if keychain:
        raise SecretError("Keychain 請用 set --keychain 的互動模式（由 security 自己提示輸入，值不經過本程式）")
    d = secrets_dir()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    f = d / name
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(value + "\n")
    os.chmod(f, 0o600)
    return "file"


def _cli(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd, args = argv[0], argv[1:]
    try:
        if cmd == "check":
            if not args:
                print("用法：secret check <name> [...]", file=sys.stderr)
                return 2
            ok = True
            for n in args:
                src = source(n)
                print(f"{n}: {'存在（' + src + '）' if src else '缺少'}")
                ok = ok and bool(src)
            return 0 if ok else 1
        if cmd == "get":
            if len(args) != 1:
                print("用法：secret get <name>", file=sys.stderr)
                return 2
            sys.stdout.write(get(args[0]))
            return 0
        if cmd == "set":
            names = [a for a in args if not a.startswith("--")]
            if len(names) != 1:
                print("用法：secret set <name> [--keychain]", file=sys.stderr)
                return 2
            if "--keychain" in args:
                _check_name(names[0])
                if not (_keychain_enabled() and sys.stdin.isatty()):
                    raise SecretError("--keychain 需要在 macOS 的互動終端執行")
                # -w 放最後且不帶值：security 自己提示輸入，值不出現在任何程序參數或 log
                r = subprocess.run(["security", "add-generic-password", "-U",
                                    "-a", os.environ.get("USER") or "aaf", "-s", names[0], "-w"])
                if r.returncode != 0:
                    raise SecretError(f"寫入 Keychain 失敗（exit {r.returncode}）")
                print(f"{names[0]}: 已寫入（keychain）")
                return 0
            value = getpass.getpass(f"{names[0]} 的值（不會顯示）：") if sys.stdin.isatty() \
                else sys.stdin.read().rstrip("\n")
            where = set_value(names[0], value)
            print(f"{names[0]}: 已寫入（{where}）")
            return 0
    except SecretError as e:
        print(f"secret: {e}", file=sys.stderr)
        return 1
    print(f"未知指令：{cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
