"""Regression guard: the test suite must never reach the real R2 bucket.

A test once called the real `run.server._auth_dependency` without the
`fake_r2` fixture. That silently resolved a handle through the identity
registry and wrote a user into the PRODUCTION Cloudflare bucket. Around
ninety such users accumulated -- person0..person49, alice, bob, aarav-test --
and were only discovered by inspecting the registry by hand.

These tests pin that failure mode shut.
"""
import sys
import time
from pathlib import Path

import jwt as pyjwt
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


class FakeRequest:
    def __init__(self, headers):
        self.headers = headers


def _bearer_token(secret):
    return pyjwt.encode({"handle": "wiretest",
                         "exp": int(time.time()) + 60},
                        secret, algorithm="HS256")


class TestRealR2IsBlocked:
    """The exact call that caused the pollution must now raise."""

    def test_auth_through_real_server_is_refused(self, no_network_guard):
        from conftest import TEST_SECRET
        import run

        with pytest.raises(RuntimeError, match="real Cloudflare R2 bucket"):
            run.server._auth_dependency(
                FakeRequest({"Authorization":
                             f"Bearer {_bearer_token(TEST_SECRET)}"}))

    def test_identity_resolution_is_refused(self, no_network_guard):
        from joyverse import identity

        with pytest.raises(RuntimeError, match="real Cloudflare R2 bucket"):
            identity.resolve_handle("wiretest")

    def test_a_tool_read_cannot_reach_the_real_bucket(self, no_network_guard):
        """Tools swallow exceptions and return an error string instead.

        That is deliberate -- a broken R2 connection must not crash a tool
        call. It does mean the guard surfaces here as an R2 error rather than
        a raised exception, so this asserts the blocked outcome: an error
        mentioning the guard, and definitely not real user data.
        """
        import json
        from joyverse import profile as prof

        out = json.loads(prof.get_profile({"user_id": "u_a1b2c3d4e5f6"}))
        assert "error" in out
        assert "real Cloudflare R2 bucket" in out["error"]


class TestFakeR2StillWorks:
    """The guard must not break legitimate tests -- it only blocks real I/O."""

    def test_resolve_handle_writes_to_the_fake(self, fake_r2):
        from joyverse import identity

        uid = identity.resolve_handle("wiretest")
        assert identity.is_user_id(uid)
        assert any(k.startswith("users/_registry/") for k in fake_r2.store)

    def test_auth_through_real_server_writes_to_the_fake(self, fake_r2):
        from conftest import TEST_SECRET
        import run

        ctx = run.server._auth_dependency(
            FakeRequest({"Authorization": f"Bearer {_bearer_token(TEST_SECRET)}"}))
        assert ctx["user_id"]
        assert any(k.startswith("users/_registry/") for k in fake_r2.store)


class TestGuardIsSessionWide:
    """A test that forgets `fake_r2` fails loudly instead of silently writing."""

    def test_guard_applies_without_requesting_the_fixture(self):
        from joyverse import config as jv_config

        # Autouse, so it is already in force with nothing requested here.
        with pytest.raises(RuntimeError, match="real Cloudflare R2 bucket"):
            jv_config.get_r2_client()