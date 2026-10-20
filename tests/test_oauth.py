"""Unit tests for mcppro/auth.py -- OAuth 2.1 bearer validation.

Real RS256 keys are generated per session with `cryptography` and real tokens
are signed, so signature verification is genuinely exercised. No network: the
JWKS client is always injected as a stub.
"""
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from mcppro.auth import oauth_bearer_auth, any_auth, extract_bearer_token, api_key_auth

ISSUER = "https://tenant.example.auth0.com/"
AUDIENCE = "https://joyverse.example.com"
KID = "test-key-1"


class FakeRequest:
    def __init__(self, headers=None):
        self.headers = headers or {}

    @classmethod
    def with_token(cls, token):
        return cls({"Authorization": f"Bearer {token}"})


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def rsa_pub(rsa_key):
    return rsa_key.public_key()


@pytest.fixture
def jwk_client(rsa_pub):
    """Stand-in for PyJWKClient that resolves from an in-memory public key."""
    class StubJWKClient:
        def __init__(self):
            self.fetches = 0

        def get_signing_key_from_jwt(self, token):
            self.fetches += 1
            return type("K", (), {"key": rsa_pub})()

    return StubJWKClient()


@pytest.fixture
def mint(rsa_key):
    """Sign a real RS256 token with test claims."""
    def _mint(**overrides):
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "auth0|1234567890abcdef",
            "exp": now + 3600,
            "iat": now,
            "scope": "joyverse:read joyverse:write",
        }
        claims.update(overrides)
        return jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": KID})
    return _mint


@pytest.fixture
def dep(jwk_client):
    return oauth_bearer_auth(issuer=ISSUER, audience=AUDIENCE,
                             jwk_client=jwk_client)


class TestExtractBearerToken:
    def test_returns_the_token(self):
        assert extract_bearer_token(
            FakeRequest({"Authorization": "Bearer abc"})) == "abc"

    def test_missing_header_is_401(self):
        with pytest.raises(HTTPException) as e:
            extract_bearer_token(FakeRequest())
        assert e.value.status_code == 401

    def test_bare_bearer_is_rejected(self):
        # "Bearer" with no token must not be read as a token of ""
        with pytest.raises(HTTPException):
            extract_bearer_token(FakeRequest({"Authorization": "Bearer"}))

    def test_wrong_scheme_is_rejected(self):
        with pytest.raises(HTTPException):
            extract_bearer_token(FakeRequest({"Authorization": "Bearertoken"}))

    def test_empty_bearer_is_rejected(self):
        with pytest.raises(HTTPException):
            extract_bearer_token(FakeRequest({"Authorization": "Bearer   "}))


class TestValidTokens:
    def test_returns_subject(self, dep, mint):
        assert dep(FakeRequest.with_token(mint()))["subject"] == "auth0|1234567890abcdef"

    def test_returns_issuer(self, dep, mint):
        assert dep(FakeRequest.with_token(mint()))["issuer"] == ISSUER

    def test_scopes_are_parsed_to_a_list(self, dep, mint):
        assert dep(FakeRequest.with_token(mint()))["scopes"] == [
            "joyverse:read", "joyverse:write"]

    def test_claims_are_exposed(self, dep, mint):
        ctx = dep(FakeRequest.with_token(mint(email="a@b.com", org="acme")))
        assert ctx["claims"]["email"] == "a@b.com"
        assert ctx["claims"]["org"] == "acme"

    def test_auth_method_is_tagged(self, dep, mint):
        assert dep(FakeRequest.with_token(mint()))["auth_method"] == "oauth"

    def test_context_has_no_user_id(self, dep, mint):
        # the framework must not invent an application identity concept
        assert "user_id" not in dep(FakeRequest.with_token(mint()))

    def test_token_without_scope_claim_is_accepted(self, dep, mint):
        assert dep(FakeRequest.with_token(mint(scope=None)))["scopes"] == []


class TestTokenRejection:
    """Every one of these must fail closed."""

    def test_expired_token(self, dep, mint):
        # well beyond the default 30s clock-skew leeway
        with pytest.raises(HTTPException) as e:
            dep(FakeRequest.with_token(mint(exp=int(time.time()) - 120)))
        assert e.value.status_code == 401
        assert "expired" in e.value.detail.lower()

    def test_recent_expiry_is_tolerated_for_clock_skew(self, dep, mint):
        # a token that expired 10s ago is still accepted: leeway absorbs the
        # small clock differences between us and the authorization server
        assert dep(FakeRequest.with_token(mint(exp=int(time.time()) - 10)))

    def test_zero_leeway_rejects_immediately(self, jwk_client, mint):
        strict = oauth_bearer_auth(issuer=ISSUER, audience=AUDIENCE,
                                   jwk_client=jwk_client, leeway=0)
        with pytest.raises(HTTPException):
            strict(FakeRequest.with_token(mint(exp=int(time.time()) - 1)))

    def test_wrong_audience_is_rejected(self, dep, mint):
        # the token-confusion case: a token minted for another service
        with pytest.raises(HTTPException) as e:
            dep(FakeRequest.with_token(mint(aud="https://other.example.com")))
        assert e.value.status_code == 401
        assert "audience" in e.value.detail.lower()

    def test_wrong_issuer_is_rejected(self, dep, mint):
        with pytest.raises(HTTPException) as e:
            dep(FakeRequest.with_token(mint(iss="https://evil.example.com/")))
        assert e.value.status_code == 401
        assert "issuer" in e.value.detail.lower()

    def test_garbage_token_is_rejected(self, dep):
        with pytest.raises(HTTPException) as e:
            dep(FakeRequest({"Authorization": "Bearer not-a-jwt"}))
        assert e.value.status_code == 401

    def test_tampered_signature_is_rejected(self, dep, mint):
        # signed with a key the server does not trust
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        forged = jwt.encode({"iss": ISSUER, "aud": AUDIENCE, "sub": "attacker",
                             "exp": int(time.time()) + 3600},
                            other, algorithm="RS256", headers={"kid": KID})
        with pytest.raises(HTTPException):
            dep(FakeRequest.with_token(forged))

    def test_missing_subject_is_rejected(self, dep, mint):
        with pytest.raises(HTTPException) as e:
            dep(FakeRequest.with_token(mint(sub=None)))
        assert e.value.status_code == 401

    def test_missing_expiry_is_rejected(self, dep, mint):
        tok = jwt.encode({"iss": ISSUER, "aud": AUDIENCE, "sub": "x"},
                         "k", algorithm="HS256")
        with pytest.raises(HTTPException):
            dep(FakeRequest.with_token(tok))

    def test_unsigned_alg_none_is_rejected(self, dep):
        forged = jwt.encode({"iss": ISSUER, "aud": AUDIENCE, "sub": "x",
                             "exp": int(time.time()) + 60},
                            key="", algorithm="none")
        with pytest.raises(HTTPException):
            dep(FakeRequest.with_token(forged))

    def test_no_token_is_rejected(self, dep):
        with pytest.raises(HTTPException) as e:
            dep(FakeRequest())
        assert e.value.status_code == 401


class TestRequiredScopesAtAuthTime:
    def test_satisfied_requirement_passes(self, jwk_client, mint):
        d = oauth_bearer_auth(issuer=ISSUER, audience=AUDIENCE,
                              jwk_client=jwk_client,
                              required_scopes=["joyverse:read"])
        assert d(FakeRequest.with_token(mint()))["subject"]

    def test_missing_scope_is_403(self, jwk_client, mint):
        d = oauth_bearer_auth(issuer=ISSUER, audience=AUDIENCE,
                              jwk_client=jwk_client,
                              required_scopes=["admin"])
        with pytest.raises(HTTPException) as e:
            d(FakeRequest.with_token(mint()))
        assert e.value.status_code == 403

    def test_token_with_no_scopes_is_denied(self, jwk_client, mint):
        # fails closed: an unscoped token must not satisfy a requirement
        d = oauth_bearer_auth(issuer=ISSUER, audience=AUDIENCE,
                              jwk_client=jwk_client, required_scopes=["read"])
        with pytest.raises(HTTPException) as e:
            d(FakeRequest.with_token(mint(scope=None)))
        assert e.value.status_code == 403


class TestConstruction:
    def test_issuer_is_required(self):
        with pytest.raises(ValueError):
            oauth_bearer_auth(issuer="", audience=AUDIENCE)

    def test_audience_is_required(self):
        with pytest.raises(ValueError):
            oauth_bearer_auth(issuer=ISSUER, audience="")

    def test_none_algorithm_is_refused_at_configuration(self, jwk_client):
        with pytest.raises(ValueError):
            oauth_bearer_auth(issuer=ISSUER, audience=AUDIENCE,
                              algorithms=("none",), jwk_client=jwk_client)

    def test_returns_a_callable(self, jwk_client):
        assert callable(oauth_bearer_auth(issuer=ISSUER, audience=AUDIENCE,
                                          jwk_client=jwk_client))


class TestDefaultJwkClient:
    """The real (non-stubbed) client, with only the network faked.

    Regression: `uri` was once shadowed by a read-only property, but PyJWT
    assigns to it in __init__ -- so constructing the client raised
    AttributeError and every real OAuth request failed. Only surfaced when
    OAUTH_ENABLED was actually turned on.
    """

    def test_constructs_without_error(self):
        from mcppro.auth import default_jwk_client

        client = default_jwk_client(ISSUER)
        assert client is not None

    def test_uri_is_writable(self):
        # PyJWT assigns self.uri internally; it must not be read-only
        from mcppro.auth import default_jwk_client

        client = default_jwk_client(ISSUER)
        client.uri = "https://example.test/keys.json"
        assert client.uri == "https://example.test/keys.json"

    def test_starts_at_the_discovery_url(self):
        from mcppro.auth import default_jwk_client

        client = default_jwk_client(ISSUER)
        assert client.uri.endswith("/.well-known/openid-configuration")

    def test_fetch_resolves_jwks_uri_from_discovery(self, monkeypatch):
        from mcppro.auth import default_jwk_client

        class FakeResp:
            def __init__(self, payload):
                self.payload = payload

            def read(self):
                return json.dumps(self.payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        # A real key, so PyJWKSet can actually build a signing key from it. A stub
        # dict fails with "did not contain any usable keys", which would be an
        # artefact of the fixture rather than a real result.
        from cryptography.hazmat.primitives.asymmetric import rsa as _rsa_mod

        private = _rsa_mod.generate_private_key(public_exponent=65537,
                                                key_size=2048)
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
        jwk.update({"kid": "k1", "alg": "RS256", "use": "sig"})

        # The discovery document, then the JWKS itself.
        responses = [
            FakeResp({"jwks_uri": "https://as.example/.well-known/jwks.json"}),
            FakeResp({"keys": [jwk]}),
        ]

        # PyJWT builds a urllib opener and calls opener.open(...) -- it does not
        # call urlopen directly -- so build_opener is what has to be
        # intercepted for the JWKS fetch.
        class FakeOpener:
            def open(self, request, timeout=None):
                return responses.pop(0)

        monkeypatch.setattr("urllib.request.build_opener",
                            lambda *handlers: FakeOpener())

        # Our own discovery step calls urlopen() directly, so patch that too.
        monkeypatch.setattr("urllib.request.urlopen",
                            lambda *a, **k: responses.pop(0))

        client = default_jwk_client(ISSUER)
        data = client.fetch_data()

        # The parent must now be pointed at the real JWKS endpoint...
        assert client.uri == "https://as.example/.well-known/jwks.json"
        # ...and the keys must have come back.
        assert data["keys"][0]["kid"] == "k1"
        # Both responses consumed: discovery first, then JWKS.
        assert not responses

    def test_missing_jwks_uri_is_an_error(self, monkeypatch):
        from mcppro.auth import default_jwk_client
        import jwt as pyjwt_mod

        class FakeResp:
            def read(self):
                return json.dumps({"issuer": "x"}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: FakeResp())
        client = default_jwk_client(ISSUER)
        with pytest.raises(pyjwt_mod.PyJWKClientError):
            client.fetch_data()


class TestAnyAuth:
    """Composite strategy for running OAuth and legacy bearer side by side."""

    def test_requires_at_least_one_strategy(self):
        with pytest.raises(ValueError):
            any_auth()

    def test_oauth_token_authenticates(self, dep, mint):
        chain = any_auth(dep, api_key_auth(["legacy"]))
        assert chain(FakeRequest.with_token(mint()))["subject"] == \
            "auth0|1234567890abcdef"

    def test_legacy_key_falls_through(self, dep):
        # the whole point: both credential types keep working
        chain = any_auth(dep, api_key_auth(["legacy-key"]))
        assert chain(FakeRequest({"X-API-Key": "legacy-key"}))["api_key"] == "legacy-key"

    def test_first_error_is_surfaced_not_the_last(self, dep):
        # an expired OAuth token must not be reported as "Invalid API Key"
        chain = any_auth(dep, api_key_auth(["legacy-key"]))
        with pytest.raises(HTTPException) as e:
            chain(FakeRequest({"X-API-Key": "wrong"}))
        assert e.value.status_code == 401
        assert "API Key" not in e.value.detail

    def test_forbidden_is_not_downgraded(self, jwk_client, mint):
        # 403 means authenticated-but-not-permitted; falling through could
        # only weaken it
        strict = oauth_bearer_auth(issuer=ISSUER, audience=AUDIENCE,
                                   jwk_client=jwk_client,
                                   required_scopes=["admin"])
        chain = any_auth(strict, api_key_auth(["legacy-key"]))
        with pytest.raises(HTTPException) as e:
            chain(FakeRequest.with_token(mint()))
        assert e.value.status_code == 403

    def test_single_strategy_chain_still_works(self, dep, mint):
        assert any_auth(dep)(FakeRequest.with_token(mint()))["subject"]