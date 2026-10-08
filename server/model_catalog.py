"""相容墊片：模型目錄已移入 drivers/hermes_catalog.py（v2 S5）。AA Forum 經 driver 的 model_catalog() 取用。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from drivers.hermes_catalog import *  # noqa: E402,F401,F403
from drivers.hermes_catalog import model_catalog  # noqa: E402,F401
