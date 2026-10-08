"""Resource registry for mcppro.

Tools are actions a caller invokes; resources are addressable things a client
reads. A client browsing a resource tree sees exactly what the server holds,
which is something a tool list cannot express -- especially for data that
exists per-user and changes over time.

Kept deliberately provider- and application-agnostic: the registry knows URIs
and readers, not what any of them mean.
"""
from typing import Callable, Dict, List, Optional

from mcppro.types import (
    MCPResource,
    MCPResourceAnnotations,
    MCPResourceTemplate,
)


class ResourceRegistry:
    """Holds concrete resources, URI templates, and optional prefix readers.

    Three registration styles, because servers legitimately need all three:

    * `add_resource` -- a fixed URI such as ``notes://profile``.
    * `add_template` -- a parameterised URI such as
      ``notes://items/{id}``, advertised via resources/templates/list.
    * `add_prefix_reader` -- a family of URIs whose members are only known at
      request time (a per-user set of records, say). The reader is consulted
      after exact and template lookups both miss.
    """

    def __init__(self):
        self._resources: Dict[str, MCPResource] = {}
        self._readers: Dict[str, Callable] = {}
        self._templates: List[MCPResourceTemplate] = []
        self._prefix_readers: List[tuple] = []   # (prefix, reader, mime_type)
        # Optional hook: called per request to contribute live resources,
        # e.g. one entry per record the caller actually owns.
        self._lister: Optional[Callable] = None

    # --------------------------------------------------
    # Registration
    # --------------------------------------------------

    def add_resource(self, uri: str, reader: Callable, *, name: str = "",
                     description: str = "", mime_type: str = "text/plain",
                     title: Optional[str] = None, size: Optional[int] = None,
                     priority: Optional[float] = None,
                     audience: Optional[List[str]] = None,
                     last_modified: Optional[str] = None,
                     scopes: Optional[List[str]] = None):
        self._resources[uri] = MCPResource(
            uri=uri,
            name=name or uri,
            title=title,
            description=description or None,
            mimeType=mime_type,
            size=size,
            annotations=_annotations(priority, audience, last_modified),
        )
        self._readers[uri] = reader
        self._scopes = getattr(self, "_scopes", {})
        self._scopes[uri] = list(scopes or [])

    def add_template(self, uri_template: str, reader: Callable, *,
                     name: str = "", description: str = "",
                     mime_type: str = "text/plain",
                     title: Optional[str] = None,
                     priority: Optional[float] = None,
                     audience: Optional[List[str]] = None,
                     last_modified: Optional[str] = None,
                     scopes: Optional[List[str]] = None):
        self._templates.append(MCPResourceTemplate(
            uriTemplate=uri_template,
            name=name or uri_template,
            title=title,
            description=description or None,
            mimeType=mime_type,
            annotations=_annotations(priority, audience, last_modified),
        ))
        self._template_readers = getattr(self, "_template_readers", {})
        self._template_readers[uri_template] = reader
        self._template_scopes = getattr(self, "_template_scopes", {})
        self._template_scopes[uri_template] = list(scopes or [])

    def add_prefix_reader(self, prefix: str, reader: Callable, *,
                          mime_type: str = "text/plain",
                          scopes: Optional[List[str]] = None):
        self._prefix_readers.append((prefix, reader, mime_type))
        self._prefix_scopes = getattr(self, "_prefix_scopes", {})
        self._prefix_scopes[prefix] = list(scopes or [])

    def set_lister(self, lister: Callable):
        """Register a hook contributing live resources for the current caller."""
        self._lister = lister

    # --------------------------------------------------
    # Queries
    # --------------------------------------------------

    def has_any(self) -> bool:
        return bool(self._resources or self._templates
                    or self._prefix_readers or self._lister)

    def list_resources(self, user_context: Optional[dict] = None) -> List[MCPResource]:
        out = list(self._resources.values())
        if self._lister is not None:
            try:
                extra = _call(self._lister, user_context)
            except Exception:
                # A lister that fails must not break discovery for the
                # statically-declared resources.
                extra = []
            for item in extra or []:
                out.append(item if isinstance(item, MCPResource)
                           else MCPResource(**item))
        return out

    def list_templates(self) -> List[MCPResourceTemplate]:
        return list(self._templates)

    def required_scopes(self, uri: str) -> List[str]:
        scopes = getattr(self, "_scopes", {})
        if uri in scopes:
            return scopes[uri]
        tscopes = getattr(self, "_template_scopes", {})
        pscopes = getattr(self, "_prefix_scopes", {})
        for template, reader in getattr(self, "_template_readers", {}).items():
            if _matches_template(uri, template):
                return tscopes.get(template, [])
        for prefix, _, _ in self._prefix_readers:
            if uri.startswith(prefix):
                return pscopes.get(prefix, [])
        return []

    def resolve(self, uri: str, user_context: Optional[dict]) -> tuple:
        """Find a reader for `uri`.

        Returns (reader, mime_type). Raises KeyError when nothing matches, so
        the router can answer with the spec's -32002.
        """
        if uri in self._readers:
            return self._readers[uri], self._resources[uri].mimeType or "text/plain"

        for template, reader in getattr(self, "_template_readers", {}).items():
            if _matches_template(uri, template):
                tpl = next((t for t in self._templates
                            if t.uriTemplate == template), None)
                mime = (tpl.mimeType if tpl else None) or "text/plain"
                return reader, mime

        for prefix, reader, mime in self._prefix_readers:
            if uri.startswith(prefix):
                return reader, mime

        raise KeyError(uri)


def _annotations(priority, audience, last_modified):
    if priority is None and audience is None and last_modified is None:
        return None
    return MCPResourceAnnotations(
        audience=list(audience) if audience else None,
        priority=priority,
        lastModified=last_modified,
    )


def _call(fn: Callable, user_context, uri: Optional[str] = None):
    """Invoke a reader/lister, injecting only the arguments it declares.

    Concrete-resource readers take `user`; template and prefix readers take
    `uri` (they resolve the parameters themselves) and optionally `user`.
    Injecting by signature keeps all three styles on one code path.
    """
    import inspect

    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return fn()

    kwargs = {}
    if "user" in params:
        kwargs["user"] = user_context
    if uri is not None and "uri" in params:
        kwargs["uri"] = uri
    return fn(**kwargs)


def _matches_template(uri: str, template: str) -> bool:
    """True when `uri` fills every {placeholder} in `template`.

    Written by hand rather than via str.format so a URI containing braces
    cannot raise, and so placeholders are matched anywhere in the string.
    """
    import re

    pattern = re.escape(template)
    pattern = re.sub(r"\\\{[^{}]+\\\}", r"[^/]+", pattern)
    return re.fullmatch(pattern, uri) is not None
