"""Unit tests for mcppro/decorators.py -- schema inference and registration."""
import typing

import pytest
from mcppro.decorators import (
    infer_input_schema, create_tool_decorator, TYPE_MAP)


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


class TestOptionalResolution:
    """Optional[X] must advertise X's type.

    The original suite enumerated the six types in TYPE_MAP and stopped, so
    every type NOT in the map was assumed to be a string. `Optional[list]` is a
    Union, missed the lookup, and published three real tool params as strings
    (add_to_log.tags, add_to_log.files, get_log.tags) -- caught by an agent,
    not by these tests.
    """

    @pytest.mark.parametrize("hint,json_type", [
        (typing.Optional[list], "array"),
        (typing.Optional[str], "string"),
        (typing.Optional[int], "integer"),
        (typing.Optional[float], "number"),
        (typing.Optional[bool], "boolean"),
        (typing.Optional[dict], "object"),
    ])
    def test_optional_maps_to_inner_type(self, hint, json_type):
        def fn(a: hint):
            ...
        assert infer_input_schema(fn)["properties"]["a"]["type"] == json_type

    def test_pep604_union_syntax(self):
        def fn(a: list | None):
            ...
        assert infer_input_schema(fn)["properties"]["a"]["type"] == "array"

    def test_mixed_union_degrades_to_string(self):
        """str | int has no single honest JSON type; string is safe."""
        def fn(a: typing.Union[str, int]):
            ...
        assert infer_input_schema(fn)["properties"]["a"]["type"] == "string"

    def test_optional_without_default_is_still_required(self):
        def fn(a: typing.Optional[str]):
            ...
        assert infer_input_schema(fn)["required"] == ["a"]

    def test_optional_with_none_default_is_not_required(self):
        def fn(a: typing.Optional[list] = None):
            ...
        assert infer_input_schema(fn)["required"] == []


class TestEveryToolSchemaMatchesItsSignature:
    """Reflection guard over the real registered tools.

    The unit tests above prove the resolver works in isolation; this proves no
    registered tool ships a schema that disagrees with its own signature. It is
    the check that would have caught the Optional[list] bug at the moment the
    tools were written, rather than when an agent called them.
    """

    @pytest.fixture(scope="class")
    def schemas(self):
        import run
        return {s.name: s for s in run.server._tool_schemas}

    def test_no_list_param_is_published_as_a_string(self, schemas):
        """The specific regression: a list-typed param advertised as a string.

        Checked by reflecting on the function rather than hard-coding names, so
        a new Optional[list] param fails here on the day it is added.
        """
        import inspect
        import typing

        import run
        from mcppro.decorators import _unwrap_optional

        bad = []
        for name, schema in schemas.items():
            func = run.server._tool_functions[name]
            hints = typing.get_type_hints(func)
            for param in schema.inputSchema.get("properties", {}):
                hint = _unwrap_optional(hints.get(param))
                origin = typing.get_origin(hint) or hint
                if origin in (list, dict):
                    published = schema.inputSchema["properties"][param]["type"]
                    if published != TYPE_MAP[origin]:
                        bad.append(f"{name}.{param} published {published}, "
                                   f"signature says {origin}")
        assert not bad, bad

    @pytest.mark.parametrize("tool_name,param_name,json_type", [
        ("add_to_log", "tags", "array"),
        ("add_to_log", "files", "array"),
        ("get_log", "tags", "array"),
    ])
    def test_list_params_are_published_as_arrays(
            self, schemas, tool_name, param_name, json_type):
        """The three that shipped wrong. Named so a regression is obvious."""
        spec = schemas[tool_name].inputSchema["properties"][param_name]
        assert spec["type"] == json_type, (
            f"{tool_name}.{param_name} published as {spec['type']}; agents read "
            "this and reject the call")

    def test_schema_matches_live_signature_for_every_tool(self, schemas):
        """Compare the published schema against the function's own hints."""
        import run
        from mcppro.decorators import infer_input_schema

        mismatches = []
        for name, schema in schemas.items():
            func = run.server._tool_functions[name]
            inferred = infer_input_schema(func)
            published = schema.inputSchema.get("properties", {})
            for param, spec in published.items():
                want = inferred["properties"].get(param, {}).get("type")
                if want and spec.get("type") != want:
                    mismatches.append(
                        f"{name}.{param}: published {spec.get('type')}, "
                        f"signature says {want}")
        assert not mismatches, mismatches


class TestParamDocs:
    """Per-parameter descriptions inside the published schema.

    add_to_log's `client` shipped as a bare {"type": "string"} and agents
    guessed its meaning from the conversation -- in agency-flavoured chats
    that guess landed on "who the work is for". A model chooses argument
    values from the schema, so the description belongs there too.
    """

    def test_doc_lands_in_the_property(self):
        def fn(a: str, b: int = 1):
            ...
        schema = infer_input_schema(fn, {"b": "b is the count"})
        assert schema["properties"]["b"]["description"] == "b is the count"
        assert schema["properties"]["b"]["type"] == "integer"
        assert schema["properties"]["a"] == {"type": "string"}

    def test_omitted_docs_change_nothing(self):
        def fn(a: str):
            ...
        assert infer_input_schema(fn)["properties"] == {"a": {"type": "string"}}

    def test_doc_for_an_absent_param_is_ignored(self):
        def fn(a: str):
            ...
        schema = infer_input_schema(fn, {"nope": "ghost param"})
        assert "nope" not in schema["properties"]

    def test_doc_on_an_any_typed_param_keeps_the_empty_schema(self):
        def fn(a: typing.Any = None):
            ...
        schema = infer_input_schema(fn, {"a": "whatever"})
        assert schema["properties"]["a"] == {"description": "whatever"}

    def test_decorator_publishes_param_docs(self):
        server = FakeServer()
        tool = create_tool_decorator(server)

        @tool(description="d", param_docs={"a": "a doc"})
        def fn(a: str):
            ...

        prop = server._tool_schemas[0].inputSchema["properties"]["a"]
        assert prop == {"type": "string", "description": "a doc"}


class TestRealToolsUseParamDocs:
    """Drift guard: the tool whose bug introduced param_docs must keep
    documenting `client`, or the fix quietly disappears while the framework
    keeps passing the tests above.
    """

    @pytest.fixture(scope="class")
    def schemas(self):
        import run
        return {s.name: s for s in run.server._tool_schemas}

    def test_add_to_log_client_is_documented_in_the_schema(self, schemas):
        prop = schemas["add_to_log"].inputSchema["properties"]["client"]
        assert prop["type"] == "string"
        doc = prop["description"].lower()
        assert "not the project" in doc
        assert "tags" in doc
        assert "list_clients" in doc
