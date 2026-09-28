"""Unit tests for joyverse/auth.py -- the JWT auth strategy.

These are pure-function tests: jwt_auth takes a Request, reads headers, and
returns a user context dict or raises HTTPException.
"""
import time
import jwt as pyjwt
import pytest
from fastapi import HTTPException

from conftest import TEST_SECRET
import joyverse.auth as jv_auth


class FakeRequest:
    def __init__(self, headers=None):
        self.headers = headers or {}

    @classmethod
    def with_token(cls, token):
        return cls({"Authorization": f"Bearer {token}"})


def make_token(payload, secret=TEST_SECRET, **kw):
    return pyjwt.encode(payload, secret, algorithm=kw.pop("algorithm", "HS256"))


class TestValidToken:
    def test_returns_username(self):
        tok = make_token({"username": "joydip", "exp": int(time.time()) + 60})
        assert jv_auth.jwt_auth(FakeRequest.with_token(tok)) == {"username": "joydip"}

    def test_extra_claims_are_allowed(self):
        tok = make_token({"username": "a", "exp": int(time.time()) + 60,
                          "role": "admin", "iss": "somewhere"})
        assert jv_auth.jwt_auth(FakeRequest.with_token(tok))["username"] == "a"

    def test_username_with_dash_and_underscore(self):
        tok = make_token({"username": "joy-dip_99", "exp": int(time.time()) + 60})
        assert jv_auth.jwt_auth(FakeRequest.with_token(tok))["username"] == "joy-dip_99"


class TestMissingToken:
    def test_no_header(self):
        with pytest.raises(HTTPException) as e:
            jv_auth.jwt_auth(FakeRequest({}))
        assert e.value.status_code == 401

    def test_empty_header(self):
        with pytest.raises(HTTPException):
            jv_auth.jwt_auth(FakeRequest({"Authorization": ""}))

    def test_bare_bearer(self):
        with pytest.raises(HTTPException):
            jv_auth.jwt_auth(FakeRequest({"Authorization": "Bearer "}))

    def test_detail_mentions_missing(self):
        with pytest.raises(HTTPException) as e:
            jv_auth.jwt_auth(FakeRequest({}))
        assert "Missing" in e.value.detail


class TestInvalidToken:
    def test_garbage_string(self):
        with pytest.raises(HTTPException) as e:
            jv_auth.jwt_auth(FakeRequest.with_token("not-a-jwt"))
        assert e.value.status_code == 401

    def test_wrong_secret_is_rejected(self):
        tok = make_token({"username": "a", "exp": int(time.time()) + 60},
                         secret="attacker-secret")
        with pytest.raises(HTTPException):
            jv_auth.jwt_auth(FakeRequest.with_token(tok))

    def test_tampered_payload_is_rejected(self):
        # Swap in a DIFFERENT payload but keep the original signature -- the
        # signature must no longer verify.
        import base64
        import json as _json

        def seg(obj):
            raw = base64.urlsafe_b64encode(
                _json.dumps(obj).encode()).rstrip(b"=").decode()
            return raw

        tok = make_token({"username": "a", "exp": int(time.time()) + 60})
        head, _payload_b64, sig = tok.split(".")
        tampered = seg({"username": "admin", "exp": int(time.time()) + 60})
        with pytest.raises(HTTPException):
            jv_auth.jwt_auth(
                FakeRequest.with_token(f"{head}.{tampered}.{sig}"))

    def test_missing_username_claim(self):
        tok = make_token({"exp": int(time.time()) + 60})
        with pytest.raises(HTTPException) as e:
            jv_auth.jwt_auth(FakeRequest.with_token(tok))
        assert e.value.status_code == 401

    def test_empty_username_claim(self):
        tok = make_token({"username": "", "exp": int(time.time()) + 60})
        with pytest.raises(HTTPException):
            jv_auth.jwt_auth(FakeRequest.with_token(tok))


class TestExpiredToken:
    def test_expired_is_rejected(self):
        tok = make_token({"username": "a", "exp": int(time.time()) - 60})
        with pytest.raises(HTTPException) as e:
            jv_auth.jwt_auth(FakeRequest.with_token(tok))
        assert e.value.status_code == 401

    def test_expired_detail_says_expired(self):
        tok = make_token({"username": "a", "exp": int(time.time()) - 60})
        with pytest.raises(HTTPException) as e:
            jv_auth.jwt_auth(FakeRequest.with_token(tok))
        assert e.value.detail == "Token expired"

    def test_token_expiring_in_future_is_accepted(self):
        tok = make_token({"username": "a", "exp": int(time.time()) + 300})
        assert jv_auth.jwt_auth(FakeRequest.with_token(tok))["username"] == "a"


class TestPathTraversalGuard:
    """username flows into an R2 key, so it must not contain separators."""

    @pytest.mark.parametrize("bad", [
        "../evil", "..", "a/b", "/etc/passwd", "user/../other", "..%2F",
    ])
    def test_traversal_usernames_rejected(self, bad):
        tok = make_token({"username": bad, "exp": int(time.time()) + 60})
        with pytest.raises(HTTPException) as e:
            jv_auth.jwt_auth(FakeRequest.with_token(tok))
        assert e.value.status_code == 401

    def test_guard_runs_after_signature_check(self):
        # An unsigned forged token with a traversal name must still fail
        with pytest.raises(HTTPException):
            jv_auth.jwt_auth(FakeRequest.with_token(
                "eyJhbGciOiJIUzI1NiJ9.eyJ1c2VybmFtZSI6Ii4uIn0.bad"))


class TestSecretWiring:
    def test_module_secret_is_loaded(self):
        assert jv_auth.JWT_SECRET

    def test_algorithm_is_pinned_to_hs256(self):
        # a token signed with HS512 must not verify
        tok = pyjwt.encode({"username": "a", "exp": int(time.time()) + 60},
                           TEST_SECRET, algorithm="HS512")
        with pytest.raises(HTTPException):
            jv_auth.jwt_auth(FakeRequest.with_token(tok))
