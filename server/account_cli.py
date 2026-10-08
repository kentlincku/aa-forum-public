"""bin/aaf account：在終端建立／重設 AA Forum 帳號（SPEC-1.0 S1）。

  aaf account create             帳號＝roles.json 第一位 rank=human；互動輸入密碼（隱藏、輸兩次）；已有帳號時需輸入通行碼
  aaf account list               列出帳號名
  aaf account ensure             沒有任何帳號時才互動建立；非互動環境印出 /setup 網址（給 up 呼叫）
密碼只經 getpass，不出現在參數、環境或 log。
"""
from __future__ import annotations

import getpass
import os
import secrets
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))


def _app():
    import app  # noqa: E402  讀同一份 AAF_SERVER_STATE
    return app


def _ask_password() -> str | None:
    for _ in range(3):
        p1 = getpass.getpass("密碼（至少 8 字，不會顯示）：")
        if len(p1) < 8:
            print("太短，至少 8 字。")
            continue
        if getpass.getpass("再輸入一次：") != p1:
            print("兩次不一樣。")
            continue
        return p1
    return None


def create(username: str | None) -> int:
    app = _app()
    if app.list_accounts():
        pc = getpass.getpass("已有帳號；請輸入通行碼（var/server/login_passcode.txt）：")
        if not secrets.compare_digest(pc.encode(), app.login_passcode().encode()):
            print("通行碼錯誤", file=sys.stderr)
            return 1
    # 一個實例一個擁有者：帳號名＝roles.json 第一位 rank=human（app.INSTANCE）
    if username and username != app.INSTANCE:
        print(f"此實例的帳號只能是 {app.INSTANCE}（roles.json 第一位 rank=human）", file=sys.stderr)
        return 2
    username = app.INSTANCE
    print(f"帳號：{username}")
    pw = _ask_password()
    if not pw:
        return 1
    app.save_account(username, pw)
    print(f"AA Forum 帳號 {username} 已建立。")
    return 0


def ensure() -> int:
    app = _app()
    if getattr(app, "AUTH_MODE", "local") == "os":
        print(f"AA Forum 登入：用這台主機的系統帳號（{app.os_user()}）與密碼。")
        return 0
    if app.list_accounts():
        return 0
    port = os.environ.get("AAF_SERVER_PORT", "8111")
    if not sys.stdin.isatty():
        print(f"AA Forum 還沒有帳號：開 http://127.0.0.1:{port}/app/ 建立（或在終端執行 bin/aaf account create）。")
        return 0
    print("AA Forum 還沒有帳號，現在建立第一個（之後用它登入網頁）。")
    return create(None)


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "list"
    if cmd == "list":
        for a in _app().list_accounts():
            print(a)
        return 0
    if cmd == "create":
        return create(argv[1] if len(argv) > 1 else None)
    if cmd == "ensure":
        return ensure()
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
