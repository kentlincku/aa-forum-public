"""作業系統帳號登入（AAF_AUTH=os）：用本機 SSH 驗證「執行 AA Forum 的那個 OS 帳號」的密碼。

- 只驗證本服務執行身分自己的帳號，不能拿來探測別的帳號。
- 密碼只經 stdin 傳給子程序，不進命令列、不落檔、不記 log。
- 驗證走 127.0.0.1 的 sshd；主機金鑰固定為本機 /etc/ssh/ssh_host_ed25519_key.pub，不接受其他金鑰。
- 只做認證，不開 shell、不執行任何指令。
需要：本機 sshd 允許密碼登入、python 有 paramiko（AAF_AUTH_PYTHON 指定直譯器，預設 /usr/bin/python3）。
"""
import base64
import json
import os
import pwd
import sys
from pathlib import Path


def main() -> int:
    try:
        import paramiko
    except ImportError:
        return 3
    raw = sys.stdin.buffer.read(4097)
    if len(raw) > 4096:
        return 1
    try:
        body = json.loads(raw)
        username, password = body["username"], body["password"]
    except (ValueError, KeyError, TypeError):
        return 1
    if username != pwd.getpwuid(os.geteuid()).pw_name:
        return 1
    if not isinstance(password, str) or not 1 <= len(password) <= 200:
        return 1
    try:
        kind, encoded, *_ = Path("/etc/ssh/ssh_host_ed25519_key.pub").read_text().split()
    except OSError:
        return 3
    if kind != "ssh-ed25519":
        return 3
    client = paramiko.SSHClient()
    try:
        client.get_host_keys().add("127.0.0.1", kind, paramiko.Ed25519Key(data=base64.b64decode(encoded)))
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        client.connect("127.0.0.1", username=username, password=password, allow_agent=False,
                       look_for_keys=False, timeout=5, banner_timeout=5, auth_timeout=8)
        t = client.get_transport()
        return 0 if t is not None and t.is_authenticated() else 1
    except Exception:
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
