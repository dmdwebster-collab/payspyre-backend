"""Unit tests for the Zumrails token helper — respx mocks httpx (no live HTTP),
no DB.

Covers: token fetch success, caching (a second call within TTL does NOT
re-authorize), refresh after expiry, error → typed transient/permanent error,
and no credentials in logs / error messages. The assumed Zumrails authorize
shape (see ``zumrails_auth`` / ``zumrails_adapter`` docstrings) is encoded in the
mock responses here.
"""
import httpx
import pytest
import respx

from app.services.payments import zumrails_auth
from app.services.payments.zumrails_auth import (
    PermanentZumrailsAuthError,
    TransientZumrailsAuthError,
    ZumrailsTokenProvider,
    get_zumrails_token,
    reset_zumrails_token_cache,
)

_BASE_URL = "https://api.zumrails.com"
_AUTHORIZE_URL = f"{_BASE_URL}/api/authorize"

_API_KEY = "zum-api-key"
_API_SECRET = "zum-api-secret-very-secret-value"

# Strings that must NEVER appear in logs or raised error messages.
_SECRETS = (_API_SECRET,)


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_zumrails_token_cache()
    yield
    reset_zumrails_token_cache()


@pytest.fixture
def fake_clock(monkeypatch):
    state = {"now": 1000.0}
    monkeypatch.setattr(zumrails_auth.time, "monotonic", lambda: state["now"])
    return state


def _provider() -> ZumrailsTokenProvider:
    return ZumrailsTokenProvider(
        api_key=_API_KEY,
        api_secret=_API_SECRET,
        base_url=_BASE_URL,
    )


def _authorize_response(token="jwt-1", **extra_result):
    result = {"Token": token, **extra_result}
    return httpx.Response(200, json={"result": result})


# ---------------------------------------------------------------------------
# Fetch success
# ---------------------------------------------------------------------------


@respx.mock
def test_fetch_success_returns_token(fake_clock):
    route = respx.post(_AUTHORIZE_URL).mock(return_value=_authorize_response("jwt-1"))
    assert _provider().get_token() == "jwt-1"
    assert route.call_count == 1

    request = route.calls.last.request
    body = request.content.decode()
    assert '"username"' in body and '"password"' in body
    assert _API_KEY in body  # username is the (non-secret) api_key


@respx.mock
def test_convenience_function_returns_token(fake_clock):
    respx.post(_AUTHORIZE_URL).mock(return_value=_authorize_response("conv-jwt"))
    token = get_zumrails_token(
        api_key=_API_KEY, api_secret=_API_SECRET, base_url=_BASE_URL
    )
    assert token == "conv-jwt"


@respx.mock
def test_case_tolerant_token_extraction(fake_clock):
    respx.post(_AUTHORIZE_URL).mock(
        return_value=httpx.Response(200, json={"Result": {"token": "lower-jwt"}})
    )
    assert _provider().get_token() == "lower-jwt"


# ---------------------------------------------------------------------------
# Caching — second call within TTL does NOT re-authorize
# ---------------------------------------------------------------------------


@respx.mock
def test_caching_second_call_within_ttl_does_not_reauthorize(fake_clock):
    route = respx.post(_AUTHORIZE_URL).mock(return_value=_authorize_response("cached"))
    provider = _provider()

    first = provider.get_token()
    fake_clock["now"] += 60  # inside default 3600s TTL minus skew
    second = provider.get_token()

    assert first == second == "cached"
    assert route.call_count == 1


@respx.mock
def test_convenience_function_shares_cache(fake_clock):
    route = respx.post(_AUTHORIZE_URL).mock(return_value=_authorize_response("shared"))
    kwargs = dict(api_key=_API_KEY, api_secret=_API_SECRET, base_url=_BASE_URL)
    assert get_zumrails_token(**kwargs) == "shared"
    fake_clock["now"] += 100
    assert get_zumrails_token(**kwargs) == "shared"
    assert route.call_count == 1


# ---------------------------------------------------------------------------
# Refresh after expiry
# ---------------------------------------------------------------------------


@respx.mock
def test_reauthorizes_after_expiry(fake_clock):
    route = respx.post(_AUTHORIZE_URL).mock(
        side_effect=[
            _authorize_response("jwt-1"),
            _authorize_response("jwt-2"),
        ]
    )
    provider = _provider()

    assert provider.get_token() == "jwt-1"
    fake_clock["now"] += 10_000  # well past default TTL
    assert provider.get_token() == "jwt-2"
    assert route.call_count == 2


@respx.mock
def test_honors_explicit_ttl_in_response(fake_clock):
    route = respx.post(_AUTHORIZE_URL).mock(
        side_effect=[
            _authorize_response("jwt-1", ExpiresIn=100),
            _authorize_response("jwt-2", ExpiresIn=100),
        ]
    )
    provider = _provider()
    assert provider.get_token() == "jwt-1"
    fake_clock["now"] += 50  # inside 100s TTL minus skew? 50 < 100-120 is false...
    # 100s TTL with 120s skew means it is already "not fresh" — so advancing even
    # a little forces re-auth. Verify the explicit-TTL path drives re-auth.
    assert provider.get_token() == "jwt-2"
    assert route.call_count == 2


# ---------------------------------------------------------------------------
# Errors → typed transient / permanent
# ---------------------------------------------------------------------------


@respx.mock
def test_server_error_raises_transient(fake_clock):
    respx.post(_AUTHORIZE_URL).mock(return_value=httpx.Response(502, text="bad gateway"))
    with pytest.raises(TransientZumrailsAuthError):
        _provider().get_token()


@respx.mock
def test_rate_limit_raises_transient(fake_clock):
    respx.post(_AUTHORIZE_URL).mock(return_value=httpx.Response(429, text="slow down"))
    with pytest.raises(TransientZumrailsAuthError):
        _provider().get_token()


@respx.mock
def test_bad_credentials_raises_permanent(fake_clock):
    respx.post(_AUTHORIZE_URL).mock(
        return_value=httpx.Response(401, json={"message": "unauthorized"})
    )
    with pytest.raises(PermanentZumrailsAuthError):
        _provider().get_token()


@respx.mock
def test_missing_token_raises_permanent(fake_clock):
    respx.post(_AUTHORIZE_URL).mock(
        return_value=httpx.Response(200, json={"result": {}})
    )
    with pytest.raises(PermanentZumrailsAuthError):
        _provider().get_token()


@respx.mock
def test_network_error_raises_transient(fake_clock):
    respx.post(_AUTHORIZE_URL).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(TransientZumrailsAuthError):
        _provider().get_token()


# ---------------------------------------------------------------------------
# No credentials in logs / error messages
# ---------------------------------------------------------------------------


@respx.mock
def test_no_secrets_in_error_message(fake_clock):
    respx.post(_AUTHORIZE_URL).mock(
        return_value=httpx.Response(403, text="forbidden")
    )
    with pytest.raises(PermanentZumrailsAuthError) as exc:
        _provider().get_token()
    message = str(exc.value)
    for secret in _SECRETS:
        assert secret not in message


@respx.mock
def test_no_secrets_in_logs(fake_clock, caplog):
    import logging

    respx.post(_AUTHORIZE_URL).mock(return_value=_authorize_response("logged"))
    with caplog.at_level(logging.DEBUG):
        _provider().get_token()
    combined = "\n".join(r.getMessage() for r in caplog.records)
    for secret in _SECRETS:
        assert secret not in combined
