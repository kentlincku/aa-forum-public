"""#40-2：conftest 已清掉角色環境變數（自帶前綴清單，不 import conftest，避免依賴 rootdir）。"""
import os

PREFIXES = ('MBOX_', 'AAF_')
NAMES = ('AAF_SERVER_STATE', 'AAF_ROLE')
SET_BY_MODULE_FIXTURES = ('MBOX_HOME', 'MBOX_URL', 'AAF_SERVER_STATE')  # test_server 等自設


def test_role_env_cleared():
    leaked = [n for n in os.environ if (n.startswith(PREFIXES) or n in NAMES) and n not in SET_BY_MODULE_FIXTURES]
    assert leaked == []
    assert 'MBOX_TOKEN' not in os.environ and 'MBOX_AGENT' not in os.environ and 'AAF_AGENT' not in os.environ
