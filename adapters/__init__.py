"""相容層（S2）：adapters 即 drivers。S5 移除；新程式請 import drivers。"""
import sys

import drivers as _drivers

sys.modules[__name__] = _drivers
