"""Agent identity, and the split between the user's log and agent journals.

Two problems these guard against:

* The model was asked who it is, and answered differently each time -- the
  same assistant as chatgpt / gpt / openai-gpt, one agent as atlas /
  hermes-atlas, or the name of the project it was working on.
* Agents kept their own running log in the user's log, so an agent ticking
  every few minutes wrote almost all of it.
"""
import json

import pytest

from conftest import TEST_BUCKET, TEST_USER_ID
from joyverse import auth as jv_auth
from joyverse import clients, logs, maintenance, storage
from joyverse import data as data_mod

USER = {"user_id": TEST_USER_ID}


def as_agent(name, source="header"):
    return {"user_id": TEST_USER_ID, "client": name, "client_source": source}


class Req:
    def __init__(self, headers=None, query=None):
        # Case-insensitive, like the real request's headers.
        from starlette.datastructures import Headers
        self.headers = Headers(headers or {})
        self.query_params = query or {}


def registered(fake_r2):
    return set(json.loads(storage.list_clients(USER))["clients"])


def user_log(fake_r2, day=None):
    day = day or logs._day_str(logs._now())
    return logs._read_day(TEST_USER_ID, day)


def journal(fake_r2, client, day=None):
    day = day or logs._day_str(logs._now())
    return logs._read_entries(logs.journal_key(TEST_USER_ID, client, day), day)


# ==========================================
# NAME FOLDING
# ==========================================

class TestCanonicalNames:
    @pytest.mark.parametrize("spelling", [
        "chatgpt", "ChatGPT", "gpt", "openai-gpt", "chat-gpt", "openai-mcp",
        "  GPT  "])
    def test_every_chatgpt_spelling_is_one_client(self, spelling):
        assert clients.canonical(spelling) == "chatgpt"

    def test_registered_spelling_wins_over_the_default(self):
        assert clients.canonical("claude", ["claude-chat"]) == "claude-chat"
        assert clients.canonical("claude-ai", ["claude-chat"]) == "claude-chat"
        assert clients.canonical("claude") == "claude"

    def test_claude_code_is_not_folded_into_claude(self):
        assert clients.canonical("claude-code", ["claude-chat"]) == \
            "claude-code"

    def test_platform_prefix_folds_onto_a_known_agent(self):
        assert clients.canonical("hermes-atlas", ["atlas"]) == "atlas"
        # ...but only when that agent exists; otherwise it is its own name.
        assert clients.canonical("hermes-zara", ["atlas"]) == "hermes-zara"

    def test_exact_registered_name_is_never_rewritten(self):
        assert clients.canonical("gpt", ["gpt"]) == "gpt"

    def test_unknown_name_is_left_alone(self):
        assert clients.canonical("nextbiz-studio", ["chatgpt"]) == \
            "nextbiz-studio"


# ==========================================
# DETECTION FROM THE CONNECTION
# ==========================================

class TestDetection:
    def test_header_names_the_agent(self):
        assert clients.detect(Req({"X-Joyverse-Agent": "Atlas"})) == \
            ("atlas", "header")

    def test_url_parameter_names_the_agent(self):
        assert clients.detect(Req(query={"agent": "bolt"})) == ("bolt", "url")

    def test_token_claim_names_the_agent(self):
        assert clients.detect(Req(), {"agent": "cleo"}) == ("cleo", "token")

    @pytest.mark.parametrize("ua, product", [
        ("openai-mcp/1.0.0", "chatgpt"),
        ("Claude-User", "claude"),
        ("claude-code/2.1 (cli)", "claude-code"),
        ("Cursor/0.48", "cursor"),
    ])
    def test_user_agent_identifies_the_product(self, ua, product):
        assert clients.detect(Req({"User-Agent": ua})) == \
            (product, "user-agent")

    def test_header_beats_user_agent(self):
        """Five agents share one platform, so the platform cannot name them."""
        req = Req({"X-Joyverse-Agent": "atlas", "User-Agent": "hermes/1.0"})
        assert clients.detect(req)[0] == "atlas"

    def test_unusable_value_is_ignored(self):
        assert clients.detect(Req({"X-Joyverse-Agent": "../etc"})) == \
            (None, None)

    def test_nothing_to_go_on(self):
        assert clients.detect(Req({"User-Agent": "python-httpx/0.27"})) == \
            (None, None)

    def test_auth_wrapper_adds_the_client_to_the_context(self):
        dependency = jv_auth.with_client(lambda request: {"user_id": "u"})
        context = dependency(Req({"X-Joyverse-Agent": "atlas"}))
        assert context["client"] == "atlas"
        assert context["client_source"] == "header"

    def test_auth_wrapper_leaves_an_unidentified_caller_alone(self):
        dependency = jv_auth.with_client(lambda request: {"user_id": "u"})
        assert "client" not in dependency(Req())

    def test_bearer_token_agent_claim_reaches_the_context(self, fake_r2,
                                                          make_token):
        token = make_token("joydip", agent="Atlas")
        request = Req({"Authorization": f"Bearer {token}"})
        context = jv_auth.with_client(jv_auth.jwt_auth)(request)
        assert context["client"] == "atlas"
        assert context["client_source"] == "token"


# ==========================================
# FILE TOOLS WITHOUT A CLIENT ARGUMENT
# ==========================================

class TestFilesUseTheDetectedAgent:
    def test_save_needs_no_client_and_no_registration(self, fake_r2):
        out = json.loads(storage.save_file_text("notes.md", "hi",
                                                user=as_agent("atlas")))
        assert out["ok"] is True and out["client"] == "atlas"
        assert f"users/{TEST_USER_ID}/storage/atlas/notes.md" in fake_r2.store
        assert registered(fake_r2) == {"atlas"}

    def test_detected_name_folds_onto_the_existing_folder(self, fake_r2):
        storage.register_client("chatgpt", user=USER)
        out = json.loads(storage.save_file_text(
            "a.md", "x", user=as_agent("openai-mcp", "user-agent")))
        assert out["client"] == "chatgpt"
        assert registered(fake_r2) == {"chatgpt"}

    def test_explicit_alias_lands_in_the_existing_folder(self, fake_r2):
        storage.register_client("chatgpt", user=USER)
        storage.save_file_text("a.md", "x", client="gpt", user=USER)
        assert f"users/{TEST_USER_ID}/storage/chatgpt/a.md" in fake_r2.store
        assert not any("/storage/gpt/" in k for k in fake_r2.store)

    def test_model_supplied_name_is_not_auto_registered(self, fake_r2):
        """That is how a project name became a client."""
        storage.save_file_text("a.md", "x", client="nextbiz-studio",
                               user=USER)
        assert registered(fake_r2) == set()

    def test_another_agents_file_is_read_by_naming_it(self, fake_r2):
        storage.save_file_text("spec.md", "x", user=as_agent("atlas"))
        out = json.loads(storage.get_file("spec.md", client="hermes-atlas",
                                          user=as_agent("bolt")))
        assert out["ok"] is True and out["client"] == "atlas"

    def test_no_identity_at_all_is_still_an_error(self, fake_r2):
        out = json.loads(storage.save_file_text("a.md", "x", user=USER))
        assert "client is required" in out["error"]
        assert "X-Joyverse-Agent" in out["hint"]

    def test_registering_an_alias_points_at_the_existing_name(self, fake_r2):
        storage.register_client("chatgpt", user=USER)
        out = json.loads(storage.register_client("openai-gpt", user=USER))
        assert "already registered" in out["error"]
        assert out["name"] == "chatgpt"
        assert registered(fake_r2) == {"chatgpt"}

    def test_registering_an_alias_first_uses_the_canonical_name(self,
                                                                fake_r2):
        out = json.loads(storage.register_client("gpt", user=USER))
        assert out["name"] == "chatgpt" and "chatgpt" in out["note"]

    def test_list_clients_says_who_you_are(self, fake_r2):
        storage.register_client("atlas", user=USER)
        out = json.loads(storage.list_clients(as_agent("hermes-atlas")))
        assert out["you_are"] == "atlas"
        assert json.loads(storage.list_clients(USER))["you_are"] is None


# ==========================================
# THE USER'S LOG VS AGENT JOURNALS
# ==========================================

class TestLogSeparation:
    def test_user_activity_goes_to_the_user_log(self, fake_r2):
        out = json.loads(logs.add_to_log(
            "Practised graph problems", tags=["dsa"], user=as_agent("claude")))
        assert out["stored_in"] == "user_log"
        assert [e["summary"] for e in user_log(fake_r2)] == [
            "Practised graph problems"]
        assert user_log(fake_r2)[0]["client"] == "claude"

    @pytest.mark.parametrize("tag", ["tick", "route", "digest", "Monitors"])
    def test_bookkeeping_is_filed_in_the_agents_journal(self, fake_r2, tag):
        out = json.loads(logs.add_to_log(
            "Route tick 02:00Z quiet", tags=[tag], user=as_agent("atlas")))
        assert out["ok"] is True and out["stored_in"] == "agent_journal"
        assert "add_to_agent_log" in out["note"]
        assert user_log(fake_r2) == []
        assert len(journal(fake_r2, "atlas")) == 1

    def test_a_running_log_spills_into_the_journal(self, fake_r2,
                                                   monkeypatch):
        monkeypatch.setattr(logs, "USER_LOG_DAILY_PER_CLIENT", 3)
        results = [json.loads(logs.add_to_log(f"update {i}",
                                              user=as_agent("atlas")))
                   for i in range(5)]
        assert [r["stored_in"] for r in results] == \
            ["user_log"] * 3 + ["agent_journal"] * 2
        assert len(user_log(fake_r2)) == 3
        assert len(journal(fake_r2, "atlas")) == 2

    def test_one_agents_volume_does_not_block_another(self, fake_r2,
                                                      monkeypatch):
        monkeypatch.setattr(logs, "USER_LOG_DAILY_PER_CLIENT", 1)
        logs.add_to_log("a", user=as_agent("atlas"))
        out = json.loads(logs.add_to_log("b", user=as_agent("claude")))
        assert out["stored_in"] == "user_log"

    def test_bookkeeping_with_no_identity_is_refused(self, fake_r2):
        out = json.loads(logs.add_to_log("tick", tags=["tick"], user=USER))
        assert "add_to_agent_log" in out["hint"]
        assert user_log(fake_r2) == []

    def test_explicit_alias_is_attributed_to_the_real_name(self, fake_r2):
        storage.register_client("chatgpt", user=USER)
        out = json.loads(logs.add_to_log("x", client="gpt", user=USER))
        assert out["logged"]["client"] == "chatgpt"
        assert "warning" not in out

    def test_detected_agent_is_registered_by_logging(self, fake_r2):
        logs.add_to_log("x", user=as_agent("bolt"))
        assert registered(fake_r2) == {"bolt"}


class TestAgentJournal:
    def test_write_and_read_back(self, fake_r2):
        out = json.loads(logs.add_to_agent_log(
            "Swept 8 channels, nothing new", tags=["tick"],
            user=as_agent("atlas")))
        assert out["ok"] is True and out["path"].startswith("journal/")

        read = json.loads(logs.get_agent_log(user=as_agent("atlas")))
        assert read["client"] == "atlas"
        assert [e["summary"] for e in read["entries"]] == [
            "Swept 8 channels, nothing new"]

    def test_journal_never_appears_in_the_user_log(self, fake_r2):
        logs.add_to_agent_log("note", user=as_agent("atlas"))
        assert json.loads(logs.get_log(user=USER))["entries"] == []
        assert json.loads(logs.list_log_days(user=USER))["days"] == []

    def test_journal_lives_in_the_agents_storage_folder(self, fake_r2):
        logs.add_to_agent_log("note", user=as_agent("atlas"))
        day = logs._day_str(logs._now())
        assert (f"users/{TEST_USER_ID}/storage/atlas/journal/{day}.jsonl"
                in fake_r2.store)
        files = json.loads(storage.list_files(client="atlas", user=USER))
        assert [f["path"] for f in files["files"]] == [f"journal/{day}.jsonl"]

    def test_journals_are_separate_per_agent(self, fake_r2):
        logs.add_to_agent_log("atlas note", user=as_agent("atlas"))
        logs.add_to_agent_log("bolt note", user=as_agent("bolt"))
        mine = json.loads(logs.get_agent_log(user=as_agent("bolt")))
        assert [e["summary"] for e in mine["entries"]] == ["bolt note"]

    def test_one_agent_can_read_anothers_journal(self, fake_r2):
        logs.add_to_agent_log("handoff: deploy pending", user=as_agent("atlas"))
        theirs = json.loads(logs.get_agent_log(client="atlas",
                                               user=as_agent("bolt")))
        assert theirs["entries"][0]["summary"] == "handoff: deploy pending"

    def test_long_note_is_not_cut_at_the_user_log_limit(self, fake_r2):
        logs.add_to_agent_log("x" * 3000, user=as_agent("atlas"))
        assert len(journal(fake_r2, "atlas")[0]["summary"]) == 3000

    def test_needs_an_identity(self, fake_r2):
        out = json.loads(logs.add_to_agent_log("note", user=USER))
        assert "client is required" in out["error"]

    def test_filter_by_tag(self, fake_r2):
        logs.add_to_agent_log("a", tags=["deploy"], user=as_agent("atlas"))
        logs.add_to_agent_log("b", tags=["tick"], user=as_agent("atlas"))
        out = json.loads(logs.get_agent_log(tags=["deploy"],
                                            user=as_agent("atlas")))
        assert [e["summary"] for e in out["entries"]] == ["a"]


# ==========================================
# END TO END THROUGH THE REAL SERVER
# ==========================================

class TestOverMCP:
    def call(self, app, token, tool, arguments, headers=None, url="/mcp"):
        resp = app.post(url, headers={"Authorization": f"Bearer {token}",
                                      **(headers or {})},
                        json={"jsonrpc": "2.0", "id": 1,
                              "method": "tools/call",
                              "params": {"name": tool,
                                         "arguments": arguments}})
        frame = json.loads(resp.text.split("data: ", 1)[1])
        return json.loads(frame["result"]["content"][0]["text"])

    @pytest.fixture
    def app(self, fake_r2):
        from fastapi.testclient import TestClient
        import run
        return TestClient(run.server._app)

    def test_header_identifies_the_agent(self, app, fake_r2, make_token):
        token = make_token("joydip")
        out = self.call(app, token, "save_file_text",
                        {"path": "a.md", "content": "x"},
                        headers={"X-Joyverse-Agent": "atlas"})
        assert out["client"] == "atlas"

    def test_url_parameter_identifies_the_agent(self, app, fake_r2,
                                                make_token):
        out = self.call(app, make_token("joydip"), "list_clients", {},
                        url="/mcp?agent=bolt")
        assert out["you_are"] == "bolt"

    def test_two_agents_on_one_account_get_separate_folders(self, app,
                                                            fake_r2,
                                                            make_token):
        token = make_token("joydip")
        for agent in ("atlas", "bolt"):
            self.call(app, token, "add_to_agent_log", {"summary": "tick"},
                      headers={"X-Joyverse-Agent": agent})
        folders = {k.split("/storage/")[1].split("/")[0]
                   for k in fake_r2.store if "/journal/" in k}
        assert folders == {"atlas", "bolt"}

    def test_new_tools_are_documented_in_the_instructions(self):
        import run
        text = run.server.instructions
        assert "add_to_agent_log" in text and "get_agent_log" in text
        assert "what the\nUSER did" in text or "USER did" in text


# ==========================================
# TOPIC DESCRIPTIONS
# ==========================================

def test_purpose_is_used_as_a_topic_description(fake_r2):
    data_mod.replace_data("requests", json.dumps(
        {"purpose": "Requests between agents", "items": []}), user=USER)
    topics = json.loads(data_mod.list_topics(USER))["topics"]
    assert topics[0]["description"] == "Requests between agents"


# ==========================================
# MIGRATING EXISTING DATA
# ==========================================

def seed_log(fake_r2, day, entries):
    fake_r2.seed(f"users/{TEST_USER_ID}/logs/{day}.jsonl",
                 "".join(json.dumps(e) + "\n" for e in entries))


@pytest.fixture
def messy(fake_r2):
    """A small copy of what production looked like."""
    for name in ("atlas", "hermes-atlas", "chatgpt"):
        fake_r2.seed(
            f"users/{TEST_USER_ID}/storage/_clients.json",
            json.dumps({"clients": {
                n: {"name": n, "first_seen": f"2026-10-0{i}T00:00:00Z"}
                for i, n in enumerate(("atlas", "hermes-atlas", "chatgpt"), 1)
            }}))
    fake_r2.seed(f"users/{TEST_USER_ID}/storage/hermes-atlas/brief.md", "B")
    fake_r2.seed(f"users/{TEST_USER_ID}/storage/atlas/spec.md", "S")
    seed_log(fake_r2, "2026-10-04", [
        {"ts": "2026-10-04T09:00:00Z", "summary": "Fixed auth",
         "client": "hermes-atlas"},
        {"ts": "2026-10-04T10:00:00Z", "summary": "Practised DSA"},
    ])
    seed_log(fake_r2, "2026-10-05", [
        {"ts": "2026-10-05T01:00:00Z", "summary": "Route tick quiet",
         "client": "atlas", "tags": ["route", "tick"]},
        {"ts": "2026-10-05T02:00:00Z", "summary": "Shipped carousel",
         "client": "chatgpt", "tags": ["instagram"]},
        {"ts": "2026-10-05T03:00:00Z", "summary": "Real work by atlas",
         "client": "atlas"},
    ])
    return fake_r2


class TestMergeClients:
    MAP = {"hermes-atlas": "atlas"}

    def test_dry_run_changes_nothing(self, messy):
        before = dict(messy.store)
        report = maintenance.merge_clients(messy, TEST_BUCKET, TEST_USER_ID,
                                           self.MAP)
        assert messy.store == before
        assert report["applied"] is False
        assert report["files_moved"] == [
            "hermes-atlas/brief.md -> atlas/brief.md"]
        assert report["log_entries_relabelled"] == {"hermes-atlas -> atlas": 1}

    def test_apply_moves_files_registry_and_attribution(self, messy):
        maintenance.merge_clients(messy, TEST_BUCKET, TEST_USER_ID, self.MAP,
                                  apply=True)
        root = f"users/{TEST_USER_ID}/storage/"
        assert messy.store[root + "atlas/brief.md"] == b"B"
        assert not any(k.startswith(root + "hermes-atlas/")
                       for k in messy.store)

        clients_now = json.loads(messy.store[root + "_clients.json"])
        assert set(clients_now["clients"]) == {"atlas", "chatgpt"}
        assert clients_now["clients"]["atlas"]["merged_from"] == [
            "hermes-atlas"]
        assert logs._read_day(TEST_USER_ID, "2026-10-04")[0]["client"] == \
            "atlas"

    def test_apply_backs_up_every_day_it_rewrites(self, messy):
        report = maintenance.merge_clients(messy, TEST_BUCKET, TEST_USER_ID,
                                           self.MAP, apply=True)
        backups = [k for k in messy.store if k.startswith(report["backup"])]
        assert [k.rsplit("/", 1)[-1] for k in backups] == ["2026-10-04.jsonl"]
        assert b"hermes-atlas" in messy.store[backups[0]]

    def test_a_file_that_already_exists_is_not_overwritten(self, messy):
        root = f"users/{TEST_USER_ID}/storage/"
        messy.seed(root + "atlas/brief.md", "DIFFERENT")
        report = maintenance.merge_clients(messy, TEST_BUCKET, TEST_USER_ID,
                                           self.MAP, apply=True)
        assert report["file_conflicts"] == ["hermes-atlas/brief.md"]
        assert messy.store[root + "atlas/brief.md"] == b"DIFFERENT"
        assert messy.store[root + "hermes-atlas/brief.md"] == b"B"

    def test_backups_do_not_show_up_as_log_days(self, messy):
        maintenance.merge_clients(messy, TEST_BUCKET, TEST_USER_ID, self.MAP,
                                  apply=True)
        days = json.loads(logs.list_log_days(user=USER))["days"]
        assert [d["date"] for d in days] == ["2026-10-05", "2026-10-04"]


class TestMoveAgentLogs:
    def test_dry_run_changes_nothing(self, messy):
        before = dict(messy.store)
        report = maintenance.move_agent_logs(messy, TEST_BUCKET, TEST_USER_ID)
        assert messy.store == before
        assert report["moved"] == {"atlas": 1} and report["kept"] == 4

    def test_only_bookkeeping_moves_by_default(self, messy):
        maintenance.move_agent_logs(messy, TEST_BUCKET, TEST_USER_ID,
                                    apply=True)
        left = [e["summary"] for e in
                logs._read_day(TEST_USER_ID, "2026-10-05")]
        assert left == ["Shipped carousel", "Real work by atlas"]
        moved = logs._read_entries(
            logs.journal_key(TEST_USER_ID, "atlas", "2026-10-05"),
            "2026-10-05")
        assert [e["summary"] for e in moved] == ["Route tick quiet"]

    def test_all_from_moves_everything_an_agent_wrote(self, messy):
        report = maintenance.move_agent_logs(
            messy, TEST_BUCKET, TEST_USER_ID,
            all_from=["atlas", "chatgpt"], rename={"hermes-atlas": "atlas"},
            apply=True)
        assert report["moved"] == {"atlas": 3, "chatgpt": 1}
        # The user's own entry -- no client -- is all that remains.
        assert [e["summary"] for e in
                logs._read_day(TEST_USER_ID, "2026-10-04")] == [
                    "Practised DSA"]
        # A day with nothing left is removed rather than left empty.
        assert f"users/{TEST_USER_ID}/logs/2026-10-05.jsonl" not in \
            messy.store
        assert report["days_emptied"] == 1

    def test_running_twice_does_not_duplicate(self, messy):
        for _ in range(2):
            maintenance.move_agent_logs(messy, TEST_BUCKET, TEST_USER_ID,
                                        apply=True)
        moved = logs._read_entries(
            logs.journal_key(TEST_USER_ID, "atlas", "2026-10-05"),
            "2026-10-05")
        assert len(moved) == 1

    def test_bookkeeping_with_no_client_stays_put(self, messy):
        seed_log(messy, "2026-10-06", [
            {"ts": "2026-10-06T01:00:00Z", "summary": "tick",
             "tags": ["tick"]}])
        report = maintenance.move_agent_logs(messy, TEST_BUCKET, TEST_USER_ID,
                                             apply=True)
        assert report["left_without_client"] == 1
        assert len(logs._read_day(TEST_USER_ID, "2026-10-06")) == 1

    def test_originals_are_backed_up(self, messy):
        report = maintenance.move_agent_logs(messy, TEST_BUCKET, TEST_USER_ID,
                                             apply=True)
        backup = messy.store[report["backup"] + "2026-10-05.jsonl"]
        assert b"Route tick quiet" in backup

    def test_summary_is_read_only(self, messy):
        before = dict(messy.store)
        summary = maintenance.client_summary(messy, TEST_BUCKET, TEST_USER_ID)
        assert messy.store == before
        assert summary["log_entries"]["atlas"] == {"total": 2,
                                                  "operational": 1}
        assert summary["files_per_folder"] == {"hermes-atlas": 1, "atlas": 1}
