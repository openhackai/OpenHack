import json
import os
from pathlib import Path
from unittest.mock import patch

from openhack.config import (
    CONFIG_PATH,
    Settings,
    load_user_config,
    resolve_provider,
    save_user_config,
)


class TestLoadSaveConfig:
    def test_roundtrip(self, tmp_path):
        config_path = tmp_path / "config"
        with patch("openhack.config.CONFIG_PATH", config_path), \
             patch("openhack.config.CONFIG_DIR", tmp_path):
            save_user_config({"provider": "openhack", "model": "kimi-k2.5"})
            loaded = load_user_config()
            assert loaded["provider"] == "openhack"
            assert loaded["model"] == "kimi-k2.5"

    def test_load_missing_file(self, tmp_path):
        config_path = tmp_path / "nonexistent"
        with patch("openhack.config.CONFIG_PATH", config_path):
            result = load_user_config()
            assert result == {}

    def test_save_merges(self, tmp_path):
        config_path = tmp_path / "config"
        with patch("openhack.config.CONFIG_PATH", config_path), \
             patch("openhack.config.CONFIG_DIR", tmp_path):
            save_user_config({"a": 1})
            save_user_config({"b": 2})
            loaded = load_user_config()
            assert loaded == {"a": 1, "b": 2}

    def test_removed_provider_falls_back_to_openhack(self, tmp_path):
        config_path = tmp_path / "config"
        config_path.write_text(json.dumps({
            "provider": "opencode-go",
            "model": "qwen3.7-plus",
        }))
        with patch("openhack.config.CONFIG_PATH", config_path):
            loaded = load_user_config()

        assert loaded["provider"] == "openhack"
        assert loaded["model"] == "grok-4.5"
        assert resolve_provider("opencode") == "openhack"


class TestSettings:
    def test_defaults(self):
        with patch.dict(os.environ, {}, clear=False):
            s = Settings()
            assert s.llm_provider == "openhack"
            assert s.openhack_dev is False
            assert s.max_feature_hunters == 7

    def test_dev_mode_urls(self):
        with patch.dict(os.environ, {"OPENHACK_DEV": "1"}, clear=False):
            s = Settings()
            assert "localhost" in s.openhack_app_url
            assert "localhost" in s.openhack_base_url

    def test_prod_mode_urls(self):
        with patch.dict(os.environ, {"OPENHACK_DEV": "0"}, clear=False):
            s = Settings()
            assert "app.openhack.com" in s.openhack_app_url
            assert "api.openhack.com" in s.openhack_base_url
