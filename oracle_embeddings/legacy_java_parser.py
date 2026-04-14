"""Regex-based Java parser for legacy Spring + MyBatis projects.

Extracts per-class metadata (package, imports, stereotype, @RequestMapping,
@Autowired fields, endpoints, RFC calls) without a full AST, so the analyzer
can walk Controller → Service → Mapper chains and match URLs against a DB
menu table. Encoding-safe for EUC-KR/CP949 legacy sources via _read_file_safe.
"""

import logging
import os
import re

from .mybatis_parser import _read_file_safe

logger = logging.getLogger(__name__)


SKIP_DIRS = {"target", "build", ".git", ".gradle", ".idea", "bin", "out"}


def scan_java_dir(base_dir: str) -> list[str]:
    """Walk ``base_dir`` and return absolute paths of all ``.java`` files.

    Skips typical build/test output directories and any path whose segment
    matches ``SKIP_DIRS`` (case-insensitive).
    """
    java_files = []
    for root, dirs, files in os.walk(base_dir):
        dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS]
        for f in files:
            if f.endswith(".java"):
                java_files.append(os.path.join(root, f))
    logger.info("Found %d java files in %s", len(java_files), base_dir)
    return java_files


# Strip single-line // comments and block /* */ comments but keep strings
# (RFC detection relies on string literals, so we only strip comments).
_COMMENT_LINE = re.compile(r"//[^\n]*")
_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.DOTALL)


def _strip_comments(src: str) -> str:
    src = _COMMENT_BLOCK.sub(" ", src)
    src = _COMMENT_LINE.sub(" ", src)
    return src


_PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.MULTILINE)
_IMPORT_RE = re.compile(r"^\s*import\s+(static\s+)?([\w.]+(?:\.\*)?)\s*;", re.MULTILINE)

# @RequestMapping(value="/x")  /  @RequestMapping("/x")  /  @RequestMapping(path="/x")
# Also handles @RequestMapping({"/a","/b"}) — we return the first path only
# at class level; the caller may expand per-method as needed.
_MAPPING_PATH_RE = re.compile(
    r"""@(?P<ann>RequestMapping|GetMapping|PostMapping|PutMapping|DeleteMapping|PatchMapping)
        \s*\(\s*
        (?:
            (?:value|path)\s*=\s*
        )?
        (?:
            \{\s*(?P<list>[^}]*)\}   # array form
          | "(?P<single>[^"]*)"       # single string
        )
        [^)]*\)
    """,
    re.VERBOSE,
)

# Class / interface declarations. The ``extends`` and ``implements``
# clauses can contain nested generics (``Map<String, List<User>>``), FQCNs
# (``io.vertx.core.AbstractVerticle``), and line breaks. We allow one
# level of nested ``<...>`` and accept any characters that are plausibly
# part of a type list up to the opening ``{``.
#
# We also allow generics on the class name itself
# (``public class Foo<T extends BaseEntity>``).
_TYPE_LIST = r"[\w.][\w.\s,<>\?\&]*?"
_CLASS_DECL_RE = re.compile(
    r"""(?:public\s+|protected\s+|private\s+)?
        (?P<mod>abstract\s+|final\s+)?
        class\s+(?P<name>\w+)
        (?:\s*<[^{]*?>)?                                 # class-name generics
        (?:\s+extends\s+(?P<parent>""" + _TYPE_LIST + r"""))?
        (?:\s+implements\s+(?P<impls>""" + _TYPE_LIST + r"""))?
        \s*\{
    """,
    re.VERBOSE | re.MULTILINE | re.DOTALL,
)

_INTERFACE_DECL_RE = re.compile(
    r"""(?:public\s+|protected\s+|private\s+)?
        interface\s+(?P<name>\w+)
        (?:\s*<[^{]*?>)?
        (?:\s+extends\s+(?P<parent>""" + _TYPE_LIST + r"""))?
        \s*\{
    """,
    re.VERBOSE | re.MULTILINE | re.DOTALL,
)

# Stereotype annotations (searched for independently in the pre-class window)
_STEREOTYPE_RE = re.compile(
    r"@(Controller|RestController|Service|Component|Repository|Mapper)\b"
)

# @Autowired private OrderService orderService;  (also @Resource, @Inject)
_AUTOWIRED_FIELD_RE = re.compile(
    r"""@(?:Autowired|Resource|Inject)\b[^\n]*\n\s*
        (?:private|protected|public)?\s*
        (?:final\s+)?
        (?P<type>[\w.]+)(?:\s*<[^>]*>)?\s+
        (?P<name>\w+)\s*[=;]
    """,
    re.VERBOSE,
)

# private final OrderService orderService; (Lombok @RequiredArgsConstructor)
_FINAL_FIELD_RE = re.compile(
    r"""^\s*(?:private|protected)\s+final\s+
        (?P<type>[\w.]+)(?:\s*<[^>]*>)?\s+
        (?P<name>\w+)\s*;
    """,
    re.VERBOSE | re.MULTILINE,
)

# Constructor injection: public ClassName(OrderService orderService, UserService userService)
_CONSTRUCTOR_RE = re.compile(
    r"""public\s+(?P<cls>\w+)\s*\(
        (?P<params>[^)]*)
        \)\s*\{
    """,
    re.VERBOSE,
)

_PARAM_RE = re.compile(
    r"(?:final\s+)?(?:@\w+\s+)*(?P<type>[\w.]+)(?:\s*<[^>]*>)?\s+(?P<name>\w+)"
)

# Method-level mapping annotations. We use a two-step approach: first find
# the annotation (optional arg list, for bare ``@PostMapping`` on a create
# endpoint), then look forward for the first real method signature
# (``word(`` that isn't a Java keyword).
_METHOD_ANNOTATION_RE = re.compile(
    r"@(?P<ann>RequestMapping|GetMapping|PostMapping|PutMapping|DeleteMapping|PatchMapping)"
    r"(?:\s*\(\s*(?P<args>(?:[^()]|\([^()]*\))*)\))?"
    r"(?=\s)"  # require whitespace after (so we don't match e.g. @GetMappingX)
)

_JAVA_METHOD_SIG_RE = re.compile(r"\b(?P<name>\w+)\s*\(")

_METHOD_KEYWORDS = {
    "public", "protected", "private", "static", "final", "abstract",
    "synchronized", "native", "transient", "volatile", "strictfp",
    "if", "while", "for", "switch", "return", "new", "try", "catch",
    "throw", "throws", "do", "else", "case", "default", "class",
    "interface", "enum", "extends", "implements", "package", "import",
}

_REQUEST_METHOD_RE = re.compile(r"RequestMethod\.(\w+)")

# Vert.x routing DSL patterns:
#   router.get("/order/list").handler(...)
#   router.post("/order/save").handler(this::save)
#   router.route("/any/path").handler(...)                  ← ANY method
#   router.route().path("/any").method(HttpMethod.GET)...   ← chained form
#
# We REQUIRE a ``.handler(...)`` call to follow the route definition. This
# is what distinguishes a real Vert.x route setup from ordinary method
# calls that happen to look similar (``map.get("key")`` /
# ``config.get("timeout")`` / ``request.get("/api/path")``). Without the
# ``.handler`` anchor the regex produces an unacceptable number of false
# positives on large legacy codebases.
_VERTX_ROUTE_LITERAL_RE = re.compile(
    r"""\b\w+\.(?P<method>get|post|put|delete|patch|options|head|route)
        \s*\(\s*"(?P<path>[^"]+)"\s*\)
        \s*(?://[^\n]*\n\s*)*        # optional line comments between calls
        \.\s*handler\s*\(\s*
        (?:
            this\s*::\s*(?P<this_ref>\w+)
          | (?P<cls_simple>[A-Z]\w*)\s*::\s*(?P<cls_ref>\w+)
          | (?P<lambda_var>\w+)\s*->
          | new\s+(?P<inline_cls>[A-Z]\w*)
        )?
    """,
    re.VERBOSE | re.DOTALL,
)
_VERTX_ROUTE_CHAIN_RE = re.compile(
    r"""\b\w+\.route\s*\(\s*\)
        (?P<chain>(?:\s*\.\s*\w+\s*\([^)]*\))+?\s*\.\s*handler\s*\([^)]*\))
    """,
    re.VERBOSE,
)
_VERTX_CHAIN_PATH_RE = re.compile(r'\.path\s*\(\s*"([^"]+)"\s*\)')
_VERTX_CHAIN_METHOD_RE = re.compile(r"\.method\s*\(\s*HttpMethod\.(\w+)\s*\)")

# Custom Vert.x project annotation used as a one-class-per-endpoint
# pattern. Example:
#
#   @RestVerticle(url = "/api/order/list", isAuth = true)
#   public class OrderListHandler { ... }
#
# URL is mandatory; method defaults to ``ANY`` if absent. Additional
# attributes (``isAuth``, ``role``, …) are ignored — only ``url`` /
# ``method`` drive the endpoint.
_REST_VERTICLE_ANNO_RE = re.compile(
    r"@RestVerticle\s*\((?P<args>(?:[^()]|\([^()]*\))*)\)"
)
_REST_VERTICLE_URL_RE = re.compile(r'\burl\s*=\s*"([^"]+)"')
_REST_VERTICLE_METHOD_RE = re.compile(r'\bmethod\s*=\s*(?:HttpMethod\.)?(\w+)')


def _extract_rest_verticle(content: str, class_start: int) -> dict | None:
    """Look for ``@RestVerticle`` just above the class and return its attrs.

    Returns ``{"url": str, "method": str}`` if found, otherwise ``None``.
    Scans a generous window so that other annotations stacked on top of
    ``@RestVerticle`` don't hide it.
    """
    window_start = max(0, class_start - 1500)
    window = content[window_start:class_start]
    m = _REST_VERTICLE_ANNO_RE.search(window)
    if not m:
        return None
    args = m.group("args") or ""
    url_m = _REST_VERTICLE_URL_RE.search(args)
    if not url_m:
        return None
    method_m = _REST_VERTICLE_METHOD_RE.search(args)
    return {
        "url": url_m.group(1),
        "method": method_m.group(1).upper() if method_m else "ANY",
    }


# Vert.x stereotype is detected by class inheritance rather than annotation.
# The direct Vert.x base is ``AbstractVerticle``, but real projects often
# wrap this in a project-local base (``BaseVerticle``, ``ReactiveVerticle``,
# ``RouterVerticle`` etc.). Matching any simple type ending in ``Verticle``
# covers the common 90% without pulling in a type-resolution phase.
_VERTX_KNOWN_BASES = {"AbstractVerticle", "Verticle", "Routable"}


def _is_verticle_base(name: str) -> bool:
    """Return True if ``name`` looks like a Verticle base class name."""
    if not name:
        return False
    simple = name.rsplit(".", 1)[-1]
    if simple in _VERTX_KNOWN_BASES:
        return True
    return simple.endswith("Verticle")


# RFC patterns. Supports both forms:
#   * SAP JCo standard: `destination.getFunction("Z_NAME")`
#   * Project-local util:  `JCoUtil.getCoFunction("Z_NAME")`
# (The receiver name doesn't matter — we match the final method call.)
_RFC_GETFUNCTION_STR_RE = re.compile(
    r'\.(?:getFunction|getCoFunction)\s*\(\s*"([^"]+)"\s*\)'
)
_RFC_GETFUNCTION_VAR_RE = re.compile(
    r"\.(?:getFunction|getCoFunction)\s*\(\s*(\w+)\s*\)"
)
_RFC_CONST_RE = re.compile(
    r"""(?:public\s+|private\s+|protected\s+)?
        (?:static\s+)?(?:final\s+)?
        String\s+(\w+)\s*=\s*"([^"]+)"\s*;
    """,
    re.VERBOSE,
)


_ANNOTATION_TO_HTTP = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH",
}


def _parse_mapping_paths(args: str) -> list[str]:
    """Given the arg list of a mapping annotation, return the list of paths.

    Handles: ``("/x")`` / ``(value="/x")`` / ``({"/a","/b"})`` / ``(path={"/a"})``.

    IMPORTANT: Java string literals can contain ``{...}`` path variables, so
    we must detect array form by the presence of ``{`` *outside* strings. We
    do that by stripping string contents first before testing for the array
    brace.
    """
    if args is None:
        return [""]

    # Extract all string literals upfront
    strings = re.findall(r'"([^"]*)"', args)

    # Strip string literals from a scratch copy to check for array braces
    scratch = re.sub(r'"[^"]*"', '""', args)

    # Array form: `{ ... }` on the outside, with or without ``value=``/``path=``
    if re.search(r"=\s*\{", scratch) or re.match(r"\s*\{", scratch):
        return [s for s in strings] or [""]

    # Otherwise take the first string literal
    return [strings[0]] if strings else [""]


def _http_method_from_args(args: str, annotation: str) -> str:
    """Determine HTTP method for a mapping annotation."""
    if annotation in _ANNOTATION_TO_HTTP:
        return _ANNOTATION_TO_HTTP[annotation]

    # @RequestMapping(method=RequestMethod.GET) / method={RequestMethod.GET, ...}
    m = _REQUEST_METHOD_RE.search(args or "")
    if m:
        return m.group(1).upper()
    return "ANY"


def _combine_paths(base: str, sub: str) -> str:
    """Combine class-level and method-level paths, collapsing duplicate slashes."""
    base = (base or "").rstrip("/")
    sub = (sub or "").strip()
    if not sub:
        return base or "/"
    if not sub.startswith("/"):
        sub = "/" + sub
    combined = base + sub
    return re.sub(r"/+", "/", combined) or "/"


def _extract_package(content: str) -> str:
    m = _PACKAGE_RE.search(content)
    return m.group(1) if m else ""


def _extract_imports(content: str) -> dict:
    """Return {SimpleName: FQCN} plus an entry ``*`` -> [wildcard packages]."""
    imports = {}
    wildcards = []
    for m in _IMPORT_RE.finditer(content):
        fqcn = m.group(2)
        if fqcn.endswith(".*"):
            wildcards.append(fqcn[:-2])
        else:
            simple = fqcn.rsplit(".", 1)[-1]
            imports[simple] = fqcn
    if wildcards:
        imports["*"] = wildcards
    return imports


def _stereotype_before(content: str, pos: int) -> str:
    """Return the last stereotype annotation appearing before ``pos``.

    Scans a 600-char window so ``@Controller`` / ``@RequestMapping(...)`` /
    ``public class`` on consecutive lines works.
    """
    window_start = max(0, pos - 600)
    window = content[window_start:pos]
    matches = _STEREOTYPE_RE.findall(window)
    return matches[-1] if matches else ""


def _extract_class_info(content: str) -> dict:
    """Find the first class/interface declaration and return its metadata."""
    m = _CLASS_DECL_RE.search(content)
    if m:
        return {
            "kind": "class",
            "stereotype": _stereotype_before(content, m.start()),
            "name": m.group("name"),
            "abstract": bool((m.group("mod") or "").strip() == "abstract"),
            "extends": (m.group("parent") or "").strip(),
            "implements": [s.strip() for s in (m.group("impls") or "").split(",") if s.strip()],
            "start": m.start(),
            "header_end": m.end(),
        }
    m = _INTERFACE_DECL_RE.search(content)
    if m:
        return {
            "kind": "interface",
            "stereotype": _stereotype_before(content, m.start()),
            "name": m.group("name"),
            "abstract": True,
            "extends": (m.group("parent") or "").strip(),
            "implements": [],
            "start": m.start(),
            "header_end": m.end(),
        }
    return {}


def _extract_class_mapping(content: str, class_start: int) -> list[str]:
    """Find @RequestMapping immediately above the class declaration.

    Searches the 500-char window before ``class_start`` for a mapping.
    Returns the list of paths (may contain empty string).
    """
    window_start = max(0, class_start - 500)
    window = content[window_start:class_start]
    m = _MAPPING_PATH_RE.search(window)
    if not m:
        return [""]
    if m.group("list") is not None:
        paths = [p.strip() for p in re.findall(r'"([^"]*)"', m.group("list"))]
        return paths or [""]
    return [m.group("single") or ""]


# Plain Java field: `private OrderService orderService;` — captures
# annotation-free fields so Vert.x / plain-Java projects work without
# ``@Autowired``. Type must start with an uppercase letter so we skip
# primitives (``int x``). ``static`` is intentionally NOT allowed here
# because ``private static final String FN_XXX = "..."`` style constants
# are never DI targets — keeping them in would pollute the field list
# (analyzer would filter them, but the stats become misleading).
_PLAIN_FIELD_RE = re.compile(
    r"""^\s*(?:private|protected)\s+
        (?:final\s+|transient\s+|volatile\s+)*
        (?P<type>[A-Z]\w*)(?:\s*<[^>]*>)?\s+
        (?P<name>\w+)\s*[=;]
    """,
    re.VERBOSE | re.MULTILINE,
)


def _extract_autowired_fields(content: str, class_info: dict) -> list[dict]:
    """Extract candidate dependency-injection fields of the class.

    Covers, in decreasing specificity:
      * ``@Autowired``/``@Resource``/``@Inject`` annotated fields (Spring)
      * Lombok ``@RequiredArgsConstructor``/``@AllArgsConstructor`` final
        fields (Spring)
      * Constructor parameters (Spring + plain Java DI)
      * Plain ``private`` / ``protected`` class fields with a user-defined
        (Uppercase) type — Vert.x, Guice, or hand-wired projects

    The analyzer downstream filters by the service/mapper index, so
    over-capturing here (e.g. ``private Logger log;``) is harmless.
    """
    if not class_info:
        return []

    fields = []
    seen = set()

    def _add(type_simple: str, name: str):
        key = (type_simple, name)
        if key in seen:
            return
        seen.add(key)
        fields.append({"name": name, "type_simple": type_simple})

    for m in _AUTOWIRED_FIELD_RE.finditer(content):
        _add(m.group("type"), m.group("name"))

    # Lombok: trigger on @RequiredArgsConstructor or @AllArgsConstructor
    if re.search(r"@(?:RequiredArgsConstructor|AllArgsConstructor)\b", content):
        for m in _FINAL_FIELD_RE.finditer(content):
            _add(m.group("type"), m.group("name"))

    # Constructor injection (match constructors with the same class name)
    class_name = class_info.get("name", "")
    if class_name:
        for m in _CONSTRUCTOR_RE.finditer(content):
            if m.group("cls") != class_name:
                continue
            params = m.group("params")
            for pm in _PARAM_RE.finditer(params):
                _add(pm.group("type"), pm.group("name"))

    # Plain-Java fields without any DI annotation. Restricted to the class
    # body (after the class header) so method locals are not captured.
    body_start = class_info.get("header_end", 0)
    body = content[body_start:]
    for m in _PLAIN_FIELD_RE.finditer(body):
        _add(m.group("type"), m.group("name"))

    return fields


def _find_method_name_after(content: str, start: int) -> str:
    """Scan forward from ``start`` for the first Java method signature.

    Walks token-by-token, skipping annotations, modifiers, and return-type
    tokens until it finds ``word(`` where ``word`` isn't a keyword. Gives up
    after 300 characters (one long method signature).
    """
    window = content[start:start + 400]
    # Strip any other stacked annotations (e.g. @ResponseBody, @Override)
    window = re.sub(r"@\w+\s*(?:\([^)]*\))?", " ", window)
    for m in _JAVA_METHOD_SIG_RE.finditer(window):
        name = m.group("name")
        if name in _METHOD_KEYWORDS:
            continue
        return name
    return ""


def _extract_endpoints(content: str, class_paths: list[str]) -> list[dict]:
    """Extract HTTP endpoints (method-level mappings) from a controller class.

    Returns a list of endpoint dicts — one per (class_path, method_path)
    combination. If either side defines an array, the full cross-product
    is produced.
    """
    endpoints = []
    for m in _METHOD_ANNOTATION_RE.finditer(content):
        annotation = m.group("ann")
        args = m.group("args") or ""
        # The annotation above the class also matches — filter by position:
        # class-level mapping lives BEFORE the first class decl, so skip if
        # the caller passed a post-header content. We accept duplicates here
        # because the outer loop already isolated per-class content.
        method_name = _find_method_name_after(content, m.end())
        if not method_name:
            continue
        method_paths = _parse_mapping_paths(args)
        http = _http_method_from_args(args, annotation)
        line_number = content.count("\n", 0, m.start()) + 1
        for cp in class_paths:
            for mp in method_paths:
                endpoints.append({
                    "annotation": annotation,
                    "http_method": http,
                    "path": mp,
                    "full_url": _combine_paths(cp, mp),
                    "method_name": method_name,
                    "line_number": line_number,
                })
    return endpoints


def _extract_vertx_endpoints(content: str, class_paths: list[str]) -> list[dict]:
    """Extract HTTP endpoints from Vert.x routing DSL.

    Handles two forms:

    1. Literal: ``router.get("/order/list")`` / ``router.post("/x")`` etc.
       Method is taken from the call name. For ``router.route("/x")`` the
       HTTP method is ``ANY``.
    2. Chained: ``router.route().path("/x").method(HttpMethod.GET)...``.
       We pull ``path`` and ``method`` out of the chain; missing parts
       default to ``/`` and ``ANY``.

    The method name recorded on each endpoint is the ``.handler(...)``
    reference when available (``this::doList`` → ``doList``), otherwise
    the fallback ``"handler"``. This mirrors the "controller method name"
    slot used for the program_name fallback in the analyzer.
    """
    endpoints = []

    def _emit(http_method: str, path: str, method_name: str, line: int):
        for cp in class_paths:
            endpoints.append({
                "annotation": "Vert.x",
                "http_method": http_method,
                "path": path,
                "full_url": _combine_paths(cp, path),
                "method_name": method_name,
                "line_number": line,
            })

    # 1) literal form
    for m in _VERTX_ROUTE_LITERAL_RE.finditer(content):
        method = m.group("method").upper()
        path = m.group("path")
        if method == "ROUTE":
            http_method = "ANY"
        else:
            http_method = method
        handler_name = (
            m.group("this_ref")
            or m.group("cls_ref")
            or m.group("lambda_var")
            or m.group("inline_cls")
            or "handler"
        )
        line = content.count("\n", 0, m.start()) + 1
        _emit(http_method, path, handler_name, line)

    # 2) chained form. Because the chain regex is greedy it usually also
    # includes ``.handler(...)`` inside the captured chain, so we search
    # the chain text first and only fall back to the post-match window.
    for m in _VERTX_ROUTE_CHAIN_RE.finditer(content):
        chain = m.group("chain") or ""
        path_match = _VERTX_CHAIN_PATH_RE.search(chain)
        method_match = _VERTX_CHAIN_METHOD_RE.search(chain)
        if not path_match:
            continue
        path = path_match.group(1)
        http_method = method_match.group(1).upper() if method_match else "ANY"
        hm = _VERTX_HANDLER_REF_RE.search(chain)
        handler_name = ""
        if hm:
            handler_name = hm.group("ref") or hm.group("cls_ref") or ""
        if not handler_name:
            handler_name = _find_vertx_handler_after(content, m.end())
        handler_name = handler_name or "handler"
        line = content.count("\n", 0, m.start()) + 1
        _emit(http_method, path, handler_name, line)

    return endpoints


_VERTX_HANDLER_REF_RE = re.compile(
    r"""\.handler\s*\(\s*
        (?:
            this\s*::\s*(?P<ref>\w+)
          | (?P<lambda>\w+)\s*->        # lambda variable (just a label)
          | (?P<cls>\w+)\s*::\s*(?P<cls_ref>\w+)
        )
    """,
    re.VERBOSE,
)


def _find_vertx_handler_after(content: str, start: int) -> str:
    """Find the first ``.handler(...)`` reference after ``start``.

    Returns the referenced method name (``this::foo`` → ``foo``), or an
    empty string if none is found within the next 200 characters.
    """
    window = content[start:start + 300]
    m = _VERTX_HANDLER_REF_RE.search(window)
    if not m:
        return ""
    return m.group("ref") or m.group("cls_ref") or ""


def _extract_rfc_calls(content: str) -> list[dict]:
    """Find SAP JCo RFC function calls.

    Two-pass:
      1) ``String FN_XXX = "Z_..."`` constants in the same file.
      2) ``.getFunction("LITERAL")`` or ``.getFunction(FN_XXX)`` — constants
         resolved via the first pass.
    """
    constants = {
        name: value for name, value in _RFC_CONST_RE.findall(content)
    }

    calls = []
    seen = set()
    for m in _RFC_GETFUNCTION_STR_RE.finditer(content):
        name = m.group(1)
        if name and name not in seen:
            seen.add(name)
            line = content.count("\n", 0, m.start()) + 1
            calls.append({"name": name, "line": line, "resolved_from": "literal"})

    for m in _RFC_GETFUNCTION_VAR_RE.finditer(content):
        ident = m.group(1)
        if ident in constants:
            name = constants[ident]
            if name not in seen:
                seen.add(name)
                line = content.count("\n", 0, m.start()) + 1
                calls.append({"name": name, "line": line, "resolved_from": f"const:{ident}"})

    return calls


def resolve_type_fqcn(type_simple: str, imports: dict, package: str) -> str:
    """Resolve a simple type name to a fully-qualified class name.

    Lookup order:
      1) Exact import (e.g. ``OrderService`` → ``com.x.OrderService``)
      2) If already dotted, assume it's an FQCN
      3) Same package fallback
      4) ``java.lang`` fallback (common primitives like ``String``)
    """
    if not type_simple:
        return ""
    if "." in type_simple:
        return type_simple
    if type_simple in imports:
        return imports[type_simple]
    if package:
        return f"{package}.{type_simple}"
    return type_simple


def parse_java_file(filepath: str) -> dict:
    """Parse a single .java file into a structured metadata dict.

    Returns an empty dict if no class/interface is found. Always safe on
    invalid Java — we're best-effort regex, not a compiler.
    """
    try:
        raw = _read_file_safe(filepath)
    except Exception as e:
        logger.warning("Cannot read %s: %s", filepath, e)
        return {}

    # Strip comments for most work; keep raw for string-literal-based RFC scan
    content_nc = _strip_comments(raw)

    class_info = _extract_class_info(content_nc)
    if not class_info:
        return {}

    package = _extract_package(content_nc)
    imports = _extract_imports(content_nc)
    class_name = class_info["name"]
    fqcn = f"{package}.{class_name}" if package else class_name

    # Custom project-level Vert.x annotation (one-class-per-endpoint
    # pattern). We check this BEFORE stereotype inference so the class is
    # promoted to Verticle regardless of what it extends.
    rest_vert = _extract_rest_verticle(content_nc, class_info["start"])

    # Stereotype: annotation (Spring) wins; then @RestVerticle; then
    # inheritance-based fallback (extends AbstractVerticle / BaseVerticle
    # / implements Verticle). This keeps plain-Java Vert.x projects
    # working without any framework annotation.
    stereotype = class_info.get("stereotype", "")
    if not stereotype and rest_vert:
        stereotype = "Verticle"
    if not stereotype:
        extends_clean = re.sub(r"<.*$", "", class_info.get("extends", "")).strip()
        if _is_verticle_base(extends_clean):
            stereotype = "Verticle"
        else:
            for impl in class_info.get("implements", []):
                impl_clean = re.sub(r"<.*$", "", impl).strip()
                if _is_verticle_base(impl_clean):
                    stereotype = "Verticle"
                    break

    # Always look for a class-level @RequestMapping (no-op if absent), then
    # run BOTH endpoint extractors regardless of stereotype. The Spring
    # extractor only fires on @Mapping annotations, the Vert.x one only
    # fires on ``router.get/post/...`` DSL calls, so a file without the
    # corresponding patterns simply yields zero endpoints. This matters
    # for real Vert.x projects where route setup lives in plain
    # "router builder" classes (e.g. ``OrderRouter``) that neither
    # extend ``AbstractVerticle`` nor carry any annotation — those
    # classes still end up as HTTP entry points in the analyzer.
    class_paths = _extract_class_mapping(content_nc, class_info["start"]) or [""]

    autowired = _extract_autowired_fields(content_nc, class_info)
    # Resolve FQCNs for each autowired field
    for f in autowired:
        f["type_fqcn"] = resolve_type_fqcn(f["type_simple"], imports, package)

    body = content_nc[class_info["header_end"]:]
    endpoints = _extract_endpoints(body, class_paths)
    # Vert.x routes have no class-level prefix concept
    endpoints += _extract_vertx_endpoints(body, [""])

    # @RestVerticle annotation — emit a single endpoint synthesized from
    # the annotation attributes. The handler method name defaults to the
    # class name since this pattern uses one class per endpoint.
    if rest_vert:
        endpoints.append({
            "annotation": "RestVerticle",
            "http_method": rest_vert["method"],
            "path": rest_vert["url"],
            "full_url": rest_vert["url"],
            "method_name": class_name,
            "line_number": 1,
        })

    rfc_calls = _extract_rfc_calls(raw)

    # Resolve extends to FQCN too (may need this for abstract controller chain)
    extends_fqcn = ""
    if class_info.get("extends"):
        # Strip generics: "BaseController<Order>" -> "BaseController"
        parent_simple = re.sub(r"<.*$", "", class_info["extends"]).strip()
        extends_fqcn = resolve_type_fqcn(parent_simple, imports, package)

    return {
        "filepath": filepath,
        "package": package,
        "class_name": class_name,
        "fqcn": fqcn,
        "kind": class_info["kind"],
        "stereotype": stereotype,
        "abstract": class_info.get("abstract", False),
        "imports": imports,
        "class_request_mapping": class_paths,
        "autowired_fields": autowired,
        "endpoints": endpoints,
        "rfc_calls": rfc_calls,
        "extends": extends_fqcn,
        "implements": class_info.get("implements", []),
    }


def parse_all_java(base_dir: str) -> list[dict]:
    """Parse every .java file in ``base_dir`` and return a list of metadata.

    Files without a class/interface are skipped.
    """
    results = []
    for fp in scan_java_dir(base_dir):
        info = parse_java_file(fp)
        if info:
            results.append(info)
    logger.info("Parsed %d java files with class/interface declarations", len(results))
    return results
