"""Unit tests for joyverse/profile.py -- profile read/update tool logic."""
import json
import pytest
from joyverse import profile as prof


USER = {"user_id": "u_a1b2c3d4e5f6"}
PROFILE_MD = "## Identity\nname: Joydip\nage: 22\nlocation: Kolkata\n"


class TestGetProfile:
    def test_returns_stored_markdown(self, fake_r2):
        fake_r2.seed("users/u_a1b2c3d4e5f6/profile.md", PROFILE_MD)
        assert prof.get_profile(USER) == PROFILE_MD

    def test_missing_profile_returns_json_error(self, fake_r2):
        out = prof.get_profile(USER)
        assert "error" in json.loads(out)
        assert "u_a1b2c3d4e5f6" in json.loads(out)["error"]

    def test_reads_the_users_own_key(self, fake_r2):
        fake_r2.seed("users/u_a1b2c3d4e5f6/profile.md", "x")
        prof.get_profile(USER)
        assert fake_r2.puts == []  # read-only, no writes


class TestUpdateProfile:
    def test_replaces_existing_field(self, fake_r2):
        fake_r2.seed("users/u_a1b2c3d4e5f6/profile.md", PROFILE_MD)
        out = prof.update_profile("age", "23", USER)
        assert "Updated" in out
        body = fake_r2.store["users/u_a1b2c3d4e5f6/profile.md"].decode()
        assert "age: 23" in body

    def test_preserves_other_fields(self, fake_r2):
        fake_r2.seed("users/u_a1b2c3d4e5f6/profile.md", PROFILE_MD)
        prof.update_profile("age", "23", USER)
        body = fake_r2.store["users/u_a1b2c3d4e5f6/profile.md"].decode()
        assert "name: Joydip" in body and "location: Kolkata" in body

    def test_unknown_field_is_appended(self, fake_r2):
        fake_r2.seed("users/u_a1b2c3d4e5f6/profile.md", PROFILE_MD)
        prof.update_profile("occupation", "SDE", USER)
        body = fake_r2.store["users/u_a1b2c3d4e5f6/profile.md"].decode()
        assert "occupation: SDE" in body

    def test_writes_markdown_content_type(self, fake_r2):
        fake_r2.seed("users/u_a1b2c3d4e5f6/profile.md", PROFILE_MD)
        prof.update_profile("age", "23", USER)
        assert fake_r2.puts[0]["ContentType"] == "text/markdown"

    def test_only_first_occurrence_is_replaced(self, fake_r2):
        fake_r2.seed("users/u_a1b2c3d4e5f6/profile.md", "age: 1\nage: 2\n")
        prof.update_profile("age", "9", USER)
        body = fake_r2.store["users/u_a1b2c3d4e5f6/profile.md"].decode()
        assert body == "age: 9\nage: 2\n"

    def test_missing_profile_is_created_not_rejected(self, fake_r2):
        # Was: returned "Profile does not exist. Run setup_profile first."
        out = prof.update_profile("age", "23", USER)
        assert "Error" not in out
        assert "age: 23" in fake_r2.store["users/u_a1b2c3d4e5f6/profile.md"].decode()

    def test_value_containing_colon_is_preserved(self, fake_r2):
        fake_r2.seed("users/u_a1b2c3d4e5f6/profile.md", "role: x\n")
        prof.update_profile("role", "a: b", USER)
        assert "role: a: b" in fake_r2.store["users/u_a1b2c3d4e5f6/profile.md"].decode()

    def test_write_failure_is_reported(self, fake_r2, monkeypatch):
        fake_r2.seed("users/u_a1b2c3d4e5f6/profile.md", PROFILE_MD)

        def boom(**kw):
            raise Exception("network down")
        monkeypatch.setattr(fake_r2, "put_object", boom)

        assert "Error" in prof.update_profile("age", "23", USER)


class TestCreatesProfile:
    """Regression: update_profile used to bail on a missing profile, so a
    brand-new user could never be onboarded. The LLM hit this on a live run."""

    def test_first_write_creates_the_profile(self, fake_r2):
        out = prof.update_profile("name", "Aarav Mehta", USER)
        assert "Updated" in out
        assert "users/u_a1b2c3d4e5f6/profile.md" in fake_r2.store

    def test_no_error_on_first_write(self, fake_r2):
        assert "Error" not in prof.update_profile("name", "A", USER)

    def test_seeded_document_has_h1_header(self, fake_r2):
        prof.update_profile("name", "A", USER)
        assert fake_r2.store["users/u_a1b2c3d4e5f6/profile.md"].decode().startswith("# ")

    def test_created_profile_is_readable(self, fake_r2):
        prof.update_profile("name", "Aarav Mehta", USER)
        assert "Aarav Mehta" in prof.get_profile(USER)

    def test_multiple_first_writes_accumulate(self, fake_r2):
        prof.update_profile("name", "Aarav", USER)
        prof.update_profile("age", "24", USER)
        body = fake_r2.store["users/u_a1b2c3d4e5f6/profile.md"].decode()
        assert "name: Aarav" in body and "age: 24" in body

    def test_creates_without_a_prior_read(self, fake_r2):
        # The old code required a profile to already exist; a new user must be
        # onboardable with writes only.
        prof.update_profile("name", "A", USER)
        assert "name: A" in prof.get_profile(USER)


class TestSectionFields:
    """The LLM's first instinct was field="Identity", value="name: Aarav\\nage: 24".
    Without section support that produced a literal 'Identity: name: Aarav' line."""

    def test_section_creates_header(self, fake_r2):
        prof.update_profile("Identity", "name: Aarav Mehta", USER)
        body = fake_r2.store["users/u_a1b2c3d4e5f6/profile.md"].decode()
        assert "## Identity" in body

    def test_multiline_block_preserved(self, fake_r2):
        prof.update_profile("Identity", "name: Aarav\nage: 24", USER)
        body = fake_r2.store["users/u_a1b2c3d4e5f6/profile.md"].decode()
        assert "name: Aarav" in body and "age: 24" in body

    def test_block_is_not_wrapped_in_a_bogus_field(self, fake_r2):
        prof.update_profile("Identity", "name: Aarav", USER)
        body = fake_r2.store["users/u_a1b2c3d4e5f6/profile.md"].decode()
        assert "Identity: name:" not in body

    def test_h1_precedes_section(self, fake_r2):
        prof.update_profile("Identity", "name: A", USER)
        body = fake_r2.store["users/u_a1b2c3d4e5f6/profile.md"].decode()
        assert body.index("# ") < body.index("## Identity")

    def test_two_sections_coexist(self, fake_r2):
        prof.update_profile("Identity", "name: A", USER)
        prof.update_profile("Skills", "Python: expert", USER)
        body = fake_r2.store["users/u_a1b2c3d4e5f6/profile.md"].decode()
        assert "## Identity" in body and "## Skills" in body

    def test_repeat_section_does_not_duplicate_header(self, fake_r2):
        prof.update_profile("Identity", "name: A", USER)
        prof.update_profile("Identity", "name: B", USER)
        body = fake_r2.store["users/u_a1b2c3d4e5f6/profile.md"].decode()
        assert body.count("## Identity") == 1

    def test_hashed_section_name_is_accepted(self, fake_r2):
        prof.update_profile("## Skills", "Go: intermediate", USER)
        body = fake_r2.store["users/u_a1b2c3d4e5f6/profile.md"].decode()
        assert "## Skills" in body

    def test_plain_key_is_still_key_value(self, fake_r2):
        prof.update_profile("age", "24", USER)
        body = fake_r2.store["users/u_a1b2c3d4e5f6/profile.md"].decode()
        assert "age: 24" in body
        assert "## age" not in body

    def test_field_with_spaces_is_treated_as_a_key(self, fake_r2):
        prof.update_profile("current role", "Backend Engineer", USER)
        body = fake_r2.store["users/u_a1b2c3d4e5f6/profile.md"].decode()
        assert "current role: Backend Engineer" in body


class TestSectionReplace:
    """Regression: section re-calls appended instead of replacing, so stale
    content accumulated in the live R2 profile."""

    def test_single_line_recall_replaces(self, fake_r2):
        prof.update_profile("Identity", "name: A", USER)
        prof.update_profile("Identity", "name: B", USER)
        body = fake_r2.store["users/u_a1b2c3d4e5f6/profile.md"].decode()
        assert "name: B" in body and "name: A" not in body

    def test_multiline_recall_replaces(self, fake_r2):
        prof.update_profile("Identity", "name: A", USER)
        prof.update_profile("Identity", "name: B\nage: 24", USER)
        body = fake_r2.store["users/u_a1b2c3d4e5f6/profile.md"].decode()
        assert "name: A" not in body
        assert "name: B" in body and "age: 24" in body

    def test_sibling_sections_survive_a_recall(self, fake_r2):
        prof.update_profile("Identity", "name: A", USER)
        prof.update_profile("Skills", "Go: expert", USER)
        prof.update_profile("Identity", "name: B", USER)
        body = fake_r2.store["users/u_a1b2c3d4e5f6/profile.md"].decode()
        assert "name: B" in body and "Go: expert" in body

    def test_no_inline_field_leftovers(self, fake_r2):
        prof.update_profile("Identity", "name: A", USER)
        prof.update_profile("Skills", "Go: expert", USER)
        prof.update_profile("Identity", "name: B", USER)
        body = fake_r2.store["users/u_a1b2c3d4e5f6/profile.md"].decode()
        assert "\nIdentity:" not in body and "\nSkills:" not in body

    def test_recall_keeps_section_order(self, fake_r2):
        prof.update_profile("Identity", "name: A", USER)
        prof.update_profile("Skills", "Go: x", USER)
        prof.update_profile("Background", "text", USER)
        prof.update_profile("Identity", "name: B", USER)
        body = fake_r2.store["users/u_a1b2c3d4e5f6/profile.md"].decode()
        assert body.index("## Identity") < body.index("## Background")

    def test_nested_header_in_value_is_preserved(self, fake_r2):
        prof.update_profile("Skills", "langs: python\n\n## Working Style\nx: y",
                            USER)
        body = fake_r2.store["users/u_a1b2c3d4e5f6/profile.md"].decode()
        assert "## Working Style" in body and "langs: python" in body


class TestErrorMessages:
    def test_get_profile_does_not_name_a_nonexistent_tool(self, fake_r2):
        msg = json.loads(prof.get_profile(USER))["error"]
        assert "setup_profile" not in msg

    def test_get_profile_points_at_a_real_tool(self, fake_r2):
        assert "update_profile" in json.loads(prof.get_profile(USER))["error"]

    @pytest.mark.parametrize("bad", ["", "   "])
    def test_empty_field_rejected(self, fake_r2, bad):
        assert "Error" in prof.update_profile(bad, "x", USER)
        assert fake_r2.puts == []


class TestUserIsolation:
    def test_updates_do_not_cross_users(self, fake_r2):
        fake_r2.seed("users/u_a1b2c3d4e5f6/profile.md", "age: 1\n")
        fake_r2.seed("users/u_0a1b2c3d4e5f/profile.md", "age: 99\n")
        prof.update_profile("age", "50", USER)
        assert "age: 99" in fake_r2.store["users/u_0a1b2c3d4e5f/profile.md"].decode()

    def test_missing_user_does_not_see_another_users_data(self, fake_r2):
        fake_r2.seed("users/u_a1b2c3d4e5f6/profile.md", "name: Joydip\n")
        out = prof.get_profile({"user_id": "u_carolcafe1234"[:2]+"c0ffee123456"[:12]})
        assert "error" in json.loads(out)
