import json

from openhack import model_catalog


def test_excluded_plans_have_no_bundled_models():
    assert model_catalog.bundled_models("opencode") == []
    assert model_catalog.bundled_models("opencode-go") == []


def test_openhack_models_are_not_hardcoded_in_terminal():
    assert model_catalog.bundled_models("openhack") == []


def test_merge_uses_live_catalog_metadata_and_order():
    merged = model_catalog.merge_models(
        "openhack",
        [
            {
                "id": "deepseek-live",
                "label": "DeepSeek Live",
                "desc": "Deployed now",
                "family": "DeepSeek",
                "created_at": "2026-08-06T00:00:00Z",
                "tab": "openhack",
            },
            {
                "id": "gpt-live",
                "label": "GPT Live",
                "family": "GPT",
                "tab": "openai",
            },
        ],
    )
    assert [row["id"] for row in merged] == ["deepseek-live", "gpt-live"]
    assert merged[0]["family"] == "DeepSeek"
    assert merged[0]["desc"] == "Deployed now"
    assert merged[1]["tab"] == "openai"


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
