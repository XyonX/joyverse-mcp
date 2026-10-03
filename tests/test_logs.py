"""Tests for the daily conversation log.

The two cases worth reading first: a range spanning midnight (two files, must
merge and filter correctly) and the rolling 24h default (an entry exactly on the
boundary belongs, one a second older does not).
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from joyverse import logs

ALICE = {"user_id": "u_aaaaaaaaaaa1"}
BOB = {"user_id": "u_bbbbbbbbbbb2"}


def write(user_id, day, entries):
    from joyverse.config import r2_client
    body = "".join(json.dumps(e, separators=(",", ":")) + "\n" for e in entries)
    r2_client.put_object(
        Bucket="b", Key=f"users/{user_id}/logs/{day}.jsonl",
        Body=body.encode(), ContentType="application/x-ndjson")


class TestAddToLog:
    def test_creates_the_day_file(self, fake_r2):
        out = json.loads(logs.add_to_log("Worked on FlexyGrid CSS grid",
                                         client="claude", user=ALICE))
        assert out["ok"] is True
        assert len(fake_r2.store) == 1
        key = list(fake_r2.store)[0]
        assert key.endswith(f"logs/{out['date']}.jsonl")

    def test_second_entry_appends_rather_than_overwrites(self, fake_r2):
        """The whole point: an append must not clobber the day's history."""
        logs.add_to_log("first", user=ALICE)
        logs.add_to_log("second", user=ALICE)
        logs.add_to_log("third", user=ALICE)
        key = list(fake_r2.store)[0]
        lines = fake_r2.store[key].decode().strip().splitlines()
        assert len(lines) == 3
        summaries = [json.loads(l)["summary"] for l in lines]
        assert summaries == ["first", "second", "third"]

    def test_server_stamps_the_time(self, fake_r2):
        out = json.loads(logs.add_to_log("x", user=ALICE))
        ts = out["logged"]["ts"]
        assert ts.endswith("Z")
        # Within a minute of now -- the agent never supplies this.
        parsed = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
        delta = abs((datetime.now(timezone.utc)
                     - parsed.replace(tzinfo=timezone.utc)).total_seconds())
        assert delta < 60

    def test_optional_fields_are_kept(self, fake_r2):
        out = json.loads(logs.add_to_log(
            "x", client="chatgpt", tags=["nextbiz", "design"],
            files=["chatgpt/nextbiz/post.png"], user=ALICE))
        logged = out["logged"]
        assert logged["tags"] == ["nextbiz", "design"]
        assert logged["files"] == ["chatgpt/nextbiz/post.png"]
        assert logged["client"] == "chatgpt"

    def test_summary_is_required(self, fake_r2):
        for bad in ["", "   ", None, 123]:
            assert "error" in json.loads(
                logs.add_to_log(bad, user=ALICE)), bad

    def test_tags_must_be_a_list(self, fake_r2):
        assert "error" in json.loads(
            logs.add_to_log("x", tags="notalist", user=ALICE))


class TestDateAndWindow:
    def test_date_must_be_iso(self, fake_r2):
        for bad in ["2026-1-1", "01-10-2026", "yesterday", "2026/10/01"]:
            out = json.loads(logs.get_log(date=bad, user=ALICE))
            assert "error" in out, bad

    def test_since_until_reversed_is_rejected(self, fake_r2):
        out = json.loads(logs.get_log(since="2026-10-02T10:00",
                                      until="2026-10-01T10:00", user=ALICE))
        assert "before" in out["error"]

    def test_bare_date_on_until_means_end_of_day(self, fake_r2):
        """Otherwise since/until around one date returns nothing."""
        write(ALICE["user_id"], "2026-10-01", [
            {"ts": "2026-10-01T00:00:00Z", "summary": "midnight"},
            {"ts": "2026-10-01T23:59:00Z", "summary": "nearly midnight"},
        ])
        out = json.loads(logs.get_log(since="2026-10-01", until="2026-10-01",
                                      user=ALICE))
        assert out["total_matched"] == 2, out


class TestMidnightSpanningRange:
    """A range crossing a day boundary reads two files and merges them."""

    def test_entries_from_both_days_come_back(self, fake_r2):
        uid = ALICE["user_id"]
        write(uid, "2026-10-01", [
            {"ts": "2026-10-01T23:50:00Z", "summary": "before midnight"},
        ])
        write(uid, "2026-10-02", [
            {"ts": "2026-10-02T00:10:00Z", "summary": "after midnight"},
        ])
        out = json.loads(logs.get_log(since="2026-10-01T23:00",
                                      until="2026-10-02T01:00", user=ALICE))
        summaries = [e["summary"] for e in out["entries"]]
        assert sorted(summaries) == ["after midnight", "before midnight"]
        assert out["days_read"] == ["2026-10-01", "2026-10-02"]

    def test_entries_outside_the_window_are_excluded(self, fake_r2):
        uid = ALICE["user_id"]
        write(uid, "2026-10-01", [
            {"ts": "2026-10-01T09:00:00Z", "summary": "too early"},
            {"ts": "2026-10-01T23:50:00Z", "summary": "inside"},
        ])
        write(uid, "2026-10-02", [
            {"ts": "2026-10-02T00:10:00Z", "summary": "inside too"},
            {"ts": "2026-10-02T09:00:00Z", "summary": "too late"},
        ])
        out = json.loads(logs.get_log(since="2026-10-01T23:00",
                                      until="2026-10-02T01:00", user=ALICE))
        got = sorted(e["summary"] for e in out["entries"])
        assert got == ["inside", "inside too"]

    def test_ordering_across_two_days(self, fake_r2):
        uid = ALICE["user_id"]
        write(uid, "2026-10-01", [{"ts": "2026-10-01T23:50:00Z", "summary": "b"}])
        write(uid, "2026-10-02", [{"ts": "2026-10-02T00:10:00Z", "summary": "c"}])

        newest = json.loads(logs.get_log(since="2026-10-01T23:00",
                                         until="2026-10-02T01:00",
                                         order="newest", user=ALICE))
        assert [e["summary"] for e in newest["entries"]] == ["c", "b"]

        oldest = json.loads(logs.get_log(since="2026-10-01T23:00",
                                         until="2026-10-02T01:00",
                                         order="oldest", user=ALICE))
        assert [e["summary"] for e in oldest["entries"]] == ["b", "c"]


class TestRollingWindow:
    """No arguments means the last 24 hours, not 'today'."""

    def test_default_window_is_rolling_24h(self, fake_r2):
        now = datetime.now(timezone.utc)
        inside = now - timedelta(hours=23)
        outside = now - timedelta(hours=25)

        day = now.strftime("%Y-%m-%d")
        write(ALICE["user_id"], day, [
            {"ts": inside.strftime("%Y-%m-%dT%H:%M:%SZ"), "summary": "inside"},
            {"ts": outside.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "summary": "outside"},
        ])
        out = json.loads(logs.get_log(user=ALICE))
        got = [e["summary"] for e in out["entries"]]
        assert "inside" in got
        assert "outside" not in got, "a 25h-old entry leaked into a 24h window"

    def test_boundary_entry_is_included(self, fake_r2):
        """Exactly 24h old sits on the edge; it must not be dropped."""
        now = datetime.now(timezone.utc)
        edge = now - timedelta(hours=23, minutes=59, seconds=30)
        write(ALICE["user_id"], now.strftime("%Y-%m-%d"),
              [{"ts": edge.strftime("%Y-%m-%dT%H:%M:%SZ"), "summary": "edge"}])
        out = json.loads(logs.get_log(user=ALICE))
        assert [e["summary"] for e in out["entries"]] == ["edge"]

    def test_empty_log_returns_cleanly(self, fake_r2):
        out = json.loads(logs.get_log(user=ALICE))
        assert out["entries"] == []
        assert out["has_more"] is False


class TestPaging:
    def test_limit_and_has_more(self, fake_r2):
        uid = ALICE["user_id"]
        now = datetime.now(timezone.utc)
        entries = []
        for i in range(30):
            ts = now - timedelta(minutes=i)
            entries.append({"ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "summary": f"e{i}"})
        write(uid, now.strftime("%Y-%m-%d"), entries)

        out = json.loads(logs.get_log(limit=20, user=ALICE))
        assert out["count"] == 20
        assert out["total_matched"] == 30
        # Without this an agent concludes it saw everything.
        assert out["has_more"] is True

    def test_exact_limit_reports_no_more(self, fake_r2):
        uid = ALICE["user_id"]
        now = datetime.now(timezone.utc)
        write(uid, now.strftime("%Y-%m-%d"),
              [{"ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "summary": f"e{i}"}
               for i in range(5)])
        out = json.loads(logs.get_log(limit=5, user=ALICE))
        assert out["has_more"] is False

    def test_newest_returns_most_recent_first(self, fake_r2):
        uid = ALICE["user_id"]
        now = datetime.now(timezone.utc)
        entries = []
        for i in range(5):
            ts = now - timedelta(hours=i)
            entries.append({"ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "summary": f"h{i}h_ago"})
        write(uid, now.strftime("%Y-%m-%d"), entries)
        out = json.loads(logs.get_log(limit=3, user=ALICE))
        assert [e["summary"] for e in out["entries"]] == [
            "h0h_ago", "h1h_ago", "h2h_ago"]


class TestTagsAndIsolation:
    def test_tag_filter(self, fake_r2):
        uid = ALICE["user_id"]
        now = datetime.now(timezone.utc)
        write(uid, now.strftime("%Y-%m-%d"), [
            {"ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "summary": "flex",
             "tags": ["flexygrid"]},
            {"ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "summary": "other",
             "tags": ["instagram"]},
        ])
        out = json.loads(logs.get_log(tags=["flexygrid"], user=ALICE))
        assert [e["summary"] for e in out["entries"]] == ["flex"]

    def test_another_users_log_is_invisible(self, fake_r2):
        now = datetime.now(timezone.utc)
        day = now.strftime("%Y-%m-%d")
        write(BOB["user_id"], day,
              [{"ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "summary": "bob"}])
        out = json.loads(logs.get_log(user=ALICE))
        assert out["entries"] == []
        assert "bob" not in json.dumps(out)

    def test_two_users_keep_separate_days(self, fake_r2):
        logs.add_to_log("alice work", user=ALICE)
        logs.add_to_log("bob work", user=BOB)
        a = json.dumps(json.loads(logs.get_log(user=ALICE)))
        b = json.dumps(json.loads(logs.get_log(user=BOB)))
        assert "alice work" in a and "bob work" not in a
        assert "bob work" in b and "alice work" not in b


class TestListLogDays:
    def test_lists_days_newest_first(self, fake_r2):
        uid = ALICE["user_id"]
        write(uid, "2026-09-30", [{"ts": "2026-09-30T10:00:00Z", "summary": "a"}])
        write(uid, "2026-10-01", [{"ts": "2026-10-01T10:00:00Z", "summary": "b"}])
        out = json.loads(logs.list_log_days(user=ALICE))
        assert [d["date"] for d in out["days"]] == ["2026-10-01", "2026-09-30"]

    def test_empty_when_nothing_logged(self, fake_r2):
        out = json.loads(logs.list_log_days(user=ALICE))
        assert out["days"] == []
        assert "get_log" not in out.get("next_step", "") \
            or "No log entries" in out["next_step"]

    def test_include_counts(self, fake_r2):
        uid = ALICE["user_id"]
        write(uid, "2026-10-01", [
            {"ts": "2026-10-01T10:00:00Z", "summary": "a", "client": "claude"},
            {"ts": "2026-10-01T11:00:00Z", "summary": "b", "client": "claude"},
        ])
        out = json.loads(logs.list_log_days(include_counts=True, user=ALICE))
        assert out["days"][0]["entries"] == 2
        assert out["days"][0]["clients"] == ["claude"]

    def test_other_users_days_are_hidden(self, fake_r2):
        write(BOB["user_id"], "2026-10-01",
              [{"ts": "2026-10-01T10:00:00Z", "summary": "b"}])
        out = json.loads(logs.list_log_days(user=ALICE))
        assert out["days"] == []


class TestCorruptData:
    def test_a_bad_line_does_not_break_the_day(self, fake_r2):
        """One corrupt append must not make a whole day unreadable."""
        uid = ALICE["user_id"]
        key = f"users/{uid}/logs/2026-10-01.jsonl"
        good = json.dumps({"ts": "2026-10-01T10:00:00Z", "summary": "good"})
        fake_r2.store[key] = (good + "\n{not json\n" +
                              json.dumps({"ts": "2026-10-01T11:00:00Z",
                                          "summary": "also good"}) + "\n"
                              ).encode()
        out = json.loads(logs.get_log(date="2026-10-01", user=ALICE))
        got = sorted(e["summary"] for e in out["entries"])
        assert got == ["also good", "good"]
