"""Unit tests for joyverse/data.py -- per-topic data log tool logic."""
import json
import pytest
from joyverse import data as dat


USER = {"user_id": "u_a1b2c3d4e5f6"}
KEY = "users/u_a1b2c3d4e5f6/data/dsa/progress.json"


class TestGetData:
    def test_returns_stored_json_verbatim(self, fake_r2):
        fake_r2.seed(KEY, '{"done": 5}')
        assert json.loads(dat.get_data("dsa", USER)) == {"done": 5}

    def test_missing_topic_returns_error_json(self, fake_r2):
        out = json.loads(dat.get_data("dsa", USER))
        assert "error" in out and "dsa" in out["error"]

    def test_r2_failure_returns_error_json(self, fake_r2, monkeypatch):
        def boom(Bucket, Key):
            raise Exception("AccessDenied")
        monkeypatch.setattr(fake_r2, "get_object", boom)
        assert "error" in json.loads(dat.get_data("dsa", USER))

    def test_output_is_always_valid_json(self, fake_r2):
        json.loads(dat.get_data("anything", USER))


class TestUpdateData:
    def test_writes_parsed_json(self, fake_r2):
        out = dat.update_data("dsa", '{"done": 5}', USER)
        assert "Updated" in out
        assert fake_r2.get_json(KEY) == {"done": 5}

    def test_reformats_input_json(self, fake_r2):
        dat.update_data("dsa", '{"b":2,"a":1}', USER)
        assert "\n" in fake_r2.store[KEY].decode()  # indent=2

    def test_writes_json_content_type(self, fake_r2):
        dat.update_data("dsa", '{"a":1}', USER)
        assert fake_r2.puts[0]["ContentType"] == "application/json"

    def test_invalid_json_is_rejected(self, fake_r2):
        out = dat.update_data("dsa", "{not json", USER)
        assert "Invalid JSON" in out
        assert fake_r2.puts == []

    def test_json_array_is_accepted(self, fake_r2):
        dat.update_data("dsa", '[1,2]', USER)
        assert fake_r2.get_json(KEY) == [1, 2]

    def test_write_failure_is_reported(self, fake_r2, monkeypatch):
        def boom(**kw):
            raise Exception("network down")
        monkeypatch.setattr(fake_r2, "put_object", boom)
        assert "Error" in dat.update_data("dsa", '{"a":1}', USER)


class TestTopicTraversalRegression:
    """Regression: topic reached the R2 key unsanitised, so a client could
    read/write outside their own users/<name>/ prefix."""

    def test_traversal_topic_does_not_escape(self, fake_r2):
        fake_r2.seed("users/u_a1b2c3d4e5f6/profile.md", "SECRET")
        out = dat.get_data("../../../victim", USER)
        assert "error" in json.loads(out) or "SECRET" not in out

    def test_traversal_topic_cannot_write_outside(self, fake_r2):
        out = dat.update_data("../../../victim", '{"pwned":1}', USER)
        assert "Error" in out
        assert not any("victim" in p["Key"] for p in fake_r2.puts)

    def test_slash_topic_rejected(self, fake_r2):
        assert "Error" in dat.update_data("a/b", '{"x":1}', USER)

    def test_normal_topic_still_works(self, fake_r2):
        assert "Updated" in dat.update_data("dsa", '{"a":1}', USER)


class TestUserIsolation:
    def test_topics_are_namespaced_per_user(self, fake_r2):
        dat.update_data("dsa", '{"who":"alice"}', {"user_id": "u_0a1b2c3d4e5f"})
        dat.update_data("dsa", '{"who":"bob"}', USER)
        assert fake_r2.get_json("users/u_0a1b2c3d4e5f/data/dsa/progress.json") == {"who": "alice"}
        assert fake_r2.get_json(KEY) == {"who": "bob"}
