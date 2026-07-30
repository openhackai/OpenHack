import base64
import json
import os

from openhack import provider_auth


def _jwt(claims):
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"x.{body}.y"


def test_auth_store_is_separate_and_owner_only(tmp_path):
    path = tmp_path / ".openhack" / "auth.json"
    provider_auth.set_api_key("openrouter", "secret", path)

    assert provider_auth.get_credential("openrouter", path) == {
        "type": "api",
        "key": "secret",
    }
    assert os.stat(path).st_mode & 0o777 == 0o600
    provider_auth.remove_credential("openrouter", path)
    assert provider_auth.get_credential("openrouter", path) is None


def test_extract_account_id_matches_opencode_claim_priority():
    assert provider_auth.extract_account_id(
        {"id_token": _jwt({"chatgpt_account_id": "direct"})}
    ) == "direct"
    assert provider_auth.extract_account_id(
        {
            "access_token": _jwt(
                {
                    "https://api.openai.com/auth": {
                        "chatgpt_account_id": "namespaced"
                    }
                }
            )
        }
    ) == "namespaced"
    assert provider_auth.extract_account_id(
        {"id_token": _jwt({"organizations": [{"id": "org"}]})}
    ) == "org"


def test_refresh_updates_oauth_credential(monkeypatch, tmp_path):
    path = tmp_path / "auth.json"
    old = provider_auth.OAuthCredential("old-refresh", "old-access", 1, "acct")
    provider_auth.set_oauth("openai", old, path)
    monkeypatch.setattr(
        provider_auth,
        "_post_form",
        lambda *a, **k: {
            "refresh_token": "new-refresh",
            "access_token": "new-access",
            "expires_in": 3600,
        },
    )

    new = provider_auth.refresh_openai_credential(old, path=path, now_ms=2)
    assert new.access == "new-access"
    assert new.account_id == "acct"
    assert provider_auth.get_credential("openai", path)["refresh"] == "new-refresh"


def test_headless_device_flow_matches_opencode(monkeypatch, tmp_path):
    replies = iter(
        [
            (200, {"device_auth_id": "device", "user_code": "CODE", "interval": "1"}),
            (403, {}),
            (
                200,
                {"authorization_code": "authorization", "code_verifier": "verifier"},
            ),
        ]
    )
    seen = []
    monkeypatch.setattr(provider_auth, "_post_json", lambda *a, **k: next(replies))
    monkeypatch.setattr(provider_auth.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        provider_auth,
        "_post_form",
        lambda url, values: {
            "refresh_token": "refresh",
            "access_token": "access",
            "expires_in": 3600,
        },
    )

    result = provider_auth.openai_device_login(
        on_code=lambda url, code: seen.append((url, code)),
        path=tmp_path / "auth.json",
    )
    assert result.access == "access"
    assert seen == [("https://auth.openai.com/codex/device", "CODE")]
