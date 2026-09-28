"""Unit tests for joyverse/profile.py -- profile read/update tool logic."""
import json
import pytest
from joyverse import profile as prof


USER = {"username": "joydip"}
PROFILE_MD = "## Identity\nname: Joydip\nage: 22\nlocation: Kolkata\n"


class TestGetProfile:
    def test_returns_stored_markdown(self, fake_r2):
        fake_r2.seed("users/joydip/profile.md", PROFILE_MD)
        assert prof.get_profile(USER) == PROFILE_MD

    def test_missing_profile_returns_json_error(self, fake_r2):
        out = prof.get_profile(USER)
        assert "error" in json.loads(out)
        assert "joydip" in json.loads(out)["error"]

    def test_reads_the_users_own_key(self, fake_r2):
        fake_r2.seed("users/joydip/profile.md", "x")
        prof.get_profile(USER)
        assert fake_r2.puts == []  # read-only, no writes


class TestUpdateProfile:
    def test_replaces_existing_field(self, fake_r2):
        fake_r2.seed("users/joydip/profile.md", PROFILE_MD)
        out = prof.update_profile("age", "23", USER)
        assert "Updated" in out
        body = fake_r2.store["users/joydip/profile.md"].decode()
        assert "age: 23" in body

    def test_preserves_other_fields(self, fake_r2):
        fake_r2.seed("users/joydip/profile.md", PROFILE_MD)
        prof.update_profile("age", "23", USER)
        body = fake_r2.store["users/joydip/profile.md"].decode()
        assert "name: Joydip" in body and "location: Kolkata" in body

    def test_unknown_field_is_appended(self, fake_r2):
        fake_r2.seed("users/joydip/profile.md", PROFILE_MD)
        prof.update_profile("occupation", "SDE", USER)
        body = fake_r2.store["users/joydip/profile.md"].decode()
        assert "occupation: SDE" in body

    def test_writes_markdown_content_type(self, fake_r2):
        fake_r2.seed("users/joydip/profile.md", PROFILE_MD)
        prof.update_profile("age", "23", USER)
        assert fake_r2.puts[0]["ContentType"] == "text/markdown"

    def test_only_first_occurrence_is_replaced(self, fake_r2):
        fake_r2.seed("users/joydip/profile.md", "age: 1\nage: 2\n")
        prof.update_profile("age", "9", USER)
        body = fake_r2.store["users/joydip/profile.md"].decode()
        assert body == "age: 9\nage: 2\n"

    def test_missing_profile_reports_error(self, fake_r2):
        out = prof.update_profile("age", "23", USER)
        assert "does not exist" in out
        assert fake_r2.puts == []

    def test_value_containing_colon_is_preserved(self, fake_r2):
        fake_r2.seed("users/joydip/profile.md", "role: x\n")
        prof.update_profile("role", "a: b", USER)
        assert "role: a: b" in fake_r2.store["users/joydip/profile.md"].decode()

    def test_write_failure_is_reported(self, fake_r2, monkeypatch):
        fake_r2.seed("users/joydip/profile.md", PROFILE_MD)

        def boom(**kw):
            raise Exception("network down")
        monkeypatch.setattr(fake_r2, "put_object", boom)

        assert "Error" in prof.update_profile("age", "23", USER)


class TestUserIsolation:
    def test_updates_do_not_cross_users(self, fake_r2):
        fake_r2.seed("users/joydip/profile.md", "age: 1\n")
        fake_r2.seed("users/alice/profile.md", "age: 99\n")
        prof.update_profile("age", "50", USER)
        assert "age: 99" in fake_r2.store["users/alice/profile.md"].decode()

    def test_missing_user_does_not_see_another_users_data(self, fake_r2):
        fake_r2.seed("users/joydip/profile.md", "name: Joydip\n")
        out = prof.get_profile({"username": "carol"})
        assert "error" in json.loads(out)
