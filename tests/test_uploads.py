"""Tests for inline base64 uploads.

The failure this guards against is silent: a decoder that drops stray
characters produces a shorter file that uploads cleanly and is quietly wrong.
Every decode test here compares exact bytes, not just "it did not raise".
"""
import base64
import json

import pytest

from joyverse import storage, uploads

ALICE = {"user_id": "u_alice"}
BOB = {"user_id": "u_bob"}

KEY = "users/u_alice/storage/chatgpt/img/logo.png"
# Two of the tests below store to a.png / a.bin instead, and were reading the
# wrong key -- which is how a real assertion can silently pass by never running.
A_KEY = "users/u_alice/storage/chatgpt/a.png"
BIN_KEY = "users/u_alice/storage/chatgpt/a.bin"


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


class TestRoundTrip:
    def test_exact_bytes_survive(self, fake_r2):
        """The whole point: not one byte may change."""
        raw = bytes(range(256)) * 8          # every byte value, 2 KB
        out = json.loads(uploads.save_file_base64(
            "img/logo.png", b64(raw), client="chatgpt",
            mime_type="image/png", user=ALICE))
        assert out["ok"] is True
        assert out["size_bytes"] == len(raw)
        assert fake_r2.store[KEY] == raw

    def test_realistic_png_size(self, fake_r2, monkeypatch):
        monkeypatch.setattr(uploads, "MAX_INLINE_BYTES", 8 * 1024 * 1024)
        raw = b"\x89PNG\r\n\x1a\n" + b"z" * (5 * 1024 * 1024)
        out = json.loads(uploads.save_file_base64(
            "dl01.png", b64(raw), client="chatgpt",
            mime_type="image/png", user=ALICE))
        assert out["ok"] is True
        assert out["size_bytes"] == len(raw)

    def test_mime_is_recorded(self, fake_r2):
        json.loads(uploads.save_file_base64(
            "a.png", b64(b"x"), client="chatgpt", mime_type="image/png",
            user=ALICE))
        put = [p for p in fake_r2.puts if p["Key"] == A_KEY][0]
        assert put["ContentType"] == "image/png"
        assert put["Metadata"]["upload"] == "inline"
        assert put["Metadata"]["client"] == "chatgpt"

    def test_default_mime_when_omitted(self, fake_r2):
        out = json.loads(uploads.save_file_base64(
            "a.bin", b64(b"x"), client="chatgpt", user=ALICE))
        assert out["mime_type"] == "application/octet-stream"


class TestDecodeStrictness:
    def test_whitespace_is_rejected_not_ignored(self, fake_r2):
        """validate=True: stray characters must fail loudly."""
        out = json.loads(uploads.save_file_base64(
            "a.bin", "AAAA BBBB", client="chatgpt", user=ALICE))
        assert "not valid base64" in out["error"]

    def test_data_url_prefix_is_rejected(self, fake_r2):
        """An agent passing a data: URL should be told, not silently mishandled."""
        out = json.loads(uploads.save_file_base64(
            "a.bin", "data:image/png;base64,AAAA", client="chatgpt", user=ALICE))
        assert "error" in out

    def test_non_base64_garbage_rejected(self, fake_r2):
        out = json.loads(uploads.save_file_base64(
            "a.bin", "!!!not base64!!!", client="chatgpt", user=ALICE))
        assert "not valid base64" in out["error"]

    def test_empty_data_rejected(self, fake_r2):
        out = json.loads(uploads.save_file_base64(
            "a.bin", "", client="chatgpt", user=ALICE))
        assert "error" in out

    def test_non_string_rejected(self, fake_r2):
        out = json.loads(uploads.save_file_base64(
            "a.bin", 12345, client="chatgpt", user=ALICE))
        assert "must be a base64 string" in out["error"]


class TestLimits:
    def test_at_limit_accepted(self, fake_r2, monkeypatch):
        monkeypatch.setattr(uploads, "MAX_INLINE_BYTES", 100)
        out = json.loads(uploads.save_file_base64(
            "a.bin", b64(b"x" * 100), client="chatgpt", user=ALICE))
        assert out["ok"] is True

    def test_over_inline_limit_refused_with_guidance(self, fake_r2, monkeypatch):
        monkeypatch.setattr(uploads, "MAX_INLINE_BYTES", 100)
        out = json.loads(uploads.save_file_base64(
            "a.bin", b64(b"x" * 101), client="chatgpt", user=ALICE))
        assert out["error"] == "File too large for an inline upload"
        assert "save_file_from_url" in out["hint"]
        assert KEY not in fake_r2.store

    def test_quota_applies(self, fake_r2, monkeypatch):
        monkeypatch.setattr(storage, "MAX_USER_BYTES", 150)
        uploads.save_file_base64("a.bin", b64(b"x" * 100), client="chatgpt",
                                 user=ALICE)
        storage.invalidate_usage("u_alice")
        out = json.loads(uploads.save_file_base64(
            "b.bin", b64(b"x" * 100), client="chatgpt", user=ALICE))
        assert out["error"] == "Storage limit reached"

    def test_overwrite_is_credited_back(self, fake_r2, monkeypatch):
        monkeypatch.setattr(storage, "MAX_USER_BYTES", 150)
        uploads.save_file_base64("a.bin", b64(b"x" * 100), client="chatgpt",
                                 user=ALICE)
        storage.invalidate_usage("u_alice")
        out = json.loads(uploads.save_file_base64(
            "a.bin", b64(b"y" * 100), client="chatgpt", user=ALICE))
        assert out["ok"] is True, "overwrite was wrongly refused"
        assert fake_r2.store[BIN_KEY] == b"y" * 100


class TestIsolationAndPaths:
    def test_traversal_rejected(self, fake_r2):
        out = json.loads(uploads.save_file_base64(
            "../../escape.png", b64(b"x"), client="chatgpt", user=ALICE))
        assert "error" in out
        assert not any("escape" in k for k in fake_r2.store)

    def test_cannot_write_into_another_client(self, fake_r2):
        out = json.loads(uploads.save_file_base64(
            "../claude/stolen.png", b64(b"x"), client="chatgpt", user=ALICE))
        assert "error" in out

    def test_another_users_file_is_untouched(self, fake_r2):
        fake_r2.seed("users/u_bob/storage/chatgpt/secret.png", "bob")
        json.loads(uploads.save_file_base64("mine.png", b64(b"mine"),
                                            client="chatgpt", user=ALICE))
        assert fake_r2.store["users/u_bob/storage/chatgpt/secret.png"] == b"bob"

    def test_client_is_required(self, fake_r2):
        out = json.loads(uploads.save_file_base64(
            "a.bin", b64(b"x"), user=ALICE))
        assert "client is required" in out["error"]

    def test_decoding_happens_before_any_write(self, fake_r2):
        """A bad payload must not leave a partial or empty object behind."""
        uploads.save_file_base64("a.bin", "cd /Users/joydipchakraborty/Projects/joyverse-mcp && .venv/bin/python -m pytest -p no:cacheprovider 2>&1 | tail -2cd /Users/joydipchakraborty/Projects/joyverse-mcp && .venv/bin/python -m pytest -p no:cacheprovider 2>&1 | tail -2", client="chatgpt", user=ALICE)
        assert KEY not in fake_r2.store
