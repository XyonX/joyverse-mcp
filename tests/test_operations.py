"""Rate limiting, token revocation, audit trail, upload links, export,
account deletion and MCP prompts."""
import io
import json
import time
import zipfile

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from conftest import TEST_USER, TEST_USER_ID
from joyverse import account, audit, identity, maintenance, ratelimit
from joyverse import revocation, storage, uploads
from joyverse import profile as prof
from joyverse import config as jv_config

ROOT = f"users/{TEST_USER_ID}"
BUCKET = jv_config.BUCKET_NAME


@pytest.fixture
def app(fake_r2):
    import run
    return TestClient(run.server._app)


def rpc(app, token, method, params=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    resp = app.post("/mcp", json=body,
                    headers={"Authorization": f"Bearer {token}"})
    if resp.status_code != 200:
        return resp
    for line in resp.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])


def call(app, token, name, arguments=None):
    out = rpc(app, token, "tools/call",
              {"name": name, "arguments": arguments or {}})
    return out["result"]["content"][0]["text"]


def user_id_of(handle):
    return identity.lookup_handle(handle)


# ==========================================
# RATE LIMIT
# ==========================================

class TestRateLimit:
    def test_allows_up_to_the_limit_then_refuses(self):
        for _ in range(3):
            ratelimit.check("u", 3, now=100.0)
        with pytest.raises(HTTPException) as refused:
            ratelimit.check("u", 3, now=100.5)
        assert refused.value.status_code == 429
        assert int(refused.value.headers["Retry-After"]) >= 1

    def test_window_slides(self):
        for _ in range(3):
            ratelimit.check("u", 3, now=100.0)
        ratelimit.check("u", 3, now=161.0)

    def test_accounts_are_counted_separately(self):
        for _ in range(3):
            ratelimit.check("a", 3, now=100.0)
        ratelimit.check("b", 3, now=100.0)

    def test_zero_turns_it_off(self):
        for _ in range(50):
            ratelimit.check("u", 0, now=100.0)

    def test_server_answers_429(self, app, make_token, monkeypatch):
        monkeypatch.setattr(jv_config, "RATE_LIMIT_PER_MINUTE", 2)
        token = make_token("limited")
        assert "result" in rpc(app, token, "ping")
        assert "result" in rpc(app, token, "ping")
        resp = rpc(app, token, "ping")
        assert resp.status_code == 429 and "Retry-After" in resp.headers
        # Another account is unaffected.
        assert "result" in rpc(app, make_token("someone-else"), "ping")

    def test_unauthenticated_requests_are_not_counted(self, app, make_token,
                                                      monkeypatch):
        monkeypatch.setattr(jv_config, "RATE_LIMIT_PER_MINUTE", 1)
        for _ in range(3):
            assert rpc(app, "garbage", "ping").status_code == 401
        assert "result" in rpc(app, make_token("limited"), "ping")


# ==========================================
# REVOCATION
# ==========================================

class TestRevocation:
    def test_revoked_token_is_refused_others_are_not(self, app, make_token):
        bad = make_token("tester", jti="leaked")
        good = make_token("tester", jti="fine")
        assert "result" in rpc(app, bad, "ping")
        revocation.revoke_token("leaked", int(time.time()) + 3600)
        resp = rpc(app, bad, "ping")
        assert resp.status_code == 401
        assert "result" in rpc(app, good, "ping")

    def test_handle_cut_off_retires_older_tokens(self, app, make_token):
        now = int(time.time())
        old = make_token("tester", iat=now - 100)
        legacy = make_token("tester")              # no iat, no jti
        revocation.revoke_handle("tester", before=now - 10)
        new = make_token("tester", iat=now)
        assert rpc(app, old, "ping").status_code == 401
        assert rpc(app, legacy, "ping").status_code == 401
        assert "result" in rpc(app, new, "ping")
        # A different handle is untouched.
        assert "result" in rpc(app, make_token("other-user"), "ping")

    def test_cut_off_can_be_lifted(self, app, make_token):
        token = make_token("tester", iat=int(time.time()) - 100)
        revocation.revoke_handle("tester")
        assert rpc(app, token, "ping").status_code == 401
        revocation.restore_handle("tester")
        assert "result" in rpc(app, token, "ping")

    def test_expired_entries_are_pruned(self, fake_r2):
        revocation.revoke_token("old", int(time.time()) - 5)
        data = revocation.revoke_token("new", int(time.time()) + 500)
        assert set(data["tokens"]) == {"new"}

    def test_list_is_cached_between_requests(self, fake_r2):
        revocation.revoke_token("x", None)
        reads = []
        original = fake_r2.get_object
        fake_r2.get_object = \
            lambda **kw: reads.append(kw["Key"]) or original(**kw)
        for _ in range(5):
            assert revocation.is_revoked({"jti": "x"}, "h")
        assert reads.count(revocation.REVOKED_KEY) == 1

    def test_storage_failure_does_not_lock_everyone_out(self, fake_r2):
        def broken(**kw):
            raise RuntimeError("storage down")

        fake_r2.get_object = broken
        assert revocation.is_revoked({"jti": "x"}, "h") is False

    def test_generated_tokens_carry_an_id(self):
        source = open("scripts/generate_token.py").read()
        assert '"jti"' in source and '"iat"' in source


# ==========================================
# AUDIT
# ==========================================

class TestAudit:
    def read(self, app, token, **args):
        return json.loads(call(app, token, "get_audit_log", args))

    def test_changes_are_recorded_reads_are_not(self, app, make_token):
        token = make_token("tester")
        call(app, token, "update_profile", {"field": "name", "value": "Joy"})
        call(app, token, "get_profile")
        call(app, token, "search", {"query": "joy"})
        entries = self.read(app, token)["entries"]
        assert [e["tool"] for e in entries] == ["update_profile"]
        assert entries[0]["ok"] is True and entries[0]["auth"] == "bearer"

    def test_records_the_target_but_never_the_content(self, app, make_token):
        token = make_token("tester")
        call(app, token, "update_profile",
             {"field": "name", "value": "a very private value"})
        entry = self.read(app, token)["entries"][0]
        assert entry["target"] == {"field": "name"}
        assert entry["sizes"] == {"value": len("a very private value")}
        day = time.strftime("%Y-%m-%d", time.gmtime())
        raw = jv_config.r2_client.store[
            f"users/{user_id_of('tester')}/audit/{day}.jsonl"].decode()
        assert "private" not in raw

    def test_records_the_agent_from_the_connection(self, app, make_token):
        token = make_token("tester", agent="atlas")
        call(app, token, "update_focus", {"focus": "ship it"})
        assert self.read(app, token)["entries"][0]["agent"] == "atlas"

    def test_a_refused_call_is_recorded_as_failed(self, app, make_token):
        token = make_token("tester")
        call(app, token, "delete_data", {"topic": "nope"})
        entry = self.read(app, token)["entries"][0]
        assert entry["tool"] == "delete_data" and entry["ok"] is False

    def test_filter_by_tool(self, app, make_token):
        token = make_token("tester")
        call(app, token, "update_focus", {"focus": "a"})
        call(app, token, "add_memory_trait", {"trait": "curious"})
        out = self.read(app, token, tool="add_memory_trait")
        assert [e["tool"] for e in out["entries"]] == ["add_memory_trait"]

    def test_can_be_turned_off(self, app, make_token, monkeypatch):
        monkeypatch.setattr(jv_config, "AUDIT_ENABLED", False)
        token = make_token("tester")
        call(app, token, "update_focus", {"focus": "a"})
        assert self.read(app, token)["count"] == 0

    def test_an_audit_failure_never_fails_the_call(self, app, make_token,
                                                   monkeypatch):
        def broken(*a, **kw):
            raise RuntimeError("audit down")

        monkeypatch.setattr(audit, "record", broken)
        token = make_token("tester")
        assert "Updated focus" in call(app, token, "update_focus",
                                       {"focus": "a"})

    def test_invalid_arguments_are_not_recorded(self, app, make_token):
        token = make_token("tester")
        call(app, token, "update_focus", {"nonsense": 1})
        assert self.read(app, token)["count"] == 0

    def test_audit_is_not_a_log_day_or_a_search_hit(self, app, make_token):
        token = make_token("tester")
        call(app, token, "update_focus", {"focus": "zanzibar"})
        assert json.loads(call(app, token, "list_log_days"))["days"] == []
        assert json.loads(call(app, token, "search",
                               {"query": "update_focus"}))["count"] == 0


# ==========================================
# UPLOAD LINKS
# ==========================================

USER = dict(TEST_USER, client="claude", client_source="header")
FILE_KEY = f"{ROOT}/storage/claude/renders/final.mp4"


def start(fake_r2, size=1000, **kw):
    return json.loads(uploads.request_upload(
        "renders/final.mp4", size, "video/mp4", user=USER, **kw))


class TestUploadLinks:
    def test_request_returns_a_put_link(self, fake_r2):
        out = start(fake_r2)
        assert out["ok"] and out["method"] == "PUT"
        assert FILE_KEY in out["url"]
        assert out["headers"] == {"Content-Type": "video/mp4"}
        assert out["client"] == "claude"

    def test_complete_confirms_the_file(self, fake_r2):
        out = start(fake_r2)
        fake_r2.seed(FILE_KEY, b"x" * 1000)
        done = json.loads(uploads.complete_upload(out["upload_id"], USER))
        assert done["ok"] and done["size_bytes"] == 1000
        assert "warning" not in done
        files = json.loads(storage.list_files(client="claude", user=USER))
        assert [f["path"] for f in files["files"]] == ["renders/final.mp4"]

    def test_complete_twice_says_already_done(self, fake_r2):
        out = start(fake_r2)
        fake_r2.seed(FILE_KEY, b"x" * 1000)
        uploads.complete_upload(out["upload_id"], USER)
        again = json.loads(uploads.complete_upload(out["upload_id"], USER))
        assert "error" in again and FILE_KEY in fake_r2.store

    def test_complete_before_upload(self, fake_r2):
        out = start(fake_r2)
        done = json.loads(uploads.complete_upload(out["upload_id"], USER))
        assert "error" in done and "yet" in done["error"]

    def test_bigger_than_declared_is_removed(self, fake_r2):
        out = start(fake_r2, size=1000)
        fake_r2.seed(FILE_KEY, b"x" * 5000)
        done = json.loads(uploads.complete_upload(out["upload_id"], USER))
        assert "error" in done and done["deleted"] == "renders/final.mp4"
        assert FILE_KEY not in fake_r2.store

    def test_short_upload_is_kept_with_a_warning(self, fake_r2):
        out = start(fake_r2, size=1000)
        fake_r2.seed(FILE_KEY, b"x" * 400)
        done = json.loads(uploads.complete_upload(out["upload_id"], USER))
        assert done["ok"] and "warning" in done

    def test_over_quota_upload_is_removed(self, fake_r2, monkeypatch):
        out = start(fake_r2, size=1000)
        fake_r2.seed(FILE_KEY, b"x" * 1000)
        monkeypatch.setattr(uploads, "MAX_USER_BYTES", 500)
        done = json.loads(uploads.complete_upload(out["upload_id"], USER))
        assert "error" in done and FILE_KEY not in fake_r2.store

    def test_refuses_a_file_over_the_limit(self, fake_r2):
        out = start(fake_r2, size=jv_config.MAX_FILE_BYTES + 1)
        assert "error" in out and "url" not in out

    def test_existing_file_needs_overwrite(self, fake_r2):
        fake_r2.seed(FILE_KEY, b"old")
        assert "error" in start(fake_r2)
        assert start(fake_r2, overwrite=True)["ok"]

    def test_overwrite_not_yet_arrived_keeps_the_old_file(self, fake_r2):
        fake_r2.seed(FILE_KEY, b"old")
        out = start(fake_r2, overwrite=True)
        done = json.loads(uploads.complete_upload(out["upload_id"], USER))
        assert "error" in done and fake_r2.store[FILE_KEY] == b"old"

    @pytest.mark.parametrize("path", ["../escape.mp4", "/abs.mp4", ""])
    def test_bad_paths_are_refused(self, fake_r2, path):
        out = json.loads(uploads.request_upload(path, 10, user=USER))
        assert "error" in out

    @pytest.mark.parametrize("size", [0, -5, "big", None])
    def test_bad_sizes_are_refused(self, fake_r2, size):
        out = json.loads(uploads.request_upload("a.bin", size, user=USER))
        assert "error" in out

    def test_another_users_upload_id_is_unknown(self, fake_r2):
        out = start(fake_r2)
        other = {"user_id": "u_0a1b2c3d4e5f", "client": "claude"}
        done = json.loads(uploads.complete_upload(out["upload_id"], other))
        assert "error" in done

    @pytest.mark.parametrize("upload_id", ["../x", "a/b", "", None])
    def test_malformed_upload_id(self, fake_r2, upload_id):
        assert "error" in json.loads(
            uploads.complete_upload(upload_id, USER))

    def test_pending_records_are_not_files(self, fake_r2):
        start(fake_r2)
        files = json.loads(storage.list_files(user=USER))
        assert files["files"] == []


# ==========================================
# EXPORT
# ==========================================

class TestExport:
    def bundle(self, fake_r2, **kw):
        out = json.loads(account.export_account(user=TEST_USER, **kw))
        key = next(k for k in fake_r2.store if "/exports/" in k)
        return out, zipfile.ZipFile(io.BytesIO(fake_r2.store[key]))

    @pytest.fixture
    def populated(self, fake_r2):
        prof.update_profile("name", "Joy", TEST_USER)
        prof.update_profile("name", "Joydip", TEST_USER)
        fake_r2.seed(f"{ROOT}/data/dsa/progress.json", '{"solved": 3}')
        fake_r2.seed(f"{ROOT}/data/dsa/.previous.json", '{"solved": 2}')
        fake_r2.seed(f"{ROOT}/logs/2026-03-10.jsonl", '{"summary":"x"}\n')
        fake_r2.seed(f"{ROOT}/storage/claude/journal/2026-03-10.jsonl",
                     '{"summary":"tick"}\n')
        fake_r2.seed(f"{ROOT}/storage/claude/img/a.png", b"\x89PNG" * 10)
        fake_r2.seed("users/u_ffffffffffff/profile.md", "someone else")
        return fake_r2

    def test_contains_every_document(self, populated):
        out, bundle = self.bundle(populated)
        names = set(bundle.namelist())
        assert {"profile.md", "data/dsa/progress.json",
                "logs/2026-03-10.jsonl",
                "storage/claude/journal/2026-03-10.jsonl",
                "files.json", "export.json"} <= names
        assert out["ok"] and "/exports/" in out["url"]
        assert bundle.read("profile.md").decode().count("Joydip") == 1

    def test_files_are_listed_not_copied(self, populated):
        _, bundle = self.bundle(populated)
        assert "storage/claude/img/a.png" not in bundle.namelist()
        listed = json.loads(bundle.read("files.json"))
        assert listed == [{"client": "claude", "path": "img/a.png",
                           "size_bytes": 40}]

    def test_history_only_on_request(self, populated):
        _, bundle = self.bundle(populated)
        assert not [n for n in bundle.namelist()
                    if n.startswith("history/") or ".previous" in n]
        populated.store = {k: v for k, v in populated.store.items()
                           if "/exports/" not in k}
        _, bundle = self.bundle(populated, include_history=True)
        assert [n for n in bundle.namelist() if n.startswith("history/")]
        assert "data/dsa/.previous.json" in bundle.namelist()

    def test_only_this_account(self, populated):
        _, bundle = self.bundle(populated)
        assert "someone else" not in bundle.read("profile.md").decode()
        assert json.loads(bundle.read("export.json"))["user_id"] == \
            TEST_USER_ID

    def test_a_new_export_replaces_the_old_one(self, populated):
        account.export_account(user=TEST_USER)
        populated.seed(f"{ROOT}/exports/joyverse-export-old.zip", b"old")
        account.export_account(user=TEST_USER)
        exports = [k for k in populated.store if "/exports/" in k]
        assert len(exports) == 1 and not exports[0].endswith("old.zip")
        # An export never contains an earlier export.
        bundle = zipfile.ZipFile(io.BytesIO(populated.store[exports[0]]))
        assert not [n for n in bundle.namelist() if n.startswith("exports")]

    def test_too_large_is_refused(self, populated, monkeypatch):
        monkeypatch.setattr(account, "EXPORT_MAX_BYTES", 10)
        out = json.loads(account.export_account(user=TEST_USER))
        assert "error" in out
        assert not [k for k in populated.store if "/exports/" in k]

    def test_exports_do_not_count_as_files(self, populated):
        account.export_account(user=TEST_USER)
        files = json.loads(storage.list_files(user=TEST_USER))["files"]
        assert "claude/img/a.png" in [f["path"] for f in files]
        assert not [f for f in files if "export" in f["path"]]


# ==========================================
# DELETE ACCOUNT (operator only)
# ==========================================

class TestDeleteAccount:
    @pytest.fixture
    def two_accounts(self, fake_r2):
        mine = identity.resolve_handle("mine")
        other = identity.resolve_handle("other")
        fake_r2.seed(f"users/{mine}/profile.md", "me")
        fake_r2.seed(f"users/{mine}/storage/claude/a.txt", "file")
        fake_r2.seed(f"users/{other}/profile.md", "them")
        return fake_r2, mine, other

    def test_dry_run_changes_nothing(self, two_accounts):
        fake_r2, mine, _ = two_accounts
        before = dict(fake_r2.store)
        report = maintenance.delete_account(fake_r2, BUCKET, mine)
        assert report["applied"] is False and report["objects"] >= 2
        assert report["logins_removed"]["handles"] == ["mine"]
        assert fake_r2.store == before

    def test_apply_removes_data_and_login_only_for_that_account(
            self, two_accounts):
        fake_r2, mine, other = two_accounts
        report = maintenance.delete_account(fake_r2, BUCKET, mine, apply=True)
        assert report["objects_left"] == 0
        assert not [k for k in fake_r2.store if k.startswith(f"users/{mine}/")]
        assert identity.lookup_handle("mine") is None
        assert identity.lookup_handle("other") == other
        assert fake_r2.store[f"users/{other}/profile.md"] == b"them"

    def test_rejects_anything_that_is_not_a_user_id(self, fake_r2):
        for bad in ("_registry", "", "u_../x", "mine"):
            with pytest.raises(ValueError):
                maintenance.delete_account(fake_r2, BUCKET, bad, apply=True)

    def test_is_not_exposed_as_a_tool(self):
        import run
        assert "delete_account" not in run.server._tool_functions


# ==========================================
# PROMPTS
# ==========================================

class TestPrompts:
    def test_capability_is_advertised(self, app, make_token):
        out = rpc(app, make_token("tester"), "initialize")
        assert "prompts" in out["result"]["capabilities"]

    def test_list(self, app, make_token):
        prompts = rpc(app, make_token("tester"),
                      "prompts/list")["result"]["prompts"]
        names = {p["name"] for p in prompts}
        assert {"daily_review", "weekly_review", "find_in_my_data",
                "pick_up_where_i_left_off", "set_me_up"} == names
        find = next(p for p in prompts if p["name"] == "find_in_my_data")
        assert find["arguments"][0] == {
            "name": "what", "description": "What to look for.",
            "required": True}

    def test_get_fills_in_arguments(self, app, make_token):
        out = rpc(app, make_token("tester"), "prompts/get", {
            "name": "daily_review", "arguments": {"date": "2026-03-10"}})
        message = out["result"]["messages"][0]
        assert message["role"] == "user"
        assert 'date="2026-03-10"' in message["content"]["text"]

    def test_optional_argument_can_be_left_out(self, app, make_token):
        out = rpc(app, make_token("tester"), "prompts/get",
                  {"name": "daily_review"})
        assert "today" in out["result"]["messages"][0]["content"]["text"]

    def test_missing_required_argument(self, app, make_token):
        out = rpc(app, make_token("tester"), "prompts/get",
                  {"name": "find_in_my_data"})
        assert out["error"]["code"] == -32602 and "what" in \
            out["error"]["message"]

    def test_unknown_prompt(self, app, make_token):
        out = rpc(app, make_token("tester"), "prompts/get", {"name": "nope"})
        assert out["error"]["code"] == -32602

    def test_unknown_arguments_are_ignored(self, app, make_token):
        out = rpc(app, make_token("tester"), "prompts/get", {
            "name": "weekly_review", "arguments": {"extra": "x"}})
        assert "result" in out

    def test_every_prompt_names_only_real_tools(self):
        import re
        import run
        tools = set(run.server._tool_functions)
        for name, spec in run.server._prompts.items():
            args = {a["name"]: "x" for a in spec["arguments"]}
            for mentioned in re.findall(r"`([a-z_]+)`", spec["func"](**args)):
                if mentioned in ("limit", "since", "has_more", "you_are",
                                 "timezone"):
                    continue
                assert mentioned in tools, f"{name} mentions {mentioned}"

    def test_server_without_prompts_does_not_advertise_them(
            self, client_factory, sse_rpc):
        client, _ = client_factory()
        out = sse_rpc(client, "initialize")
        assert "prompts" not in out["result"]["capabilities"]
        assert sse_rpc(client, "prompts/list")["result"]["prompts"] == []
