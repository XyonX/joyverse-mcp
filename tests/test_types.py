"""Unit tests for mcppro/types.py -- pydantic model contracts."""
import pytest
from mcppro.types import (
    MCPContent, MCPToolResult, MCPToolDefinition,
    MCPServerInfo, MCPCapabilities,
)


class TestMCPContent:
    def test_defaults_to_text(self):
        c = MCPContent()
        assert c.type == "text"

    def test_optional_fields_default_none(self):
        c = MCPContent(type="text", text="hi")
        assert c.data is None and c.mimeType is None

    def test_dump_uses_camelCase_mimeType(self):
        assert "mimeType" in MCPContent().model_dump()

    def test_text_only_construction(self):
        assert MCPContent(type="text", text="x").text == "x"


class TestMCPToolResult:
    def test_defaults(self):
        r = MCPToolResult()
        assert r.content == [] and r.isError is False

    def test_content_defaults_are_not_shared(self):
        # pydantic default_factory must give each instance its own list
        a, b = MCPToolResult(), MCPToolResult()
        a.content.append(MCPContent(text="x"))
        assert b.content == []

    def test_error_flag_is_preserved(self):
        assert MCPToolResult(isError=True).isError is True

    def test_is_json_serialisable(self):
        import json
        r = MCPToolResult(content=[MCPContent(type="text", text="hi")])
        json.dumps(r.model_dump())  # must not raise


class TestMCPToolDefinition:
    def test_requires_name(self):
        with pytest.raises(Exception):
            MCPToolDefinition()

    def test_description_and_schema_defaults(self):
        t = MCPToolDefinition(name="x")
        assert t.description == "" and t.inputSchema == {}

    def test_inputSchema_defaults_not_shared(self):
        a, b = MCPToolDefinition(name="a"), MCPToolDefinition(name="b")
        a.inputSchema["x"] = 1
        assert b.inputSchema == {}

    def test_camelCase_key_survives_roundtrip(self):
        t = MCPToolDefinition(name="x", inputSchema={"type": "object"})
        assert t.model_dump()["inputSchema"] == {"type": "object"}


class TestMCPServerInfo:
    def test_name_required(self):
        with pytest.raises(Exception):
            MCPServerInfo()

    def test_version_default(self):
        assert MCPServerInfo(name="s").version == "1.0.0"

    def test_dump(self):
        assert MCPServerInfo(name="s", version="2").model_dump() == {
            "name": "s", "version": "2"}


class TestMCPCapabilities:
    def test_declares_tools(self):
        assert "tools" in MCPCapabilities().model_dump()

    def test_empty_tools_by_default(self):
        assert MCPCapabilities().tools == {}

    def test_defaults_not_shared(self):
        a, b = MCPCapabilities(), MCPCapabilities()
        a.tools["k"] = 1
        assert b.tools == {}
