"""測試隔離（#40-2）：角色 shell 帶著 live 的 MBOX_*／AAF_*／AAF_SERVER_STATE／AAF_ROLE，
直接跑 pytest 時會洩漏進測試（例：test_mbox e2e 用到 live token → 401）。

用 session 範圍的 autouse fixture 在任何測試與 module fixture 之前清掉，結束後還原。
所有 MBOX_*／AAF_* 都清掉（包含 MBOX_BUSY_TIMEOUT_MS、MBOX_MUST_MINUTES 等調校變數）；
測試需要哪個變數，請在自己的 fixture 內用 monkeypatch.setenv 設定。
不用 function 範圍：test_server 的 module fixture 會自己設定 MBOX_HOME／MBOX_URL，
若每個測試前都再清一次，會把 module fixture 剛設好的值刪掉。
"""
import os

import pytest

LEAKY_PREFIXES = ('MBOX_', 'AAF_')
LEAKY_NAMES = ('AAF_SERVER_STATE', 'AAF_ROLE')


def leaky(name):
    return name.startswith(LEAKY_PREFIXES) or name in LEAKY_NAMES


@pytest.fixture(scope='session', autouse=True)
def _isolate_role_env():
    mp = pytest.MonkeyPatch()
    for name in [n for n in os.environ if leaky(n)]:
        mp.delenv(name)
    yield
    mp.undo()
