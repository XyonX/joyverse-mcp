"""Unit tests for joyverse/auth.py -- the JWT auth strategy.

These are pure-function tests: jwt_auth takes a Request, reads headers, and
returns a user context dict or raises HTTPException.

The context now carries a `user_id` rather than a username, because the
handle in the token is a lookup key and the user_id is what reaches storage.
Resolution goes through the identity registry, so every test here needs the
fake R2.
"""
import time
import jwt as pyjwt
import pytest
from fastapi import HTTPException

from conftest import TEST_SECRET
from joyverse import identity
import joyverse.auth as jv_auth


class FakeRequest:
    def __init__(self, headers=None):
        self.headers = headers or {}

    @classmethod
    def with_token(cls, token):
        return cls({"Authorization": f"Bearer {token}"})


def make_token(payload, secret=TEST_SECRET, **kw):
    return pyjwt.encode(payload, secret, algorithm=kw.pop("algorithm", "HS256"))


@pytest.fixture(autouse=True)
def _registry(fake_r2):
    """Every test here resolves a handle through the registry."""
    return fake_r2


class TestValidToken:
    def test_returns_a_user_id(self, fake_r2):
        tok = make_token({"handle": "joydip", "exp": int(time.time()) + 60})
        ctx = jv_auth.jwt_auth(FakeRequest.with_token(tok))
        assert identity.is_user_id(ctx["user_id"])

    def test_user_id_is_stable_for_the_same_handle(self, fake_r2):
        tok = make_token({"handle": "joydip", "exp": int(time.time()) + 60})
        first = jv_auth.jwt_auth(FakeRequest.with_token(tok))["user_id"]
        second = jv_auth.jwt_auth(FakeRequest.with_token(tok))["user_id"]
        assert first == second

    def test_returns_the_normalised_handle(self, fake_r2):
        tok = make_token({"handle": "JoyDip", "exp": int(time.time()) + 60})
        assert jv_auth.jwt_auth(FakeRequest.with_token(tok))["handle"] == "joydip"

    def test_auth_method_is_bearer(self, fake_r2):
        tok = make_token({"handle": "joydip", "exp": int(time.time()) + 60})
        assert jv_auth.jwt_auth(FakeRequest.with_token(tok))["auth_method"] == "bearer"

    def test_carries_both_scopes_by_default(self, fake_r2):
        tok = make_token({"handle": "joydip", "exp": int(time.time()) + 60})
        scopes = jv_auth.jwt_auth(FakeRequest.with_token(tok))["scopes"]
        assert "joyverse:read" in scopes and "joyverse:write" in scopes

    def test_explicit_scope_claim_narrows_access(self, fake_r2):
        tok = make_token({"handle": "joydip", "exp": int(time.time()) + 60,
                          "scope": "joyverse:read"})
        assert jv_auth.jwt_auth(FakeRequest.with_token(tok))["scopes"] == \
            ["joyverse:read"]

    def test_extra_claims_are_allowed(self, fake_r2):
        tok = make_token({"handle": "joydip", "exp": int(time.time()) + 60,
                          "role": "admin", "iss": "somewhere"})
        assert jv_auth.jwt_auth(FakeRequest.with_token(tok))["handle"] == "joydip"

    def test_handle_with_dash_and_underscore(self, fake_r2):
        tok = make_token({"handle": "joy-dip_99", "exp": int(time.time()) + 60})
        ctx = jv_auth.jwt_auth(FakeRequest.with_token(tok))
        assert ctx["handle"] == "joy-dip_99"

    def test_legacy_username_claim_still_works(self, fake_r2):
        # Tokens minted before the rename carried "username". They must keep
        # working or every existing deployment breaks on upgrade.
        tok = make_token({"username": "legacyuser", "exp": int(time.time()) + 60})
        ctx = jv_auth.jwt_auth(FakeRequest.with_token(tok))
        assert ctx["handle"] == "legacyuser"

    def test_handle_claim_wins_over_legacy_username(self, fake_r2):
        tok = make_token({"handle": "preferred", "username": "ignored",
                          "exp": int(time.time()) + 60})
        assert jv_auth.jwt_auth(FakeRequest.with_token(tok))["handle"] == "preferred"

    def test_two_handles_get_different_user_ids(self, fake_r2):
        a = make_token({"handle": "alice", "exp": int(time.time()) + 60})
        b = make_token({"handle": "bob", "exp": int(time.time()) + 60})
        assert (jv_auth.jwt_auth(FakeRequest.with_token(a))["user_id"] !=
                jv_auth.jwt_auth(FakeRequest.with_token(b))["user_id"])


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
        tok = make_token({"handle": "aaa", "exp": int(time.time()) + 60},
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

        tok = make_token({"handle": "aaa", "exp": int(time.time()) + 60})
        head, _payload_b64, sig = tok.split(".")
        tampered = seg({"handle": "admin", "exp": int(time.time()) + 60})
        with pytest.raises(HTTPException):
            jv_auth.jwt_auth(
                FakeRequest.with_token(f"{head}.{tampered}.{sig}"))

    def test_missing_handle_claim(self):
        tok = make_token({"exp": int(time.time()) + 60})
        with pytest.raises(HTTPException) as e:
            jv_auth.jwt_auth(FakeRequest.with_token(tok))
        assert e.value.status_code == 401

    def test_empty_handle_claim(self):
        tok = make_token({"handle": "", "exp": int(time.time()) + 60})
        with pytest.raises(HTTPException):
            jv_auth.jwt_auth(FakeRequest.with_token(tok))


class TestExpiredToken:
    def test_expired_is_rejected(self):
        tok = make_token({"handle": "aaa", "exp": int(time.time()) - 60})
        with pytest.raises(HTTPException) as e:
            jv_auth.jwt_auth(FakeRequest.with_token(tok))
        assert e.value.status_code == 401

    def test_expired_detail_says_expired(self):
        tok = make_token({"handle": "aaa", "exp": int(time.time()) - 60})
        with pytest.raises(HTTPException) as e:
            jv_auth.jwt_auth(FakeRequest.with_token(tok))
        assert e.value.detail == "Token expired"

    def test_token_expiring_in_future_is_accepted(self, fake_r2):
        tok = make_token({"handle": "aaa", "exp": int(time.time()) + 300})
        assert jv_auth.jwt_auth(FakeRequest.with_token(tok))["handle"] == "aaa"


class TestHandleValidation:
    """The handle is now validated by the identity registry, not ad-hoc here.

    Two layers now guard the same thing: normalise_handle() rejects anything
    malformed, and _safe_user_id() independently refuses to build a key from
    anything that was not server-minted. Both must hold.
    """

    @pytest.mark.parametrize("bad", [
        "../evil", "..", "a/b", "/etc/passwd", "user/../other", "..%2F",
        "_registry", "u_a1b2c3d4e5f6", "ab", "x" * 33, "has space",
        "sömething",
    ])
    def test_bad_handles_are_rejected(self, bad):
        tok = make_token({"handle": bad, "exp": int(time.time()) + 60})
        with pytest.raises(HTTPException) as e:
            jv_auth.jwt_auth(FakeRequest.with_token(tok))
        assert e.value.status_code == 401

    def test_guard_runs_after_signature_check(self):
        # An unsigned forged token with a traversal name must still fail
        with pytest.raises(HTTPException):
            jv_auth.jwt_auth(FakeRequest.with_token(
                'eyJhbGciOiJIUzI1NiJ9.eyJ1c2VybmFtZSI6Ii4uIn0.bad'))


class TestSecretWiring:
    def test_algorithm_is_pinned_to_hs256(self):
        # a token signed with HS512 must not verify
        tok = pyjwt.encode({"handle": "aaa", "exp": int(time.time()) + 60},
                           TEST_SECRET, algorithm="HS512")
        with pytest.raises(HTTPException):
            jv_auth.jwt_auth(FakeRequest.with_token(tok))

    def test_missing_secret_is_a_401_not_an_import_error(self):
        # Previously a missing JWT_SECRET raised at import time, which made the
        # whole package unimportable. OAuth-only deployments must work without it.
        import os
        original = os.environ.pop("JWT_SECRET", None)
        original_module = jv_auth.JWT_SECRET
        jv_auth.JWT_SECRET = None
        try:
            tok = make_token({"handle": "aaa", "exp": int(time.time()) + 60})
            with pytest.raises(HTTPException) as e:
                jv_auth.jwt_auth(FakeRequest.with_token(tok))
            assert e.value.status_code == 401
        finally:
            jv_auth.JWT_SECRET = original_module
            if original is not None:
                os.environ["JWT_SECRET"] = original
