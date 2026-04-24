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


# Strip single-line // comments and block /* */ comments but keep strings
# (RFC detection relies on string literals, so we only strip comments).
# IMPORTANT: we replace comments with whitespace of the SAME LENGTH so
# that byte offsets stay stable between ``raw`` and ``content_nc``.
# Without this, class_info offsets (computed against content_nc) would
# drift relative to raw, and any downstream pass that uses raw with
# class_info offsets (e.g. method-body extraction) would start reading
# from the wrong position in the file.
def _strip_comments(src: str) -> str:
    """Replace comments with equal-length whitespace, preserving offsets.

    String / char literals are left untouched so that SQL IDs, RFC names,
    and class-path strings survive intact. Newlines inside block comments
    are preserved so line numbers stay correct.
    """
    out = []
    i = 0
    n = len(src)
    in_str = None
    while i < n:
        c = src[i]
        # Inside a string / char literal — copy verbatim
        if in_str is not None:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(src[i + 1])
                i += 2
                continue
            if c == in_str:
                in_str = None
            i += 1
            continue
        if c == '"' or c == "'":
            in_str = c
            out.append(c)
            i += 1
            continue
        # Block comment
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            end = src.find("*/", i + 2)
            end = n if end == -1 else end + 2
            for j in range(i, end):
                out.append("\n" if src[j] == "\n" else " ")
            i = end
            continue
        # Line comment
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            end = src.find("\n", i + 2)
            if end == -1:
                end = n
            for _ in range(i, end):
                out.append(" ")
            i = end
            continue
        out.append(c)
        i += 1
    return "".join(out)


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
    r"""@(?:[\w.]+\.)?(?:Autowired|Resource|Inject)\b   # 옵셔널 FQ qualifier (@javax.inject.Inject)
        (?:\s*\([^)]*\))?                                # optional args: @Autowired(required=false)
        (?:\s+@\w+(?:\s*\([^)]*\))?)*                    # 복수 어노테이션 허용 (@Inject @Qualifier("x"))
        \s+                                               # whitespace (space OR newline OK)
        (?:private|protected|public)?\s*
        (?:final\s+|transient\s+|volatile\s+)*
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

# ---------------------------------------------------------------------------
# Nexcore (SK C&C framework) support
# ---------------------------------------------------------------------------
# Nexcore controllers extend ``Abstract*BizController`` and expose endpoints
# via method-name convention (no @RequestMapping on methods). Every public
# method whose parameters include Nexcore service-context types is an
# endpoint: the method name becomes the URL path segment.
_NEXCORE_BASE_CLASSES = {
    "AbstractMultiActionBizController",
    "AbstractSingleActionBizController",
    "AbstractBizController",
    "AbstractCommonBizController",
}

# Public method with Nexcore parameter types:
#   public Object getList(IDataSet ds, IBizServiceContext ctx) throws Exception {
# We match on parameter types that are clearly Nexcore service context.
_NEXCORE_PARAM_TYPES = {"IDataSet", "IBizServiceContext", "IOnlineContext",
                        "IDataSetHelper", "HttpServletRequest"}

# Module-level patterns dict, overridable via apply_patterns().
# When set, these extend/replace the hardcoded defaults above.
_active_patterns = None


# Generic Java Collection/Map/Optional API 메서드. `discover-patterns` 의 LLM
# 이 uppercase 상수 key 를 인수로 쓰는 `map.get("PARAM_X")` 같은 코드를 SAP
# 인터페이스 호출 패턴으로 오해해 `rfc_call_methods` 에 포함시키는 경우가
# 있었음 (예: `execute`, `get`). 이런 일반 메서드가 들어오면 `map.get(...)` /
# `list.add(...)` 가 전부 RFC false positive 로 걸리므로 `apply_patterns` 에서
# 자동으로 제거하고 경고를 출력한다. 실제 사내 "RFC 전용" 메서드명은 보통
# `execute` / `send` / `call` / `invoke` / `request` 같은 동사형.
_GENERIC_COLLECTION_METHODS = frozenset({
    "get", "put", "set", "add", "remove", "contains", "containsKey",
    "containsValue", "size", "clear", "isEmpty", "keySet", "values",
    "entrySet", "putAll", "putIfAbsent", "replace", "getOrDefault",
    "indexOf", "lastIndexOf", "subList", "addAll", "removeAll",
    "retainAll", "iterator", "forEach", "stream", "toArray",
    "toString", "equals", "hashCode", "hasNext", "next",
    # Optional API
    "orElse", "orElseGet", "orElseThrow", "ifPresent", "isPresent",
    # Generic accessors easily confused
    "getValue", "setValue",
})


def apply_patterns(patterns: dict | None) -> None:
    """Inject discovered patterns into the parser's detection logic.

    Called once before ``parse_all_java`` when ``--patterns`` is used.
    Extends the hardcoded base-class / param-type / SQL-call sets so
    that project-specific conventions are recognised.

    Safety net: generic Java Collection/Map/Optional methods are stripped
    from ``rfc_call_methods`` at load time (see ``_GENERIC_COLLECTION_METHODS``)
    so an over-fitted LLM suggestion can't cause thousands of
    ``map.get("STR")`` false positives.
    """
    global _active_patterns, _NEXCORE_BASE_CLASSES, _NEXCORE_PARAM_TYPES, _SQL_CALL_RE, _rfc_custom_re, _rfc_custom_var_re
    _active_patterns = patterns or {}

    # Extend controller base classes
    extra_bases = _active_patterns.get("controller_base_classes") or []
    if extra_bases:
        _NEXCORE_BASE_CLASSES = _NEXCORE_BASE_CLASSES | set(extra_bases)

    # Extend endpoint param types
    extra_params = _active_patterns.get("endpoint_param_types") or []
    if extra_params:
        _NEXCORE_PARAM_TYPES = _NEXCORE_PARAM_TYPES | set(extra_params)

    # Rebuild SQL call regex if custom receivers / operations provided
    extra_receivers = _active_patterns.get("sql_receivers") or []
    extra_ops = _active_patterns.get("sql_operations") or []
    if extra_receivers or extra_ops:
        _SQL_CALL_RE = _build_sql_call_re(extra_receivers, extra_ops)

    # Build custom RFC call regex if rfc_call_methods specified.
    # Filter out generic Collection/Map/Optional API names before building —
    # otherwise map.get("STR") / list.add("STR") would all be flagged as
    # RFC calls. `_active_patterns` is also updated so downstream consumers
    # (e.g. reporters) see the cleaned list.
    rfc_methods_raw = _active_patterns.get("rfc_call_methods") or []
    rfc_methods = [m for m in rfc_methods_raw
                   if m not in _GENERIC_COLLECTION_METHODS]
    rejected = [m for m in rfc_methods_raw
                if m in _GENERIC_COLLECTION_METHODS]
    if rejected:
        print(f"  apply_patterns: dropped generic method(s) from "
              f"rfc_call_methods: {rejected} "
              f"(prevents Collection/Map false positives)")
        _active_patterns["rfc_call_methods"] = rfc_methods
    if rfc_methods:
        _rfc_custom_re = _build_rfc_custom_re(rfc_methods)
        _rfc_custom_var_re = _build_rfc_custom_var_re(rfc_methods)
    else:
        _rfc_custom_re = None
        _rfc_custom_var_re = None

_NEXCORE_METHOD_RE = re.compile(
    r"""(?:public)\s+
        (?P<ret>[\w.<>,\[\]\s]+?)\s+
        (?P<name>\w+)\s*
        \(\s*(?P<params>[^)]*)\)
    """,
    re.VERBOSE,
)

def _is_nexcore_controller(class_info: dict) -> bool:
    """Return True if the class extends a known Nexcore base controller."""
    extends = class_info.get("extends", "")
    # Strip generics and package prefix
    simple = re.sub(r"<.*$", "", extends).strip().rsplit(".", 1)[-1]
    return simple in _NEXCORE_BASE_CLASSES

def _extract_nexcore_endpoints(content: str, class_paths: list[str]) -> list[dict]:
    """Extract endpoints from Nexcore controllers by method-name convention.

    In Nexcore, ``Abstract*BizController`` dispatches HTTP requests to
    public methods whose parameters include ``IDataSet`` /
    ``IBizServiceContext`` / ``IOnlineContext``. The method name itself
    becomes the URL path segment (e.g. ``getInformNoteList`` →
    ``/getInformNoteList.do``).
    """
    endpoints = []
    for m in _NEXCORE_METHOD_RE.finditer(content):
        name = m.group("name")
        params = m.group("params")
        # Check if ANY parameter type is a Nexcore context type
        param_types = {p.strip().split()[-2] if len(p.strip().split()) >= 2
                       else p.strip().split()[0]
                       for p in params.split(",") if p.strip()}
        if not (param_types & _NEXCORE_PARAM_TYPES):
            continue
        if name in _METHOD_KEYWORDS:
            continue
        line_number = content.count("\n", 0, m.start()) + 1
        suffix = get_url_suffix() or ".do"
        http = get_http_method_default()
        for cp in class_paths:
            endpoints.append({
                "annotation": "Nexcore",
                "http_method": http,
                "path": f"/{name}{suffix}",
                "full_url": _combine_paths(cp, f"/{name}{suffix}"),
                "method_name": name,
                "line_number": line_number,
            })
    return endpoints

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


# RFC patterns. We accept any method whose name starts with ``get`` and
# ends with ``Function`` — this covers:
#   * SAP JCo standard:         destination.getFunction("Z_NAME")
#   * Co-function util:         JCoUtil.getCoFunction("Z_NAME")
#   * Project-local helper:     helper.getJCoFunction("Z_NAME")
#   * RFC-specific helper:      client.getRfcFunction("Z_NAME")
#
# IMPORTANT: We only pin the FIRST argument and do NOT require the
# closing ``)``. Real projects almost always pass extra arguments:
#
#   JCoUtil.getJCoFunction("ZPM_ORDER_CREATE", timeout, session);
#
# A strict closing-paren regex would miss every one of these calls.
_RFC_GETFUNCTION_STR_RE = re.compile(
    r'\.get\w*Function\s*\(\s*"([^"]+)"'
)
_RFC_GETFUNCTION_VAR_RE = re.compile(
    r'\.get\w*Function\s*\(\s*(\w+)\b'
)

# Custom RFC call pattern: service.execute("IF-GERP-180", param, ZMM_FUNC.class)
# Captures the interface ID (string arg) and optionally the SAP function name
# from a ClassName.class argument. Active only when patterns.rfc_call_methods
# is configured.
_rfc_custom_re = None      # built dynamically via apply_patterns()
_rfc_custom_var_re = None  # variable-arg version


def _build_rfc_custom_re(methods: list[str], id_prefixes: list[str] | None = None) -> re.Pattern | None:
    """Build a regex for custom RFC call patterns like service.execute("IF-*", ..., Z*.class)."""
    if not methods:
        return None
    method_alt = "|".join(re.escape(m) for m in methods)
    # Capture: (1) string arg = interface ID, (2) optional ClassName before .class
    return re.compile(
        rf"""\b\w+\.(?:{method_alt})\s*\(
            \s*"(?P<id>[^"]+)"              # first string arg (interface ID)
            (?:[^)]*,\s*(?P<cls>\w+)\.class)?  # optional ClassName.class
        """,
        re.VERBOSE,
    )


def _build_rfc_custom_var_re(methods: list[str]) -> re.Pattern | None:
    """Build a regex for variable-arg RFC calls: service.execute(varName, ..., Z*.class)."""
    if not methods:
        return None
    method_alt = "|".join(re.escape(m) for m in methods)
    return re.compile(
        rf"""\b\w+\.(?:{method_alt})\s*\(
            \s*(?P<var>[a-zA-Z_]\w*)        # variable name (not a string literal)
            \s*(?:,[^)]*,\s*(?P<cls>\w+)\.class)?  # optional ClassName.class
        """,
        re.VERBOSE,
    )

# Overly-broad "candidate" pattern used only for the diagnostic hint
# counter. Matches anything that looks like ``...Function(`` and is
# NOT the main matcher — it lets users see how many potential RFC call
# sites exist in the source even when the strict regex returns zero.
_RFC_HINT_RE = re.compile(r'\.\s*\w*[Ff]unction\s*\(')
_RFC_CONST_RE = re.compile(
    r"""(?:public\s+|private\s+|protected\s+)?
        (?:static\s+)?(?:final\s+)?
        String\s+(\w+)\s*=\s*"([^"]+)"\s*;
    """,
    re.VERBOSE,
)


# String-based MyBatis SQL calls. Legacy projects often invoke SQL through
# a helper (``CommonSQL.selectList("order.findAll", params)``) instead of
# injecting a Mapper interface. We match when BOTH the receiver name
# carries a clear hint AND the first argument is a string containing at
# least one ``.`` (namespace separator). This keeps false positives off
# ordinary ``map.update(k,v)`` / ``list.insert(0,x)`` style calls.
_DEFAULT_SQL_RECEIVERS = (
    "commonSQL|CommonSQL|sqlSession|SqlSession|sqlClient|SqlClient"
    "|sqlExec|SqlExec|sqlHelper|SqlHelper|sqlMap|SqlMap"
    "|commonDao|CommonDao|sqlTemplate|SqlTemplate"
    "|sqlMapClientTemplate|SqlMapClientTemplate"
    "|sqlMapClient|SqlMapClient"
    r"|\w*[Dd]ao|\w*SQL|\w*Sql|\w*[Tt]emplate|queryRunner"
)
# Longer variants come first so `selectList` is preferred over `select`
# when the regex engine tries alternatives left-to-right. Plain `select`
# covers MyBatis SqlSession ResultHandler API `select(id, param, bounds, rh)`
# which is common in legacy code.
_DEFAULT_SQL_OPS = (
    "selectList|selectOne|selectMap|selectPage|selectCount|selectCursor"
    "|queryForList|queryForObject|queryForMap"
    "|insertBatch|updateBatch|deleteBatch"
    "|insert|update|delete|save|execute|call|query|select"
)


def _build_sql_call_re(extra_receivers: list[str] = None,
                        extra_ops: list[str] = None) -> re.Pattern:
    """Build _SQL_CALL_RE dynamically, merging defaults with pattern overrides."""
    receivers = _DEFAULT_SQL_RECEIVERS
    if extra_receivers:
        extras = "|".join(re.escape(r) for r in extra_receivers)
        receivers = f"{extras}|{receivers}"
    ops = _DEFAULT_SQL_OPS
    if extra_ops:
        extras = "|".join(re.escape(o) for o in extra_ops)
        ops = f"{extras}|{ops}"
    return re.compile(
        rf"""\b(?:{receivers})
            (?:\.\w+)?
            \.\s*(?P<op>{ops})
            \s*\(\s*"(?P<sqlid>[^"]+\.[^"]+)"
        """,
        re.VERBOSE,
    )


_SQL_CALL_RE = _build_sql_call_re()


def get_url_suffix() -> str:
    """Return the URL suffix from active patterns (e.g. '.do')."""
    if _active_patterns:
        return _active_patterns.get("url_suffix", "") or ""
    return ""


def get_http_method_default() -> str:
    """Return the default HTTP method from active patterns."""
    if _active_patterns:
        return _active_patterns.get("http_method_default", "POST") or "POST"
    return "POST"


# SQL call with variable prefix: sqlSession.selectList(namespace + "findList", param)
# Captures the variable name and the string suffix separately.
_SQL_CALL_VAR_RE = re.compile(
    rf"""\b(?:{_DEFAULT_SQL_RECEIVERS})
        (?:\.\w+)?
        \.\s*(?P<op>{_DEFAULT_SQL_OPS})
        \s*\(\s*(?P<var>\w+)\s*\+\s*"(?P<suffix>[^"]+)"
    """,
    re.VERBOSE,
)

# SQL call with variable prefix + ternary suffix:
#   sqlSession.update(NAMESPACE + (cond ? ".updateA" : ".updateB"), row)
# Emit BOTH branches as separate SQL calls — 런타임 조건에 따라 둘 다
# 실제 호출 가능하므로 영향분석 / XML 체인에는 두 개 다 포함돼야 한다.
# cond 내부에는 임의의 Java 표현식 (메서드 호출 / 중첩 괄호 / `"..."`
# 리터럴) 이 올 수 있어 `.+?` + `\?\s*"` anchor 로 lazy 매칭.
_SQL_CALL_TERNARY_RE = re.compile(
    rf"""\b(?:{_DEFAULT_SQL_RECEIVERS})
        (?:\.\w+)?
        \.\s*(?P<op>{_DEFAULT_SQL_OPS})
        \s*\(\s*(?P<var>\w+)\s*\+\s*
        \(
        (?P<cond>.+?)
        \?\s*
        "(?P<true_suffix>[^"]+)"
        \s*:\s*
        "(?P<false_suffix>[^"]+)"
        \s*\)
    """,
    re.VERBOSE | re.DOTALL,
)

# String field/constant that holds a namespace prefix:
#   private String namespace = "com.example.mapper.";
#   private static final String NAMESPACE = "com.example.";
#   String sqlId = "com.example.mapper.";
_NS_CONST_RE = re.compile(
    r"""(?:private|protected|public|static|final|\s)*
        String\s+(?P<name>\w+)\s*=\s*"(?P<value>[^"]+)"
    """,
    re.VERBOSE,
)

# namespace-candidate value: identifier chars + dots + hyphens. 점 없이
# `"scp"` 처럼 suffix 쪽 `.findXxx` 와 concat 되는 케이스, 그리고 MyBatis
# 에서 흔한 하이픈 namespace (``"equip-invest-10673"``) 도 허용. 다른
# 임의 문자열 (``"Hello World"`` 등) 은 boundary 체크와 함께 제외된다.
_NS_VALUE_RE = re.compile(r"^[\w.\-]+$")


def _extract_ns_constants(content: str) -> dict:
    """Collect ``{variable_name: string_value}`` for namespace prefix resolution.

    Accepts both dotted prefixes (``"com.example."``) and bare namespace
    tokens (``"scp"``) — the latter is common in projects that put the
    ``.`` on the suffix side: ``sqlSession.selectList(NS + ".findXxx")``.
    Boundary 검증은 resolve 지점 (``_extract_sql_calls`` /
    ``_collect_body_sql_calls``) 에서 `prefix.endswith(".") or
    suffix.startswith(".")` 로 처리한다.
    """
    constants = {}
    for m in _NS_CONST_RE.finditer(content):
        name = m.group("name")
        value = m.group("value")
        if _NS_VALUE_RE.match(value):
            constants[name] = value
    return constants


def _extract_sql_calls(content: str) -> list[dict]:
    """Find string-based MyBatis SQL helper calls.

    Handles two forms:
    1. Literal: ``sqlSession.selectList("namespace.sqlId", param)``
    2. Variable prefix: ``sqlSession.selectList(namespace + "sqlId", param)``
       where ``namespace`` is resolved from ``String namespace = "..."``
    """
    ns_constants = _extract_ns_constants(content)
    results = []
    seen = set()

    # 1) Literal string SQL IDs
    for m in _SQL_CALL_RE.finditer(content):
        sqlid = m.group("sqlid")
        namespace, _, sql_id = sqlid.rpartition(".")
        if not namespace:
            continue
        key = (m.group("op"), sqlid)
        if key in seen:
            continue
        seen.add(key)
        results.append({
            "op": m.group("op"),
            "sqlid": sqlid,
            "namespace": namespace,
            "sql_id": sql_id,
            "line": content.count("\n", 0, m.start()) + 1,
        })

    # 2) Variable + string suffix: sqlSession.selectList(namespace + "findList")
    for m in _SQL_CALL_VAR_RE.finditer(content):
        var = m.group("var")
        suffix = m.group("suffix")
        prefix = ns_constants.get(var, "")
        if not prefix:
            continue
        # Require a proper namespace.id boundary (dot) between the
        # resolved prefix and suffix — see _collect_body_sql_calls for
        # the detailed rationale.
        if not prefix.endswith(".") and not suffix.startswith("."):
            continue
        sqlid = prefix + suffix
        namespace, _, sql_id = sqlid.rpartition(".")
        if not namespace:
            continue
        key = (m.group("op"), sqlid)
        if key in seen:
            continue
        seen.add(key)
        results.append({
            "op": m.group("op"),
            "sqlid": sqlid,
            "namespace": namespace,
            "sql_id": sql_id,
            "line": content.count("\n", 0, m.start()) + 1,
            "resolved_from": f"var:{var}",
        })

    # 3) Variable + ternary suffix:
    #    sqlSession.update(NS + (cond ? ".a" : ".b"), row)
    # 두 branch 모두 별도 call 로 등록한다.
    for m in _SQL_CALL_TERNARY_RE.finditer(content):
        var = m.group("var")
        prefix = ns_constants.get(var, "")
        if not prefix:
            continue
        for suffix in (m.group("true_suffix"), m.group("false_suffix")):
            if not prefix.endswith(".") and not suffix.startswith("."):
                continue
            sqlid = prefix + suffix
            namespace, _, sql_id = sqlid.rpartition(".")
            if not namespace:
                continue
            key = (m.group("op"), sqlid)
            if key in seen:
                continue
            seen.add(key)
            results.append({
                "op": m.group("op"),
                "sqlid": sqlid,
                "namespace": namespace,
                "sql_id": sql_id,
                "line": content.count("\n", 0, m.start()) + 1,
                "resolved_from": f"var:{var}:ternary",
            })

    return results


_ANNOTATION_TO_HTTP = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH",
}


def _parse_mapping_paths(args: str) -> list[str]:
    """Given the arg list of a mapping annotation, return the list of paths.

    Handles the common Spring MVC forms:

    * Bare string:       ``("/x")``
    * Bare array:        ``({"/a", "/b"})``
    * ``value`` / ``path`` key: ``(value = "/x")`` / ``(path = "/x")``
    * Array with key:    ``(value = {"/a", "/b"})`` / ``(path = {"/a"})``
    * Plus extra attrs:  ``(value = "/x", consumes = "...", produces = {...})``

    The tricky cases are when OTHER attributes (``consumes``, ``produces``,
    ``params``, ``headers``) also contain string literals or braces. We
    must not pick those up as paths. So we look ONLY at ``value`` /
    ``path`` keys, and if no key is present we treat the **first** arg
    (before any ``,``) as the bare value.
    """
    if args is None:
        return [""]

    # 1. value = {...} or path = {...}  (array with key)
    for key in ("value", "path"):
        m = re.search(rf'\b{key}\s*=\s*\{{([^}}]*)\}}', args)
        if m:
            paths = re.findall(r'"([^"]*)"', m.group(1))
            return paths or [""]

    # 2. value = "..." or path = "..."  (single with key)
    for key in ("value", "path"):
        m = re.search(rf'\b{key}\s*=\s*"([^"]*)"', args)
        if m:
            return [m.group(1)]

    # 3. No ``value``/``path`` key — the mapping uses the bare form.
    #    Only look at the first positional argument (i.e. everything
    #    before the first comma that is not inside a string/brace).
    #    This prevents ``consumes = "..."`` or ``produces = {...}`` from
    #    being parsed as paths.
    first = _first_positional_arg(args)
    stripped = first.lstrip()

    # 3a. Bare array: {"/a", "/b"}
    if stripped.startswith("{"):
        m = re.match(r"\{([^}]*)\}", stripped)
        if m:
            paths = re.findall(r'"([^"]*)"', m.group(1))
            return paths or [""]

    # 3b. Bare string: "/x"
    m = re.match(r'\s*"([^"]*)"', first)
    if m:
        return [m.group(1)]

    return [""]


def _first_positional_arg(args: str) -> str:
    """Return the first positional argument of an annotation argument list.

    Walks the arg string respecting string literals and balanced
    ``{}``/``()`` brackets, stopping at the first top-level comma. The
    point is to isolate the bare ``value`` from something like::

        ("/x", consumes = MediaType.APPLICATION_JSON_VALUE)

    so that a later ``value =`` lookup in a different argument does not
    confuse the parser.
    """
    depth_paren = 0
    depth_brace = 0
    in_str = None
    for i, c in enumerate(args):
        if in_str is not None:
            if c == "\\" and i + 1 < len(args):
                continue
            if c == in_str:
                in_str = None
            continue
        if c == '"' or c == "'":
            in_str = c
            continue
        if c == "(":
            depth_paren += 1
        elif c == ")":
            depth_paren -= 1
        elif c == "{":
            depth_brace += 1
        elif c == "}":
            depth_brace -= 1
        elif c == "," and depth_paren == 0 and depth_brace == 0:
            return args[:i]
    return args


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


def _strip_annotations_balanced(text: str) -> str:
    """Remove ``@Name(...)`` annotations respecting nested parentheses.

    The simple ``@\\w+\\s*\\([^)]*\\)`` regex fails when annotation
    arguments contain parentheses of their own — e.g. Spring Security's
    ``@PreAuthorize("hasRole('ADMIN') or (hasRole('USER') and ...)")`` —
    because ``[^)]*`` stops at the first ``)``. That leaves stray
    tokens (``or``, ``hasRole``) behind which the method-name finder
    then picks up as false positives.

    We walk the text manually and consume matched ``(`` / ``)`` pairs,
    ignoring parentheses that appear inside string / char literals.
    Replaced ranges are filled with spaces so that byte offsets stay
    aligned (handy if any caller reports line numbers later).
    """
    out = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == '@':
            start = i
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] == '_'):
                j += 1
            # Optional ``.`` for fully-qualified annotations
            while j < n and text[j] == '.':
                j += 1
                while j < n and (text[j].isalnum() or text[j] == '_'):
                    j += 1
            # Allow whitespace between annotation name and `(`
            k = j
            while k < n and text[k].isspace():
                k += 1
            if k < n and text[k] == '(':
                depth = 0
                in_str = None
                j = k
                while j < n:
                    c = text[j]
                    if in_str is not None:
                        if c == '\\' and j + 1 < n:
                            j += 2
                            continue
                        if c == in_str:
                            in_str = None
                    elif c == '"' or c == "'":
                        in_str = c
                    elif c == '(':
                        depth += 1
                    elif c == ')':
                        depth -= 1
                        if depth == 0:
                            j += 1
                            break
                    j += 1
            out.append(' ' * (j - start))
            i = j
            continue
        out.append(text[i])
        i += 1
    return ''.join(out)


def _find_method_name_after(content: str, start: int) -> str:
    """Scan forward from ``start`` for the first Java method signature.

    Strips any stacked annotations (even ones whose arguments contain
    nested parentheses or quoted parens) and then searches for the
    first ``word(`` token that isn't a Java keyword. The search window
    is 600 chars — long enough to cover a multi-annotation method
    header and short enough to avoid crossing into the next method.
    """
    window = content[start:start + 600]
    window = _strip_annotations_balanced(window)
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


def _count_rfc_hints(content: str) -> int:
    """Return a rough count of ``*Function(`` call sites in ``content``.

    Purely a diagnostic helper — when the strict RFC regex returns zero
    but this count is non-zero the user knows the regex is missing the
    project's actual method-name pattern.
    """
    return len(_RFC_HINT_RE.findall(content))


def _extract_rfc_calls(content: str) -> list[dict]:
    """Find SAP JCo RFC function calls.

    Three sources:
      1) ``.getXxxFunction("LITERAL")`` — standard JCo pattern
      2) ``.getXxxFunction(FN_XXX)`` — constant resolved via 2-pass
      3) ``service.execute("IF-GERP-180", ..., ZMM_FUNC.class)`` —
         custom call pattern (active when ``rfc_call_methods`` set in
         patterns.yaml). Captures interface ID + optional class name.
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

    # Custom RFC patterns (e.g., service.execute("IF-GERP-180", ..., ZMM_FUNC.class))
    if _rfc_custom_re:
        for m in _rfc_custom_re.finditer(content):
            iface_id = m.group("id")
            cls_name = m.group("cls") if m.group("cls") else ""
            rfc_name = f"{iface_id} ({cls_name})" if cls_name else iface_id
            if rfc_name not in seen:
                seen.add(rfc_name)
                line = content.count("\n", 0, m.start()) + 1
                calls.append({"name": rfc_name, "line": line, "resolved_from": "custom-rfc"})

    # Variable-arg custom RFC: service.execute(interfaceId, ..., ZMM.class)
    # where interfaceId = "IF_SKYN_001" is a String constant
    if _rfc_custom_var_re:
        for m in _rfc_custom_var_re.finditer(content):
            var = m.group("var")
            if var.startswith('"') or var[0].isdigit():
                continue
            resolved = constants.get(var, "")
            if not resolved:
                continue
            cls_name = m.group("cls") if m.group("cls") else ""
            rfc_name = f"{resolved} ({cls_name})" if cls_name else resolved
            if rfc_name not in seen:
                seen.add(rfc_name)
                line = content.count("\n", 0, m.start()) + 1
                calls.append({"name": rfc_name, "line": line, "resolved_from": f"custom-rfc-var:{var}"})

    return calls


# Method body extraction — we walk the class body with balanced-brace
# awareness, collecting each top-level method's ``(start, end)`` offsets.
# String/char literals and line/block comments are skipped so braces
# inside them don't confuse depth counting.
_METHOD_SIG_RE = re.compile(
    r"""(?:public|protected|private|static|final|abstract|synchronized|native|default)\s+
        (?:@\w+(?:\s*\([^)]*\))?\s+)*   # inline annotations: @ResponseBody, @Override, @SuppressWarnings("...")
        (?:<[^{}>]*>\s+)?
        [\w.<>?,\[\]\s&]+?
        \s+(?P<name>\w+)\s*
        \(
    """,
    re.VERBOSE,
)

# Field call pattern inside method bodies. The analyzer filters by the
# class's autowired_fields so false positives on utility calls are safe.
_FIELD_CALL_RE = re.compile(r"\b(?P<receiver>\w+)\s*\.\s*(?P<method>\w+)\s*\(")

# Bare (no receiver) method call pattern inside method bodies. Vert.x /
# 레거시 ServiceImpl 코드는 같은 클래스의 helper 메서드를 ``this.`` 없이
# 바로 호출하는 경우가 흔하다 (``saveInformNoteDetailList(dataset)``).
# 이런 호출은 ``_FIELD_CALL_RE`` 가 잡지 못하므로 분석기의 체인 walker 가
# helper 안의 SQL/RFC 를 놓친다. 우리는 bare call 도 ``receiver="this"``
# 로 synthetic 하게 수집해서 기존 same-class resolver 경로를 재사용한다.
#
# False-positive 억제는 두 단계로:
#   1. 여기서 Java 키워드 / ``new X(`` 생성자 호출을 skip
#   2. resolve 시 ``_find_method_in_class`` 가 같은 클래스에 실제로 그
#      이름의 메서드가 없으면 조용히 drop (static import 된 util,
#      type cast 형태 등)
_BARE_CALL_RE = re.compile(r"(?<!\.)\b(?P<method>\w+)\s*\(")

_BARE_CALL_SKIP = frozenset({
    # 제어 흐름 키워드 (괄호가 따라옴)
    "if", "while", "for", "switch", "catch", "return", "throw", "assert",
    "synchronized", "try", "do", "else", "yield",
    # 생성자 / 참조 키워드
    "new", "super", "this",
    # 원시 타입 (cast context 등 드문 경우 대비)
    "int", "long", "short", "byte", "char", "boolean", "float", "double", "void",
    "instanceof",
})

# Nested type declaration (inner class / interface / enum). When we see
# one of these in the outer class body we must skip its entire ``{...}``
# block so its methods are NOT picked up as top-level methods of the
# outer class — otherwise inner Builder/DTO methods leak SQL/RFC calls
# into the wrong row.
_NESTED_TYPE_DECL_RE = re.compile(
    r"""(?:\b(?:public|protected|private|static|final|abstract)\b\s+)*
        \b(?P<kind>class|interface|enum)\b\s+\w+
        [^{;]*?
        \{
    """,
    re.VERBOSE | re.DOTALL,
)

_METHOD_NAME_RESERVED = {
    "if", "while", "for", "switch", "catch", "return", "new", "throw",
    "synchronized", "try", "else", "do",
}


def _scan_balanced_braces(text: str, start: int) -> int:
    """Given ``text`` and an index ``start`` pointing at a ``{``, return
    the index just past the matching ``}``.

    Handles Java string literals (``"..."``) and char literals (``'.'``)
    plus line (``//``) and block (``/* */``) comments so that braces
    inside them do not affect depth.
    """
    n = len(text)
    if start >= n or text[start] != "{":
        return start
    depth = 0
    i = start
    in_str = None
    while i < n:
        c = text[i]
        if in_str is not None:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == in_str:
                in_str = None
            i += 1
            continue
        if c == '"' or c == "'":
            in_str = c
            i += 1
            continue
        if c == "/" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "/":
                nl = text.find("\n", i + 2)
                i = n if nl == -1 else nl + 1
                continue
            if nxt == "*":
                end = text.find("*/", i + 2)
                i = n if end == -1 else end + 2
                continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _extract_method_bodies(content: str, class_info: dict) -> list[dict]:
    """Walk the class body and return one entry per top-level method.

    Each entry has:
      * ``name``, ``signature``, ``line``
      * ``sig_start``, ``body_start``, ``body_end``
      * ``body`` — the body text

    Nested classes / inner classes / anonymous class methods are not
    promoted to top-level entries (the balanced-brace walker skips
    over any nested ``{...}`` encountered inside a method body).
    """
    if not class_info:
        return []
    header_end = class_info.get("header_end", 0)
    if header_end <= 0 or header_end > len(content):
        return []
    # Find the class's opening '{' (header_end points just past it).
    open_brace = header_end - 1
    if open_brace >= len(content) or content[open_brace] != "{":
        # Best effort: search forward for the first unescaped '{'
        open_brace = content.find("{", header_end - 1)
        if open_brace == -1:
            return []
    class_body_end = _scan_balanced_braces(content, open_brace)

    methods = []
    cursor = open_brace + 1
    while cursor < class_body_end:
        # Whichever comes first — a method signature or a nested type
        # (inner class / interface / enum) — gets processed. If a
        # nested type comes first, skip its entire balanced ``{...}``
        # so its methods are not promoted to the outer class.
        method_match = _METHOD_SIG_RE.search(content, cursor, class_body_end)
        nested_match = _NESTED_TYPE_DECL_RE.search(content, cursor, class_body_end)

        if nested_match is not None and (
            method_match is None or nested_match.start() < method_match.start()
        ):
            nested_open = nested_match.end() - 1  # position of the '{'
            nested_close = _scan_balanced_braces(content, nested_open)
            cursor = nested_close
            continue

        if method_match is None:
            break
        m = method_match
        name = m.group("name")
        if name in _METHOD_NAME_RESERVED or name in _METHOD_KEYWORDS:
            cursor = m.end()
            continue
        # Walk past the parameter list, respecting string literals and
        # nested parens.
        paren_depth = 1
        i = m.end()
        in_str = None
        found_close = False
        while i < class_body_end:
            c = content[i]
            if in_str is not None:
                if c == "\\" and i + 1 < class_body_end:
                    i += 2
                    continue
                if c == in_str:
                    in_str = None
            elif c == '"' or c == "'":
                in_str = c
            elif c == "(":
                paren_depth += 1
            elif c == ")":
                paren_depth -= 1
                if paren_depth == 0:
                    i += 1
                    found_close = True
                    break
            i += 1
        if not found_close:
            cursor = m.end()
            continue
        # Find the first '{' or ';' after the parameter list
        j = i
        while j < class_body_end and content[j] not in "{;":
            j += 1
        if j >= class_body_end or content[j] == ";":
            # Abstract method or interface signature — no body
            cursor = j + 1 if j < class_body_end else class_body_end
            continue
        b_end = _scan_balanced_braces(content, j)
        sig_text = content[m.start():j].strip()
        methods.append({
            "name": name,
            "signature": sig_text,
            "sig_start": m.start(),
            "body_start": j + 1,
            "body_end": b_end - 1,
            "body": content[j + 1:b_end - 1],
            "line": content.count("\n", 0, m.start()) + 1,
        })
        cursor = b_end
    return methods


def _collect_body_rfc_calls(body: str, constants: dict) -> list[dict]:
    """RFC calls inside a single method body (uses file-level constants).

    각 결과 dict 에 ``offset`` (body 내 byte offset) 포함 — 시퀀스
    다이어그램 Phase A 에서 호출 순서 정렬에 사용.
    """
    calls = []
    seen = set()
    for m in _RFC_GETFUNCTION_STR_RE.finditer(body):
        name = m.group(1)
        if name and name not in seen:
            seen.add(name)
            calls.append({"name": name, "resolved_from": "literal",
                          "offset": m.start()})
    for m in _RFC_GETFUNCTION_VAR_RE.finditer(body):
        ident = m.group(1)
        if ident in constants:
            name = constants[ident]
            if name not in seen:
                seen.add(name)
                calls.append({"name": name, "resolved_from": f"const:{ident}",
                              "offset": m.start()})
    if _rfc_custom_re:
        for m in _rfc_custom_re.finditer(body):
            iface_id = m.group("id")
            cls_name = m.group("cls") if m.group("cls") else ""
            rfc_name = f"{iface_id} ({cls_name})" if cls_name else iface_id
            if rfc_name not in seen:
                seen.add(rfc_name)
                calls.append({"name": rfc_name, "resolved_from": "custom-rfc",
                              "offset": m.start()})
    if _rfc_custom_var_re:
        for m in _rfc_custom_var_re.finditer(body):
            var = m.group("var")
            if var.startswith('"') or var[0].isdigit():
                continue
            resolved = constants.get(var, "")
            if not resolved:
                continue
            cls_name = m.group("cls") if m.group("cls") else ""
            rfc_name = f"{resolved} ({cls_name})" if cls_name else resolved
            if rfc_name not in seen:
                seen.add(rfc_name)
                calls.append({"name": rfc_name,
                              "resolved_from": f"custom-rfc-var:{var}",
                              "offset": m.start()})
    return calls


def _collect_body_sql_calls(body: str, ns_constants: dict | None = None) -> list[dict]:
    """SQL helper calls in a method body.

    Handles both literal ``"ns.id"`` and variable prefix ``var + "id"``
    forms. ``ns_constants`` is the class-level namespace string map.
    """
    ns_constants = ns_constants or {}
    results = []
    seen = set()

    for m in _SQL_CALL_RE.finditer(body):
        sqlid = m.group("sqlid")
        namespace, _, sql_id = sqlid.rpartition(".")
        if not namespace:
            continue
        key = (m.group("op"), sqlid)
        if key in seen:
            continue
        seen.add(key)
        results.append({
            "op": m.group("op"),
            "sqlid": sqlid,
            "namespace": namespace,
            "sql_id": sql_id,
            "offset": m.start(),
        })

    for m in _SQL_CALL_VAR_RE.finditer(body):
        var = m.group("var")
        suffix = m.group("suffix")
        prefix = ns_constants.get(var, "")
        if not prefix:
            continue
        # Sanity: the var + "suffix" concatenation must produce a
        # well-formed "namespace.id" string. If neither side carries the
        # "." separator, the resolved prefix is probably already a full
        # sqlid (e.g. `FIND_X = "scaStat.findX"` used as `FIND_X + "Y"`)
        # — concatenating would yield garbage like
        # "scaStat.findXY" which then misses every statement index and
        # pollutes the row via namespace-level fallback.
        if not prefix.endswith(".") and not suffix.startswith("."):
            continue
        sqlid = prefix + suffix
        namespace, _, sql_id = sqlid.rpartition(".")
        if not namespace:
            continue
        key = (m.group("op"), sqlid)
        if key in seen:
            continue
        seen.add(key)
        results.append({
            "op": m.group("op"),
            "sqlid": sqlid,
            "namespace": namespace,
            "sql_id": sql_id,
            "resolved_from": f"var:{var}",
            "offset": m.start(),
        })

    # Variable + ternary suffix: NS + (cond ? ".a" : ".b"). 두 branch 모두
    # 등록 — 런타임 조건에 따라 둘 다 호출될 수 있어 영향분석 / XML
    # 체인에 두 sqlid 모두 포함돼야 정확하다.
    for m in _SQL_CALL_TERNARY_RE.finditer(body):
        var = m.group("var")
        prefix = ns_constants.get(var, "")
        if not prefix:
            continue
        for suffix in (m.group("true_suffix"), m.group("false_suffix")):
            if not prefix.endswith(".") and not suffix.startswith("."):
                continue
            sqlid = prefix + suffix
            namespace, _, sql_id = sqlid.rpartition(".")
            if not namespace:
                continue
            key = (m.group("op"), sqlid)
            if key in seen:
                continue
            seen.add(key)
            results.append({
                "op": m.group("op"),
                "sqlid": sqlid,
                "namespace": namespace,
                "sql_id": sql_id,
                "resolved_from": f"var:{var}:ternary",
            })
    return results


def _collect_body_field_calls(body: str) -> list[dict]:
    """Collect ``receiver.method(`` + bare ``method(`` calls inside a body.

    Explicit ``receiver.method()`` calls are returned with the receiver
    as the field name. **Bare** ``method()`` calls (no receiver, no
    ``this.`` prefix) are returned with synthetic ``receiver="this"`` so
    the analyzer's same-class resolver handles them uniformly with
    ``this.method()`` calls — this covers the Vert.x / 레거시 패턴에서
    같은 클래스의 helper 메서드를 ``this.`` 없이 직접 호출하는 경우.

    False-positive 억제:
      * 여기서 Java 키워드 / ``new X(`` 생성자 호출 skip
      * resolve 시 ``_find_method_in_class`` 가 실제 메서드가 아니면 drop

    각 결과 dict 는 ``offset`` (첫 등장 지점의 body 내 byte offset)
    을 포함. 같은 ``(receiver, method)`` 쌍이 여러 번 나오면 첫 등장
    의 offset 만 유지 (다른 소비자 호환 유지). 시퀀스 다이어그램은
    이 offset 을 기준으로 call 순서를 정렬.
    """
    results = []
    seen = set()
    for m in _FIELD_CALL_RE.finditer(body):
        recv = m.group("receiver")
        meth = m.group("method")
        if recv in _METHOD_NAME_RESERVED:
            continue
        key = (recv, meth)
        if key in seen:
            continue
        seen.add(key)
        results.append({"receiver": recv, "method": meth, "offset": m.start()})

    for m in _BARE_CALL_RE.finditer(body):
        name = m.group("method")
        if name in _METHOD_NAME_RESERVED or name in _BARE_CALL_SKIP:
            continue
        # ``new X(`` 형태에서 ``X`` 만 bare call 처럼 매칭되는 케이스 제외.
        # ``new`` 자체는 _BARE_CALL_SKIP 로 걸렀지만 뒤따르는 타입명은
        # 여전히 매칭되기 때문에 앞 8글자 문맥을 확인.
        start = m.start()
        pre = body[max(0, start - 8):start]
        if re.search(r"\bnew\s+$", pre):
            continue
        key = ("this", name)
        if key in seen:
            continue
        seen.add(key)
        results.append({"receiver": "this", "method": name, "offset": start})
    return results


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
    # Nexcore (SK C&C framework): method-name convention endpoints
    if _is_nexcore_controller(class_info) and not endpoints:
        endpoints += _extract_nexcore_endpoints(body, class_paths)

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
    rfc_hint_count = _count_rfc_hints(raw)
    sql_calls = _extract_sql_calls(raw)

    # Per-method body extraction for precise call-graph resolution.
    # Both ``class_info`` and the body scan operate on ``content_nc``:
    # ``_strip_comments`` is offset-preserving so class_info.header_end
    # is a valid offset into content_nc, and comment content (which
    # could otherwise contain stray ``{`` like Javadoc ``{@link X}``)
    # has been blanked out — this matters for method body brace
    # balancing.
    rfc_constants = {n: v for n, v in _RFC_CONST_RE.findall(raw)}
    ns_constants = _extract_ns_constants(raw)
    methods = _extract_method_bodies(content_nc, class_info)
    for meth in methods:
        body = meth["body"]
        meth["body_sql_calls"] = _collect_body_sql_calls(body, ns_constants)
        meth["body_rfc_calls"] = _collect_body_rfc_calls(body, rfc_constants)
        meth["body_field_calls"] = _collect_body_field_calls(body)
        meth["is_endpoint"] = False

    # Link endpoint entries to the matching method (if any) so the
    # analyzer can find the correct method body at resolution time.
    method_by_name = {}
    for meth in methods:
        method_by_name.setdefault(meth["name"], []).append(meth)

    def _pick_handler_method():
        """Pick the most likely handler body for @RestVerticle-style
        one-class-per-endpoint patterns where ``method_name`` is the
        class name instead of the actual Java method.

        HTTP verb method names (``POST`` / ``GET`` / ``PUT`` / ``DELETE``
        / ``PATCH``) win over generic dispatcher names (``handle`` /
        ``execute`` / ``run`` / ``process``). Projects that pair
        ``@RestVerticle`` with a ``BaseRestHandler`` convention put the
        real per-verb business logic in the uppercase verb methods,
        while ``handle`` usually comes from the base class as an empty
        dispatcher — picking it would leave the endpoint row with no
        field calls and drop the service chain entirely.
        """
        for preferred in ("POST", "GET", "PUT", "DELETE", "PATCH",
                          "post", "get", "put", "delete", "patch"):
            if preferred in method_by_name:
                return method_by_name[preferred][0]
        for preferred in ("handle", "execute", "run", "process"):
            if preferred in method_by_name:
                return method_by_name[preferred][0]
        return methods[0] if methods else None

    for ep in endpoints:
        mname = ep.get("method_name")
        candidates = method_by_name.get(mname) if mname else None
        if not candidates and ep.get("annotation") == "RestVerticle":
            pick = _pick_handler_method()
            if pick is not None:
                candidates = [pick]
        if candidates:
            candidates[0]["is_endpoint"] = True
            ep["_method_idx"] = methods.index(candidates[0])

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
        "methods": methods,
        "rfc_calls": rfc_calls,
        "rfc_hint_count": rfc_hint_count,
        "sql_calls": sql_calls,
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
