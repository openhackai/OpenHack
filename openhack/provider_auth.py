"""Secure provider credentials and ChatGPT subscription authentication.

The ChatGPT OAuth flow is a Python adaptation of OpenCode's MIT-licensed Codex
auth plugin (packages/opencode/src/plugin/openai/codex.ts).  It intentionally
keeps credentials separate from ordinary scanner preferences.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional

AUTH_PATH = Path.home() / ".openhack" / "auth.json"

OPENAI_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
OPENAI_ISSUER = "https://auth.openai.com"
OPENAI_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
OPENAI_CALLBACK_PORT = 1455
OPENAI_CALLBACK_PATH = "/auth/callback"
OPENAI_SUBSCRIPTION_MODELS = (
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
    "gpt-5.3-codex-spark",
    "gpt-5.4",
    "gpt-5.4-mini",
)


class ProviderAuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class OAuthCredential:
    refresh: str
    access: str
    expires: int
    account_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "type": "oauth",
            "refresh": self.refresh,
            "access": self.access,
            "expires": self.expires,
        }
        if self.account_id:
            result["accountId"] = self.account_id
        return result


def _load(path: Path = AUTH_PATH) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save(data: dict[str, dict[str, Any]], path: Path = AUTH_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def all_credentials(path: Path = AUTH_PATH) -> dict[str, dict[str, Any]]:
    return _load(path)


def get_credential(provider_id: str, path: Path = AUTH_PATH) -> Optional[dict[str, Any]]:
    value = _load(path).get(provider_id.rstrip("/"))
    return value if isinstance(value, dict) else None


def set_api_key(provider_id: str, key: str, path: Path = AUTH_PATH) -> None:
    data = _load(path)
    data[provider_id.rstrip("/")] = {"type": "api", "key": key}
    _save(data, path)


def set_oauth(provider_id: str, credential: OAuthCredential, path: Path = AUTH_PATH) -> None:
    data = _load(path)
    data[provider_id.rstrip("/")] = credential.to_dict()
    _save(data, path)


def remove_credential(provider_id: str, path: Path = AUTH_PATH) -> None:
    data = _load(path)
    data.pop(provider_id.rstrip("/"), None)
    _save(data, path)


def _base64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def parse_jwt_claims(token: str) -> Optional[dict[str, Any]]:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded))
        return value if isinstance(value, dict) else None
    except (ValueError, json.JSONDecodeError):
        return None


def extract_account_id(tokens: dict[str, Any]) -> Optional[str]:
    for token_key in ("id_token", "access_token"):
        claims = parse_jwt_claims(str(tokens.get(token_key) or ""))
        if not claims:
            continue
        direct = claims.get("chatgpt_account_id")
        if isinstance(direct, str) and direct:
            return direct
        namespaced = claims.get("https://api.openai.com/auth")
        if isinstance(namespaced, dict):
            account_id = namespaced.get("chatgpt_account_id")
            if isinstance(account_id, str) and account_id:
                return account_id
        organizations = claims.get("organizations")
        if isinstance(organizations, list) and organizations:
            first = organizations[0]
            if isinstance(first, dict) and isinstance(first.get("id"), str):
                return first["id"]
    return None


def _post_form(url: str, values: dict[str, str], timeout: float = 30.0) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(values).encode(),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "openhack",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode())
    except Exception as exc:
        raise ProviderAuthError(f"OpenAI token request failed: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProviderAuthError("OpenAI token response was not an object")
    return payload


def _post_json(
    url: str, values: dict[str, str], timeout: float = 30.0
) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        url,
        data=json.dumps(values).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "openhack"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode())
            return response.status, payload if isinstance(payload, dict) else {}
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode())
        except (ValueError, json.JSONDecodeError):
            payload = {}
        return exc.code, payload if isinstance(payload, dict) else {}
    except Exception as exc:
        raise ProviderAuthError(f"OpenAI device authorization failed: {exc}") from exc


def _credential_from_tokens(tokens: dict[str, Any]) -> OAuthCredential:
    access = str(tokens.get("access_token") or "")
    refresh = str(tokens.get("refresh_token") or "")
    if not access or not refresh:
        raise ProviderAuthError("OpenAI token response did not include access and refresh tokens")
    return OAuthCredential(
        refresh=refresh,
        access=access,
        expires=int(time.time() * 1000) + int(tokens.get("expires_in") or 3600) * 1000,
        account_id=extract_account_id(tokens),
    )


def refresh_openai_credential(
    credential: OAuthCredential,
    *,
    path: Path = AUTH_PATH,
    now_ms: Optional[int] = None,
) -> OAuthCredential:
    now = int(time.time() * 1000) if now_ms is None else now_ms
    if credential.access and credential.expires > now + 30_000:
        return credential
    tokens = _post_form(
        f"{OPENAI_ISSUER}/oauth/token",
        {
            "grant_type": "refresh_token",
            "refresh_token": credential.refresh,
            "client_id": OPENAI_CLIENT_ID,
        },
    )
    refreshed = _credential_from_tokens(tokens)
    if not refreshed.account_id and credential.account_id:
        refreshed = OAuthCredential(
            refresh=refreshed.refresh,
            access=refreshed.access,
            expires=refreshed.expires,
            account_id=credential.account_id,
        )
    set_oauth("openai", refreshed, path)
    return refreshed


def get_openai_oauth(path: Path = AUTH_PATH) -> Optional[OAuthCredential]:
    raw = get_credential("openai", path)
    if not raw or raw.get("type") != "oauth":
        return None
    try:
        return OAuthCredential(
            refresh=str(raw["refresh"]),
            access=str(raw.get("access") or ""),
            expires=int(raw.get("expires") or 0),
            account_id=(
                str(raw["accountId"]) if raw.get("accountId") else None
            ),
        )
    except (KeyError, TypeError, ValueError):
        return None


def openai_browser_login(
    *,
    open_browser: bool = True,
    timeout: float = 300.0,
    path: Path = AUTH_PATH,
) -> OAuthCredential:
    """Authenticate a ChatGPT Plus/Pro account through PKCE and localhost."""
    chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    verifier = "".join(secrets.choice(chars) for _ in range(43))
    challenge = _base64url(hashlib.sha256(verifier.encode()).digest())
    state = _base64url(secrets.token_bytes(32))
    redirect_uri = (
        f"http://localhost:{OPENAI_CALLBACK_PORT}{OPENAI_CALLBACK_PATH}"
    )
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": OPENAI_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "scope": "openid profile email offline_access",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "state": state,
            "originator": "openhack",
        }
    )
    authorize_url = f"{OPENAI_ISSUER}/oauth/authorize?{query}"
    result: dict[str, str] = {}
    ready = threading.Event()

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != OPENAI_CALLBACK_PATH:
                self.send_error(404)
                return
            values = urllib.parse.parse_qs(parsed.query)
            result["code"] = (values.get("code") or [""])[0]
            result["state"] = (values.get("state") or [""])[0]
            result["error"] = (
                values.get("error_description") or values.get("error") or [""]
            )[0]
            body = (
                "<html><body><h2>OpenHack is connected to ChatGPT.</h2>"
                "<p>You can close this window and return to your terminal.</p>"
                "</body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())
            ready.set()

        def log_message(self, format: str, *args: Any) -> None:
            return

    try:
        server = ThreadingHTTPServer(("localhost", OPENAI_CALLBACK_PORT), CallbackHandler)
    except OSError as exc:
        raise ProviderAuthError(
            f"Could not start OAuth callback on port {OPENAI_CALLBACK_PORT}: {exc}"
        ) from exc

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if open_browser:
            webbrowser.open(authorize_url)
        if not ready.wait(timeout):
            raise ProviderAuthError("OpenAI login timed out")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    if result.get("error"):
        raise ProviderAuthError(result["error"])
    if not result.get("code"):
        raise ProviderAuthError("OpenAI callback did not include an authorization code")
    if not secrets.compare_digest(result.get("state", ""), state):
        raise ProviderAuthError("OpenAI callback state did not match")

    tokens = _post_form(
        f"{OPENAI_ISSUER}/oauth/token",
        {
            "grant_type": "authorization_code",
            "code": result["code"],
            "redirect_uri": redirect_uri,
            "client_id": OPENAI_CLIENT_ID,
            "code_verifier": verifier,
        },
    )
    credential = _credential_from_tokens(tokens)
    set_oauth("openai", credential, path)
    return credential


def openai_device_login(
    *,
    on_code: Optional[Callable[[str, str], None]] = None,
    timeout: float = 600.0,
    path: Path = AUTH_PATH,
) -> OAuthCredential:
    """Authenticate on a headless machine with OpenCode's device-code flow."""
    status, device = _post_json(
        f"{OPENAI_ISSUER}/api/accounts/deviceauth/usercode",
        {"client_id": OPENAI_CLIENT_ID},
    )
    if status != 200:
        raise ProviderAuthError("Failed to initiate OpenAI device authorization")
    device_auth_id = str(device.get("device_auth_id") or "")
    user_code = str(device.get("user_code") or "")
    if not device_auth_id or not user_code:
        raise ProviderAuthError("OpenAI device response was incomplete")
    interval = max(int(device.get("interval") or 5), 1)
    verification_url = f"{OPENAI_ISSUER}/codex/device"
    if on_code:
        on_code(verification_url, user_code)

    deadline = time.monotonic() + timeout
    authorization: Optional[dict[str, Any]] = None
    while time.monotonic() < deadline:
        status, payload = _post_json(
            f"{OPENAI_ISSUER}/api/accounts/deviceauth/token",
            {"device_auth_id": device_auth_id, "user_code": user_code},
        )
        if status == 200:
            authorization = payload
            break
        if status not in (403, 404):
            raise ProviderAuthError(
                f"OpenAI device authorization failed with status {status}"
            )
        time.sleep(interval + 3)
    if authorization is None:
        raise ProviderAuthError("OpenAI device authorization timed out")

    tokens = _post_form(
        f"{OPENAI_ISSUER}/oauth/token",
        {
            "grant_type": "authorization_code",
            "code": str(authorization.get("authorization_code") or ""),
            "redirect_uri": f"{OPENAI_ISSUER}/deviceauth/callback",
            "client_id": OPENAI_CLIENT_ID,
            "code_verifier": str(authorization.get("code_verifier") or ""),
        },
    )
    credential = _credential_from_tokens(tokens)
    set_oauth("openai", credential, path)
    return credential
