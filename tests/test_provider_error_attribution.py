"""Provider errors must never send BYOK/subscription users to OpenHack billing."""

from types import SimpleNamespace

from openhack.agents.llm import LLMClient


def _client(provider: str, *, auth_type: str = "api") -> LLMClient:
    client = LLMClient.__new__(LLMClient)
    client.provider = provider
    client._resolved = (
        None
        if provider == "openhack"
        else SimpleNamespace(name=provider, auth_type=auth_type)
    )
    return client


def test_openai_subscription_quota_error_is_not_called_openhack_credits():
    message = _client("openai", auth_type="oauth")._permission_denied_message(
        "insufficient credits"
    )

    assert "OpenAI subscription" in message
    assert "OpenHack credits" not in message
    assert "settings/billing" not in message
    assert "/connect" in message


def test_openhack_credit_error_keeps_openhack_billing_guidance():
    message = _client("openhack")._permission_denied_message(
        "insufficient credits"
    )

    assert "Insufficient OpenHack credits" in message
    assert "settings/billing" in message


def test_openai_subscription_auth_error_reconnects_openai():
    message = _client("openai", auth_type="oauth")._authentication_error_message(
        "expired token"
    )

    assert "OpenAI subscription" in message
    assert "Reconnect OpenAI with /connect" in message
    assert "OpenHack" not in message


def test_exhausted_quota_is_not_retryable():
    error = RuntimeError(
        "429 insufficient_quota: no credits remaining "
        "(credit_balance_exhausted)"
    )

    assert LLMClient._is_exhausted_quota_error(error)
    assert not LLMClient._is_exhausted_quota_error(
        RuntimeError("429 requests per minute exceeded")
    )
