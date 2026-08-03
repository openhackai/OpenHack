from openhack import config


def test_reload_settings_updates_existing_imports(monkeypatch, tmp_path):
    imported_alias = config.settings
    monkeypatch.setattr(
        imported_alias,
        "openhack_model_id",
        imported_alias.openhack_model_id,
    )
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config")
    monkeypatch.delenv("OPENHACK_MODEL_ID", raising=False)

    config.save_user_config({"openhack_model_id": "newly-connected-model"})
    config.reload_settings()

    assert config.settings is imported_alias
    assert imported_alias.openhack_model_id == "newly-connected-model"
