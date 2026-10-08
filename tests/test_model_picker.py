"""任務 #27：AA Forum 模型下拉清單（後端清單 API ＋ 前端純函式）。"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

CONFIG = """model:
  default: claude-opus-5.5
  provider: copilot
  context_length: 262144
agent:
  x: 1
providers:
  omlx:
    name: oMLX
    base_url: http://127.0.0.1:8000/v1
    api_key: SECRET-SHOULD-NOT-LEAK
    default_model: Qwen3.6-27B-4bit
  bad provider:
    default_model: x
onboarding:
  y: 1
"""

CATALOG = {
    'github-copilot': {'name': 'GitHub Copilot', 'models': {
        'gpt-5.4': {'name': 'GPT-5.4', 'tool_call': True, 'limit': {'context': 1050000}},
        'claude-opus-5.5': {'name': 'Claude Opus 5.5', 'tool_call': True, 'limit': {'context': 1000000}},
        'embed-1': {'name': 'Embed', 'tool_call': False, 'limit': {'context': 8000}},   # 不可用工具 → 不列
        'bad id;rm': {'tool_call': True},                                                # 不合 regex → 不列
        'str-ctx': {'tool_call': True, 'limit': {'context': '1M'}},                      # context 非 int → None
    }},
    'openai': {'models': {'gpt-x': {'tool_call': True}}},   # 本機未設定 openai → 不列
}


@pytest.fixture()
def mc(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'server'))
    monkeypatch.syspath_prepend(str(ROOT))
    import model_catalog
    return model_catalog


def home_with(tmp_path, config=CONFIG, catalog=CATALOG):
    home = tmp_path / 'hermes'
    home.mkdir()
    if config is not None:
        (home / 'config.yaml').write_text(config)
    if catalog is not None:
        (home / 'models_dev_cache.json').write_text(catalog if isinstance(catalog, str) else json.dumps(catalog))
    return home


def test_catalog_provider_mapping_and_filters(mc, tmp_path):
    c = mc.model_catalog(home_with(tmp_path))
    assert c['default'] == {'provider': 'copilot', 'model': 'claude-opus-5.5'} and c['warnings'] == []
    ids = [p['id'] for p in c['providers']]
    assert ids == ['copilot', 'omlx']                     # openai 未設定不列；不合法 provider 名不列
    cop = c['providers'][0]
    assert cop['label'] == 'GitHub Copilot'               # copilot → github-copilot 目錄
    models = {m['id']: m for m in cop['models']}
    assert set(models) == {'claude-opus-5.5', 'gpt-5.4', 'str-ctx'}
    assert models['gpt-5.4']['context'] == 1050000 and models['str-ctx']['context'] is None
    assert c['providers'][1]['models'] == [{'id': 'Qwen3.6-27B-4bit', 'name': 'Qwen3.6-27B-4bit', 'context': None}]
    assert 'SECRET' not in json.dumps(c)                  # 不外帶憑證


def test_catalog_no_cache(mc, tmp_path):
    c = mc.model_catalog(home_with(tmp_path, catalog=None))
    assert [p['id'] for p in c['providers']] == ['copilot', 'omlx']
    assert [m['id'] for m in c['providers'][0]['models']] == ['claude-opus-5.5']   # 預設模型仍在
    assert 'models.dev 快取不可用' in c['warnings'][0]


@pytest.mark.parametrize('raw', ['{broken', '[1,2]', json.dumps({'github-copilot': {'models': 'x'}}),
                                 json.dumps({'github-copilot': 5})])
def test_catalog_bad_cache(mc, tmp_path, raw):
    c = mc.model_catalog(home_with(tmp_path, catalog=raw))
    assert c['providers'][0]['id'] == 'copilot'
    assert [m['id'] for m in c['providers'][0]['models']] == ['claude-opus-5.5']


def test_catalog_no_config(mc, tmp_path):
    c = mc.model_catalog(home_with(tmp_path, config=None))
    assert c['providers'] == [] and 'config.yaml' in c['warnings'][0]


# API 路由（Owner 限定）的測試在 tests/test_server.py::test_health_control_quota_files，用真實 app 與 token。
