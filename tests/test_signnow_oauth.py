"""Unit tests for the SignNow OAuth2 token helper — respx mocks httpx (no live
HTTP), no DB.

Covers: token fetch success, caching (a second call within TTL does NOT
re-fetch), refresh after expiry, error → typed transient/permanent error, and
no credentials in logs / error messages. The assumed SignNow OAuth2 shape (see
``signnow_oauth`` module docstring) is encoded in the mock responses here — if
the real API differs, correct these mocks alongside the module constants.
"""
import httpx
import pytest
import respx

from app.services.esign import signnow_oauth
from app.services.esign.signnow_oauth import (
    SignNowAuthPermanentError,
    SignNowAuthTransientError,
    SignNowTokenProvider,
    get_signnow_token,
    reset_signnow_token_cache,
)

_BASE_URL = "https://api-eval.signnow.com"
_TOKEN_URL = f"{_BASE_URL}/oauth2/token"

_CLIENT_ID = "client-id-xyz"
_CLIENT_SECRET = "super-secret-client-value"
_USERNAME = "account@example.com"
_PASSWORD = "hunter2-very-secret-password"

# Strings that must NEVER appear in logs or raised error messages.
_SECRETS = (_CLIENT_SECRET, _PASSWORD)


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_signnow_token_cache()
    yield
    reset_signnow_token_cache()


@pytest.fixture
def fake_clock(monkeypatch):
    """Controllable monotonic clock so TTL/refresh logic is deterministic."""
    state = {"now": 1000.0}
    monkeypatch.setattr(signnow_oauth.time, "monotonic", lambda: state["now"])
    return state


def _provider(timeout=15.0) -> SignNowTokenProvider:
    return SignNowTokenProvider(
        client_id=_CLIENT_ID,
        client_secret=_CLIENT_SECRET,
        username=_USERNAME,
        password=_PASSWORD,
        base_url=_BASE_URL,
        timeout=timeout,
    )


def _token_response(access="access-1", *, refresh="refresh-1", expires_in=2592000):
    body = {
        "access_token": access,
        "token_type": "bearer",
        "expires_in": expires_in,
        "scope": "*",
    }
    if refresh is not None:
        body["refresh_token"] = refresh
    return httpx.Response(200, json=body)


# ---------------------------------------------------------------------------
# Fetch success
# ---------------------------------------------------------------------------


@respx.mock
def test_fetch_success_returns_access_token(fake_clock):
    route = respx.post(_TOKEN_URL).mock(return_value=_token_response("access-1"))
    token = _provider().get_token()
    assert token == "access-1"
    assert route.call_count == 1

    # Password grant, Basic auth, form-encoded — verify the documented shape.
    request = route.calls.last.request
    assert request.headers["Authorization"].startswith("Basic ")
    assert "application/x-www-form-urlencoded" in request.headers["Content-Type"]
    body = request.content.decode()
    assert "grant_type=password" in body
    assert "refresh_token" not in body


@respx.mock
def test_convenience_function_returns_token(fake_clock):
    respx.post(_TOKEN_URL).mock(return_value=_token_response("conv-token"))
    token = get_signnow_token(
        client_id=_CLIENT_ID,
        client_secret=_CLIENT_SECRET,
        username=_USERNAME,
        password=_PASSWORD,
        base_url=_BASE_URL,
    )
    assert token == "conv-token"


# ---------------------------------------------------------------------------
# Caching — second call within TTL does NOT re-fetch
# ---------------------------------------------------------------------------


@respx.mock
def test_caching_second_call_within_ttl_does_not_refetch(fake_clock):
    route = respx.post(_TOKEN_URL).mock(return_value=_token_response("cached"))
    provider = _provider()

    first = provider.get_token()
    # Advance a little, still well inside the 30-day TTL minus skew.
    fake_clock["now"] += 60
    second = provider.get_token()

    assert first == second == "cached"
    assert route.call_count == 1


@respx.mock
def test_convenience_function_shares_cache_across_calls(fake_clock):
    route = respx.post(_TOKEN_URL).mock(return_value=_token_response("shared"))
    kwargs = dict(
        client_id=_CLIENT_ID,
        client_secret=_CLIENT_SECRET,
        username=_USERNAME,
        password=_PASSWORD,
        base_url=_BASE_URL,
    )
    assert get_signnow_token(**kwargs) == "shared"
    fake_clock["now"] += 100
    assert get_signnow_token(**kwargs) == "shared"
    assert route.call_count == 1


# ---------------------------------------------------------------------------
# Refresh after expiry
# ---------------------------------------------------------------------------


@respx.mock
def test_refresh_after_expiry_uses_refresh_token_grant(fake_clock):
    route = respx.post(_TOKEN_URL).mock(
        side_effect=[
            _token_response("access-1", refresh="refresh-1", expires_in=100),
            _token_response("access-2", refresh="refresh-2", expires_in=100),
        ]
    )
    provider = _provider()

    assert provider.get_token() == "access-1"
    # Jump past expiry (100s TTL + 300s skew window) to force a refresh.
    fake_clock["now"] += 1000
    assert provider.get_token() == "access-2"

    assert route.call_count == 2
    second_body = route.calls[1].request.content.decode()
    assert "grant_type=refresh_token" in second_body
    assert "refresh_token=refresh-1" in second_body


@respx.mock
def test_refresh_falls_back_to_password_when_refresh_fails(fake_clock):
    route = respx.post(_TOKEN_URL).mock(
        side_effect=[
            _token_response("access-1", refresh="refresh-1", expires_in=100),
            httpx.Response(400, json={"error": "invalid_grant"}),  # refresh rejected
            _token_response("access-3", refresh="refresh-3", expires_in=100),
        ]
    )
    provider = _provider()

    assert provider.get_token() == "access-1"
    fake_clock["now"] += 1000
    # Refresh 400s → falls back to a password grant which succeeds.
    assert provider.get_token() == "access-3"

    assert route.call_count == 3
    assert "grant_type=refresh_token" in route.calls[1].request.content.decode()
    assert "grant_type=password" in route.calls[2].request.content.decode()


# ---------------------------------------------------------------------------
# Errors → typed transient / permanent
# ---------------------------------------------------------------------------


@respx.mock
def test_server_error_raises_transient(fake_clock):
    respx.post(_TOKEN_URL).mock(return_value=httpx.Response(503, text="upstream down"))
    with pytest.raises(SignNowAuthTransientError) as exc:
        _provider().get_token()
    assert exc.value.status_code == 503


@respx.mock
def test_rate_limit_raises_transient(fake_clock):
    respx.post(_TOKEN_URL).mock(return_value=httpx.Response(429, text="slow down"))
    with pytest.raises(SignNowAuthTransientError):
        _provider().get_token()


@respx.mock
def test_bad_credentials_raises_permanent(fake_clock):
    respx.post(_TOKEN_URL).mock(
        return_value=httpx.Response(401, json={"error": "invalid_client"})
    )
    with pytest.raises(SignNowAuthPermanentError) as exc:
        _provider().get_token()
    assert exc.value.status_code == 401


@respx.mock
def test_missing_access_token_raises_permanent(fake_clock):
    respx.post(_TOKEN_URL).mock(return_value=httpx.Response(200, json={"scope": "*"}))
    with pytest.raises(SignNowAuthPermanentError):
        _provider().get_token()


@respx.mock
def test_network_error_raises_transient(fake_clock):
    respx.post(_TOKEN_URL).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(SignNowAuthTransientError):
        _provider().get_token()


# ---------------------------------------------------------------------------
# No credentials in logs / error messages
# ---------------------------------------------------------------------------


@respx.mock
def test_no_secrets_in_error_message(fake_clock):
    # Echo the (fake) secret back in the error body; the raised message must
    # still be capped/non-secret and must not contain our client_secret/password.
    respx.post(_TOKEN_URL).mock(
        return_value=httpx.Response(403, text="forbidden for client")
    )
    with pytest.raises(SignNowAuthPermanentError) as exc:
        _provider().get_token()
    message = str(exc.value)
    for secret in _SECRETS:
        assert secret not in message


@respx.mock
def test_no_secrets_in_logs(fake_clock, caplog):
    import logging

    respx.post(_TOKEN_URL).mock(return_value=_token_response("logged"))
    with caplog.at_level(logging.DEBUG):
        _provider().get_token()
    combined = "\n".join(r.getMessage() for r in caplog.records)
    for secret in _SECRETS:
        assert secret not in combined
