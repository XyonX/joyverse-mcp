"""Unit tests for joyverse/identity.py -- the user identity registry.

The invariant under test: identity is derived from the person, never from the
client, so one human signing in from several MCP clients resolves to exactly
one profile.
"""
import json

import pytest

from joyverse import identity
from joyverse.identity import (
    new_user_id, is_user_id, normalise_handle,
    resolve_handle, resolve_identity, lookup_handle, link_identity,
)


@pytest.fixture(autouse=True)
def _registry(fake_r2):
    """Every test here reads and writes the registry."""
    return fake_r2


class TestUserIds:
    def test_shape(self):
        uid = new_user_id()
        assert uid.startswith("u_") and len(uid) == 14

    def test_is_lowercase_hex(self):
        assert all(c in "0123456789abcdef" for c in new_user_id()[2:])

    def test_ids_are_unique(self):
        assert len({new_user_id() for _ in range(200)}) == 200

    def test_is_user_id_accepts_generated(self):
        assert is_user_id(new_user_id())

    @pytest.mark.parametrize("bad", [
        "joydip", "u_", "u_short", "U_a1b2c3d4e5f6", "u_a1b2c3d4e5fG",
        "u_a1b2c3d4e5f67", "a1b2c3d4e5f6", "", None, 123,
        "u_A1B2C3D4E5F6",
    ])
    def test_is_user_id_rejects_everything_else(self, bad):
        assert not is_user_id(bad)

    def test_no_case_folding_ambiguity(self):
        # Lowercase-only is deliberate: users/Joy/ and users/joy/ resolving to
        # one directory is what prevents a duplicated profile.
        assert not is_user_id(new_user_id().upper())


class TestHandleValidation:
    def test_lowercases(self):
        assert normalise_handle("JoyDip") == "joydip"

    def test_strips_surrounding_space(self):
        assert normalise_handle("  joydip  ") == "joydip"

    @pytest.mark.parametrize("ok", ["abc", "a-b_c.d", "user123", "x" * 32])
    def test_accepts_valid_handles(self, ok):
        assert normalise_handle(ok) == ok

    @pytest.mark.parametrize("bad", [
        "", "   ", "ab", "x" * 33, "has space", "sömething", "a/b",
        "../evil", "a\\b", "user@host", "a+b", "_registry", "u_a1b2c3d4e5f6",
        "tab\tchar", "emoji🙂",
    ])
    def test_rejects_invalid_handles(self, bad):
        with pytest.raises(ValueError):
            normalise_handle(bad)

    def test_rejects_non_string(self):
        with pytest.raises(ValueError):
            normalise_handle(123)

    def test_registry_namespace_is_reserved(self):
        with pytest.raises(ValueError):
            normalise_handle("_Registry")

    def test_handle_cannot_masquerade_as_a_user_id(self):
        # Otherwise someone could type a literal path and collide with an id
        with pytest.raises(ValueError):
            normalise_handle("u_a1b2c3d4e5f6")


class TestResolveHandle:
    def test_first_use_creates_a_user(self):
        assert is_user_id(resolve_handle("newperson"))

    def test_second_use_returns_the_same_user(self):
        assert resolve_handle("repeat") == resolve_handle("repeat")

    def test_case_variants_are_the_same_person(self):
        assert resolve_handle("MixedCase") == resolve_handle("mixedcase")

    def test_whitespace_does_not_create_a_second_user(self):
        assert resolve_handle("spaced") == resolve_handle(" spaced ")

    def test_different_handles_are_different_users(self):
        assert resolve_handle("alice") != resolve_handle("bob")

    def test_lookup_does_not_create(self):
        assert lookup_handle("ghost") is None
        assert lookup_handle("ghost") is None

    def test_lookup_returns_the_id(self):
        uid = resolve_handle("findme")
        assert lookup_handle("findme") == uid

    def test_lookup_is_case_insensitive(self):
        uid = resolve_handle("Casey")
        assert lookup_handle("casey") == uid

    def test_lookup_of_invalid_handle_is_none_not_an_error(self):
        assert lookup_handle("../evil") is None

    def test_read_only_mode_refuses_to_create(self):
        with pytest.raises(KeyError):
            resolve_handle("readonly", allow_create=False)

    def test_read_only_mode_still_resolves(self):
        uid = resolve_handle("existing")
        assert resolve_handle("existing", allow_create=False) == uid

    def test_get_user_returns_the_record(self):
        assert identity.get_user(resolve_handle("recorded"))["handle"] == "recorded"

    def test_get_user_of_unknown_is_none(self):
        assert identity.get_user("u_deadbeefcafe") is None


class TestResolveIdentity:
    ISSUER = "https://tenant.auth0.com/"
    SUB = "auth0|abc123"

    def test_first_use_creates_a_user(self):
        assert is_user_id(resolve_identity(self.ISSUER, self.SUB))

    def test_same_identity_always_resolves_to_the_same_user(self):
        assert (resolve_identity(self.ISSUER, self.SUB) ==
                resolve_identity(self.ISSUER, self.SUB))

    def test_different_subjects_are_different_users(self):
        assert (resolve_identity(self.ISSUER, "auth0|aaa") !=
                resolve_identity(self.ISSUER, "auth0|bbb"))

    def test_same_subject_at_different_issuers_is_different(self):
        # `sub` is only unique within an issuer, so the key must be composite
        assert (resolve_identity("https://a.example/", "sub") !=
                resolve_identity("https://b.example/", "sub"))

    def test_requires_issuer_and_subject(self):
        with pytest.raises(ValueError):
            resolve_identity("", self.SUB)
        with pytest.raises(ValueError):
            resolve_identity(self.ISSUER, "")

    def test_opaque_sub_with_a_pipe_is_handled(self):
        # Auth0 subs contain "|", which is not a legal path character -- the
        # reason the registry keys on a composite string, not the raw sub
        assert is_user_id(resolve_identity(self.ISSUER, "auth0|weird|value"))

    def test_verified_email_is_recorded(self):
        uid = resolve_identity(self.ISSUER, self.SUB,
                               email="a@b.com", email_verified=True)
        assert identity.get_user(uid)["email"] == "a@b.com"

    def test_unverified_email_is_not_recorded(self):
        # an unverified address is attacker-supplied and must not be indexed
        uid = resolve_identity(self.ISSUER, self.SUB,
                               email="evil@x.com", email_verified=False)
        assert "email" not in identity.get_user(uid)


class TestAccountLinking:
    ISSUER = "https://tenant.auth0.com/"

    def test_second_identity_with_same_verified_email_shares_the_profile(self):
        # The person signed up with Google, then added GitHub. They must land
        # on the profile they already have, not a second empty one.
        first = resolve_identity(self.ISSUER, "google|sub1",
                                 email="person@example.com",
                                 email_verified=True)
        second = resolve_identity(self.ISSUER, "github|sub2",
                                  email="person@example.com",
                                  email_verified=True)
        assert first == second

    def test_linking_records_both_identities(self):
        uid = resolve_identity(self.ISSUER, "a|1",
                               email="p@example.com", email_verified=True)
        resolve_identity(self.ISSUER, "b|2",
                         email="p@example.com", email_verified=True)
        links = identity.get_user(uid)["links"]
        assert {"issuer": self.ISSUER, "sub": "a|1"} in links
        assert {"issuer": self.ISSUER, "sub": "b|2"} in links

    def test_unverified_email_cannot_claim_an_existing_account(self):
        # Otherwise anyone could sign up with a stolen address and inherit
        # somebody's profile.
        first = resolve_identity(self.ISSUER, "a|1",
                                 email="person@example.com",
                                 email_verified=True)
        second = resolve_identity(self.ISSUER, "b|2",
                                  email="person@example.com",
                                  email_verified=False)
        assert first != second

    def test_missing_email_never_links(self):
        assert (resolve_identity(self.ISSUER, "a|1") !=
                resolve_identity(self.ISSUER, "b|2"))

    def test_email_case_does_not_block_linking(self):
        first = resolve_identity(self.ISSUER, "a|1",
                                 email="Person@Example.com",
                                 email_verified=True)
        second = resolve_identity(self.ISSUER, "b|2",
                                  email="person@example.com",
                                  email_verified=True)
        assert first == second


class TestLinkIdentity:
    ISSUER = "https://tenant.auth0.com/"

    def test_links_to_an_existing_account(self):
        uid = resolve_handle("manual")
        assert link_identity(uid, self.ISSUER, "manual|sub") == uid

    def test_linked_identity_then_resolves_to_that_account(self):
        uid = resolve_handle("manual")
        link_identity(uid, self.ISSUER, "manual|sub")
        assert resolve_identity(self.ISSUER, "manual|sub") == uid

    def test_cannot_steal_an_identity_from_another_account(self):
        uid = resolve_handle("mine")
        resolve_identity(self.ISSUER, "someone|elsesub")
        with pytest.raises(ValueError):
            link_identity(uid, self.ISSUER, "someone|elsesub")

    def test_relinking_the_same_identity_is_idempotent(self):
        uid = resolve_handle("twice")
        link_identity(uid, self.ISSUER, "twice|sub")
        assert link_identity(uid, self.ISSUER, "twice|sub") == uid

    def test_unknown_user_id_is_refused(self):
        with pytest.raises(KeyError):
            link_identity("u_deadbeefcafe", self.ISSUER, "x")

    def test_malformed_user_id_is_refused(self):
        with pytest.raises(ValueError):
            link_identity("joydip", self.ISSUER, "x")


class TestCrossPathConvergence:
    """The core requirement: one person, one profile, however they arrive."""

    ISSUER = "https://tenant.auth0.com/"

    def test_oauth_across_many_lookups_is_one_user(self):
        # Simulates Claude, Cursor and ChatGPT each presenting a fresh token
        # for the same person. Tokens differ; identity must not.
        uid = resolve_identity(self.ISSUER, "auth0|sameperson")
        for _ in range(5):
            assert resolve_identity(self.ISSUER, "auth0|sameperson") == uid

    def test_bearer_and_oauth_are_separate_accounts(self):
        # Deliberate: a handle and an OAuth identity are different inputs and
        # must not silently merge, or anyone could claim a handle they do not own.
        assert resolve_handle("joydip880") != \
            resolve_identity(self.ISSUER, "auth0|someone")

    def test_a_new_handle_never_aliases_an_oauth_user(self):
        oauth_uid = resolve_identity(self.ISSUER, "auth0|x")
        assert resolve_handle("somehandle") != oauth_uid

    def test_many_users_get_distinct_profiles(self):
        assert len({resolve_handle(f"person{i}") for i in range(50)}) == 50


class TestRegistryPersistence:
    ISSUER = "https://tenant.auth0.com/"

    def test_registry_is_stored_as_json(self, fake_r2):
        uid = resolve_handle("persist")
        raw = fake_r2.store["users/_registry/identity.json"].decode()
        assert uid in json.loads(raw)["users"]

    def test_registry_key_is_outside_the_user_namespace(self):
        # _registry sorts before u_ ids and must never be reachable as a user
        assert identity.REGISTRY_KEY.startswith("users/_registry/")

    def test_account_key_is_namespaced(self):
        assert identity.account_key("u_a1b2c3d4e5f6") == \
            "users/u_a1b2c3d4e5f6/account.json"

    def test_list_user_ids(self):
        uid = resolve_identity(self.ISSUER, "auth0|listed")
        resolve_handle("listed")
        ids = identity.list_user_ids()
        assert uid in ids and len(ids) >= 2


class TestFakeR2CoversEveryModule:
    """Guard: a new module touching R2 must be added to the fake_r2 fixture.

    Each joyverse module does `from joyverse.config import r2_client`, which
    binds the name locally. Patching config alone would leave the others
    pointing at the real client -- so a test would quietly read or write the
    live bucket instead of the fake.
    """

    def test_every_module_importing_r2_client_is_patched(self):
        import inspect
        import pathlib
        import re

        import tests.conftest as tc

        source = inspect.getsource(tc.fake_r2)
        patched = set(re.findall(r'"(\w+)"', source))

        touching = set()
        for path in pathlib.Path("joyverse").glob("*.py"):
            if re.search(r"import.*r2_client", path.read_text()):
                touching.add(path.stem)

        assert touching, "expected some modules to use r2_client"
        assert touching <= patched, (
            f"not faked in tests: {sorted(touching - patched)}")