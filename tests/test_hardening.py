"""Regression tests for the concurrency, auth and resource-limit hardening.

Each class pins one failure that used to be possible:

* a slow tool freezing every other request
* two writers silently overwriting each other (registry, logs, data, ...)
* auth refetching keys and the registry on every request
* bearer tokens that never expire, or that an empty scope left unrestricted
* a bearer account and an OAuth login being two separate people
* save_file_from_url failing on HEAD, ignoring quota, or truncating a stream
* unbounded request bodies and documents
"""
import asyncio
import io
import json
import time

import jwt as pyjwt
import pytest

from conftest import TEST_BUCKET, TEST_SECRET, TEST_USER, TEST_USER_ID
from joyverse import atomic, auth as jv_auth, identity, logs, storage
from joyverse import config as jv_config
from joyverse import data as data_mod
from joyverse import memory as memory_mod
from joyverse import profile as profile_mod


class FakeRequest:
    def __init__(self, token):
        self.headers = {"Authorization": f"Bearer {token}"}


def race_once(fake, key_suffix, competing_write):
    """Run `competing_write` right after the next read of a matching key.

    This is the exact interleaving that loses an update: the caller reads a
    version, somebody else writes, and the caller then writes what it read.
    """
    original = fake.get_object
    state = {"fired": False}

    def get_object(Bucket, Key):
        fire = Key.endswith(key_suffix) and not state["fired"]
        if fire:
            state["fired"] = True
        try:
            return original(Bucket, Key)
        finally:
            if fire:
                competing_write()

    fake.get_object = get_object
    return state


# ==========================================
# EVENT LOOP
# ==========================================

class TestToolsDoNotBlockTheServer:
    def test_health_answers_while_a_tool_is_running(self):
        import httpx
        from mcppro import MCPServer

        srv = MCPServer(name="t")

        @srv.tool(description="slow")
        def slow() -> str:
            time.sleep(1.0)
            return "done"

        async def scenario():
            transport = httpx.ASGITransport(app=srv._app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://t") as client:
                call = asyncio.create_task(client.post("/mcp", json={
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "slow"}}))
                await asyncio.sleep(0.1)
                started = time.monotonic()
                health = await client.get("/health")
                waited = time.monotonic() - started
                return health.status_code, waited, (await call).text

        status, waited, body = asyncio.run(scenario())
        assert status == 200
        assert waited < 0.5, f"/health waited {waited:.2f}s behind a tool"
        assert "done" in body

    def test_async_tools_are_awaited(self, client_factory, sse_rpc):
        client, srv = client_factory()

        @srv.tool(description="async")
        async def later() -> str:
            await asyncio.sleep(0)
            return "async-result"

        out = sse_rpc(client, "tools/call", {"name": "later"})
        assert out["result"]["content"][0]["text"] == "async-result"

    def test_null_arguments_are_treated_as_none_given(self, client_factory,
                                                      sse_rpc):
        client, srv = client_factory()

        @srv.tool(description="noargs")
        def hello() -> str:
            return "hi"

        out = sse_rpc(client, "tools/call",
                      {"name": "hello", "arguments": None})
        assert out["result"]["isError"] is False


# ==========================================
# LOST UPDATES
# ==========================================

class TestConcurrentWritesAreNotLost:
    def test_two_first_logins_both_keep_their_account(self, fake_r2):
        race_once(fake_r2, "identity.json",
                  lambda: identity.resolve_handle("other"))
        mine = identity.resolve_handle("mine")

        identity._LOOKUP_CACHE.clear()
        registry = identity._load()
        assert registry["handles"]["mine"] == mine
        assert "other" in registry["handles"], \
            "the competing sign-up was overwritten"
        assert registry["handles"]["other"] != mine

    def test_racing_log_entries_both_survive(self, fake_r2):
        day = "2026-11-11"
        race_once(fake_r2, f"{day}.jsonl",
                  lambda: logs._append_day(TEST_USER_ID, day, {"n": "theirs"}))
        logs._append_day(TEST_USER_ID, day, {"n": "mine"})

        seen = {e["n"] for e in logs._read_day(TEST_USER_ID, day)}
        assert seen == {"mine", "theirs"}

    def test_racing_edits_to_one_topic_both_apply(self, fake_r2):
        data_mod.edit_data("notes", "set", value={"seed": 1}, user=TEST_USER)
        race_once(fake_r2, "data/notes/progress.json",
                  lambda: data_mod.edit_data("notes", "set",
                                             value={"theirs": 1},
                                             user=TEST_USER))
        data_mod.edit_data("notes", "set", value={"mine": 1}, user=TEST_USER)

        doc = json.loads(data_mod.get_data("notes", TEST_USER))
        assert doc == {"seed": 1, "theirs": 1, "mine": 1}

    def test_racing_memory_updates_both_apply(self, fake_r2):
        memory_mod.get_memory(TEST_USER)
        race_once(fake_r2, "memory.json",
                  lambda: memory_mod.update_focus("their focus", TEST_USER))
        memory_mod.add_memory_trait("curious", TEST_USER)

        mem = json.loads(memory_mod.get_memory(TEST_USER))
        assert mem["personality"] == ["curious"]
        assert mem["current_context"]["main_focus"] == "their focus"

    def test_racing_profile_updates_both_apply(self, fake_r2):
        profile_mod.update_profile("name", "Joy", TEST_USER)
        race_once(fake_r2, "profile.md",
                  lambda: profile_mod.update_profile("city", "Pune",
                                                     TEST_USER))
        profile_mod.update_profile("age", "25", TEST_USER)

        text = profile_mod.get_profile(TEST_USER)
        assert "city: Pune" in text and "age: 25" in text

    def test_racing_client_registrations_both_survive(self, fake_r2):
        race_once(fake_r2, "_clients.json",
                  lambda: storage.register_client("chatgpt", user=TEST_USER))
        storage.register_client("claude", user=TEST_USER)

        names = set(json.loads(storage.list_clients(TEST_USER))["clients"])
        assert names == {"claude", "chatgpt"}

    def test_a_write_that_keeps_losing_reports_a_conflict(self, fake_r2,
                                                         monkeypatch):
        monkeypatch.setattr(atomic.time, "sleep", lambda s: None)
        day = "2026-11-11"
        original = fake_r2.get_object

        def always_raced(Bucket, Key):
            try:
                return original(Bucket, Key)
            finally:
                # Someone else writes after every single read.
                fake_r2.store[Key] = fake_r2.store.get(Key, b"") + b"{}\n"

        fake_r2.get_object = always_raced
        with pytest.raises(atomic.Conflict):
            logs._append_day(TEST_USER_ID, day, {"n": "mine"})

    def test_conditional_writes_can_be_switched_off(self, fake_r2,
                                                    monkeypatch):
        monkeypatch.setattr(jv_config, "CONDITIONAL_WRITES", False)
        fake_r2.seed("k", b"old")
        # A stale version would be refused with the guard on.
        atomic.put_versioned(fake_r2, TEST_BUCKET, "k", b"new", '"stale"')
        assert fake_r2.store["k"] == b"new"

    def test_missing_object_is_not_confused_with_a_failure(self, fake_r2):
        class Boom:
            def get_object(self, **kw):
                raise RuntimeError("connection reset")

        assert atomic.get_versioned(fake_r2, TEST_BUCKET, "nope") == \
            (None, atomic.MISSING)
        with pytest.raises(RuntimeError):
            atomic.get_versioned(Boom(), TEST_BUCKET, "nope")


# ==========================================
# AUTH COST
# ==========================================

class TestAuthDoesNotRefetchEveryRequest:
    def test_one_jwks_client_serves_many_requests(self, monkeypatch):
        import mcppro.auth as framework_auth

        built = []
        monkeypatch.setattr(framework_auth, "default_jwk_client",
                            lambda issuer, timeout=30.0: built.append(issuer)
                            or object())
        monkeypatch.setattr(jv_config, "AUTH0_DOMAIN", "tenant.example.com")
        monkeypatch.setattr(jv_config, "AUTH0_AUDIENCE", "https://api")

        for _ in range(5):
            with pytest.raises(Exception):
                jv_auth.oauth_auth(FakeRequest("not.a.jwt"))
        assert len(built) == 1

    def test_known_handle_costs_no_registry_read(self, fake_r2):
        first = identity.resolve_handle("joydip")
        reads = []
        original = fake_r2.get_object
        fake_r2.get_object = lambda **kw: reads.append(kw) or original(**kw)

        assert identity.resolve_handle("joydip") == first
        assert identity.resolve_handle("JoyDip") == first
        assert reads == []

    def test_known_oauth_identity_costs_no_registry_read(self, fake_r2):
        first = identity.resolve_identity("https://t/", "auth0|1")
        reads = []
        original = fake_r2.get_object
        fake_r2.get_object = lambda **kw: reads.append(kw) or original(**kw)

        assert identity.resolve_identity("https://t/", "auth0|1") == first
        assert reads == []

    def test_unknown_identity_is_never_served_from_cache(self, fake_r2):
        a = identity.resolve_handle("alice")
        b = identity.resolve_handle("bob")
        assert a != b
        assert identity.lookup_handle("carol") is None


# ==========================================
# BEARER TOKENS
# ==========================================

def _token(**claims):
    return pyjwt.encode(claims, TEST_SECRET, algorithm="HS256")


class TestBearerTokens:
    def test_token_without_expiry_is_rejected(self, fake_r2):
        with pytest.raises(Exception) as e:
            jv_auth.jwt_auth(FakeRequest(_token(handle="joydip")))
        assert e.value.status_code == 401
        assert "exp" in e.value.detail

    def test_empty_scope_claim_grants_nothing(self, fake_r2):
        tok = _token(handle="joydip", exp=int(time.time()) + 60, scope="")
        assert jv_auth.jwt_auth(FakeRequest(tok))["scopes"] == []

    def test_absent_scope_claim_still_means_full_access(self, fake_r2):
        tok = _token(handle="joydip", exp=int(time.time()) + 60)
        assert set(jv_auth.jwt_auth(FakeRequest(tok))["scopes"]) == {
            "joyverse:read", "joyverse:write"}


# ==========================================
# ONE PERSON, ONE ACCOUNT
# ==========================================

class TestLinkingBearerAndOAuth:
    ISSUER, SUB = "https://tenant.example.com/", "auth0|abc"

    def test_linked_login_resolves_to_the_bearer_account(self, fake_r2):
        bearer = identity.resolve_handle("joydip")
        identity.link_identity(bearer, self.ISSUER, self.SUB)
        assert identity.resolve_identity(self.ISSUER, self.SUB) == bearer

    def test_login_with_its_own_account_is_not_moved_silently(self, fake_r2):
        bearer = identity.resolve_handle("joydip")
        oauth = identity.resolve_identity(self.ISSUER, self.SUB)
        assert oauth != bearer
        with pytest.raises(ValueError):
            identity.link_identity(bearer, self.ISSUER, self.SUB)
        assert identity.resolve_identity(self.ISSUER, self.SUB) == oauth

    def test_repoint_moves_the_login_and_keeps_the_old_account(self, fake_r2):
        bearer = identity.resolve_handle("joydip")
        oauth = identity.resolve_identity(self.ISSUER, self.SUB)

        identity.link_identity(bearer, self.ISSUER, self.SUB, repoint=True)

        # Takes effect at once, despite the lookup cache.
        assert identity.resolve_identity(self.ISSUER, self.SUB) == bearer
        registry = identity._load()
        assert oauth in registry["users"], "old account must not be deleted"
        assert registry["users"][oauth].get("links") == []
        assert {"issuer": self.ISSUER, "sub": self.SUB} in \
            registry["users"][bearer]["links"]

    def test_registry_timestamps_are_utc(self, fake_r2, monkeypatch):
        from datetime import datetime, timezone
        before = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
        user_id = identity.resolve_handle("joydip")
        assert identity.get_user(user_id)["created"].startswith(before)


# ==========================================
# save_file_from_url
# ==========================================

class _Resp:
    def __init__(self, body=b"", ctype="image/png", length=None, step=None):
        self._buf = io.BytesIO(body)
        self._step = step
        self.headers = {"Content-Type": ctype}
        if length is not None:
            self.headers["Content-Length"] = str(length)

    def read(self, n=-1):
        if self._step and (n is None or n < 0 or n > self._step):
            n = self._step            # short reads, like a real socket
        return self._buf.read(n)

    def close(self):
        pass


class TestFetchHardening:
    def _save(self, monkeypatch, opener):
        monkeypatch.setattr(storage, "_open", opener)
        return json.loads(storage.save_file_from_url(
            "https://example.com/a.png", "a.png", client="claude",
            user=TEST_USER))

    def test_host_that_refuses_head_is_still_saved(self, fake_r2,
                                                   monkeypatch):
        payload = b"image-bytes"

        def opener(url, timeout=None, method="GET"):
            if method == "HEAD":
                raise storage.StorageError({
                    "error": "Could not fetch URL (HTTP 403)",
                    "url": url, "http_status": 403})
            return _Resp(payload), url

        out = self._save(monkeypatch, opener)
        assert out["ok"] is True and out["size_bytes"] == len(payload)

    def test_blocked_address_on_head_is_still_fatal(self, fake_r2,
                                                    monkeypatch):
        def opener(url, timeout=None, method="GET"):
            raise storage.StorageError({
                "error": "Refusing to fetch a non-public address"})

        out = self._save(monkeypatch, opener)
        assert "non-public" in out["error"]
        assert not any("a.png" in k for k in fake_r2.store)

    def test_quota_holds_when_no_length_is_declared(self, fake_r2,
                                                    monkeypatch):
        monkeypatch.setattr(storage, "MAX_USER_BYTES", 1000)
        out = self._save(monkeypatch,
                         lambda url, **k: (_Resp(b"z" * 5000), url))
        assert out["error"] == "Storage limit reached"
        assert not any("a.png" in k for k in fake_r2.store)

    def test_quota_holds_when_the_declared_length_is_a_lie(self, fake_r2,
                                                          monkeypatch):
        monkeypatch.setattr(storage, "MAX_USER_BYTES", 1000)
        out = self._save(monkeypatch,
                         lambda url, **k: (_Resp(b"z" * 5000, length=10), url))
        assert out["error"] == "Storage limit reached"

    def test_full_storage_is_refused_before_downloading(self, fake_r2,
                                                        monkeypatch):
        fake_r2.seed(f"users/{TEST_USER_ID}/storage/claude/old.bin",
                     b"x" * 2000)
        monkeypatch.setattr(storage, "MAX_USER_BYTES", 1000)
        opened = []

        def opener(url, timeout=None, method="GET"):
            opened.append(method)
            return _Resp(b"z" * 10), url

        out = self._save(monkeypatch, opener)
        assert out["error"] == "Storage limit reached"
        assert "GET" not in opened

    def test_reader_returns_exact_sizes_until_the_end(self):
        """A short read mid-stream makes the uploader truncate the file."""
        payload = bytes(range(256)) * 400            # 102,400 bytes
        reader = storage._StreamReader(_Resp(payload, step=777), 10 ** 9)
        assert reader.prime()

        out = []
        while True:
            chunk = reader.read(10_000)
            if not chunk:
                break
            out.append(chunk)
        assert all(len(c) == 10_000 for c in out[:-1])
        assert b"".join(out) == payload

    def test_slow_download_hits_the_total_deadline(self):
        reader = storage._StreamReader(_Resp(b"x" * 100), 10 ** 9,
                                       deadline=time.monotonic() - 1)
        with pytest.raises(storage.StorageError) as e:
            reader.read(10)
        assert e.value.payload["error"] == "Download took too long"

    def test_rebinding_host_is_blocked_at_connect_time(self, monkeypatch):
        """Validation sees a public address; the connection sees loopback."""
        import socket
        answers = iter(["93.184.215.14", "127.0.0.1", "127.0.0.1"])

        def flip(host, port, *a, **k):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                     (next(answers), port))]

        monkeypatch.setattr(storage.socket, "getaddrinfo", flip)
        with pytest.raises(storage.StorageError) as e:
            storage._open("http://rebind.test:9/")
        assert e.value.payload["address"] == "127.0.0.1"


# ==========================================
# SIZE LIMITS
# ==========================================

class TestSizeLimits:
    def test_oversized_request_body_is_refused(self):
        from fastapi.testclient import TestClient
        from mcppro import MCPServer

        client = TestClient(MCPServer(name="t", max_body_bytes=1000)._app)
        big = {"jsonrpc": "2.0", "id": 1, "method": "tools/list",
               "params": {"pad": "x" * 5000}}
        assert client.post("/mcp", json=big).status_code == 413
        small = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        assert client.post("/mcp", json=small).status_code == 200

    def test_oversized_chunked_body_is_refused(self):
        from fastapi.testclient import TestClient
        from mcppro import MCPServer

        client = TestClient(MCPServer(name="t", max_body_bytes=1000)._app)

        def chunks():                      # no Content-Length is sent
            for _ in range(50):
                yield b"x" * 100

        assert client.post("/mcp", content=chunks()).status_code == 413

    def test_inline_upload_still_fits_under_the_request_cap(self):
        assert jv_config.MAX_REQUEST_BYTES >= \
            jv_config.MAX_INLINE_BYTES * 4 // 3 + 4096

    def test_data_log_cannot_grow_past_the_document_limit(self, fake_r2,
                                                          monkeypatch):
        monkeypatch.setattr(jv_config, "MAX_DOC_BYTES", 500)
        out = json.loads(data_mod.edit_data(
            "notes", "set", value={"blob": "x" * 2000}, user=TEST_USER))
        assert "limit" in out["error"]
        assert fake_r2.puts == []

        out = data_mod.replace_data(
            "notes", json.dumps({"blob": "x" * 2000}), user=TEST_USER)
        assert out.startswith("Error:") and fake_r2.puts == []

    def test_profile_cannot_grow_past_the_document_limit(self, fake_r2,
                                                         monkeypatch):
        monkeypatch.setattr(jv_config, "MAX_DOC_BYTES", 500)
        out = profile_mod.update_profile("notes", "x" * 2000, TEST_USER)
        assert out.startswith("Error:") and fake_r2.puts == []

    def test_text_file_has_a_size_limit(self, fake_r2, monkeypatch):
        monkeypatch.setattr(storage, "MAX_DOC_BYTES", 500)
        out = json.loads(storage.save_file_text(
            "big.txt", "x" * 2000, client="claude", user=TEST_USER))
        assert out["error"] == "Text too large"
