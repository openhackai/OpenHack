import json

from openhack import model_catalog


def test_excluded_plans_have_no_bundled_models():
    assert model_catalog.bundled_models("opencode") == []
    assert model_catalog.bundled_models("opencode-go") == []


def test_openhack_bundles_all_hosted_model_metadata():
    models = model_catalog.bundled_models("openhack")
    assert [model["id"] for model in models] == [
        "glm-5.2",
        "grok-4.5",
        "kimi-k2.5",
        "gemma-4-31b",
        "gpt-oss-120b",
        "deepseek-v3.2",
        "minimax-m2.5",
        "gemini-3-flash",
        "nemotron-3-super",
        "gpt-5.6-luna",
        "gpt-5.6-luna-pro",
        "gpt-5.6-terra",
        "gpt-5.6-terra-pro",
        "gpt-5.6-sol",
        "gpt-5.6-sol-pro",
    ]


def test_existing_openhack_models_keep_top_rank():
    ranked = model_catalog.rank_model_ids(
        ["other", "kimi-k2.5", "grok-4.5", "glm-5.2", "last"]
    )
    assert ranked == ["glm-5.2", "grok-4.5", "kimi-k2.5", "other", "last"]


def test_merge_uses_live_ids_but_catalog_labels():
    merged = model_catalog.merge_models(
        "openhack", ["unknown-live", "glm-5.2", "grok-4.5"]
    )
    assert [row["id"] for row in merged] == [
        "glm-5.2",
        "grok-4.5",
        "unknown-live",
    ]
    assert merged[0]["label"] == "GLM 5.2"


def test_models_dev_cache_and_compatible_provider_discovery(monkeypatch, tmp_path):
    payload = {
        "opencode": {
            "id": "opencode",
            "name": "Excluded plan",
            "npm": "@ai-sdk/openai-compatible",
            "api": "https://excluded.test/v1",
            "env": ["EXCLUDED_KEY"],
            "models": {"excluded": {"name": "Excluded"}},
        },
        "compatible": {
            "id": "compatible",
            "name": "Compatible",
            "npm": "@ai-sdk/openai-compatible",
            "api": "https://example.test/v1",
            "env": ["EXAMPLE_KEY"],
            "models": {"model-a": {"name": "Model A"}},
        },
        "native": {
            "id": "native",
            "name": "Native only",
            "npm": "@ai-sdk/anthropic",
            "api": "https://native.test",
            "env": ["NATIVE_KEY"],
            "models": {"native-a": {"name": "Native A"}},
        },
    }

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(payload).encode()

    cache = tmp_path / "models.json"
    monkeypatch.setattr(model_catalog.urllib.request, "urlopen", lambda *a, **k: Response())
    loaded = model_catalog.load_models_dev(refresh=True, cache_path=cache)
    assert loaded == payload
    assert json.loads(cache.read_text()) == payload

    monkeypatch.setattr(model_catalog, "load_models_dev", lambda **kwargs: payload)
    found = model_catalog.discover_compatible_providers()
    assert [provider.id for provider in found] == ["compatible"]
    assert model_catalog.provider_from_models_dev(
        "opencode", catalog=payload
    ) is None


def test_unresolved_endpoint_template_is_not_offered(monkeypatch):
    monkeypatch.delenv("ACCOUNT_ID", raising=False)
    catalog = {
        "templated": {
            "name": "Templated",
            "api": "https://${ACCOUNT_ID}.example.test/v1",
            "env": ["KEY"],
            "models": {"m": {"name": "M"}},
        }
    }
    assert model_catalog.provider_from_models_dev(
        "templated", catalog=catalog
    ) is None


def test_model_catalog_does_not_require_models_dev_api_url(monkeypatch):
    catalog = {
        "openai": {
            "name": "OpenAI",
            "models": {
                "gpt-a": {"name": "GPT A"},
                "old": {"name": "Old", "status": "deprecated"},
            },
        }
    }
    monkeypatch.setattr(
        model_catalog,
        "load_models_dev",
        lambda **kwargs: catalog,
    )

    assert model_catalog.bundled_models("openai") == [
        {"id": "gpt-a", "label": "GPT A", "desc": ""}
    ]
    assert model_catalog.provider_from_models_dev(
        "openai", catalog=catalog
    ) is None


def test_cache_only_discovery_never_waits_for_network(monkeypatch, tmp_path):
    payload = {
        "cached": {
            "name": "Cached",
            "npm": "@ai-sdk/openai-compatible",
            "api": "https://cached.test/v1",
            "env": ["CACHED_KEY"],
            "models": {"model": {"name": "Model"}},
        }
    }
    cache = tmp_path / "models.json"
    cache.write_text(json.dumps(payload))
    monkeypatch.setattr(model_catalog, "_MEMORY_CATALOG", None)
    calls = []
    monkeypatch.setattr(
        model_catalog.urllib.request,
        "urlopen",
        lambda *args, **kwargs: calls.append(True),
    )

    loaded = model_catalog.load_models_dev(
        allow_network=False,
        cache_path=cache,
    )

    assert loaded == payload
    assert calls == []
