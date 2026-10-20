"""Tests for joyverse/data.py topic discovery.

An agent that cannot see which topics exist has to guess, and a wrong guess
returns a bare error it cannot recover from. These pin both halves of the fix:
list_topics, and get_data's error that points back at it.
"""
import json

import pytest

from conftest import TEST_USER_ID as UID, TEST_USER_ID_2 as UID2
from joyverse import data as dat

USER = {"user_id": UID}
OTHER = {"user_id": UID2}

TOPICS = [
    ("dsa", {"summary": "150+ DSA problems solved", "last_updated": "2025-06-11"}),
    ("mobile_games", {"summary": "Deep mobile gaming history", "last_updated": "2025-06-11"}),
    ("steam_games", {"summary": "153 games in Steam library", "last_updated": "2025-06-11"}),
]


def seed_topics(fake_r2, topics=TOPICS, user_id=UID):
    for topic, payload in topics:
        fake_r2.seed(f"users/{user_id}/data/{topic}/progress.json",
                     json.dumps(payload))


class TestListTopics:
    def test_lists_every_seeded_topic(self, fake_r2):
        seed_topics(fake_r2)
        out = json.loads(dat.list_topics(USER))
        assert out["count"] == 3
        assert {t["topic"] for t in out["topics"]} == {"dsa", "mobile_games", "steam_games"}

    def test_uses_summary_as_the_description(self, fake_r2):
        seed_topics(fake_r2)
        out = json.loads(dat.list_topics(USER))
        desc = {t["topic"]: t["description"] for t in out["topics"]}
        assert desc["mobile_games"] == "Deep mobile gaming history"
        assert desc["steam_games"] == "153 games in Steam library"

    def test_explicit_description_wins_over_summary(self, fake_r2):
        fake_r2.seed(f"users/{UID}/data/gaming/progress.json",
                     json.dumps({"summary": "short", "description": "the real one"}))
        out = json.loads(dat.list_topics(USER))
        assert out["topics"][0]["description"] == "the real one"

    def test_topics_are_sorted(self, fake_r2):
        seed_topics(fake_r2)
        names = [t["topic"] for t in json.loads(dat.list_topics(USER))["topics"]]
        assert names == sorted(names)

    def test_reports_size(self, fake_r2):
        seed_topics(fake_r2)
        out = json.loads(dat.list_topics(USER))
        assert all(t["size_bytes"] > 0 for t in out["topics"])

    def test_empty_user_gets_an_empty_list_not_an_error(self, fake_r2):
        out = json.loads(dat.list_topics(USER))
        assert out["count"] == 0 and out["topics"] == []

    def test_includes_the_hint(self, fake_r2):
        assert "hint" in json.loads(dat.list_topics(USER))

    def test_ignores_the_registry_object(self, fake_r2):
        # users/_registry/... must never appear as a topic
        fake_r2.seed("users/_registry/identity.json", "{}")
        out = json.loads(dat.list_topics(USER))
        assert all("registry" not in t["topic"] for t in out["topics"])

    def test_never_lists_another_users_topics(self, fake_r2):
        # The isolation property that matters most: no cross-tenant leak.
        seed_topics(fake_r2)
        out = json.loads(dat.list_topics(OTHER))
        assert out["count"] == 0

    def test_a_topic_with_unreadable_json_still_lists(self, fake_r2):
        fake_r2.seed(f"users/{UID}/data/broken/progress.json", "not json at all")
        out = json.loads(dat.list_topics(USER))
        assert out["count"] == 1
        assert out["topics"][0]["description"] == ""

    def test_filename_does_not_affect_the_topic_name(self, fake_r2):
        # steam_games lives at data/steam_games/progress.json today, but the
        # topic must come from the folder, not from filename_map.
        fake_r2.seed(f"users/{UID}/data/electronics/stack.json",
                     json.dumps({"summary": "lab kit"}))
        out = json.loads(dat.list_topics(USER))
        assert out["topics"][0]["topic"] == "electronics"


class TestGetDataMissIsHelpful:
    """The error must make a wrong guess recoverable, not a dead end."""

    def test_lists_available_topics(self, fake_r2):
        seed_topics(fake_r2)
        out = json.loads(dat.get_data("nope", USER))
        assert out["available_topics"] == ["dsa", "mobile_games", "steam_games"]

    @pytest.mark.parametrize("probe,expected", [
        ("game", "mobile_games"),      # edit distance
        ("games", "steam_games"),       # substring via the plural
        ("mobile", "mobile_games"),     # substring
        ("mobilegames", "mobile_games"),  # separator folded
        ("steam", "steam_games"),       # prefix
    ])
    def test_suggests_a_near_miss(self, fake_r2, probe, expected):
        seed_topics(fake_r2)
        out = json.loads(dat.get_data(probe, USER))
        assert expected in out["did_you_mean"]

    def test_unrelated_probe_recovers_via_available_topics(self, fake_r2):
        """A probe nothing resembles still leaves the caller able to recover.

        "gaming" is the real case from a client that guessed the name: it is
        not similar enough to steam_games or mobile_games for any similarity
        metric, so the recovery path is the full topic list, not a suggestion.
        """
        seed_topics(fake_r2)
        out = json.loads(dat.get_data("gaming", USER))
        assert out["available_topics"] == ["dsa", "mobile_games", "steam_games"]
        assert "list_topics" in out["hint"]

    def test_points_at_list_topics(self, fake_r2):
        seed_topics(fake_r2)
        out = json.loads(dat.get_data("nope", USER))
        assert "list_topics" in out["hint"]

    def test_no_suggestions_for_a_wildly_wrong_name(self, fake_r2):
        seed_topics(fake_r2)
        out = json.loads(dat.get_data("zzzzqqqq", USER))
        assert "did_you_mean" not in out

    def test_available_topics_empty_when_user_has_none(self, fake_r2):
        out = json.loads(dat.get_data("anything", USER))
        assert out["available_topics"] == []

    def test_miss_never_lists_another_users_topics(self, fake_r2):
        seed_topics(fake_r2)
        out = json.loads(dat.get_data("nope", OTHER))
        assert out["available_topics"] == []

    def test_still_reports_the_original_error(self, fake_r2):
        seed_topics(fake_r2)
        assert "No data found" in json.loads(dat.get_data("nope", USER))["error"]


class TestGetDataStillWorks:
    """Discovery must not regress the happy path."""

    def test_returns_stored_log(self, fake_r2):
        seed_topics(fake_r2)
        out = json.loads(dat.get_data("mobile_games", USER))
        assert out["summary"] == "Deep mobile gaming history"

    def test_update_then_list(self, fake_r2):
        dat.update_data("reading", json.dumps({"summary": "two books"}), USER)
        out = json.loads(dat.list_topics(USER))
        assert out["topics"][0]["topic"] == "reading"
        assert out["topics"][0]["description"] == "two books"
