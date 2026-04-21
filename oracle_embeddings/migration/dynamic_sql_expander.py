"""Expand MyBatis dynamic SQL into a small set of representative static paths.

Level 1 (this module, Steps 4): produces **two** rendered paths per statement:

* ``maximum`` — every ``<if>``/``<when>`` is treated as true; the first ``<when>``
  inside a ``<choose>`` wins; ``<foreach>`` is rendered with a single dummy item.
* ``minimum`` — every ``<if>``/``<when>`` is treated as false; ``<otherwise>``
  wins inside ``<choose>``; ``<foreach>`` is omitted entirely.

The two paths together cover the vast majority of "will this SQL still parse"
checks. Level 2 (coverage greedy) and Level 3 (foreach sampling) are added
later in Step 10 — the current shape intentionally keeps the render loop small
and deterministic.

Public API: :func:`expand_paths` (statement-element in, list of
:class:`ExpandedPath` out) and :func:`build_sql_includes` (root mapper tree →
``{refid: element}`` map used for ``<include>`` inlining).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from lxml import etree

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ExpandedPath:
    """One rendered static SQL, with the activation pattern that produced it.

    ``activations`` maps a per-tag key (``if[1]`` / ``choose[0]/when[0]`` …)
    to ``True`` / ``False`` so downstream tooling can tell which AS-IS branch
    a given TO-BE SQL corresponds to.
    ``covered_columns`` is empty in Level 1 and populated by Level 2.
    """

    rendered_sql: str
    activations: Dict[str, bool] = field(default_factory=dict)
    covered_columns: Set[str] = field(default_factory=set)
    label: str = ""


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


_DYNAMIC_TAGS = {
    "if", "choose", "when", "otherwise",
    "where", "set", "trim", "foreach", "bind", "include",
}
_STATEMENT_TAGS = {"select", "insert", "update", "delete", "sql"}


def build_sql_includes(root: etree._Element) -> Dict[str, etree._Element]:
    """Collect ``<sql id="...">`` fragments from a MyBatis mapper.

    ``<include refid="x">`` references are resolved in-place during expansion
    by looking up ``x`` in this map. We only index the direct mapper for now —
    cross-file includes (rare) would need a namespace-aware resolver.
    """

    out: Dict[str, etree._Element] = {}
    for sql in root.iter():
        if _local(sql.tag) != "sql":
            continue
        refid = sql.get("id")
        if refid:
            out[refid] = sql
    return out


def expand_paths(
    stmt_elem: etree._Element,
    *,
    sql_includes: Optional[Dict[str, etree._Element]] = None,
    max_paths: int = 10,
    level: int = 2,
) -> List[ExpandedPath]:
    """Return representative static SQL paths for ``stmt_elem``.

    ``level`` controls how aggressively the alternatives are sampled:

    * ``1``: max (all ``<if>`` true, first ``<when>`` wins, foreach n=1)
      plus min (all ``<if>`` false, ``<otherwise>`` wins, foreach n=0).
    * ``2``: adds one path per non-first ``<when>`` / ``<otherwise>`` branch
      of every ``<choose>`` so each branch's columns are exercised.
    * ``3``: additionally adds a ``foreach n=2`` sample — useful for catching
      separator handling issues.

    Duplicate renderings (common when a ``<choose>`` branch produces the same
    SQL as an earlier path) are dropped. The final list is capped at
    ``max_paths`` entries.
    """

    includes = sql_includes if sql_includes is not None else {}

    paths: List[ExpandedPath] = []

    maximum = _render(stmt_elem, activate_all=True, includes=includes)
    maximum.label = "max"
    paths.append(maximum)

    minimum = _render(stmt_elem, activate_all=False, includes=includes)
    minimum.label = "min"
    paths.append(minimum)

    if level >= 2:
        chooses = [
            c for c in stmt_elem.iter() if _local(c.tag) == "choose"
        ]
        for c_idx, choose in enumerate(chooses):
            whens = [w for w in choose if _local(w.tag) == "when"]
            otherwise = [w for w in choose if _local(w.tag) == "otherwise"]
            # Max already exercises whens[0]; add branches >= 1 and <otherwise>.
            for w_idx in range(1, len(whens)):
                p = _render(
                    stmt_elem, activate_all=True, includes=includes,
                    choose_override={id(choose): w_idx},
                )
                p.label = f"choose[{c_idx}]/when[{w_idx}]"
                paths.append(p)
            if otherwise:
                p = _render(
                    stmt_elem, activate_all=True, includes=includes,
                    choose_override={id(choose): -1},  # -1 = otherwise
                )
                p.label = f"choose[{c_idx}]/otherwise"
                paths.append(p)

    if level >= 3:
        foreachs = [
            c for c in stmt_elem.iter() if _local(c.tag) == "foreach"
        ]
        if foreachs:
            p = _render(
                stmt_elem, activate_all=True, includes=includes,
                foreach_items=2,
            )
            p.label = "foreach[n=2]"
            paths.append(p)

    # Dedup + cap
    seen: Dict[str, bool] = {}
    unique: List[ExpandedPath] = []
    for p in paths:
        if p.rendered_sql in seen:
            continue
        seen[p.rendered_sql] = True
        unique.append(p)
        if len(unique) >= max_paths:
            break
    return unique


# ---------------------------------------------------------------------------
# Internal renderer
# ---------------------------------------------------------------------------


def _render(
    elem: etree._Element,
    *,
    activate_all: bool,
    includes: Dict[str, etree._Element],
    choose_override: Optional[Dict[int, int]] = None,
    foreach_items: int = 1,
) -> ExpandedPath:
    """Render one path. ``choose_override`` maps ``id(choose_elem) → branch
    index`` (``-1`` = ``<otherwise>``). ``foreach_items`` controls how many
    dummy iterations ``<foreach>`` emits when ``activate_all`` is True."""
    activations: Dict[str, bool] = {}
    parts = _walk(
        elem,
        activate_all=activate_all,
        activations=activations,
        includes=includes,
        include_stack=set(),
        counter=_Counter(),
        choose_override=choose_override or {},
        foreach_items=foreach_items,
    )
    sql = _clean(" ".join(p for p in parts if p))
    return ExpandedPath(rendered_sql=sql, activations=activations)


def _walk(
    elem: etree._Element,
    *,
    activate_all: bool,
    activations: Dict[str, bool],
    includes: Dict[str, etree._Element],
    include_stack: Set[str],
    counter: "_Counter",
    choose_override: Dict[int, int],
    foreach_items: int,
) -> List[str]:
    """Walk ``elem`` yielding SQL text chunks.

    The caller passes a shared ``_Counter`` so activation keys are stable
    across the whole statement (not just per-subtree)."""

    parts: List[str] = []
    if elem.text:
        parts.append(elem.text)

    for child in elem:
        tag = _local(child.tag)
        parts.extend(
            _render_child(
                child, tag,
                activate_all=activate_all,
                activations=activations,
                includes=includes,
                include_stack=include_stack,
                counter=counter,
                choose_override=choose_override,
                foreach_items=foreach_items,
            )
        )
        if child.tail:
            parts.append(child.tail)
    return parts


def _render_child(
    child: etree._Element,
    tag: str,
    *,
    activate_all: bool,
    activations: Dict[str, bool],
    includes: Dict[str, etree._Element],
    include_stack: Set[str],
    counter: "_Counter",
    choose_override: Dict[int, int],
    foreach_items: int,
) -> List[str]:
    kwargs = dict(
        activate_all=activate_all,
        activations=activations,
        includes=includes,
        include_stack=include_stack,
        counter=counter,
        choose_override=choose_override,
        foreach_items=foreach_items,
    )

    if tag == "if":
        key = counter.emit("if")
        test = child.get("test", "")
        activations[f"{key}[test={test}]"] = activate_all
        if activate_all:
            return _walk(child, **kwargs)
        return []

    if tag == "choose":
        return _render_choose(child, **kwargs)
    if tag == "where":
        return _render_where(child, **kwargs)
    if tag == "set":
        return _render_set(child, **kwargs)
    if tag == "trim":
        return _render_trim(child, **kwargs)
    if tag == "foreach":
        return _render_foreach(child, **kwargs)
    if tag == "bind":
        return []  # binds don't emit SQL
    if tag == "include":
        return _render_include(child, **kwargs)

    # Unknown/other element — walk through and emit its text + tail
    return _walk(child, **kwargs)


def _render_choose(
    elem: etree._Element,
    *,
    activate_all: bool,
    activations: Dict[str, bool],
    includes: Dict[str, etree._Element],
    include_stack: Set[str],
    counter: "_Counter",
    choose_override: Dict[int, int],
    foreach_items: int,
) -> List[str]:
    choose_key = counter.emit("choose")
    whens = [c for c in elem if _local(c.tag) == "when"]
    otherwise = [c for c in elem if _local(c.tag) == "otherwise"]

    kwargs = dict(
        activate_all=activate_all,
        activations=activations,
        includes=includes,
        include_stack=include_stack,
        counter=counter,
        choose_override=choose_override,
        foreach_items=foreach_items,
    )

    # Max mode: first <when> wins (or override index). Min: <otherwise>.
    if activate_all and whens:
        override = choose_override.get(id(elem))
        # override == -1 → take otherwise; valid index → that when; else 0.
        if override == -1 and otherwise:
            for i, _ in enumerate(whens):
                activations[f"{choose_key}/when[{i}]"] = False
            activations[f"{choose_key}/otherwise"] = True
            return _walk(otherwise[0], **kwargs)
        chosen = override if (isinstance(override, int) and 0 <= override < len(whens)) else 0
        for i, w in enumerate(whens):
            activations[f"{choose_key}/when[{i}]"] = (i == chosen)
        if otherwise:
            activations[f"{choose_key}/otherwise"] = False
        return _walk(whens[chosen], **kwargs)

    # Min mode
    for i, w in enumerate(whens):
        activations[f"{choose_key}/when[{i}]"] = False
    if otherwise:
        activations[f"{choose_key}/otherwise"] = True
        return _walk(otherwise[0], **kwargs)
    return []


def _render_where(elem, **kwargs) -> List[str]:
    inner = " ".join(p for p in _walk(elem, **kwargs) if p).strip()
    if not inner:
        return []
    # Strip leading AND / OR (case-insensitive)
    inner = re.sub(r"^(AND|OR)\s+", "", inner, flags=re.IGNORECASE)
    if not inner:
        return []
    return [f"WHERE {inner}"]


def _render_set(elem, **kwargs) -> List[str]:
    inner = " ".join(p for p in _walk(elem, **kwargs) if p).strip()
    if not inner:
        return []
    # Strip trailing comma
    inner = re.sub(r",\s*$", "", inner)
    if not inner:
        return []
    return [f"SET {inner}"]


def _render_trim(elem, **kwargs) -> List[str]:
    prefix = elem.get("prefix", "") or ""
    suffix = elem.get("suffix", "") or ""
    prefix_overrides = (elem.get("prefixOverrides") or "").split("|")
    suffix_overrides = (elem.get("suffixOverrides") or "").split("|")

    inner = " ".join(p for p in _walk(elem, **kwargs) if p).strip()
    if not inner:
        return []

    # Strip prefixOverrides / suffixOverrides. MyBatis matches the literal
    # substring at the boundary — NOT word-bounded. So ``suffixOverrides=","``
    # must match a trailing comma directly (`REG_DT = #{dt},`), while
    # ``prefixOverrides="AND |OR "`` must match leading ``AND `` or ``OR ``.
    # Longest-first so "AND " wins over "A".
    for p in sorted((s for s in prefix_overrides if s), key=len, reverse=True):
        if inner.upper().startswith(p.upper()):
            inner = inner[len(p):].lstrip()
            break
    for s in sorted((x for x in suffix_overrides if x), key=len, reverse=True):
        if inner.upper().endswith(s.upper()):
            inner = inner[: -len(s)].rstrip()
            break

    if not inner:
        return []
    pieces = [prefix.strip(), inner, suffix.strip()]
    return [" ".join(x for x in pieces if x)]


def _render_foreach(
    elem: etree._Element,
    *,
    activate_all: bool,
    activations: Dict[str, bool],
    includes: Dict[str, etree._Element],
    include_stack: Set[str],
    counter: "_Counter",
    choose_override: Dict[int, int],
    foreach_items: int,
) -> List[str]:
    fe_key = counter.emit("foreach")
    if not activate_all:
        activations[f"{fe_key}[items=0]"] = True
        return []

    open_ = elem.get("open", "") or ""
    close_ = elem.get("close", "") or ""
    separator = elem.get("separator", "") or ""

    inner_parts = _walk(
        elem,
        activate_all=activate_all,
        activations=activations,
        includes=includes,
        include_stack=include_stack,
        counter=counter,
        choose_override=choose_override,
        foreach_items=foreach_items,
    )
    inner = " ".join(p for p in inner_parts if p).strip()
    if not inner:
        activations[f"{fe_key}[items=1]"] = True
        return [f"{open_}{close_}".strip()] if (open_ or close_) else []

    n = max(1, int(foreach_items))
    activations[f"{fe_key}[items={n}]"] = True

    if n == 1:
        rendered = (open_ + " " + inner + " " + close_).strip()
    else:
        body_list = (" " + separator + " ").join([inner] * n)
        rendered = (open_ + " " + body_list + " " + close_).strip()
    return [rendered]


def _render_include(
    elem: etree._Element,
    *,
    activate_all: bool,
    activations: Dict[str, bool],
    includes: Dict[str, etree._Element],
    include_stack: Set[str],
    counter: "_Counter",
    choose_override: Dict[int, int],
    foreach_items: int,
) -> List[str]:
    refid = elem.get("refid")
    if not refid:
        return []
    if refid in include_stack:
        logger.warning("Circular <include refid=%r>; skipping", refid)
        return []
    target = includes.get(refid)
    if target is None:
        logger.warning("Unresolved <include refid=%r>", refid)
        return []
    include_stack.add(refid)
    try:
        return _walk(
            target,
            activate_all=activate_all,
            activations=activations,
            includes=includes,
            include_stack=include_stack,
            counter=counter,
            choose_override=choose_override,
            foreach_items=foreach_items,
        )
    finally:
        include_stack.discard(refid)


# ---------------------------------------------------------------------------
# Misc utilities
# ---------------------------------------------------------------------------


class _Counter:
    """Monotonic per-tag counter for stable activation keys."""

    def __init__(self) -> None:
        self._counts: Dict[str, int] = {}

    def emit(self, tag: str) -> str:
        i = self._counts.get(tag, 0)
        self._counts[tag] = i + 1
        return f"{tag}[{i}]"


_WHITESPACE_RE = re.compile(r"[ \t\r\n]+")


def _clean(s: str) -> str:
    """Collapse whitespace runs and trim surrounding space."""
    return _WHITESPACE_RE.sub(" ", s).strip()


def _local(tag: str) -> str:
    """Strip namespace prefix from an lxml tag."""
    if isinstance(tag, str) and "}" in tag:
        return tag.split("}", 1)[-1]
    return tag
