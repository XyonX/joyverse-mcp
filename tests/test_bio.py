"""Unit tests for joyverse/bio.py -- biography read/update tool logic."""
import json
import pytest
from joyverse import bio as bio_mod


USER = {"username": "joydip"}
KEY = "users/joydip/bio.md"
BIO_MD = """# User Biography

## Background
Aarav grew up in Pune and moved to Bangalore for college.

## Journey
2018: Started college
2022: First internship
"""


def stored(fake_r2):
    return fake_r2.store[KEY].decode()


class TestGetBio:
    def test_returns_stored_markdown(self, fake_r2):
        fake_r2.seed(KEY, BIO_MD)
        assert bio_mod.get_bio(USER) == BIO_MD

    def test_missing_bio_returns_json_error(self, fake_r2):
        out = json.loads(bio_mod.get_bio(USER))
        assert "error" in out and "joydip" in out["error"]

    def test_r2_failure_returns_json_error(self, fake_r2, monkeypatch):
        def boom(Bucket, Key):
            raise Exception("AccessDenied")
        monkeypatch.setattr(fake_r2, "get_object", boom)
        assert "error" in json.loads(bio_mod.get_bio(USER))

    def test_read_is_side_effect_free(self, fake_r2):
        fake_r2.seed(KEY, BIO_MD)
        bio_mod.get_bio(USER)
        assert fake_r2.puts == []


class TestUpdateBioCreates:
    def test_creates_bio_when_missing(self, fake_r2):
        out = bio_mod.update_bio("Background", "Grew up in Pune.", USER)
        assert "Added" in out
        assert "## Background" in stored(fake_r2)

    def test_creates_with_content(self, fake_r2):
        bio_mod.update_bio("Background", "Grew up in Pune.", USER)
        assert "Grew up in Pune." in stored(fake_r2)

    def test_writes_markdown_content_type(self, fake_r2):
        bio_mod.update_bio("Background", "text", USER)
        assert fake_r2.puts[0]["ContentType"] == "text/markdown"

    def test_writes_to_bio_key(self, fake_r2):
        bio_mod.update_bio("Background", "text", USER)
        assert fake_r2.puts[0]["Key"] == KEY

    def test_sections_accumulate(self, fake_r2):
        bio_mod.update_bio("Background", "b", USER)
        bio_mod.update_bio("Goals", "g", USER)
        body = stored(fake_r2)
        assert "## Background" in body and "## Goals" in body

    def test_order_is_preserved(self, fake_r2):
        bio_mod.update_bio("Background", "b", USER)
        bio_mod.update_bio("Journey", "j", USER)
        body = stored(fake_r2)
        assert body.index("## Background") < body.index("## Journey")


class TestUpdateBioReplaces:
    def test_replaces_existing_section_body(self, fake_r2):
        fake_r2.seed(KEY, BIO_MD)
        out = bio_mod.update_bio("Background", "New narrative here.", USER)
        assert "Updated" in out
        assert "New narrative here." in stored(fake_r2)

    def test_old_body_is_removed(self, fake_r2):
        fake_r2.seed(KEY, BIO_MD)
        bio_mod.update_bio("Background", "New narrative.", USER)
        assert "moved to Bangalore" not in stored(fake_r2)

    def test_replaces_only_the_named_section(self, fake_r2):
        fake_r2.seed(KEY, BIO_MD)
        bio_mod.update_bio("Background", "New narrative.", USER)
        body = stored(fake_r2)
        assert "Started college" in body       # Journey untouched

    def test_section_count_is_stable_after_update(self, fake_r2):
        fake_r2.seed(KEY, BIO_MD)
        bio_mod.update_bio("Background", "New narrative.", USER)
        assert stored(fake_r2).count("## ") == 2

    def test_repeated_updates_do_not_duplicate(self, fake_r2):
        fake_r2.seed(KEY, BIO_MD)
        for _ in range(3):
            bio_mod.update_bio("Background", "same", USER)
        assert stored(fake_r2).count("## Background") == 1


class TestSectionNameNormalisation:
    @pytest.mark.parametrize("name", ["Journey", "## Journey", "# Journey", "  ## Journey  "])
    def test_header_prefixes_are_accepted(self, fake_r2, name):
        bio_mod.update_bio(name, "content", USER)
        assert stored(fake_r2).count("## Journey") == 1

    def test_prefixed_name_finds_existing_section(self, fake_r2):
        fake_r2.seed(KEY, BIO_MD)
        out = bio_mod.update_bio("## Journey", "2024: New thing", USER)
        assert "Updated" in out

    def test_case_is_significant(self, fake_r2):
        fake_r2.seed(KEY, BIO_MD)
        bio_mod.update_bio("journey", "x", USER)
        assert stored(fake_r2).count("## Journey") == 1

    @pytest.mark.parametrize("bad", ["", "   ", "##", "#", "  #  "])
    def test_empty_section_name_rejected(self, fake_r2, bad):
        out = bio_mod.update_bio(bad, "content", USER)
        assert "Error" in out
        assert fake_r2.puts == []


class TestMultilineContent:
    def test_preserves_internal_line_breaks(self, fake_r2):
        bio_mod.update_bio("Journey", "2019: A\n2020: B\n2021: C", USER)
        body = stored(fake_r2)
        assert "2019: A" in body and "2021: C" in body

    def test_content_containing_hashes_is_preserved(self, fake_r2):
        # a '#' in prose must not be mistaken for a new section header
        bio_mod.update_bio("Background", "Loves C# and C++", USER)
        assert "Loves C# and C++" in stored(fake_r2)

    def test_leading_blank_lines_are_trimmed(self, fake_r2):
        bio_mod.update_bio("Background", "\n\n\nreal content\n\n\n", USER)
        assert "real content" in stored(fake_r2)


class TestErrors:
    def test_traversal_username_rejected(self, fake_r2):
        out = bio_mod.get_bio({"username": "../evil"})
        assert "error" in json.loads(out)
        assert fake_r2.puts == []

    def test_r2_read_failure_on_update(self, fake_r2, monkeypatch):
        def boom(Bucket, Key):
            raise Exception("AccessDenied")
        monkeypatch.setattr(fake_r2, "get_object", boom)
        assert "Error" in bio_mod.update_bio("Background", "x", USER)

    def test_write_failure_is_reported(self, fake_r2, monkeypatch):
        def boom(**kw):
            raise Exception("network down")
        monkeypatch.setattr(fake_r2, "put_object", boom)
        assert "Error" in bio_mod.update_bio("Background", "x", USER)


class TestUserIsolation:
    def test_updates_do_not_cross_users(self, fake_r2):
        fake_r2.seed("users/alice/bio.md", "## Background\nAlice's story\n")
        bio_mod.update_bio("Background", "Aarav's story", USER)
        assert "Alice's story" in fake_r2.store["users/alice/bio.md"].decode()

    def test_missing_user_sees_no_other_bio(self, fake_r2):
        fake_r2.seed("users/joydip/bio.md", "SECRET STORY\n")
        out = json.dumps(json.loads(bio_mod.get_bio({"username": "carol"})))
        assert "SECRET" not in out
