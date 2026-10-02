"""Tests for the per-user file store.

Each test corresponds to a bug that reached review, so the names say what
broke rather than what was called.
"""
import io
import json

import pytest

from joyverse import storage
from joyverse.paths import resolve_file_path, normalise_client_name

ALICE = {"user_id": "u_alice"}
BOB = {"user_id": "u_bob"}


# ==========================================
# CLIENT REGISTRY ISOLATION
# ==========================================

class TestClientRegistry:
    def test_list_clients_does_not_leak_other_users(self, fake_r2):
        storage.register_client("chatgpt", user=ALICE)
        storage.register_client("claude", user=BOB)

        alice_view = json.loads(storage.list_clients(ALICE))
        names = {e["name"] for e in alice_view["clients"].values()}

        assert names == {"chatgpt"}, "Bob's client leaked into Alice's list"
        assert alice_view["count"] == 1

    def test_registry_stored_under_the_owner_prefix(self, fake_r2):
        """A shared registry would let one user deny another a client name."""
        storage.register_client("chatgpt", user=ALICE)
        assert fake_r2.store
        assert all(k.startswith("users/u_alice/") for k in fake_r2.store)

    def test_same_name_allowed_for_two_different_users(self, fake_r2):
        """The original global registry made 'chatgpt' exclusive worldwide."""
        a = json.loads(storage.register_client("chatgpt", user=ALICE))
        b = json.loads(storage.register_client("chatgpt", user=BOB))
        assert a["ok"] and b["ok"]
        assert a["client_id"] != b["client_id"]

    def test_duplicate_name_within_one_user_is_rejected(self, fake_r2):
        json.loads(storage.register_client("chatgpt", user=ALICE))
        again = json.loads(storage.register_client("chatgpt", user=ALICE))
        assert "already registered" in again["error"]

    @pytest.mark.parametrize("bad", ["", "  ", "a", "../evil", "with/slash",
                                      "x" * 40, "UPPER!", "_registry"])
    def test_invalid_names_rejected(self, fake_r2, bad):
        out = json.loads(storage.register_client(bad, user=ALICE))
        assert "error" in out, f"{bad!r} was accepted"


# ==========================================
# PATH TRAVERSAL
# ==========================================

class TestPathTraversal:
    @pytest.mark.parametrize("bad", [
        "../other/file.png",
        "../../etc/passwd",
        "images/../../escape.png",
        "/absolute/path.png",
        "..",
        "images/..",
        "a/b/c/d/e/f/g/h/i/j/k/l/m/n.png",
        "x" * 200 + ".png",
    ])
    def test_escape_attempts_rejected(self, bad):
        with pytest.raises(ValueError):
            resolve_file_path("u_alice", "chatgpt", bad)

    def test_backslash_becomes_a_separator_not_an_escape(self):
        """Windows-style separators are normalised, and stay inside the root."""
        key, rel = resolve_file_path("u_alice", "chatgpt", "back\\slash.png")
        assert key == "users/u_alice/storage/chatgpt/back/slash.png"
        assert rel == "back/slash.png"

    def test_backslash_traversal_still_rejected(self):
        with pytest.raises(ValueError):
            resolve_file_path("u_alice", "chatgpt", "..\\..\\etc\\passwd")

    def test_legitimate_nested_path_allowed(self):
        key, rel = resolve_file_path("u_alice", "chatgpt", "images/2026/a.png")
        assert key == "users/u_alice/storage/chatgpt/images/2026/a.png"
        assert rel == "images/2026/a.png"

    def test_write_cannot_escape_another_client(self, fake_r2):
        out = json.loads(storage.save_file_text(
            "../bob/steal.txt", "x", client="chatgpt", user=ALICE))
        assert "error" in out
        assert not any("u_bob" in k for k in fake_r2.store)


# ==========================================
# CROSS-USER FILE ISOLATION
# ==========================================

class TestFileIsolation:
    def test_read_another_users_file_is_not_possible(self, fake_r2):
        fake_r2.seed("users/u_bob/storage/chatgpt/secret.txt", "bob data")
        out = json.loads(storage.get_file("secret.txt", client="chatgpt",
                                          user=ALICE))
        assert "error" in out

    def test_listing_only_returns_own_files(self, fake_r2):
        fake_r2.seed("users/u_alice/storage/chatgpt/mine.txt", "a")
        fake_r2.seed("users/u_bob/storage/chatgpt/theirs.txt", "b")
        out = json.loads(storage.list_files(user=ALICE))
        paths = [f["path"] for f in out["files"]]
        assert paths == ["chatgpt/mine.txt"]
        assert "theirs.txt" not in json.dumps(out)

    def test_client_scoped_listing_uses_bare_paths(self, fake_r2):
        fake_r2.seed("users/u_alice/storage/chatgpt/mine.txt", "a")
        fake_r2.seed("users/u_alice/storage/claude/other.txt", "b")
        out = json.loads(storage.list_files(client="chatgpt", user=ALICE))
        assert [f["path"] for f in out["files"]] == ["mine.txt"]

    def test_client_names_are_case_folded(self):
        assert normalise_client_name("ChatGPT") == "chatgpt"


# ==========================================
# LIMITS
# ==========================================

class TestLimits:
    def test_per_file_limit(self, fake_r2, monkeypatch):
        monkeypatch.setattr(storage, "MAX_FILE_BYTES", 100)
        out = json.loads(storage.save_file_text("big.txt", "x" * 101,
                                                client="chatgpt", user=ALICE))
        assert out["error"] == "File too large"

    def test_file_exactly_at_limit_is_accepted(self, fake_r2, monkeypatch):
        monkeypatch.setattr(storage, "MAX_FILE_BYTES", 100)
        out = json.loads(storage.save_file_text("ok.txt", "x" * 100,
                                                client="chatgpt", user=ALICE))
        assert out["ok"] is True

    def test_user_total_limit(self, fake_r2, monkeypatch):
        monkeypatch.setattr(storage, "MAX_USER_BYTES", 150)
        storage.save_file_text("a.txt", "x" * 100, client="chatgpt", user=ALICE)
        storage.invalidate_usage("u_alice")
        out = json.loads(storage.save_file_text("b.txt", "x" * 100,
                                                client="chatgpt", user=ALICE))
        assert out["error"] == "Storage limit reached"
        assert out["scope"] == "user"

    def test_overwrite_is_credited_back(self, fake_r2, monkeypatch):
        """Replacing a file must not count as adding a second copy."""
        monkeypatch.setattr(storage, "MAX_USER_BYTES", 150)
        storage.save_file_text("a.txt", "x" * 100, client="chatgpt", user=ALICE)
        storage.invalidate_usage("u_alice")
        out = json.loads(storage.save_file_text("a.txt", "y" * 100,
                                                client="chatgpt", user=ALICE))
        assert out["ok"] is True, "overwrite was wrongly refused"
        assert fake_r2.store["users/u_alice/storage/chatgpt/a.txt"] == b"y" * 100

    def test_usage_updates_after_delete(self, fake_r2):
        storage.save_file_text("a.txt", "x" * 100, client="chatgpt", user=ALICE)
        storage.invalidate_usage("u_alice")
        assert storage.user_usage("u_alice", use_cache=False) == (100, 1)
        json.loads(storage.delete_file("a.txt", client="chatgpt", user=ALICE))
        assert storage.user_usage("u_alice", use_cache=False) == (0, 0)


# ==========================================
# SSRF
# ==========================================

class TestSSRF:
    @pytest.mark.parametrize("url", [
        "http://127.0.0.1:8001/admin",
        "http://localhost/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/x",
        "http://192.168.1.1/x",
        "http://172.16.0.1/x",
        "http://[::1]/x",
        "http://[fe80::1]/x",
        "file:///etc/passwd",
        "ftp://example.com/x",
        "gopher://example.com/x",
    ])
    def test_internal_targets_blocked(self, url):
        with pytest.raises(storage.StorageError):
            storage._validate_url(url)

    def test_redirect_to_private_is_rejected(self, monkeypatch):
        """A public URL that redirects inward must not be followed."""
        import urllib.error

        def fake_open(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 302, "Found",
                {"Location": "http://169.254.169.254/"}, None)

        monkeypatch.setattr(storage.urllib.request, "build_opener",
                            lambda *a: type("O", (), {"open": staticmethod(fake_open)})())
        with pytest.raises(storage.StorageError):
            storage._open("https://example.com/file.png")

    def test_redirect_loop_bounded(self, monkeypatch):
        import urllib.error

        def fake_open(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 302, "Found", {"Location": req.full_url}, None)

        monkeypatch.setattr(storage.urllib.request, "build_opener",
                            lambda *a: type("O", (), {"open": staticmethod(fake_open)})())
        with pytest.raises(storage.StorageError) as e:
            storage._open("https://example.com/a")
        assert e.value.payload["error"] == "Too many redirects"


# ==========================================
# FETCH BEHAVIOUR
# ==========================================

class _FakeResp:
    def __init__(self, body=b"", ctype="image/png", length=None):
        self._buf = io.BytesIO(body)
        self.headers = {"Content-Type": ctype}
        if length is not None:
            self.headers["Content-Length"] = str(length)
        self.closed = False

    def read(self, n=-1):
        return self._buf.read(n)

    def close(self):
        self.closed = True


class TestFetch:
    def test_probe_uses_head_not_get(self, monkeypatch):
        """probe_url must send HEAD, or it downloads the whole body."""
        seen = {}

        class Opener:
            def open(self, req, timeout=None):
                seen["method"] = req.get_method()
                return _FakeResp(b"x" * 10, length=10)

        monkeypatch.setattr(storage.urllib.request, "build_opener",
                            lambda *a: Opener())
        monkeypatch.setattr(storage, "_validate_url", lambda u: None)
        size, mime, _ = storage.probe_url("https://example.com/a.png")
        assert seen["method"] == "HEAD"
        assert size == 10

    def test_streaming_upload_writes_whole_body(self, monkeypatch, fake_r2):
        """Body must reach R2 intact via the streaming reader."""
        payload = b"z" * (1024 * 64)
        monkeypatch.setattr(storage, "_open",
                            lambda *a, **k: (_FakeResp(payload, length=len(payload)), "u"))
        monkeypatch.setattr(storage, "_validate_url", lambda u: None)

        written, mime, _ = storage.fetch_to_r2(
            "https://example.com/big.png",
            "users/u_alice/storage/chatgpt/big.png", {})
        assert written == len(payload)
        assert fake_r2.store["users/u_alice/storage/chatgpt/big.png"] == payload

    def test_oversized_stream_aborts(self, monkeypatch):
        monkeypatch.setattr(storage, "_open",
                            lambda *a, **k: (_FakeResp(b"z" * 5000), "u"))
        monkeypatch.setattr(storage, "_validate_url", lambda u: None)
        with pytest.raises(storage.StorageError) as e:
            storage.fetch_to_r2("https://example.com/big", "k", {}, max_bytes=100)
        assert e.value.payload["error"] == "File too large"

    def test_html_response_rejected(self, monkeypatch):
        """A login wall is not a file."""
        monkeypatch.setattr(storage, "_open",
                            lambda *a, **k: (_FakeResp(b"<html>", "text/html"), "u"))
        monkeypatch.setattr(storage, "_validate_url", lambda u: None)
        with pytest.raises(storage.StorageError) as e:
            storage.fetch_to_r2("https://example.com/x", "k", {})
        assert "HTML" in e.value.payload["error"]

    def test_empty_body_rejected(self, monkeypatch):
        monkeypatch.setattr(storage, "_open",
                            lambda *a, **k: (_FakeResp(b"", "image/png"), "u"))
        monkeypatch.setattr(storage, "_validate_url", lambda u: None)
        with pytest.raises(storage.StorageError):
            storage.fetch_to_r2("https://example.com/x", "k", {})

    def test_save_from_url_end_to_end(self, fake_r2, monkeypatch):
        payload = b"binary-image-bytes"
        monkeypatch.setattr(storage, "_open",
                            lambda *a, **k: (_FakeResp(payload, length=len(payload)), "u"))
        monkeypatch.setattr(storage, "_validate_url", lambda u: None)
        out = json.loads(storage.save_file_from_url(
            "https://cdn/x.png", "images/x.png", client="chatgpt", user=ALICE))
        assert out["ok"] is True
        assert out["size_bytes"] == len(payload)
        assert fake_r2.store["users/u_alice/storage/chatgpt/images/x.png"] == payload


# ==========================================
# RETRIEVAL
# ==========================================

class TestRetrieval:
    def test_get_file_returns_presigned_url(self, fake_r2):
        storage.save_file_text("notes.txt", "hello", client="chatgpt", user=ALICE)
        out = json.loads(storage.get_file("notes.txt", client="chatgpt",
                                          user=ALICE))
        assert out["ok"] is True
        assert out["url"].startswith("https://")
        assert out["size_bytes"] == 5
        assert out["expires_in_hours"] == 168

    def test_get_missing_file_errors_clearly(self, fake_r2):
        out = json.loads(storage.get_file("nope.txt", client="chatgpt",
                                          user=ALICE))
        assert "error" in out
        assert "list_files" in out["hint"]

    def test_metadata_records_provenance(self, fake_r2):
        storage.save_file_text("a.txt", "x", client="claude", user=ALICE)
        put = [p for p in fake_r2.puts if p["Key"].endswith("a.txt")][0]
        assert put["Metadata"]["client"] == "claude"

    def test_missing_client_argument_is_rejected(self, fake_r2):
        assert "error" in json.loads(storage.get_file("a.txt", user=ALICE))
        assert "error" in json.loads(storage.save_file_text("a.txt", "x", user=ALICE))

    def test_usage_reports_per_client_breakdown(self, fake_r2):
        json.loads(storage.register_client("chatgpt", user=ALICE))
        storage.save_file_text("a.txt", "x" * 100, client="chatgpt", user=ALICE)
        storage.invalidate_usage("u_alice")
        out = json.loads(storage.list_files(user=ALICE))
        assert out["files"] == [out["files"][0]]
        assert out["files"][0]["size_bytes"] == 100
        assert "chatgpt" in out["usage"]["by_client_gb"]
        out = json.loads(storage.list_files(user=ALICE))
        assert "chatgpt" in out["usage"]["by_client_gb"]

    def test_delete_frees_space_and_is_idempotent(self, fake_r2):
        storage.save_file_text("a.txt", "x", client="chatgpt", user=ALICE)
        first = json.loads(storage.delete_file("a.txt", client="chatgpt", user=ALICE))
        second = json.loads(storage.delete_file("a.txt", client="chatgpt", user=ALICE))
        assert first["ok"] and second["ok"]
        assert "users/u_alice/storage/chatgpt/a.txt" not in fake_r2.store

    def test_pagination_is_followed(self, fake_r2, monkeypatch):
        """Usage scan must page; R2 caps each response at 1000 keys."""
        import datetime
        for i in range(2500):
            fake_r2.store[f"users/u_alice/storage/chatgpt/f{i}.txt"] = b"x"
        total, count = storage.user_usage("u_alice", use_cache=False)
        assert count == 2500
        assert total == 2500
