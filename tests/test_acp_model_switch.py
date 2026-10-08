"""ACP 模型切換方式的選擇（pi-acp 走 config option；hermes／claude 走 set_model；codex 不回報）。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from drivers.acp_catalog import model_switch  # noqa: E402

PI = {"configOptions": [{"id": "model", "category": "model", "currentValue": "omlx/A",
                         "options": [{"value": "omlx/A"}, {"value": "omlx/B"}]}]}
HM = {"models": {"currentModelId": "copilot:x", "availableModels": [{"modelId": "copilot:x"}, {"modelId": "custom:omlx:B"}]}}


def test_config_option_route_for_pi():
    assert model_switch(PI, "omlx/B") == ("session/set_config_option", {"configId": "model", "value": "omlx/B"}, "omlx/B")
    assert model_switch(PI, "B")[2] == "omlx/B"                    # 前綴可省
    assert model_switch(PI, "omlx/A") is None                      # 已是該模型
    with pytest.raises(ValueError):
        model_switch(PI, "omlx/nope")


def test_set_model_route():
    assert model_switch(HM, "custom:omlx:B") == ("session/set_model", {"modelId": "custom:omlx:B"}, "custom:omlx:B")
    assert model_switch(HM, "omlx:B")[2] == "custom:omlx:B"
    with pytest.raises(ValueError):
        model_switch(HM, "ghost")


def test_no_list_reported():
    assert model_switch({}, "gpt-x") is None
