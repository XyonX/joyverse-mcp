"""Search, delete tools, whoami, timezones, document history, annotations."""
import json

import pytest

from conftest import TEST_USER, TEST_USER_ID
from joyverse import account, bio, history, logs, timezones
from joyverse import data as data_mod
from joyverse import memory as memory_mod
from joyverse import profile as prof
from joyverse import search as search_mod
from joyverse import storage

ROOT = f"users/{TEST_USER_ID}"


def write_day(fake_r2, day, entries):
    fake_r2.seed(f"{ROOT}/logs/{day}.jsonl",
                 "".join(json.dumps(e) + "\n" for e in entries))


def find(query, **kw):
    return json.loads(search_mod.search(query, user=TEST_USER, **kw))


@pytest.fixture
def populated(fake_r2):
    write_day(fake_r2, "2026-03-10", [
        {"ts": "2026-03-10T10:00:00Z", "client": "claude",
         "summary": "Practised graph problems: BFS and Dijkstra",
         "tags": ["dsa"]},
        {"ts": "2026-03-10T15:00:00Z", "summary": "Read two chapters"}])
    write_day(fake_r2, "2026-08-02", [
        {"ts": "2026-08-02T09:00:00Z",
         "summary": "Shipped the pricing page for FlexyGrid",
         "tags": ["flexygrid"], "files": ["renders/pricing.png"]}])
    fake_r2.seed(f"{ROOT}/data/projects/active.json", json.dumps({
        "summary": "Active projects",
        "projects": [{"name": "FlexyGrid", "status": "pricing page live"},
                     {"name": "OmniHome", "status": "paused"}]}))
    fake_r2.seed(f"{ROOT}/data/projects/.previous.json",
                 json.dumps({"projects": [{"name": "Ghost pricing"}]}))
    fake_r2.seed(f"{ROOT}/profile.md",
                 "# User Profile\n\n## Identity\nname: Joy\n"
                 "occupation: backend engineer\n")
    fake_r2.seed(f"{ROOT}/bio.md",
                 "## Journey\n\nStarted with graph theory at university.\n")
    fake_r2.seed(f"{ROOT}/memory.json", json.dumps({
        "personality": ["prefers pricing kept simple"],
        "observed_patterns": [], "preferences": {}, "current_context": {}}))
    fake_r2.seed(f"{ROOT}/storage/_clients.json",
                 json.dumps({"clients": {"claude": {"name": "claude"}}}))
    fake_r2.seed(f"{ROOT}/storage/claude/renders/pricing-page.png", b"png")
    fake_r2.seed(f"{ROOT}/storage/claude/journal/2026-08-02.jsonl",
                 json.dumps({"ts": "2026-08-02T09:05:00Z",
                             "summary": "tick: pricing sweep"}) + "\n")
    return fake_r2


# ==========================================
# SEARCH
# ==========================================

class TestSearch:
    def test_finds_a_log_entry_without_a_date(self, populated):
        out = find("dijkstra")
        assert out["count"] == 1
        hit = out["results"][0]
        assert hit["where"] == "log" and hit["date"] == "2026-03-10"
        assert "Dijkstra" in hit["summary"]

    def test_searches_every_store_by_default(self, populated):
        out = find("pricing")
        assert set(out["matches_by_area"]) == {
            "log", "data", "memory", "files"}
        assert "journal" not in out["matches_by_area"]

    def test_journals_only_when_asked(self, populated):
        out = find("pricing", where=["journals"])
        assert [r["where"] for r in out["results"]] == ["journal"]
        assert out["results"][0]["client"] == "claude"
        assert "journal" in find("pricing", where="all")["matches_by_area"]

    def test_every_word_must_match(self, populated):
        out = find("pricing flexygrid", where=["log"])
        assert out["matched"] == "all_words" and out["count"] == 1

    def test_falls_back_to_any_word_and_says_so(self, populated):
        out = find("dijkstra zebra", where=["log"])
        assert out["matched"] == "any_word"
        assert out["count"] == 1 and "note" in out

    def test_tags_and_files_are_searchable(self, populated):
        assert find("dsa", where=["log"])["count"] == 1
        assert find("renders/pricing.png", where=["log"])["count"] == 1

    def test_data_hit_gives_a_path_matching_by_name(self, populated):
        out = find("paused", where=["data"])
        assert out["results"][0]["topic"] == "projects"
        assert out["results"][0]["path"] == "projects[name=OmniHome].status"

    def test_snapshot_of_a_topic_is_not_searched(self, populated):
        assert find("ghost")["count"] == 0

    def test_profile_and_bio_report_their_section(self, populated):
        hit = find("backend", where=["profile"])["results"][0]
        assert hit["section"] == "Identity"
        hit = find("university", where=["bio"])["results"][0]
        assert hit["section"] == "Journey"

    def test_file_names_match_across_separators(self, populated):
        out = find("pricing page", where=["files"])
        assert out["results"][0]["path"] == "renders/pricing-page.png"
        assert out["results"][0]["client"] == "claude"

    def test_journal_files_are_not_listed_as_files(self, populated):
        assert find("journal", where=["files"])["count"] == 0

    def test_since_and_until_narrow_the_log(self, populated):
        assert find("pricing", where=["log"],
                    until="2026-04-01")["count"] == 0
        assert find("pricing", where=["log"],
                    since="2026-04-01")["count"] == 1

    def test_limit_reports_has_more(self, populated):
        out = find("pricing", limit=1)
        assert out["count"] == 1 and out["has_more"]
        assert out["total_matched"] > 1

    def test_newest_log_entry_first(self, fake_r2):
        write_day(fake_r2, "2026-01-01",
                  [{"ts": "2026-01-01T10:00:00Z", "summary": "ran old"}])
        write_day(fake_r2, "2026-02-01",
                  [{"ts": "2026-02-01T10:00:00Z", "summary": "ran new"}])
        out = find("ran")
        assert [r["date"] for r in out["results"]] == [
            "2026-02-01", "2026-01-01"]

    def test_no_match_gives_a_hint(self, populated):
        out = find("xylophone")
        assert out["count"] == 0 and "hint" in out

    @pytest.mark.parametrize("query", ["", "   ", None])
    def test_empty_query_is_refused(self, fake_r2, query):
        assert "error" in json.loads(
            search_mod.search(query, user=TEST_USER))

    def test_unknown_area_lists_the_valid_ones(self, fake_r2):
        out = find("x", where=["emails"])
        assert "error" in out and "log" in out["areas"]

    def test_long_summary_is_cut_around_the_match(self, fake_r2):
        write_day(fake_r2, "2026-01-01", [{
            "ts": "2026-01-01T10:00:00Z",
            "summary": "a " * 400 + "needle " + "b " * 400}])
        text = find("needle")["results"][0]["summary"]
        assert "needle" in text and len(text) < 300

    def test_empty_account_searches_cleanly(self, fake_r2):
        out = find("anything")
        assert out["count"] == 0 and "could_not_search" not in out

    def test_other_users_are_never_searched(self, populated):
        populated.seed("users/u_ffffffffffff/logs/2026-03-10.jsonl",
                       json.dumps({"ts": "2026-03-10T10:00:00Z",
                                   "summary": "someone else's secret"}) + "\n")
        assert find("secret")["count"] == 0


# ==========================================
# DELETE TOOLS
# ==========================================

class TestDeleteData:
    def test_topic_is_removed_and_no_longer_listed(self, populated):
        out = json.loads(data_mod.delete_data("projects", TEST_USER))
        assert out["ok"] and out["deleted"] == "projects"
        topics = json.loads(data_mod.list_topics(TEST_USER))["topics"]
        assert topics == []
        assert "error" in json.loads(data_mod.get_data("projects", TEST_USER))

    def test_restore_data_brings_it_back(self, populated):
        before = populated.store[f"{ROOT}/data/projects/active.json"]
        data_mod.delete_data("projects", TEST_USER)
        assert json.loads(data_mod.restore_data("projects", TEST_USER))["ok"]
        assert populated.store[f"{ROOT}/data/projects/active.json"] == before

    def test_unknown_topic_lists_what_exists(self, populated):
        out = json.loads(data_mod.delete_data("project", TEST_USER))
        assert "error" in out and out["available_topics"] == ["projects"]

    def test_failed_backup_stops_the_delete(self, populated):
        original = populated.put_object

        def refuse(**kw):
            if kw["Key"].endswith(".previous.json"):
                raise RuntimeError("storage down")
            return original(**kw)

        populated.put_object = refuse
        out = json.loads(data_mod.delete_data("projects", TEST_USER))
        assert "error" in out
        assert f"{ROOT}/data/projects/active.json" in populated.store


class TestDeleteProfileField:
    def test_removes_a_key_line(self, populated):
        out = json.loads(prof.delete_profile_field("Occupation", TEST_USER))
        assert out["deleted"] == {"field": "occupation",
                                  "was": "backend engineer"}
        text = populated.store[f"{ROOT}/profile.md"].decode()
        assert "occupation" not in text and "name: Joy" in text

    def test_removes_a_whole_section(self, populated):
        prof.update_profile("Skills", "Go\nRust", TEST_USER)
        out = json.loads(prof.delete_profile_field("## skills", TEST_USER))
        assert out["deleted"]["section"] == "Skills"
        text = populated.store[f"{ROOT}/profile.md"].decode()
        assert "Skills" not in text and "Rust" not in text
        assert "name: Joy" in text

    def test_unknown_name_lists_what_exists(self, populated):
        out = json.loads(prof.delete_profile_field("shoe size", TEST_USER))
        assert "error" in out
        assert "name" in out["fields"] and "Identity" in out["sections"]

    def test_no_profile(self, fake_r2):
        assert "error" in json.loads(
            prof.delete_profile_field("name", TEST_USER))

    def test_can_be_undone(self, populated):
        before = populated.store[f"{ROOT}/profile.md"]
        prof.delete_profile_field("name", TEST_USER)
        history.restore_version("profile", user=TEST_USER)
        assert populated.store[f"{ROOT}/profile.md"] == before


class TestDeleteBioSection:
    def test_removes_heading_and_body(self, populated):
        bio.update_bio("Goals", "Ship things.", TEST_USER)
        out = json.loads(bio.delete_bio_section("journey", TEST_USER))
        assert out["deleted"]["section"] == "Journey"
        text = populated.store[f"{ROOT}/bio.md"].decode()
        assert "university" not in text and "Ship things." in text

    def test_unknown_section_lists_what_exists(self, populated):
        out = json.loads(bio.delete_bio_section("Hobbies", TEST_USER))
        assert out["sections"] == ["Journey"]

    def test_update_matches_section_case_insensitively(self, populated):
        bio.update_bio("journey", "New text.", TEST_USER)
        text = populated.store[f"{ROOT}/bio.md"].decode()
        assert text.count("## ") == 1 and "## Journey" in text


class TestDeleteMemoryItem:
    def test_removes_by_unique_part(self, populated):
        out = json.loads(memory_mod.delete_memory_item(
            "trait", "PRICING", TEST_USER))
        assert out["ok"]
        assert populated.get_json(f"{ROOT}/memory.json")["personality"] == []

    def test_ambiguous_text_removes_nothing(self, fake_r2):
        memory_mod.add_observed_pattern("asks for tests first", TEST_USER)
        memory_mod.add_observed_pattern("asks for diagrams", TEST_USER)
        out = json.loads(memory_mod.delete_memory_item(
            "pattern", "asks for", TEST_USER))
        assert "error" in out and len(out["matches"]) == 2
        stored = fake_r2.get_json(f"{ROOT}/memory.json")
        assert len(stored["observed_patterns"]) == 2

    def test_exact_text_wins_over_a_longer_entry(self, fake_r2):
        memory_mod.add_memory_trait("direct", TEST_USER)
        memory_mod.add_memory_trait("direct but kind", TEST_USER)
        memory_mod.delete_memory_item("trait", "direct", TEST_USER)
        stored = fake_r2.get_json(f"{ROOT}/memory.json")
        assert stored["personality"] == ["direct but kind"]

    def test_no_match_shows_the_list(self, populated):
        out = json.loads(memory_mod.delete_memory_item(
            "trait", "zzz", TEST_USER))
        assert out["personality"] == ["prefers pricing kept simple"]

    def test_unknown_kind(self, fake_r2):
        assert "error" in json.loads(memory_mod.delete_memory_item(
            "mood", "x", TEST_USER))


class TestDeleteLogEntry:
    def test_removes_one_entry_and_keeps_the_rest(self, populated):
        out = json.loads(logs.delete_log_entry(
            "2026-03-10T10:00:00Z", user=TEST_USER))
        assert out["ok"] and "graph" in out["deleted"]["summary"]
        assert out["entries_left_that_day"] == 1
        left = populated.store[f"{ROOT}/logs/2026-03-10.jsonl"].decode()
        assert "Read two chapters" in left and "graph" not in left

    def test_last_entry_removes_the_day(self, populated):
        logs.delete_log_entry("2026-08-02T09:00:00Z", user=TEST_USER)
        assert f"{ROOT}/logs/2026-08-02.jsonl" not in populated.store
        days = json.loads(logs.list_log_days(user=TEST_USER))["days"]
        assert [d["date"] for d in days] == ["2026-03-10"]

    def test_wrong_time_lists_that_days_entries(self, populated):
        out = json.loads(logs.delete_log_entry(
            "2026-03-10T11:11:11Z", user=TEST_USER))
        assert "error" in out and len(out["entries_that_day"]) == 2

    def test_shared_timestamp_needs_match(self, fake_r2):
        write_day(fake_r2, "2026-01-01", [
            {"ts": "2026-01-01T10:00:00Z", "summary": "alpha"},
            {"ts": "2026-01-01T10:00:00Z", "summary": "beta"}])
        out = json.loads(logs.delete_log_entry(
            "2026-01-01T10:00:00Z", user=TEST_USER))
        assert "error" in out and len(out["matches"]) == 2
        out = json.loads(logs.delete_log_entry(
            "2026-01-01T10:00:00Z", match="BETA", user=TEST_USER))
        assert out["deleted"]["summary"] == "beta"
        assert "alpha" in fake_r2.store[
            f"{ROOT}/logs/2026-01-01.jsonl"].decode()

    def test_unparseable_line_survives_untouched(self, fake_r2):
        fake_r2.seed(f"{ROOT}/logs/2026-01-01.jsonl",
                     'not json\n{"ts":"2026-01-01T10:00:00Z","summary":"x"}\n')
        logs.delete_log_entry("2026-01-01T10:00:00Z", user=TEST_USER)
        assert fake_r2.store[f"{ROOT}/logs/2026-01-01.jsonl"] == b"not json\n"

    def test_a_bare_date_is_refused(self, populated):
        out = json.loads(logs.delete_log_entry("2026-03-10", user=TEST_USER))
        assert "error" in out
        assert len(populated.store[
            f"{ROOT}/logs/2026-03-10.jsonl"].splitlines()) == 2

    def test_journal_entry(self, populated):
        out = json.loads(logs.delete_log_entry(
            "2026-08-02T09:05:00Z", journal=True, client="claude",
            user=TEST_USER))
        assert out["removed_from"] == "journal of claude"
        assert f"{ROOT}/storage/claude/journal/2026-08-02.jsonl" \
            not in populated.store
        # The user's log for that day is untouched.
        assert f"{ROOT}/logs/2026-08-02.jsonl" in populated.store

    def test_journal_without_a_known_client(self, populated):
        out = json.loads(logs.delete_log_entry(
            "2026-08-02T09:05:00Z", journal=True, user=TEST_USER))
        assert "error" in out


# ==========================================
# HISTORY
# ==========================================

class TestHistory:
    def test_each_change_keeps_the_version_it_replaced(self, fake_r2):
        prof.update_profile("name", "Joy", TEST_USER)      # nothing before
        prof.update_profile("name", "Joydip", TEST_USER)
        out = json.loads(history.list_history("profile", TEST_USER))
        assert out["count"] == 1

    def test_restore_brings_back_the_previous_text(self, fake_r2):
        bio.update_bio("Journey", "The real story.", TEST_USER)
        bio.update_bio("Journey", "Overwritten by mistake.", TEST_USER)
        out = json.loads(history.restore_version("bio", user=TEST_USER))
        assert out["changed"]
        assert "The real story." in fake_r2.store[f"{ROOT}/bio.md"].decode()

    def test_restore_can_be_undone(self, fake_r2):
        bio.update_bio("Journey", "one", TEST_USER)
        bio.update_bio("Journey", "two", TEST_USER)
        history.restore_version("bio", user=TEST_USER)
        history.restore_version("bio", user=TEST_USER)
        assert "two" in fake_r2.store[f"{ROOT}/bio.md"].decode()

    def test_a_named_version(self, fake_r2):
        for value in ("one", "two", "three"):
            bio.update_bio("Journey", value, TEST_USER)
        versions = json.loads(
            history.list_history("bio", TEST_USER))["versions"]
        oldest = versions[-1]["version"]
        history.restore_version("bio", version=oldest, user=TEST_USER)
        assert "one" in fake_r2.store[f"{ROOT}/bio.md"].decode()

    def test_unknown_version_lists_the_real_ones(self, fake_r2):
        bio.update_bio("Journey", "one", TEST_USER)
        bio.update_bio("Journey", "two", TEST_USER)
        out = json.loads(history.restore_version(
            "bio", version="nope", user=TEST_USER))
        assert "error" in out and len(out["versions"]) == 1

    def test_memory_is_covered(self, fake_r2):
        memory_mod.add_memory_trait("curious", TEST_USER)
        memory_mod.add_memory_trait("wrong trait", TEST_USER)
        history.restore_version("memory", user=TEST_USER)
        stored = fake_r2.get_json(f"{ROOT}/memory.json")
        assert stored["personality"] == ["curious"]

    def test_only_the_newest_versions_are_kept(self, fake_r2):
        for i in range(history.KEEP + 5):
            prof.update_profile("mood", f"m{i}", TEST_USER)
        out = json.loads(history.list_history("profile", TEST_USER))
        assert out["count"] == history.KEEP

    def test_nothing_to_restore(self, fake_r2):
        assert "error" in json.loads(
            history.restore_version("profile", user=TEST_USER))

    def test_unknown_document(self, fake_r2):
        out = json.loads(history.list_history("diary", TEST_USER))
        assert out["documents"] == ["bio", "memory", "profile"]

    def test_history_is_not_a_topic_a_file_or_a_search_hit(self, fake_r2):
        prof.update_profile("name", "Zanzibar", TEST_USER)
        prof.update_profile("name", "Joy", TEST_USER)
        assert json.loads(data_mod.list_topics(TEST_USER))["topics"] == []
        assert json.loads(storage.list_files(user=TEST_USER))["files"] == []
        assert find("zanzibar")["count"] == 0

    def test_snapshot_failure_does_not_block_the_write(self, fake_r2):
        prof.update_profile("name", "Joy", TEST_USER)
        original = fake_r2.put_object

        def refuse(**kw):
            if "/history/" in kw["Key"]:
                raise RuntimeError("storage down")
            return original(**kw)

        fake_r2.put_object = refuse
        out = prof.update_profile("name", "Joydip", TEST_USER)
        assert not out.startswith("Error")
        assert "name: Joydip" in fake_r2.store[f"{ROOT}/profile.md"].decode()


# ==========================================
# TIMEZONES
# ==========================================

class TestTimezoneParsing:
    @pytest.mark.parametrize("value,label", [
        ("Asia/Kolkata", "Asia/Kolkata"),
        ("Asia/Kolkata (IST, UTC+5:30)", "Asia/Kolkata"),
        ("IST", "Asia/Kolkata"),
        ("UTC+5:30", "UTC+05:30"),
        ("+0530", "UTC+05:30"),
        ("GMT-8", "UTC-08:00"),
        ("UTC", "UTC"),
        ("utc+0", "UTC"),
    ])
    def test_readable_forms(self, value, label):
        assert timezones.parse(value)[1] == label

    @pytest.mark.parametrize("value", ["", None, "home", "UTC+99",
                                       "Mars/Olympus"])
    def test_unreadable_forms(self, value):
        assert timezones.parse(value) is None


class TestTimezoneInLogs:
    @pytest.fixture
    def kolkata(self, fake_r2):
        prof.update_profile("timezone", "Asia/Kolkata", TEST_USER)
        # 02:00 on the 11th in Kolkata is 20:30 UTC on the 10th.
        write_day(fake_r2, "2026-03-10", [
            {"ts": "2026-03-10T03:00:00Z", "summary": "morning of the 10th"},
            {"ts": "2026-03-10T20:30:00Z", "summary": "early on the 11th"}])
        return fake_r2

    def test_a_date_is_the_users_day(self, kolkata):
        out = json.loads(logs.get_log(date="2026-03-11", user=TEST_USER))
        assert [e["summary"] for e in out["entries"]] == ["early on the 11th"]
        assert out["timezone"] == "Asia/Kolkata"
        assert out["entries"][0]["local"] == "2026-03-11 02:00"
        assert out["entries"][0]["ts"] == "2026-03-10T20:30:00Z"

        out = json.loads(logs.get_log(date="2026-03-10", user=TEST_USER))
        assert [e["summary"] for e in out["entries"]] == [
            "morning of the 10th"]

    def test_days_are_listed_as_the_user_sees_them(self, kolkata):
        out = json.loads(logs.list_log_days(include_counts=True,
                                            user=TEST_USER))
        assert [(d["date"], d["entries"]) for d in out["days"]] == [
            ("2026-03-11", 1), ("2026-03-10", 1)]

    def test_a_time_without_an_offset_is_local(self, kolkata):
        out = json.loads(logs.get_log(since="2026-03-11T01:00",
                                      until="2026-03-11T03:00",
                                      user=TEST_USER))
        assert out["count"] == 1

    def test_no_timezone_is_utc_and_adds_nothing(self, fake_r2):
        write_day(fake_r2, "2026-03-10",
                  [{"ts": "2026-03-10T20:30:00Z", "summary": "x"}])
        out = json.loads(logs.get_log(date="2026-03-10", user=TEST_USER))
        assert out["timezone"] == "UTC" and out["count"] == 1
        assert "local" not in out["entries"][0]

    def test_changing_the_profile_takes_effect_at_once(self, kolkata):
        prof.update_profile("timezone", "UTC", TEST_USER)
        out = json.loads(logs.get_log(date="2026-03-10", user=TEST_USER))
        assert out["count"] == 2

    def test_update_profile_says_how_it_was_read(self, fake_r2):
        assert "Asia/Kolkata" in prof.update_profile(
            "timezone", "IST", TEST_USER)
        assert "stay in UTC" in prof.update_profile(
            "timezone", "my house", TEST_USER)

    def test_unreadable_timezone_falls_back_to_utc(self, fake_r2):
        prof.update_profile("timezone", "my house", TEST_USER)
        write_day(fake_r2, "2026-03-10",
                  [{"ts": "2026-03-10T20:30:00Z", "summary": "x"}])
        out = json.loads(logs.get_log(date="2026-03-10", user=TEST_USER))
        assert out["timezone"] == "UTC" and out["count"] == 1

    def test_delete_accepts_the_local_time(self, kolkata):
        out = json.loads(logs.delete_log_entry("2026-03-11T02:00",
                                               user=TEST_USER))
        assert out["deleted"]["summary"] == "early on the 11th"


# ==========================================
# WHOAMI
# ==========================================

class TestWhoami:
    def test_new_account(self, fake_r2):
        out = json.loads(account.whoami(TEST_USER))
        assert out["user_id"] == TEST_USER_ID
        assert out["stored"]["has_profile"] is False
        assert "next_step" in out
        assert out["time"]["timezone"] == "UTC" and "note" in out["time"]

    def test_counts_what_is_stored(self, populated):
        stored = json.loads(account.whoami(TEST_USER))["stored"]
        assert stored["data_topics"] == 1
        assert stored["log_days"] == 2
        assert stored["files"] == 1 and stored["journal_days"] == 1
        assert stored["clients"] == ["claude"]
        assert stored["has_profile"] and stored["has_bio"]

    def test_reports_scopes(self, fake_r2):
        user = dict(TEST_USER, scopes=["joyverse:read"])
        out = json.loads(account.whoami(user))
        assert out["can_read"] and not out["can_write"]

    def test_reports_the_detected_client(self, populated):
        user = dict(TEST_USER, client="claude-ai", client_source="header")
        out = json.loads(account.whoami(user))
        assert out["you_are"] == "claude"
        assert out["identified_by"] == "header"

    def test_says_how_to_be_identified_when_unknown(self, fake_r2):
        out = json.loads(account.whoami(TEST_USER))
        assert out["you_are"] is None and "how_to_be_identified" in out

    def test_local_date_follows_the_timezone(self, fake_r2):
        prof.update_profile("timezone", "UTC+14", TEST_USER)
        out = json.loads(account.whoami(TEST_USER))["time"]
        assert out["timezone"] == "UTC+14:00" and "note" not in out


# ==========================================
# ANNOTATIONS
# ==========================================

class TestAnnotations:
    def test_every_tool_is_classified(self):
        import run
        for schema in run.server._tool_schemas:
            assert schema.annotations, schema.name
            assert "readOnlyHint" in schema.annotations

    def test_reads_and_deletes_are_marked(self):
        import run
        hints = {s.name: s.annotations for s in run.server._tool_schemas}
        assert hints["search"]["readOnlyHint"] is True
        assert hints["get_log"]["readOnlyHint"] is True
        for name in ("delete_file", "delete_data", "replace_data",
                     "delete_log_entry"):
            assert hints[name]["destructiveHint"] is True, name
        assert hints["add_to_log"]["destructiveHint"] is False
        assert hints["save_file_from_url"]["openWorldHint"] is True
        assert hints["save_file_text"]["openWorldHint"] is False

    def test_read_tools_need_only_the_read_scope(self):
        import run
        from joyverse.config import READ_SCOPE, WRITE_SCOPE
        for schema in run.server._tool_schemas:
            scopes = run.server._tool_scopes[schema.name]
            wanted = [READ_SCOPE] if schema.annotations["readOnlyHint"] \
                else [WRITE_SCOPE]
            assert scopes == wanted, schema.name

    def test_an_unclassified_tool_fails_at_start_up(self):
        import run
        from mcppro.types import MCPToolDefinition

        class Stub:
            _tool_schemas = [MCPToolDefinition(name="mystery")]

        with pytest.raises(RuntimeError, match="mystery"):
            run._annotate(Stub())

    def test_instructions_flag_every_destructive_tool(self):
        import run
        text = run.server.instructions
        for name in run._DESTRUCTIVE:
            assert f"`{name}` **DESTRUCTIVE**" in text, name
        assert "`search`" in text and "`whoami`" in text

    def test_tool_without_annotations_lists_without_the_key(self):
        from mcppro.types import MCPToolDefinition
        assert "annotations" not in MCPToolDefinition(
            name="x").model_dump(exclude_none=True)
