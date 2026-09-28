"""Unit tests for joyverse/memory.py -- memory read/update tool logic."""
import copy
import json
import pytest
from joyverse import memory as mem


ALICE = {"username": "alice"}
BOB = {"username": "bob"}


class TestGetMemory:
    def test_creates_default_on_first_read(self, fake_r2):
        out = mem.get_memory(ALICE)
        data = json.loads(out)
        assert data["personality"] == []
        assert "preferences" in data and "current_context" in data

    def test_first_read_persists_the_default(self, fake_r2):
        mem.get_memory(ALICE)
        assert "users/alice/memory.json" in fake_r2.store

    def test_returns_stored_memory(self, fake_r2):
        fake_r2.seed("users/alice/memory.json", json.dumps(
            {"personality": ["curious"], "observed_patterns": [],
             "preferences": {}, "current_context": {}, "last_updated": "2026-01-01"}))
        assert json.loads(mem.get_memory(ALICE))["personality"] == ["curious"]

    def test_last_updated_is_stamped(self, fake_r2):
        mem.get_memory(ALICE)
        saved = fake_r2.get_json("users/alice/memory.json")
        assert len(saved["last_updated"]) == 10  # YYYY-MM-DD


class TestAddMemoryTrait:
    def test_appends_trait(self, fake_r2):
        mem.add_memory_trait("curious", ALICE)
        assert json.loads(mem.get_memory(ALICE))["personality"] == ["curious"]

    def test_multiple_traits_accumulate(self, fake_r2):
        mem.add_memory_trait("a", ALICE)
        mem.add_memory_trait("b", ALICE)
        assert json.loads(mem.get_memory(ALICE))["personality"] == ["a", "b"]

    def test_personality_is_capped_at_twenty(self, fake_r2):
        for i in range(25):
            mem.add_memory_trait(f"t{i}", ALICE)
        traits = json.loads(mem.get_memory(ALICE))["personality"]
        assert len(traits) == 20

    def test_cap_drops_the_oldest(self, fake_r2):
        for i in range(21):
            mem.add_memory_trait(f"t{i}", ALICE)
        traits = json.loads(mem.get_memory(ALICE))["personality"]
        assert traits[-1] == "t20"
        assert "t0" not in traits

    def test_stored_as_pretty_json(self, fake_r2):
        mem.add_memory_trait("curious", ALICE)
        raw = fake_r2.store["users/alice/memory.json"].decode()
        assert "\n" in raw  # indent=2
        assert fake_r2.puts[0]["ContentType"] == "application/json"


class TestUpdateFocus:
    def test_sets_main_focus(self, fake_r2):
        mem.update_focus("ship the MVP", ALICE)
        assert json.loads(mem.get_memory(ALICE))[
            "current_context"]["main_focus"] == "ship the MVP"

    def test_overwrites_previous_focus(self, fake_r2):
        mem.update_focus("first", ALICE)
        mem.update_focus("second", ALICE)
        assert json.loads(mem.get_memory(ALICE))[
            "current_context"]["main_focus"] == "second"


class TestCrossUserLeakRegression:
    """Regression: DEFAULT_MEMORY is module-level and was returned by
    reference, so one user's in-place mutations leaked into every other
    user's memory for the life of the process."""

    def test_default_memory_is_not_mutated_by_a_user(self, fake_r2):
        pristine = copy.deepcopy(mem.DEFAULT_MEMORY)
        mem.add_memory_trait("alice-secret", ALICE)
        assert mem.DEFAULT_MEMORY == pristine

    def test_traits_do_not_leak_between_users(self, fake_r2):
        mem.add_memory_trait("alice-secret", ALICE)
        bob_traits = json.loads(mem.get_memory(BOB))["personality"]
        assert bob_traits == []

    def test_focus_does_not_leak_between_users(self, fake_r2):
        mem.update_focus("alice-focus", ALICE)
        bob_ctx = json.loads(mem.get_memory(BOB))["current_context"]
        assert bob_ctx["main_focus"] == ""

    def test_module_default_survives_a_full_test_session(self, fake_r2):
        pristine = copy.deepcopy(mem.DEFAULT_MEMORY)
        for u in (ALICE, BOB):
            mem.add_memory_trait("x", u)
            mem.update_focus("y", u)
        assert mem.DEFAULT_MEMORY == pristine


class TestErrorPropagation:
    def test_non_nosuchkey_error_propagates(self, fake_r2, monkeypatch):
        def boom(Bucket, Key):
            raise Exception("AccessDenied: nope")
        monkeypatch.setattr(fake_r2, "get_object", boom)
        with pytest.raises(Exception):
            mem.get_memory(ALICE)
