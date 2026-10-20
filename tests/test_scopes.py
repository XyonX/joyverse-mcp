"""Unit tests for mcppro/scopes.py -- scope parsing and enforcement."""
import pytest
from fastapi import HTTPException

from mcppro.scopes import (
    parse_scopes,
    has_scopes,
    missing_scopes,
    user_scopes,
    require_scopes,
)


class TestParseScopes:
    def test_space_delimited_string_is_split(self):
        assert parse_scopes("read write admin") == ["read", "write", "admin"]

    def test_single_scope(self):
        assert parse_scopes("read") == ["read"]

    def test_list_input_passes_through(self):
        assert parse_scopes(["read", "write"]) == ["read", "write"]

    def test_none_yields_empty(self):
        assert parse_scopes(None) == []

    def test_empty_string_yields_empty(self):
        assert parse_scopes("") == []

    def test_duplicates_are_collapsed(self):
        # a token claiming "read read write" must not satisfy a requirement
        # twice, nor let a caller pad a scope list
        assert parse_scopes("read read write") == ["read", "write"]

    def test_blank_entries_are_dropped(self):
        # "a  b" would otherwise yield a scope literally named ""
        assert parse_scopes("a  b") == ["a", "b"]

    def test_tabs_are_not_a_delimiter(self):
        # only the space character separates scopes, so a tab stays inside
        # the scope name rather than splitting it into two
        assert parse_scopes("a\tb") == ["a\tb"]

    def test_order_is_preserved(self):
        assert parse_scopes("z a m") == ["z", "a", "m"]

    def test_scope_names_are_case_sensitive(self):
        # RFC 6749 s3.3: scopes are case-sensitive, so Read != read
        assert parse_scopes("Read") != parse_scopes("read")

    def test_non_string_entries_are_skipped(self):
        assert parse_scopes(["read", 5, None, "write"]) == ["read", "write"]


class TestHasScopes:
    def test_all_present(self):
        assert has_scopes(["read", "write"], ["read"]) is True

    def test_one_missing(self):
        assert has_scopes(["read"], ["read", "write"]) is False

    def test_empty_required_is_satisfied(self):
        assert has_scopes([], []) is True

    def test_empty_granted_fails_requirement(self):
        # fails closed: no scopes must not mean "everything allowed"
        assert has_scopes([], ["read"]) is False


class TestMissingScopes:
    def test_returns_only_the_gaps(self):
        assert missing_scopes(["read"], ["read", "write"]) == ["write"]

    def test_preserves_required_order(self):
        assert missing_scopes([], ["a", "b"]) == ["a", "b"]

    def test_none_when_all_present(self):
        assert missing_scopes(["a", "b"], ["a"]) == []


class TestUserScopes:
    def test_reads_from_context(self):
        assert user_scopes({"scopes": "read write"}) == ["read", "write"]

    def test_empty_context_is_safe(self):
        assert user_scopes({}) == []

    def test_none_context_is_safe(self):
        assert user_scopes(None) == []

    def test_missing_key_is_safe(self):
        assert user_scopes({"other": 1}) == []


class TestRequireScopes:
    def test_no_requirement_always_passes(self):
        assert require_scopes({}, []) is None

    def test_satisfied_requirement_passes(self):
        assert require_scopes({"scopes": ["read"]}, ["read"]) is None

    def test_unsatisfied_requirement_is_403(self):
        with pytest.raises(HTTPException) as e:
            require_scopes({"scopes": ["read"]}, ["write"])
        assert e.value.status_code == 403

    def test_empty_context_fails_closed(self):
        # regression guard: a context with no scopes must be denied, not
        # treated as an unrestricted caller
        with pytest.raises(HTTPException) as e:
            require_scopes({}, ["read"])
        assert e.value.status_code == 403

    def test_detail_names_the_missing_scope(self):
        with pytest.raises(HTTPException) as e:
            require_scopes({"scopes": []}, ["admin"])
        assert "admin" in e.value.detail

    def test_challenge_advertises_required_scope(self):
        with pytest.raises(HTTPException) as e:
            require_scopes({}, ["joyverse:write"])
        header = e.value.headers["WWW-Authenticate"]
        assert 'error="insufficient_scope"' in header
        assert 'scope="joyverse:write"' in header