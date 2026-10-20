"""Regression for r-joyverse-editdata-schema-bug (reported by Tony, Oct 2026)
+ PR #5 review findings (segment validation, malformed dotted paths).

edit_data's inputSchema advertises `path` as an ARRAY (Optional[list]), but
_walk() did str(path).split('.') — a client following the advertised schema
sent path=["items"] and hit "'['items']' is not a list" because the stringified
array "['items']" was used as a dict key. The fix normalizes BOTH forms:
the advertised array AND the legacy dotted string.

Contract proven here:
  1. array path behaves identically to the equivalent dotted-string path
  2. dotted string still works (Tony's in-the-wild workaround keeps working)
  3. match-based set/add/remove ops work through an array path
  4. None/""/[] addresses the document root — the ONLY root forms
  5. malformed paths are REJECTED with ValueError, never silently retargeted:
     segments None/"" (list form), "." / "a..b" (dotted form), and paths that
     end inside a non-object all raise; edit_data surfaces them as clean
     JSON errors instead of crashing or writing to the wrong node.
"""
import json

import pytest

from joyverse.data import _walk, edit_data

UID = "u_a1b2c3d4e5f6"


@pytest.fixture
def doc():
    return {
        "purpose": "test doc",
        "items": [
            {"id": "a", "name": "Alpha", "on": True},
            {"id": "b", "name": "Beta", "on": True},
        ],
    }


class TestWalkAcceptsArrayPath:
    def test_array_path_equivalent_to_dotted(self, doc):
        assert _walk(doc, ["items"]) == _walk(doc, "items")
        # nested
        d1 = {"a": {"b": {"c": 1}}}
        assert _walk(d1, ["a", "b"]) == _walk(d1, "a.b")

    def test_array_path_resolves_the_advertised_schema_form(self, doc):
        # the exact form from the bug report: path=["items"], match={...}
        parent, last = _walk(doc, ["items"])
        assert last == "items"
        assert isinstance(parent.get(last), list)
        assert parent[last][0]["id"] == "a"

    def test_nested_array_path_preserved(self, doc):
        d1 = {"a": {"b": {"c": 1}}}
        parent, last = _walk(d1, ["a", "b"])
        # _walk returns the PARENT container and the final key
        assert parent == {"b": {"c": 1}}
        assert last == "b"
        assert parent[last] == {"c": 1}

    def test_none_and_empty_paths_are_the_root(self, doc):
        assert _walk(doc, None) == (doc, None)
        assert _walk(doc, "") == (doc, None)
        assert _walk(doc, []) == (doc, None)

    def test_stringified_array_is_not_used_as_a_key(self, doc):
        # pre-fix behaviour: "['items']" became a literal key, then ops
        # failed with "'['items']' is not a list". After the fix, no
        # such key may exist after an array-path walk.
        _walk(doc, ["items"])
        assert "['items']" not in doc
        assert "['items']" not in doc.get("items", {})

    def test_bad_type_raises_clearly(self, doc):
        with pytest.raises(ValueError):
            _walk(doc, 5)

    def test_non_object_traversal_raises(self, doc):
        with pytest.raises(ValueError):
            _walk(doc, "items.0.on")  # array element is not a dict traversal


class TestWalkRejectsMalformedPaths:
    """PR #5 review P1+P2: invalid input must raise, never silently
    drop segments (which would retarget the write) or crash with IndexError."""

    def test_none_segment_in_list_rejected(self, doc):
        with pytest.raises(ValueError):
            _walk(doc, ["items", None])

    def test_empty_string_segment_in_list_rejected(self, doc):
        with pytest.raises(ValueError):
            _walk(doc, ["items", ""])

    def test_dot_only_rejected_not_indexerror(self, doc):
        # pre-fix: "." -> parts=[] -> parts[-1] raised IndexError, which
        # edit_data does not catch (it only catches ValueError)
        with pytest.raises(ValueError):
            _walk(doc, ".")

    def test_dotted_empty_segment_rejected(self, doc):
        for bad in ("items.", ".items", "a..b", ".."):
            with pytest.raises(ValueError):
                _walk(doc, bad)

    def test_empty_tuple_rejected_not_indexerror(self, doc):
        # review round 2: () is falsy so the old caller guard skipped _walk,
        # and reaching _walk directly hit parts[-1] IndexError because ()
        # is neither None/""/[] nor a non-empty sequence.
        with pytest.raises(ValueError):
            _walk(doc, ())

    def test_numeric_segments_are_stringified_not_rejected(self, doc):
        # numbers are valid segments (stringified to dict keys); only
        # None/"" are invalid
        d1 = {"layers": {"3": {"n": 1}}}
        parent, last = _walk(d1, ["layers", 3])
        assert parent == {"3": {"n": 1}}
        assert last == "3"          # stringified, not rejected
        assert parent[last] == {"n": 1}
        # dotted-string equivalent resolves identically
        assert _walk(d1, "layers.3") == (parent, "3")

    def test_path_ending_inside_a_list_rejected(self, doc):
        # ["items","rules"] where items is a LIST: the final parent is the
        # list, not a dict — pre-fix this raised AttributeError on .get();
        # the contract is a clear ValueError.
        with pytest.raises(ValueError):
            _walk(doc, ["items", "rules"])
        with pytest.raises(ValueError):
            _walk(doc, "items.rules")


class TestEditDataThroughArrayPath:
    """Full edit_data contract through the advertised array form.

    Uses monkeypatched storage so no real R2/S3 is touched.
    """

    @pytest.fixture
    def stored(self, monkeypatch, doc):
        from joyverse import data as data_mod
        monkeypatch.setattr(data_mod, "_snapshot", lambda *a, **k: None)
        # edit_data reads via r2_client.get_object (key from get_data_key);
        # patch the client so no real S3 call ever happens
        class FakeBody:
            def __init__(self, raw): self.raw = raw
            def read(self): return self.raw
        class FakeR2:
            def __init__(self): self.raw = json.dumps(doc).encode()
            def get_object(self, Bucket=None, Key=None):
                if self.raw is None:
                    raise Exception("NoSuchKey")
                return {"Body": FakeBody(self.raw)}
            def put_object(self, Bucket=None, Key=None, Body=None, **kw):
                if isinstance(Body, bytes):
                    self.raw = Body
                elif Body is not None:
                    self.raw = Body.encode()
                return {}
        fake = FakeR2()
        monkeypatch.setattr(data_mod, "r2_client", fake)
        return fake

    def test_set_with_match_via_array_path(self, stored):
        out = edit_data(topic="t", op="set", value={"on": False},
                        path=["items"], match={"id": "a"},
                        user={"user_id": UID})
        res = json.loads(out)
        assert res.get("ok") is True
        saved_doc = json.loads(stored.raw.decode())
        assert saved_doc["items"][0]["on"] is False
        # sibling untouched — both started True (review: start the sibling
        # True so "still True" is a real assertion, not a tautology)
        assert saved_doc["items"][1]["on"] is True

    def test_dotted_string_still_works(self, stored):
        # Tony's in-the-wild workaround must not regress
        out = edit_data(topic="t", op="set", value={"on": False},
                        path="items", match={"id": "b"},
                        user={"user_id": UID})
        assert json.loads(out).get("ok") is True
        saved_doc = json.loads(stored.raw.decode())
        assert saved_doc["items"][1]["on"] is False
        assert saved_doc["items"][0]["on"] is True  # sibling untouched

    def test_add_via_array_path(self, stored):
        out = edit_data(topic="t", op="add", value={"id": "c", "name": "Gamma"},
                        path=["items"], user={"user_id": UID})
        assert json.loads(out).get("ok") is True
        saved_doc = json.loads(stored.raw.decode())
        assert [i["id"] for i in saved_doc["items"]] == ["a", "b", "c"]

    def test_remove_via_array_path(self, stored):
        out = edit_data(topic="t", op="remove", match={"id": "a"},
                        path=["items"], user={"user_id": UID})
        assert json.loads(out).get("ok") is True
        saved_doc = json.loads(stored.raw.decode())
        assert [i["id"] for i in saved_doc["items"]] == ["b"]

    def test_root_merge_without_path(self, stored):
        out = edit_data(topic="t", op="set", value={"purpose": "changed"},
                        user={"user_id": UID})
        assert json.loads(out).get("ok") is True
        saved_doc = json.loads(stored.raw.decode())
        assert saved_doc["purpose"] == "changed"
        assert len(saved_doc["items"]) == 2  # nothing dropped

    def test_malformed_path_returns_json_error_not_crash(self, stored):
        # edit_data must surface _walk's ValueError as a JSON error object —
        # never an IndexError/AttributeError leaking a 500 to the client
        out = edit_data(topic="t", op="set", value={"on": False},
                        path=["items", None], match={"id": "a"},
                        user={"user_id": UID})
        res = json.loads(out)
        assert res.get("ok") is None
        assert isinstance(res.get("error"), str)
        assert "segment" in res["error"].lower() or "path" in res["error"].lower()

    def test_dot_path_returns_json_error_not_crash(self, stored):
        out = edit_data(topic="t", op="set", value={"purpose": "x"},
                        path=".", user={"user_id": UID})
        res = json.loads(out)
        assert res.get("ok") is None
        assert isinstance(res.get("error"), str)

    def test_falsy_invalid_types_reach_validation_not_root_edit(self, stored):
        """Review round 2, finding 1: `if path else (doc, None)` in the
        caller meant path=0/False/() bypassed _walk entirely and were
        treated as root-level edits with ok:true. Falsy-but-invalid must
        reach _walk's validation and be rejected."""
        seed = json.loads(stored.raw.decode())
        for bad in (0, False, (), 0.0):
            stored.raw = json.dumps(seed).encode()
            out = edit_data(topic="t", op="set", value={"injected": True},
                            path=bad, user={"user_id": UID})
            res = json.loads(out)
            assert res.get("ok") is None, f"path={bad!r} returned ok:true"
            assert isinstance(res.get("error"), str), \
                f"path={bad!r} did not produce an error"
            # load-bearing: the write must NOT have happened at all
            assert "injected" not in json.loads(stored.raw.decode()), \
                f"path={bad!r} silently performed a root-level edit"

    def test_root_forms_still_merge_at_root(self, stored):
        """The intended root forms (None, omitted, [], "") keep working —
        validation tightening must not break the legitimate contract."""
        seed = {"purpose": "test", "items": [{"id": "a", "on": True}]}
        for root in (None, [], ""):
            stored.raw = json.dumps(seed).encode()
            out = edit_data(topic="t", op="set", value={"purpose": "ok"},
                            path=root, user={"user_id": UID})
            assert json.loads(out).get("ok") is True, f"path={root!r}"
            saved = json.loads(stored.raw.decode())
            assert saved["purpose"] == "ok"
            assert len(saved["items"]) == 1  # nothing dropped

    def test_match_without_path_is_a_clean_error(self, stored):
        """Root + match previously produced "'None' is not a list"; now it
        is an explicit 'match needs a path' error, still no write."""
        seed = json.loads(stored.raw.decode())
        stored.raw = json.dumps(seed).encode()
        out = edit_data(topic="t", op="set", value={"on": False},
                        match={"id": "a"}, user={"user_id": UID})
        res = json.loads(out)
        assert res.get("ok") is None
        assert "path" in res["error"]
        assert json.loads(stored.raw.decode()) == seed, \
            "document changed on a rejected call"

    def test_append_without_path_needs_a_path_error_not_null_key(self, stored):
        """Root + append previously created a literal None key ("null" in
        JSON) and returned ok:true — silent corruption. Now a clean error
        and the stored document untouched."""
        seed = json.loads(stored.raw.decode())
        stored.raw = json.dumps(seed).encode()
        out = edit_data(topic="t", op="append", value=["x"],
                        user={"user_id": UID})
        res = json.loads(out)
        assert res.get("ok") is None
        assert "path" in res["error"]
        saved = json.loads(stored.raw.decode())
        assert "null" not in {k if isinstance(k, str) else "null"
                              for k in saved}, "a None key was written"
        assert saved == seed, "document changed on a rejected call"
