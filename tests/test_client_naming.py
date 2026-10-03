"""Guards against duplicate client registrations.

The failure this came from: an agent registered `chatgpt`, hit "already
registered", and the error told it to choose a different name -- so it created
`nextbiz-studio-agent` instead. Instructions also claimed only 9 tools existed
while file tools were undocumented, so the agent had no naming convention to
follow and guessed one.
"""
import json
import re

import pytest

from joyverse import storage
from joyverse.prompts import USER_DATA

ALICE = {"user_id": "u_alice"}


class TestDuplicateHint:
    def test_hint_does_not_tell_the_agent_to_rename(self, fake_r2):
        """The old hint said "choose a different name" -- that caused the bug."""
        storage.register_client("chatgpt", user=ALICE)
        out = json.loads(storage.register_client("chatgpt", user=ALICE))
        hint = out["hint"].lower()
        assert "different name" not in hint
        assert "do not register a new name" in hint
        assert "list_clients" in hint

    def test_hint_names_the_existing_client(self, fake_r2):
        storage.register_client("chatgpt", user=ALICE)
        out = json.loads(storage.register_client("chatgpt", user=ALICE))
        assert "chatgpt" in out["hint"]
        assert out["client_id"]

    def test_no_new_client_is_created_by_a_collision(self, fake_r2):
        """A rejected re-registration must not leave anything behind."""
        storage.register_client("chatgpt", user=ALICE)
        for _ in range(3):
            json.loads(storage.register_client("chatgpt", user=ALICE))
        clients = json.loads(storage.list_clients(ALICE))["clients"]
        assert len(clients) == 1


class TestPlatformFolding:
    def test_platform_is_case_folded(self, fake_r2):
        """It had drifted into 'chatgpt', 'ChatGPT' and 'claude.ai'."""
        out = json.loads(storage.register_client(
            "chatgpt", platform="ChatGPT", user=ALICE))
        assert out["platform"] == "chatgpt"
        out2 = json.loads(storage.register_client(
            "claude", platform="  Claude.AI  ", user=ALICE))
        assert out2["platform"] == "claude.ai"

    def test_blank_platform_is_none(self, fake_r2):
        out = json.loads(storage.register_client(
            "hermes", platform="   ", user=ALICE))
        assert out["platform"] is None


class TestInstructionsAreAccurate:
    """The root cause: instructions that never mentioned the file tools."""

    @pytest.fixture(scope="class")
    def registered(self):
        import run
        return {s.name for s in run.server._tool_schemas}

    def test_every_registered_tool_is_documented(self, registered):
        missing = sorted(n for n in registered
                         if f"`{n}`" not in USER_DATA)
        assert not missing, f"undocumented tools: {missing}"

    def test_tool_count_in_instructions_is_correct(self, registered):
        """It claimed 9 tools while 19 were registered."""
        claimed = re.findall(r"Only the (\d+) above exist", USER_DATA)
        assert claimed, "the tool-count rule disappeared"
        assert int(claimed[0]) == len(registered), (
            f"instructions say {claimed[0]} tools, server has "
            f"{len(registered)}")

    def test_instructions_warn_against_inventing_clients(self):
        low = USER_DATA.lower()
        assert "do not register a second one" in low
        assert "never invent a new name" in low
        assert "list_clients" in low

    def test_instructions_give_the_naming_convention(self):
        for expected in ("chatgpt", "claude", "hermes"):
            assert expected in USER_DATA

    def test_instructions_point_at_the_right_ingest_tool(self):
        low = USER_DATA.lower()
        assert "save_file_base64" in low
        assert "save_file_from_url" in low
        assert "8 mb" in low

    def test_stale_no_file_paths_rule_is_scoped_to_data_tools(self):
        """It used to say 'do not write file paths', which file tools require."""
        idx = USER_DATA.find("do not try to write file paths")
        assert idx != -1
        # The scoping clause sits just ABOVE the sentence, so search backwards.
        # Unscoped, this reads as a ban on paths and contradicts the file tools.
        before = USER_DATA[max(0, idx - 160):idx]
        for tool in ("get_data", "edit_data", "replace_data"):
            assert tool in before, f"{tool} not named as the scope"
