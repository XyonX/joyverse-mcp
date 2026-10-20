"""Guards against the silent-corruption path in edit_data.

The published schema once declared `value`, `match` and `path` as strings. An
agent obeying it sent serialised JSON, RFC 7386 merge stored that string where
a list belonged, the call returned ok:true, and the topic was corrupted with no
error anywhere. Two agents hit this before it was noticed.

The load-bearing assertion in every test here is the *stored type*, not the
return value -- the failure mode is precisely that the call succeeds.
"""
import json

import pytest

from joyverse import data

UID = "u_aaaaaaaaaaa1"
USER = {"user_id": UID}

SEED = {"summary": "s", "projects": [{"name": "Old", "status": "active"}]}


@pytest.fixture
def topic(fake_r2):
    """A `builds` topic with one project, and the R2 key holding it."""
    key = data.get_data_key(UID, "builds")
    fake_r2.seed(key, json.dumps(SEED))
    return key


class TestSchemaTellsTheTruth:
    """The schema is what every client reads before it builds a call."""

    @pytest.fixture(scope="class")
    def schema(self):
        import run
        s = [t for t in run.server._tool_schemas if t.name == "edit_data"][0]
        return s.inputSchema["properties"]

    def test_value_is_not_declared_a_string(self, schema):
        """This single wrong declaration caused the corruption."""
        assert schema["value"].get("type") != "string"

    def test_match_is_an_object(self, schema):
        assert schema["match"].get("type") == "object"

    def test_path_is_an_array(self, schema):
        assert schema["path"].get("type") == "array"


class TestNoSilentCorruption:
    def test_a_json_string_stores_a_list_not_a_string(self, fake_r2, topic):
        """The exact failure: ok:true, and the field became a str."""
        data.edit_data("builds", "set", path="projects",
                       value=json.dumps([{"name": "New"}]), user=USER)
        stored = fake_r2.get_json(topic)["projects"]
        assert isinstance(stored, list), f"stored a {type(stored).__name__}"
        assert stored == [{"name": "New"}]

    def test_object_and_string_forms_are_identical(self, fake_r2, topic):
        """Either encoding must produce the same stored structure."""
        payload = [{"name": "New", "status": "active"}]
        data.edit_data("builds", "set", path="projects", value=payload,
                       user=USER)
        via_object = fake_r2.get_json(topic)["projects"]

        fake_r2.seed(topic, json.dumps(SEED))
        data.edit_data("builds", "set", path="projects",
                       value=json.dumps(payload), user=USER)
        via_string = fake_r2.get_json(topic)["projects"]

        assert via_object == via_string

    def test_native_list_is_stored_as_a_list(self, fake_r2, topic):
        data.edit_data("builds", "set", path="projects",
                       value=[{"name": "New"}], user=USER)
        assert isinstance(fake_r2.get_json(topic)["projects"], list)

    def test_truncated_json_is_not_silently_stored_as_a_string(self, fake_r2, topic):
        """A half-written JSON payload must never land verbatim in the topic.

        `{"broken": ` opens like JSON but does not close, so it is ambiguous.
        The contract that matters is the load-bearing one: whatever it is
        treated as, `projects` must not become a truncated JSON string.
        """
        data.edit_data("builds", "set", path="projects",
                       value='{"broken": ', user=USER)
        stored = fake_r2.get_json(topic)["projects"]
        assert not isinstance(stored, str), "truncated JSON stored as a string"
        assert stored == SEED["projects"], "existing list was damaged"


class TestMatchAndAdd:
    def test_match_object_replaces_one_item(self, fake_r2, topic):
        data.edit_data("builds", "set", path="projects",
                       match={"name": "Old"}, value={"status": "finished"},
                       user=USER)
        assert fake_r2.get_json(topic)["projects"] == [
            {"name": "Old", "status": "finished"}]

    def test_match_as_json_string_also_works(self, fake_r2, topic):
        """The old schema forced match to be a string too."""
        data.edit_data("builds", "set", path="projects",
                       match=json.dumps({"name": "Old"}),
                       value={"status": "finished"}, user=USER)
        assert fake_r2.get_json(topic)["projects"][0]["status"] == "finished"

    def test_add_appends_an_object(self, fake_r2, topic):
        data.edit_data("builds", "add", path="projects",
                       value={"name": "Third", "status": "active"}, user=USER)
        names = [p["name"] for p in fake_r2.get_json(topic)["projects"]]
        assert names == ["Old", "Third"]

    def test_add_accepts_a_list(self, fake_r2, topic):
        data.edit_data("builds", "add", path="projects",
                       value=[{"name": "Third"}], user=USER)
        assert len(fake_r2.get_json(topic)["projects"]) == 2


class TestPlainStringsStillWork:
    def test_a_plain_string_is_a_legitimate_value(self, fake_r2, topic):
        """Coercion must not swallow ordinary text."""
        data.edit_data("builds", "set", path="summary", value="hello",
                       user=USER)
        assert fake_r2.get_json(topic)["summary"] == "hello"

    def test_a_name_containing_a_brace_is_not_mistaken_for_json(self, fake_r2, topic):
        data.edit_data("builds", "set", path="summary",
                       value="{not json at all", user=USER)
        assert fake_r2.get_json(topic)["summary"] == "{not json at all"

    def test_numbers_survive(self, fake_r2, topic):
        data.edit_data("builds", "set", path="count", value=7, user=USER)
        assert fake_r2.get_json(topic)["count"] == 7
