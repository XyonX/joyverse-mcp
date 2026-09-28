"""Unit tests for mcppro/auth.py -- the framework's built-in auth strategies."""
import pytest
from fastapi import HTTPException
from mcppro.auth import no_auth, api_key_auth


class FakeRequest:
    def __init__(self, headers=None):
        self.headers = headers or {}


class TestNoAuth:
    def test_returns_empty_context(self):
        assert no_auth(FakeRequest()) == {}

    def test_ignores_headers(self):
        req = FakeRequest({"Authorization": "Bearer whatever"})
        assert no_auth(req) == {}


class TestApiKeyAuth:
    def test_returns_callable(self):
        assert callable(api_key_auth(["k"]))

    def test_accepts_valid_x_api_key(self):
        dep = api_key_auth(["secret-key"])
        ctx = dep(FakeRequest({"X-API-Key": "secret-key"}))
        assert ctx["api_key"] == "secret-key"

    def test_accepts_valid_bearer(self):
        dep = api_key_auth(["secret-key"])
        ctx = dep(FakeRequest({"Authorization": "Bearer secret-key"}))
        assert ctx["api_key"] == "secret-key"

    def test_bearer_stripped_exactly(self):
        # the key itself contains no "Bearer " prefix after slicing 7 chars
        dep = api_key_auth(["abc"])
        assert dep(FakeRequest({"Authorization": "Bearer abc"}))["api_key"] == "abc"

    def test_x_api_key_takes_precedence(self):
        dep = api_key_auth(["good"])
        req = FakeRequest({"X-API-Key": "good", "Authorization": "Bearer bad"})
        assert dep(req)["api_key"] == "good"

    def test_role_is_returned(self):
        dep = api_key_auth(["k"])
        assert dep(FakeRequest({"X-API-Key": "k"}))["role"] == "user"

    @pytest.mark.parametrize("headers", [
        {},
        {"X-API-Key": "wrong"},
        {"Authorization": "Bearer wrong"},
        {"X-API-Key": ""},
    ])
    def test_rejects_bad_input(self, headers):
        dep = api_key_auth(["secret-key"])
        with pytest.raises(HTTPException) as e:
            dep(FakeRequest(headers))
        assert e.value.status_code == 401

    def test_error_detail_is_specific(self):
        dep = api_key_auth(["k"])
        with pytest.raises(HTTPException) as e:
            dep(FakeRequest({"X-API-Key": "nope"}))
        assert e.value.detail == "Invalid API Key"

    def test_bare_authorization_header_is_rejected(self):
        # "Bearer" with no trailing space must not match
        dep = api_key_auth(["k"])
        with pytest.raises(HTTPException):
            dep(FakeRequest({"Authorization": "Bearerk"}))

    def test_key_membership_is_exact(self):
        # substring of a valid key must not pass
        dep = api_key_auth(["secret-key"])
        with pytest.raises(HTTPException):
            dep(FakeRequest({"X-API-Key": "secret"}))
