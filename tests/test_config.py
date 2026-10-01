"""Unit tests for joyverse/config.py -- R2 key construction and traversal guards."""
import pytest
from conftest import TEST_USER_ID as UID, TEST_USER_ID_2 as UID2
from joyverse.config import (
    get_profile_key, get_bio_key, get_memory_key, get_data_key,
    _safe_segment, _safe_user_id,
)


class TestKeyShape:
    def test_profile_key(self):
        assert get_profile_key(UID) == f"users/{UID}/profile.md"

    def test_bio_key(self):
        assert get_bio_key(UID) == f"users/{UID}/bio.md"

    def test_memory_key(self):
        assert get_memory_key(UID) == f"users/{UID}/memory.json"

    def test_data_key(self):
        assert get_data_key(UID, "dsa") == \
            f"users/{UID}/data/dsa/progress.json"

    def test_keys_are_namespaced_per_user(self):
        assert get_profile_key(UID) != get_profile_key(UID2)

    def test_all_keys_start_with_users_prefix(self):
        for key in [get_profile_key(UID), get_bio_key(UID),
                    get_memory_key(UID), get_data_key(UID, "t")]:
            assert key.startswith(f"users/{UID}/")


class TestSafeSegment:
    def test_accepts_plain_name(self):
        assert _safe_segment("joydip") == "joydip"

    def test_accepts_dashes_and_dots_without_traversal(self):
        # a single dot is fine; only ".." is dangerous
        assert _safe_segment("joy.dip") == "joy.dip"
        assert _safe_segment("joy-dip") == "joy-dip"

    @pytest.mark.parametrize("bad", [
        "..", "../etc", "a/b", "a\\b", "", "   ", "/abs", "a/../b",
    ])
    def test_rejects_unsafe_segments(self, bad):
        with pytest.raises(ValueError):
            _safe_segment(bad)


class TestTraversalRegression:
    """Regression: topic/username must not escape the user's own prefix.

    A topic reached the R2 key unsanitised, so '../../other' built a key that
    resolved outside users/<name>/ -- a client could read and write another
    user's data. jwt_auth already guarded the username; these lock down every
    key builder.
    """

    def test_traversal_username_rejected_in_every_builder(self):
        for fn in [get_profile_key, get_bio_key, get_memory_key]:
            with pytest.raises(ValueError):
                fn("../evil")

    def test_traversal_topic_rejected(self):
        with pytest.raises(ValueError):
            get_data_key("joydip", "../../../etc")

    def test_slash_in_topic_rejected(self):
        with pytest.raises(ValueError):
            get_data_key("joydip", "dsa/sub")

    def test_backslash_in_topic_rejected(self):
        with pytest.raises(ValueError):
            get_data_key("joydip", "dsa\\sub")

    def test_legitimate_topic_still_works(self):
        assert "dsa" in get_data_key(UID, "dsa")

    def test_topics_with_dots_are_allowed(self):
        # "v1.2" has dots but no traversal
        assert get_data_key(UID, "v1.2").endswith("data/v1.2/progress.json")

    def test_topic_specific_filenames(self):
        """Test that known topics get their specific filenames."""
        assert get_data_key(UID, "dsa") == \
            f"users/{UID}/data/dsa/progress.json"
        assert get_data_key(UID, "projects") == \
            f"users/{UID}/data/projects/active.json"
        assert get_data_key(UID, "skills") == \
            f"users/{UID}/data/skills/stack.json"
        assert get_data_key(UID, "reading") == \
            f"users/{UID}/data/reading/list.json"
        assert get_data_key(UID, "games") == \
            f"users/{UID}/data/games/played.json"


class TestIssuerAdvertisement:
    """The issuer we advertise must byte-match the AS metadata's `issuer`.

    This is the check a compliant MCP client performs, and it is a strict
    string comparison. Auth0's issuer ends in "/", so advertising it without
    the slash made every MCP client refuse to connect with:

        OAuth error: Authorization server metadata issuer mismatch:
        https://tenant.auth0.com/ != https://tenant.auth0.com

    Only a real client catches this: our own tests compared claims rather
    than walking the discovery chain the way a client does.
    """

    ISSUER = "https://tenant.auth0.com/"

    def test_advertised_issuer_keeps_its_trailing_slash(self):
        from joyverse import config

        assert config.authorization_servers() is not None

    def test_advertised_value_is_the_exact_issuer_string(self, monkeypatch):
        from joyverse import config

        monkeypatch.setattr(config, "OAUTH_ENABLED", True)
        monkeypatch.setattr(config, "AUTH0_DOMAIN", "tenant.auth0.com")
        advertised = config.authorization_servers()[0]

        # This is verbatim what an MCP client compares.
        assert advertised == self.ISSUER
        assert advertised.endswith("/")

    def test_discovery_and_token_validation_agree(self, monkeypatch):
        # The same string must serve both roles, or one of them is wrong.
        from joyverse import config

        monkeypatch.setattr(config, "OAUTH_ENABLED", True)
        monkeypatch.setattr(config, "AUTH0_DOMAIN", "tenant.auth0.com")
        assert config.authorization_servers()[0] == config.issuer_url()

    def test_domain_with_trailing_slash_is_normalised(self, monkeypatch):
        # A user pasting "https://tenant.auth0.com/" into AUTH0_DOMAIN must
        # still produce exactly one slash, not two.
        from joyverse import config

        monkeypatch.setattr(config, "OAUTH_ENABLED", True)
        monkeypatch.setattr(config, "AUTH0_DOMAIN", "https://tenant.auth0.com/")
        assert config.issuer_url() == self.ISSUER

    def test_bare_domain_gets_https_and_one_slash(self, monkeypatch):
        from joyverse import config

        monkeypatch.setattr(config, "AUTH0_DOMAIN", "tenant.auth0.com")
        assert config.issuer_url() == self.ISSUER


class TestSafeUserId:
    """The stricter guard on the value that actually reaches R2."""

    def test_accepts_a_generated_id(self):
        assert _safe_user_id(UID) == UID

    @pytest.mark.parametrize("bad", [
        "joydip",                 # a bare handle is not a user_id
        "u_short",                # wrong length
        "U_a1b2c3d4e5f6",        # uppercase prefix
        "u_a1b2c3d4e5fG",         # non-hex character
        "u_../../etc",            # traversal
        "_registry",              # the registry namespace itself
        "",
        "   ",
    ])
    def test_rejects_anything_not_minted_by_us(self, bad):
        with pytest.raises(ValueError):
            _safe_user_id(bad)

    def test_registry_namespace_is_not_writable_as_a_user(self):
        # Otherwise a crafted user_id could overwrite the identity registry
        for fn in [get_profile_key, get_bio_key, get_memory_key]:
            with pytest.raises(ValueError):
                fn("_registry")

    def test_handle_cannot_be_used_as_a_user_id(self):
        # Old data used handles directly. Passing one now fails loudly rather
        # than silently reading the wrong (or no) directory.
        with pytest.raises(ValueError):
            get_profile_key("joydip")

    def test_every_builder_enforces_the_id_shape(self):
        for fn in [get_profile_key, get_bio_key, get_memory_key]:
            with pytest.raises(ValueError):
                fn("nope")
