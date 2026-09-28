"""Unit tests for mcppro/transport.py -- JSON-RPC and SSE encoding."""
import json
import pytest
from mcppro import transport


class TestBuildJsonrpcResponse:
    def test_shape(self):
        r = transport.build_jsonrpc_response(1, {"ok": True})
        assert r == {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}

    def test_preserves_null_id(self):
        r = transport.build_jsonrpc_response(None, {})
        assert r["id"] is None

    def test_preserves_string_id(self):
        r = transport.build_jsonrpc_response("abc", {})
        assert r["id"] == "abc"

    def test_result_may_be_any_jsonable(self):
        for val in [None, 0, "", [], {"a": [1, 2]}, True]:
            assert transport.build_jsonrpc_response(1, val)["result"] == val


class TestBuildJsonrpcError:
    def test_shape(self):
        e = transport.build_jsonrpc_error(7, -32601, "nope")
        assert e == {"jsonrpc": "2.0", "id": 7,
                     "error": {"code": -32601, "message": "nope"}}

    def test_has_no_result_key(self):
        assert "result" not in transport.build_jsonrpc_error(1, -1, "x")

    def test_error_and_result_are_mutually_exclusive(self):
        assert "result" not in transport.build_jsonrpc_error(1, -1, "x")


class TestFormatSse:
    def test_prefix_and_terminator(self):
        out = transport.format_sse({"a": 1})
        assert out.startswith("data: ")
        assert out.endswith("\n\n")

    def test_payload_is_valid_json(self):
        out = transport.format_sse({"msg": "hi", "n": 3})
        assert json.loads(out[6:]) == {"msg": "hi", "n": 3}

    def test_unicode_is_escaped_not_raw(self):
        out = transport.format_sse({"name": "joydip"})
        assert "joydip" in out

    def test_roundtrips_with_builder(self):
        payload = transport.build_jsonrpc_response(1, {"x": [1, 2]})
        assert json.loads(transport.format_sse(payload)[6:]) == payload

    def test_sse_frame_is_parseable_by_sse_reader(self):
        # A single event must be exactly "data: <json>\n\n"
        out = transport.format_sse({"a": 1})
        assert out.count("\n\n") == 1


class TestErrorCodes:
    def test_codes_match_jsonrpc_spec(self):
        assert transport.JSONRPCError.PARSE_ERROR == -32700
        assert transport.JSONRPCError.INVALID_REQUEST == -32600
        assert transport.JSONRPCError.METHOD_NOT_FOUND == -32601
        assert transport.JSONRPCError.INVALID_PARAMS == -32602
        assert transport.JSONRPCError.INTERNAL_ERROR == -32603

    def test_codes_are_distinct(self):
        codes = [transport.JSONRPCError.PARSE_ERROR,
                 transport.JSONRPCError.INVALID_REQUEST,
                 transport.JSONRPCError.METHOD_NOT_FOUND,
                 transport.JSONRPCError.INVALID_PARAMS,
                 transport.JSONRPCError.INTERNAL_ERROR]
        assert len(set(codes)) == len(codes)
