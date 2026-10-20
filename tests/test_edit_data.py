"""Tests for joyverse/data.py edit_data and replace_data.

edit_data exists so a one-field change does not require re-sending a whole
log. replace_data still does that, but is now named for what it does and
refuses to silently drop keys.
"""
import json

import pytest

from conftest import TEST_USER_ID as UID, TEST_USER_ID_2 as UID2
from joyverse import data as dat

U = {"user_id": UID}
OTHER = {"user_id": UID2}

BUILDS = {
    "summary": "current builds",
    "last_updated": "2026-10-02",
    "projects": [
        {"name": "flexygent", "status": "active",
         "details": ["a"], "planned": ["FastAPI"]},
        {"name": "OmniHome", "status": "active", "description": "smart home"},
        {"name": "SandboxCore", "status": "planned", "future_work": ["pybind"]},
    ],
    "infrastructure": {"oci": "two VMs"},
    "note": "old note",
}


@pytest.fixture
def seeded(fake_r2):
    fake_r2.seed(f"users/{UID}/data/builds/progress.json",
                 json.dumps(BUILDS, indent=2))
    return fake_r2


def load(fake_r2):
    return json.loads(fake_r2.get_object(
        Bucket="test-bucket", Key=f"users/{UID}/data/builds/progress.json"
    )["Body"].read().decode())


class TestEditSet:
    def test_updates_a_field_in_a_named_item(self, seeded):
        out = dat.edit_data("builds", "set", {"status": "finished"},
                            path="projects", match={"name": "OmniHome"}, user=U)
        assert json.loads(out)["ok"]
        items = {p["name"]: p for p in load(seeded)["projects"]}
        assert items["OmniHome"]["status"] == "finished"

    def test_leaves_other_items_untouched(self, seeded):
        dat.edit_data("builds", "set", {"status": "x"}, path="projects",
                      match={"name": "OmniHome"}, user=U)
        items = {p["name"]: p for p in load(seeded)["projects"]}
        assert items["flexygent"]["status"] == "active"
        assert items["SandboxCore"]["status"] == "planned"

    def test_leaves_sibling_fields_untouched(self, seeded):
        dat.edit_data("builds", "set", {"status": "finished"},
                      path="projects", match={"name": "OmniHome"}, user=U)
        item = next(p for p in load(seeded)["projects"]
                    if p["name"] == "OmniHome")
        assert item["description"] == "smart home"

    def test_top_level_without_path(self, seeded):
        dat.edit_data("builds", "set", {"summary": "new"}, user=U)
        assert load(seeded)["summary"] == "new"

    def test_top_level_merges_rather_than_replaces(self, seeded):
        dat.edit_data("builds", "set", {"summary": "new"}, user=U)
        d = load(seeded)
        assert "projects" in d and "infrastructure" in d

    def test_nested_object_merge(self, seeded):
        dat.edit_data("builds", "set", {"oci": "three VMs"},
                      path="infrastructure", user=U)
        assert load(seeded)["infrastructure"]["oci"] == "three VMs"

    def test_null_deletes_a_top_level_key(self, seeded):
        dat.edit_data("builds", "set", {"note": None}, user=U)
        assert "note" not in load(seeded)

    def test_unknown_match_lists_the_available_names(self, seeded):
        out = json.loads(dat.edit_data("builds", "set", {"status": "x"},
                                       path="projects",
                                       match={"name": "Nope"}, user=U))
        assert "error" in out
        assert "OmniHome" in out["available"]

    def test_match_on_a_non_list_is_rejected(self, seeded):
        out = json.loads(dat.edit_data("builds", "set", {"a": 1},
                                       path="infrastructure",
                                       match={"name": "x"}, user=U))
        assert "error" in out


class TestAddressingIsByNameNotIndex:
    """The OmniHome trap: indices move when items are removed."""

    def test_index_would_be_wrong_after_a_removal(self, seeded):
        omni_before = [p["name"] for p in load(seeded)["projects"]].index("OmniHome")
        dat.edit_data("builds", "remove", None, path="projects",
                      match={"name": "flexygent"}, user=U)
        omni_after = [p["name"] for p in load(seeded)["projects"]].index("OmniHome")
        assert omni_before != omni_after, "indices must actually shift here"

    def test_editing_by_name_still_hits_the_right_item_after_a_shift(self, seeded):
        dat.edit_data("builds", "remove", None, path="projects",
                      match={"name": "flexygent"}, user=U)
        dat.edit_data("builds", "set", {"status": "finished"},
                      path="projects", match={"name": "OmniHome"}, user=U)
        names = [p["name"] for p in load(seeded)["projects"]]
        assert names == ["OmniHome", "SandboxCore"]
        assert load(seeded)["projects"][0]["status"] == "finished"


class TestEditAdd:
    def test_adds_an_item(self, seeded):
        dat.edit_data("builds", "add", {"name": "NewThing", "status": "active"},
                      path="projects", user=U)
        names = [p["name"] for p in load(seeded)["projects"]]
        assert names[-1] == "NewThing"

    def test_adding_to_a_non_list_is_rejected(self, seeded):
        out = json.loads(dat.edit_data("builds", "add", {"a": 1},
                                       path="infrastructure", user=U))
        assert "error" in out


class TestEditRemove:
    def test_removes_the_matched_item(self, seeded):
        dat.edit_data("builds", "remove", None, path="projects",
                      match={"name": "OmniHome"}, user=U)
        names = [p["name"] for p in load(seeded)["projects"]]
        assert "OmniHome" not in names

    def test_returns_what_was_removed(self, seeded):
        out = json.loads(dat.edit_data("builds", "remove", None, path="projects",
                                       match={"name": "OmniHome"}, user=U))
        assert out["removed"]["name"] == "OmniHome"

    def test_requires_match(self, seeded):
        out = json.loads(dat.edit_data("builds", "remove", None,
                                       path="projects", user=U))
        assert "error" in out and "match" in out["error"]


class TestEditAppend:
    def test_appends_to_a_list_inside_an_item(self, seeded):
        dat.edit_data("builds", "append", {"planned": ["SwiftUI app"]},
                      path="projects", match={"name": "flexygent"}, user=U)
        item = next(p for p in load(seeded)["projects"]
                    if p["name"] == "flexygent")
        assert item["planned"] == ["FastAPI", "SwiftUI app"]

    def test_appends_to_a_top_level_list(self, seeded):
        dat.edit_data("builds", "append", ["a new venture"],
                      path="planned_ventures", user=U)
        # planned_ventures did not exist; it is created as a list
        assert load(seeded)["planned_ventures"] == ["a new venture"]

    def test_append_to_a_scalar_is_rejected(self, seeded):
        # summary is a string, not a list -- a genuine mistake, not a
        # missing key to create.
        out = json.loads(dat.edit_data("builds", "append", ["x"],
                                       path="summary", user=U))
        assert "not a list" in out["error"]

    def test_append_to_a_missing_key_creates_the_list(self, seeded):
        out = json.loads(dat.edit_data("builds", "append", ["a venture"],
                                       path="planned_ventures", user=U))
        assert out["ok"]
        assert load(seeded)["planned_ventures"] == ["a venture"]


class TestReplaceData:
    def test_replaces_whole_log_when_allowed(self, seeded):
        # allow_drop is required here precisely because this write WOULD drop
        # keys -- that is the guard working, not a limitation.
        dat.replace_data("builds", json.dumps({"summary": "only this"}),
                         allow_drop=True, user=U)
        assert load(seeded) == {"summary": "only this"}

    def test_refuses_a_write_that_drops_keys(self, seeded):
        out = json.loads(dat.replace_data(
            "builds", json.dumps({"summary": "x"}), user=U))
        assert "would_drop" in out
        assert "drop existing keys" in out["error"]
        assert set(out["would_drop"]) == {"projects", "infrastructure",
                                         "note", "last_updated"}

    def test_refusal_leaves_the_file_untouched(self, seeded):
        before = json.dumps(load(seeded), sort_keys=True)
        dat.replace_data("builds", json.dumps({"summary": "x"}), user=U)
        assert json.dumps(load(seeded), sort_keys=True) == before

    def test_allow_drop_overrides(self, seeded):
        dat.replace_data("builds", json.dumps({"summary": "x"}),
                         allow_drop=True, user=U)
        assert load(seeded) == {"summary": "x"}

    def test_complete_write_is_allowed_without_flag(self, seeded):
        full = dict(BUILDS)
        full["summary"] = "updated"
        out = dat.replace_data("builds", json.dumps(full), user=U)
        assert "Replaced" in out
        assert load(seeded)["summary"] == "updated"

    def test_first_write_to_a_new_topic_is_allowed(self, fake_r2):
        out = dat.replace_data("brandnew", json.dumps({"summary": "s"}), user=U)
        assert "Replaced" in out

    def test_invalid_json_is_rejected(self, seeded):
        assert "Invalid JSON" in dat.replace_data("builds", "not json", user=U)

    def test_snapshots_the_previous_version(self, seeded):
        before = load(seeded)
        dat.replace_data("builds", json.dumps({"summary": "x"}),
                         allow_drop=True, user=U)
        snap = seeded.get_object(
            Bucket="test-bucket",
            Key=f"users/{UID}/data/builds/.previous.json")["Body"].read().decode()
        assert json.loads(snap) == before


class TestSnapshotIsInvisible:
    def test_list_topics_ignores_it(self, seeded):
        dat.replace_data("builds", json.dumps({"summary": "x"}),
                         allow_drop=True, user=U)
        topics = [t["topic"] for t in json.loads(dat.list_topics(U))["topics"]]
        assert ".previous" not in topics

    def test_snapshot_is_not_readable_as_a_topic(self, seeded):
        out = json.loads(dat.get_data(".previous", U))
        assert "error" in out or "available_topics" in out


class TestIsolation:
    def test_edit_does_not_leak_across_users(self, fake_r2):
        fake_r2.seed(f"users/{UID}/data/builds/progress.json",
                     json.dumps(BUILDS))
        out = dat.edit_data("builds", "set", {"status": "hacked"},
                            path="projects", match={"name": "OmniHome"},
                            user=OTHER)
        assert "error" in json.loads(out), "other user has no such topic"

    def test_replace_does_not_leak_across_users(self, fake_r2):
        dat.replace_data("builds", json.dumps({"summary": "hacked"}), user=OTHER)
        assert not any(k.startswith(f"users/{OTHER}/data/builds")
                       for k in fake_r2.store)


class TestBadInput:
    def test_unknown_op_is_rejected(self, seeded):
        out = json.loads(dat.edit_data("builds", "explode", {}, user=U))
        assert "error" in out

    def test_topic_traversal_is_rejected(self, seeded):
        out = json.loads(dat.edit_data("../../etc", "set", {}, user=U))
        assert "error" in out
