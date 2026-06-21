"""Tests for the shared outbound-HTTP helper."""
from unittest.mock import MagicMock

import httpx
import pytest

from app.core import http_client


def test_returns_response_via_passed_client():
    client = MagicMock()
    client.request.return_value = httpx.Response(
        200, request=httpx.Request("POST", "https://x.test/v3/session/")
    )
    resp = http_client.request(
        "POST", "https://x.test/v3/session/", provider="didit", op="create", client=client
    )
    assert resp.status_code == 200
    client.request.assert_called_once()


def test_raises_on_transport_error():
    client = MagicMock()
    client.request.side_effect = httpx.ConnectError("down")
    with pytest.raises(httpx.ConnectError):
        http_client.request("GET", "https://x.test", provider="p", client=client)


def test_safe_target_excludes_query_string():
    # The logged target must never include the query (it can carry tokens/ids).
    assert (
        http_client._safe_target("https://api.test/v3/session/?api_key=SUPERSECRET")
        == "api.test/v3/session/"
    )


def test_default_timeout_is_split_connect_read():
    assert http_client.DEFAULT_TIMEOUT.connect == 5.0
    assert http_client.DEFAULT_TIMEOUT.read == 15.0
