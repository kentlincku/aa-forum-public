"""P2：統一機密介面 mbox.secret_store。"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from mbox import secret_store as ss  # noqa: E402


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("AAF_SECRETS_DIR", str(tmp_path / "sec"))
    monkeypatch.setenv("AAF_SECRETS_NO_KEYCHAIN", "1")   # 測試絕不碰真的 Keychain
    for k in list(os.environ):
        if k.startswith("AAF_SECRET_"):
            monkeypatch.delenv(k)


def test_missing():
    assert ss.lookup("demo-x") == (None, None)
    with pytest.raises(ss.SecretError):
        ss.get("demo-x")


def test_file_then_env_priority(monkeypatch):
    assert ss.set_value("demo-x", "from-file") == "file"
    f = Path(os.environ["AAF_SECRETS_DIR"]) / "demo-x"
    assert oct(f.stat().st_mode & 0o777) == "0o600"
    assert ss.lookup("demo-x") == ("from-file", "file")
    monkeypatch.setenv("AAF_SECRET_DEMO_X", "from-env")
    assert ss.lookup("demo-x") == ("from-env", "env")


def test_bad_permission_refused():
    ss.set_value("demo-x", "v")
    f = Path(os.environ["AAF_SECRETS_DIR"]) / "demo-x"
    os.chmod(f, 0o644)
    with pytest.raises(ss.SecretError, match="0600"):
        ss.lookup("demo-x")


def test_bad_name():
    for bad in ("", "../x", "a b", "x/y"):
        with pytest.raises(ss.SecretError):
            ss.lookup(bad)


def test_cli_check_does_not_print_value():
    ss.set_value("demo-x", "TOPSECRET-123")
    env = dict(os.environ)
    r = subprocess.run([sys.executable, "-m", "mbox.secret_store", "check", "demo-x", "demo-missing"],
                       cwd=ROOT, env=env, capture_output=True, text=True)
    assert r.returncode == 1
    assert "TOPSECRET" not in r.stdout + r.stderr
    assert "demo-x: 存在（file）" in r.stdout and "demo-missing: 缺少" in r.stdout


def test_cli_set_from_stdin():
    r = subprocess.run([sys.executable, "-m", "mbox.secret_store", "set", "demo-y"], cwd=ROOT,
                       env=dict(os.environ), input="v-123\n", capture_output=True, text=True)
    assert r.returncode == 0 and "v-123" not in r.stdout
    assert ss.get("demo-y") == "v-123"


def test_repo_has_no_known_plaintext_patterns():
    """git 追蹤的檔案裡不得出現 BOARD_ROOT_PASS=<值> 或 RDD1_BUILD_PASS=<值> 這類明碼賦值。"""
    out = subprocess.run(["git", "grep", "-nE", r"(BOARD_ROOT_PASS|RDD1_BUILD_PASS)=['\"]?[A-Za-z0-9!@#%^&*]{4,}"],
                         cwd=ROOT, capture_output=True, text=True).stdout
    assert out.strip() == "", out
