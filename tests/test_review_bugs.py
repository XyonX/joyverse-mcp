"""Regression tests for the bugs found in review.

Grouped by the tool they belong to. Each test names the behaviour that used
to be wrong.
"""
import json

import pytest

from conftest import TEST_USER, TEST_USER_ID
from joyverse import data as data_mod
from joyverse import logs, storage
from joyverse import memory as memory_mod
from joyverse import profile as prof

PROFILE_KEY = f"users/{TEST_USER_ID}/profile.md"


def profile_text(fake_r2) -> str:
    return fake_r2.store[PROFILE_KEY].decode()


# ==========================================
# PROFILE
# ==========================================

class TestProfileFields:
    def test_hashed_section_name_is_not_doubled(self, fake_r2):
        prof.update_profile("## Identity", "name: Joy", TEST_USER)
        text = profile_text(fake_r2)
        assert "## ## Identity" not in text
        assert text.count("## Identity") == 1

    def test_hashed_and_bare_names_are_one_section(self, fake_r2):
        prof.update_profile("Skills", "Go", TEST_USER)
        prof.update_profile("## Skills", "Rust", TEST_USER)
        text = profile_text(fake_r2)
        assert text.count("## Skills") == 1
        assert "Rust" in text and "Go" not in text

    def test_section_match_ignores_case(self, fake_r2):
        prof.update_profile("Skills", "Go", TEST_USER)
        prof.update_profile("#skills", "Rust", TEST_USER)
        assert profile_text(fake_r2).lower().count("## skills") == 1

    @pytest.mark.parametrize("field", ["Name", "Age", "Location"])
    def test_capitalised_fact_is_a_key_not_a_section(self, fake_r2, field):
        prof.update_profile(field, "value", TEST_USER)
        text = profile_text(fake_r2)
        assert f"## {field}" not in text
        assert f"{field.lower()}: value" in text

    def test_capitalised_name_updates_the_existing_key(self, fake_r2):
        prof.update_profile("hobby", "chess", TEST_USER)
        prof.update_profile("Hobby", "go", TEST_USER)
        text = profile_text(fake_r2)
        assert "hobby: go" in text
        assert "## Hobby" not in text and "chess" not in text

    def test_other_capitalised_words_are_still_sections(self, fake_r2):
        prof.update_profile("Background", "Grew up in Kolkata.", TEST_USER)
        assert "## Background" in profile_text(fake_r2)

    def test_new_fact_is_filed_under_identity(self, fake_r2):
        prof.update_profile("Identity", "name: Joy", TEST_USER)
        prof.update_profile("Background", "A paragraph of story.", TEST_USER)
        prof.update_profile("age", "25", TEST_USER)

        text = profile_text(fake_r2)
        assert text.index("age: 25") < text.index("## Background"), \
            "the fact landed under the narrative section"
        assert text.index("name: Joy") < text.index("age: 25")

    def test_number_value_is_accepted(self, fake_r2):
        prof.update_profile("age", 25, TEST_USER)
        assert "age: 25" in profile_text(fake_r2)


# ==========================================
# DATA TOPICS
# ==========================================

class TestTopicListing:
    def test_description_comes_from_the_live_log_not_the_snapshot(self,
                                                                 fake_r2):
        data_mod.replace_data("dsa", json.dumps({"summary": "OLD"}),
                              user=TEST_USER)
        data_mod.replace_data("dsa", json.dumps({"summary": "NEW"}),
                              user=TEST_USER)

        topics = json.loads(data_mod.list_topics(TEST_USER))["topics"]
        assert [t["description"] for t in topics] == ["NEW"]

    def test_resource_listing_uses_the_live_log_too(self, fake_r2):
        import run

        data_mod.replace_data("dsa", json.dumps({"summary": "OLD"}),
                              user=TEST_USER)
        data_mod.replace_data("dsa", json.dumps({"summary": "NEW"}),
                              user=TEST_USER)
        listed = run._live_data_topics(TEST_USER)
        assert [r["description"] for r in listed] == ["NEW"]


class TestEditData:
    def test_misspelled_topic_is_not_created(self, fake_r2):
        data_mod.edit_data("dsa", "set", value={"solved": 1}, user=TEST_USER)
        out = json.loads(data_mod.edit_data("dsaa", "set", value={"x": 1},
                                            user=TEST_USER))
        assert out["did_you_mean"] == ["dsa"]
        assert json.loads(data_mod.list_topics(TEST_USER))["count"] == 1

    def test_unrelated_new_topic_is_still_created(self, fake_r2):
        data_mod.edit_data("dsa", "set", value={"solved": 1}, user=TEST_USER)
        out = json.loads(data_mod.edit_data("reading", "set",
                                            value={"books": []},
                                            user=TEST_USER))
        assert out["ok"] is True

    def test_array_topic_gets_a_clear_error(self, fake_r2):
        data_mod.replace_data("arr", "[1, 2, 3]", user=TEST_USER)
        out = json.loads(data_mod.edit_data("arr", "set", value={"x": 1},
                                            user=TEST_USER))
        assert "not an object" in out["error"]

    def test_corrupt_topic_gets_a_clear_error(self, fake_r2):
        fake_r2.seed(f"users/{TEST_USER_ID}/data/bad/progress.json", "{oops")
        out = json.loads(data_mod.edit_data("bad", "set", value={"x": 1},
                                            user=TEST_USER))
        assert "not valid JSON" in out["error"]

    def test_match_with_a_scalar_value_is_explained(self, fake_r2):
        data_mod.edit_data("p", "set", value={"items": [{"name": "A"}]},
                           user=TEST_USER)
        out = json.loads(data_mod.edit_data(
            "p", "set", path=["items"], match={"name": "A"}, value="oops",
            user=TEST_USER))
        assert "must be an object" in out["error"]


class TestRestoreData:
    def test_undoes_an_edit(self, fake_r2):
        data_mod.edit_data("notes", "set", value={"a": 1}, user=TEST_USER)
        data_mod.edit_data("notes", "set", value={"a": 2}, user=TEST_USER)

        out = json.loads(data_mod.restore_data("notes", user=TEST_USER))
        assert out["ok"] is True
        assert json.loads(data_mod.get_data("notes", TEST_USER)) == {"a": 1}

    def test_undoes_a_replace(self, fake_r2):
        data_mod.replace_data("notes", json.dumps({"keep": 1, "also": 2}),
                              user=TEST_USER)
        data_mod.replace_data("notes", json.dumps({"keep": 1}),
                              allow_drop=True, user=TEST_USER)
        data_mod.restore_data("notes", user=TEST_USER)
        assert json.loads(data_mod.get_data("notes", TEST_USER)) == {
            "keep": 1, "also": 2}

    def test_second_restore_redoes_the_change(self, fake_r2):
        data_mod.edit_data("notes", "set", value={"a": 1}, user=TEST_USER)
        data_mod.edit_data("notes", "set", value={"a": 2}, user=TEST_USER)
        data_mod.restore_data("notes", user=TEST_USER)
        data_mod.restore_data("notes", user=TEST_USER)
        assert json.loads(data_mod.get_data("notes", TEST_USER)) == {"a": 2}

    def test_nothing_to_restore_is_an_error(self, fake_r2):
        data_mod.replace_data("notes", "{}", user=TEST_USER)
        out = json.loads(data_mod.restore_data("notes", user=TEST_USER))
        assert "no earlier version" in out["error"]

    def test_unknown_topic_lists_what_exists(self, fake_r2):
        data_mod.replace_data("notes", "{}", user=TEST_USER)
        out = json.loads(data_mod.restore_data("nope", user=TEST_USER))
        assert out["available_topics"] == ["notes"]


# ==========================================
# MEMORY
# ==========================================

class TestMemory:
    def read(self):
        return json.loads(memory_mod.get_memory(TEST_USER))

    def test_duplicate_trait_is_not_stored_twice(self, fake_r2):
        memory_mod.add_memory_trait("curious", TEST_USER)
        out = memory_mod.add_memory_trait("  Curious ", TEST_USER)
        assert "Already recorded" in out
        assert self.read()["personality"] == ["curious"]

    def test_dropped_trait_is_reported(self, fake_r2):
        for i in range(20):
            memory_mod.add_memory_trait(f"trait {i}", TEST_USER)
        out = memory_mod.add_memory_trait("one more", TEST_USER)
        assert "trait 0" in out and "dropped" in out

    def test_empty_trait_is_refused(self, fake_r2):
        assert memory_mod.add_memory_trait("   ", TEST_USER).startswith(
            "Error")

    def test_observed_patterns_can_be_written(self, fake_r2):
        memory_mod.add_observed_pattern("asks why before how", TEST_USER)
        assert self.read()["observed_patterns"] == ["asks why before how"]

    def test_preferences_can_be_written(self, fake_r2):
        memory_mod.update_preference("code_style", "terse", TEST_USER)
        assert self.read()["preferences"]["code_style"] == "terse"

    def test_unknown_preference_is_refused(self, fake_r2):
        out = memory_mod.update_preference("colour", "blue", TEST_USER)
        assert out.startswith("Error") and "code_style" in out

    @pytest.mark.parametrize("field", ["main_focus", "immediate_next", "mood"])
    def test_every_context_field_can_be_written(self, fake_r2, field):
        memory_mod.update_context(field, "value", TEST_USER)
        assert self.read()["current_context"][field] == "value"

    def test_unknown_context_field_is_refused(self, fake_r2):
        assert memory_mod.update_context("weather", "x", TEST_USER).startswith(
            "Error")

    def test_memory_missing_parts_of_the_schema_still_works(self, fake_r2):
        fake_r2.seed(f"users/{TEST_USER_ID}/memory.json",
                     json.dumps({"personality": ["calm"]}))
        memory_mod.update_focus("ship it", TEST_USER)
        memory_mod.add_observed_pattern("works late", TEST_USER)

        mem = self.read()
        assert mem["personality"] == ["calm"]
        assert mem["current_context"]["main_focus"] == "ship it"
        assert mem["observed_patterns"] == ["works late"]


# ==========================================
# PROTOCOL
# ==========================================

@pytest.fixture
def mcp(client_factory):
    client, srv = client_factory()

    @srv.tool(description="echo")
    def echo(text: str, times: int = 1) -> str:
        return text * times

    def post(body):
        return client.post("/mcp", json=body)

    def frames(resp):
        return [json.loads(line[6:]) for line in resp.text.splitlines()
                if line.startswith("data: ")]

    post.frames = frames
    return post


def call(mcp, arguments, req_id=1):
    resp = mcp({"jsonrpc": "2.0", "id": req_id, "method": "tools/call",
                "params": {"name": "echo", "arguments": arguments}})
    return mcp.frames(resp)[0]["result"]


class TestProtocol:
    def test_batch_is_answered_per_request(self, mcp):
        resp = mcp([
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ])
        assert resp.status_code == 200
        assert [f["id"] for f in mcp.frames(resp)] == [1, 2]

    @pytest.mark.parametrize("body", [[], "text", 5, [1, 2]])
    def test_non_request_body_is_a_400_not_a_500(self, mcp, body):
        resp = mcp(body)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == -32600

    @pytest.mark.parametrize("method", [
        "notifications/initialized", "notifications/cancelled",
        "notifications/progress"])
    def test_notification_gets_202_and_no_body(self, mcp, method):
        resp = mcp({"jsonrpc": "2.0", "method": method})
        assert resp.status_code == 202 and resp.text == ""

    def test_ping_returns_an_empty_result(self, mcp):
        resp = mcp({"jsonrpc": "2.0", "id": 7, "method": "ping"})
        assert mcp.frames(resp) == [{"jsonrpc": "2.0", "id": 7, "result": {}}]

    @pytest.mark.parametrize("asked, got", [
        ("2025-06-18", "2025-06-18"),
        ("2025-03-26", "2025-03-26"),
        ("1999-01-01", "2025-03-26"),
        (None, "2025-03-26"),
    ])
    def test_protocol_version_is_negotiated(self, mcp, asked, got):
        params = {"protocolVersion": asked} if asked else {}
        resp = mcp({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": params})
        assert mcp.frames(resp)[0]["result"]["protocolVersion"] == got

    def test_text_result_has_no_null_fields(self, mcp):
        block = call(mcp, {"text": "hi"})["content"][0]
        assert block == {"type": "text", "text": "hi"}

    def test_missing_argument_is_named(self, mcp):
        result = call(mcp, {})
        assert result["isError"] is True
        assert "Missing required argument(s): text" in \
            result["content"][0]["text"]

    def test_unknown_argument_is_named(self, mcp):
        result = call(mcp, {"text": "hi", "txet": "typo"})
        assert result["isError"] is True
        text = result["content"][0]["text"]
        assert "Unknown argument(s): txet" in text and "times" in text

    def test_number_for_a_string_argument_is_converted(self, mcp):
        assert call(mcp, {"text": 5})["content"][0]["text"] == "5"

    def test_non_object_arguments_are_refused(self, mcp):
        result = call(mcp, ["hi"])
        assert result["isError"] is True

    def test_scope_refusal_is_not_called_a_server_error(self, client_factory,
                                                        sse_rpc):
        from fastapi import Request

        def no_scopes(request: Request):
            return {"scopes": []}

        client, srv = client_factory(auth=no_scopes)

        @srv.tool(description="guarded", scopes=["write"])
        def guarded() -> str:
            return "never"

        text = sse_rpc(client, "tools/call",
                       {"name": "guarded"})["result"]["content"][0]["text"]
        assert text.startswith("Insufficient scope")


# ==========================================
# FILES
# ==========================================

class TestFiles:
    def test_deleting_a_missing_file_says_so(self, fake_r2):
        out = json.loads(storage.delete_file("nope.txt", client="claude",
                                             user=TEST_USER))
        assert out["ok"] is True
        assert out["already_absent"] is True and out["deleted"] is None

    def test_deleting_a_real_file_reports_it(self, fake_r2):
        storage.save_file_text("a.txt", "x", client="claude", user=TEST_USER)
        out = json.loads(storage.delete_file("a.txt", client="claude",
                                             user=TEST_USER))
        assert out["deleted"] == "a.txt" and "already_absent" not in out

    def test_storage_outage_is_not_reported_as_a_missing_file(self, fake_r2,
                                                              monkeypatch):
        def down(**kw):
            raise RuntimeError("connection reset")

        monkeypatch.setattr(fake_r2, "head_object", down)
        got = json.loads(storage.get_file("a.txt", client="claude",
                                          user=TEST_USER))
        assert "Could not reach storage" in got["error"]
        gone = json.loads(storage.delete_file("a.txt", client="claude",
                                              user=TEST_USER))
        assert "Could not reach storage" in gone["error"]

    def test_listing_takes_a_single_pass(self, fake_r2):
        for name in ("claude", "chatgpt"):
            storage.register_client(name, user=TEST_USER)
            storage.save_file_text("a.txt", "hello", client=name,
                                   user=TEST_USER)
        calls = []
        original = fake_r2.list_objects_v2
        fake_r2.list_objects_v2 = \
            lambda **kw: calls.append(kw) or original(**kw)

        out = json.loads(storage.list_files(user=TEST_USER))
        assert len(calls) == 1
        assert set(out["usage"]["by_client_gb"]) <= {"claude", "chatgpt"}
        assert {f["path"] for f in out["files"]} == {
            "claude/a.txt", "chatgpt/a.txt"}
        assert out["usage"]["objects"] == 3        # two files + the registry

    def test_listing_is_capped_and_says_so(self, fake_r2):
        for i in range(5):
            storage.save_file_text(f"f{i}.txt", "x", client="claude",
                                   user=TEST_USER)
        out = json.loads(storage.list_files(client="claude", limit=2,
                                            user=TEST_USER))
        assert out["count"] == 2
        assert out["total_matched"] == 5 and out["has_more"] is True

    def test_prefix_filters_within_a_client(self, fake_r2):
        storage.save_file_text("images/a.txt", "x", client="claude",
                               user=TEST_USER)
        storage.save_file_text("docs/b.txt", "x", client="claude",
                               user=TEST_USER)
        out = json.loads(storage.list_files(prefix="images/", client="claude",
                                            user=TEST_USER))
        assert [f["path"] for f in out["files"]] == ["images/a.txt"]


# ==========================================
# LOGS
# ==========================================

def write_day(fake_r2, day, entries):
    fake_r2.seed(f"users/{TEST_USER_ID}/logs/{day}.jsonl",
                 "".join(json.dumps(e) + "\n" for e in entries))


class TestLogs:
    def test_until_alone_reports_the_window_it_read(self, fake_r2):
        write_day(fake_r2, "2026-10-01",
                  [{"ts": "2026-10-01T10:00:00Z", "summary": "in"}])
        write_day(fake_r2, "2026-09-20",
                  [{"ts": "2026-09-20T10:00:00Z", "summary": "too early"}])

        out = json.loads(logs.get_log(until="2026-10-01", user=TEST_USER))
        assert out["window_start"] == "2026-09-30T23:59:59Z"
        assert [e["summary"] for e in out["entries"]] == ["in"]

    def test_long_range_reads_only_days_that_exist(self, fake_r2):
        write_day(fake_r2, "2026-03-10",
                  [{"ts": "2026-03-10T10:00:00Z", "summary": "spring"}])
        write_day(fake_r2, "2026-08-02",
                  [{"ts": "2026-08-02T10:00:00Z", "summary": "summer"}])
        reads = []
        original = fake_r2.get_object
        fake_r2.get_object = \
            lambda **kw: reads.append(kw["Key"]) or original(**kw)

        out = json.loads(logs.get_log(since="2026-01-01", until="2026-12-31",
                                      user=TEST_USER))
        assert {e["summary"] for e in out["entries"]} == {"spring", "summer"}
        assert out["days_read"] == ["2026-03-10", "2026-08-02"]
        assert len(reads) == 2, f"read {len(reads)} objects for 2 days"

    def test_negative_limit_does_not_slice_from_the_end(self, fake_r2):
        for day in ("2026-10-01", "2026-10-02", "2026-10-03"):
            write_day(fake_r2, day, [{"ts": f"{day}T10:00:00Z",
                                      "summary": "x"}])
        out = json.loads(logs.list_log_days(limit=-1, user=TEST_USER))
        assert [d["date"] for d in out["days"]] == ["2026-10-03"]
