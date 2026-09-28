"""Unit tests for mcppro/decorators.py -- schema inference and registration."""
import pytest
from mcppro.decorators import infer_input_schema, create_tool_decorator


class FakeServer:
    def __init__(self):
        self._tool_functions = {}
        self._tool_schemas = []

    def tool(self, description=""):
        return create_tool_decorator(self)(description)


class TestTypeMapping:
    @pytest.mark.parametrize("py_type,json_type", [
        (int, "integer"), (float, "number"), (str, "string"),
        (bool, "boolean"), (list, "array"), (dict, "object"),
    ])
    def test_primitive_types_map_correctly(self, py_type, json_type):
        def fn(a: py_type):
            ...
        assert infer_input_schema(fn)["properties"]["a"]["type"] == json_type

    def test_unresolvable_annotation_degrades_to_empty_schema(self):
        # A forward reference (or any annotation get_type_hints cannot resolve)
        # makes the whole call raise, and infer_input_schema falls back to an
        # empty schema rather than guessing. That is lossy but safe: the tool
        # still registers, it just advertises no parameters.
        def fn(a: "SomeCustomType"):
            ...
        assert infer_input_schema(fn) == {
            "type": "object", "properties": {}, "required": []}

    def test_untyped_param_defaults_to_string(self):
        def fn(a):
            ...
        assert infer_input_schema(fn)["properties"]["a"]["type"] == "string"


class TestRequiredInference:
    def test_param_without_default_is_required(self):
        def fn(a: str):
            ...
        assert infer_input_schema(fn)["required"] == ["a"]

    def test_param_with_default_is_optional(self):
        def fn(a: str = "x"):
            ...
        assert infer_input_schema(fn)["required"] == []

    def test_mixed_required_and_optional(self):
        def fn(a: str, b: int = 1, c: bool = False):
            ...
        assert infer_input_schema(fn)["required"] == ["a"]

    def test_ordering_is_preserved(self):
        def fn(z: str, a: str, m: str):
            ...
        assert infer_input_schema(fn)["required"] == ["z", "a", "m"]


class TestInjectedParamExclusion:
    def test_user_param_is_excluded(self):
        # `user` is injected by the router, never supplied by the client
        def fn(a: str, user: dict):
            ...
        schema = infer_input_schema(fn)
        assert "user" not in schema["properties"]
        assert "user" not in schema["required"]

    def test_user_only_function_yields_empty_schema(self):
        def fn(user: dict):
            ...
        assert infer_input_schema(fn) == {
            "type": "object", "properties": {}, "required": []}

    def test_self_is_excluded(self):
        class C:
            def fn(self, a: str):
                ...
        assert "self" not in infer_input_schema(C.fn)["properties"]

    def test_cls_is_excluded(self):
        def fn(cls, a: str):
            ...
        assert "cls" not in infer_input_schema(fn)["properties"]


class TestSchemaShape:
    def test_envelope_is_always_object(self):
        def fn():
            ...
        s = infer_input_schema(fn)
        assert s["type"] == "object"
        assert s["properties"] == {}
        assert s["required"] == []

    def test_result_is_json_serialisable(self):
        import json
        def fn(a: str, user: dict, b: int = 2):
            ...
        json.dumps(infer_input_schema(fn))


class TestRegistration:
    def test_registers_function_and_schema(self):
        srv = FakeServer()

        @srv.tool(description="does a thing")
        def my_tool(x: str):
            return "ok"

        assert "my_tool" in srv._tool_functions
        assert srv._tool_functions["my_tool"] is my_tool
        assert srv._tool_schemas[0].name == "my_tool"
        assert srv._tool_schemas[0].description == "does a thing"

    def test_decorator_returns_original_function(self):
        srv = FakeServer()

        @srv.tool()
        def my_tool():
            return 42

        assert my_tool() == 42

    def test_description_falls_back_to_docstring(self):
        srv = FakeServer()

        @srv.tool()
        def my_tool():
            """Docstring description."""
            return 1

        assert srv._tool_schemas[0].description == "Docstring description."

    def test_explicit_description_wins_over_docstring(self):
        srv = FakeServer()

        @srv.tool(description="explicit")
        def my_tool():
            """docstring"""
            return 1

        assert srv._tool_schemas[0].description == "explicit"

    def test_empty_description_and_no_docstring(self):
        srv = FakeServer()

        @srv.tool()
        def my_tool():
            return 1

        assert srv._tool_schemas[0].description == ""

    def test_multiple_tools_register_independently(self):
        srv = FakeServer()

        @srv.tool(description="a")
        def tool_a():
            ...

        @srv.tool(description="b")
        def tool_b():
            ...

        assert len(srv._tool_schemas) == 2
        assert set(srv._tool_functions) == {"tool_a", "tool_b"}

    def test_schemas_are_not_shared_between_instances(self):
        a, b = FakeServer(), FakeServer()

        @a.tool()
        def t():
            ...

        assert b._tool_schemas == []
