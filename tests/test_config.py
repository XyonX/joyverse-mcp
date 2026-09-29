"""Unit tests for joyverse/config.py -- R2 key construction and traversal guards."""
import pytest
from joyverse.config import (
    get_profile_key, get_bio_key, get_memory_key, get_data_key,
    _safe_segment,
)


class TestKeyShape:
    def test_profile_key(self):
        assert get_profile_key("joydip") == "users/joydip/profile.md"

    def test_bio_key(self):
        assert get_bio_key("joydip") == "users/joydip/bio.md"

    def test_memory_key(self):
        assert get_memory_key("joydip") == "users/joydip/memory.json"

    def test_data_key(self):
        assert get_data_key("joydip", "dsa") == "users/joydip/data/dsa/progress.json"

    def test_keys_are_namespaced_per_user(self):
        assert get_profile_key("alice") != get_profile_key("bob")

    def test_all_keys_start_with_users_prefix(self):
        for key in [get_profile_key("a"), get_bio_key("a"),
                    get_memory_key("a"), get_data_key("a", "t")]:
            assert key.startswith("users/a/")


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
        assert "dsa" in get_data_key("joydip", "dsa")

    def test_topics_with_dots_are_allowed(self):
        # "v1.2" has dots but no traversal
        assert get_data_key("joydip", "v1.2").endswith("data/v1.2/progress.json")

    def test_topic_specific_filenames(self):
        """Test that known topics get their specific filenames."""
        assert get_data_key("joydip", "dsa") == "users/joydip/data/dsa/progress.json"
        assert get_data_key("joydip", "projects") == "users/joydip/data/projects/active.json"
        assert get_data_key("joydip", "skills") == "users/joydip/data/skills/stack.json"
        assert get_data_key("joydip", "reading") == "users/joydip/data/reading/list.json"
        assert get_data_key("joydip", "games") == "users/joydip/data/games/played.json"
