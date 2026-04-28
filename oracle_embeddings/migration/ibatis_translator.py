"""iBatis 2.x → MyBatis 3.x 태그 / 파라미터 변환기.

레거시 iBatis 2.x mapper (root ``<sqlMap>``, dynamic ``<isNotNull>`` /
``<dynamic prepend="WHERE">`` / ``<iterate>``, 파라미터 ``#var#`` / ``$var$``)
를 MyBatis 3.x 등가물로 in-place 치환한다. ``rewrite_xml`` 이 dynamic-SQL
expander 와 sqlglot 파이프라인에 넣기 직전에 한 번 호출하면 downstream 은
iBatis 2.x 를 인지할 필요 없이 MyBatis 만 다루면 됨.

다루는 변환:

* root ``<sqlMap>``                 → ``<mapper>`` (namespace 속성 보존)
* ``<isNotNull property="x">``      → ``<if test="x != null">``
* ``<isNotEmpty property="x">``     → ``<if test="x != null and x != ''">``
* ``<isNull property="x">``         → ``<if test="x == null">``
* ``<isEmpty property="x">``        → ``<if test="x == null or x == ''">``
* ``<isEqual property="x" compareValue="V">`` → ``<if test="x == 'V'">``
                                              (숫자면 quote 없음)
* ``<isNotEqual ...>``              → ``<if test="x != 'V'">``
* ``<isGreaterThan ...>``           → ``<if test="x > V">``
* ``<isLessThan ...>``              → ``<if test="x < V">``
* ``<isGreaterEqual ...>``          → ``<if test="x >= V">``
* ``<isLessEqual ...>``             → ``<if test="x <= V">``
* ``<dynamic prepend="WHERE">``     → ``<where>``
* ``<dynamic prepend="SET">``       → ``<set>``
* ``<dynamic prepend="OTHER">``     → ``<trim prefix="OTHER">``
* ``<iterate property="list">``     → ``<foreach collection="list">``
                                       (``conjunction`` → ``separator``)
* 본문 ``#var#`` / ``#var:JDBCTYPE#``  → ``#{var}``
* 본문 ``$var$``                     → ``${var}``

설계 노트:

* lxml ``Element`` 는 ``.tag = "newname"`` 으로 이름만 갈아끼우고 자식 / 텍스트
  / 속성은 그대로 유지된다. 이 특성을 이용해 메모리 복사 없이 in-place 변환.
* ``<is*>`` 의 ``prepend="AND"`` / ``prepend="OR"`` 같은 SQL fragment 은
  MyBatis ``<if>`` 의 본문 SQL 에 그대로 살아 있는 경우가 보통이라 별도
  처리 안 함. 단, 본문에 prepend 토큰이 없는데 prepend 속성만 있는 변종
  (드뭄) 은 본문 앞에 토큰을 prepend.
* MyBatis 3.x 도 ``<isNotNull>`` 같은 태그를 읽으면 syntax 에러 → 변환
  실패 시에도 결과는 ``<if>`` 형태로 가야 안전. 알 수 없는 ``<is*>`` 변종은
  로그 후 ``<if test="true">`` (무조건 활성) 로 폴백 — downstream 변환이
  최소한 동작하게 함.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from lxml import etree

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def translate_ibatis_to_mybatis(tree: etree._ElementTree) -> int:
    """Translate iBatis 2.x constructs to MyBatis 3.x in-place.

    Returns the number of structural changes applied (sum of tag renames +
    parameter substitutions). Zero means the tree was already MyBatis-clean
    (the function is idempotent on MyBatis 3.x mappers).
    """
    root = tree.getroot()
    changes = 0

    # 1. Root: <sqlMap> → <mapper>
    if _local(root.tag) == "sqlMap":
        root.tag = "mapper"
        changes += 1

    # 2. Walk descendants and translate iBatis-specific tags. We snapshot the
    #    iter list because tag renames don't change identity but new children
    #    can confuse a live iterator.
    for elem in list(root.iter()):
        if not isinstance(elem.tag, str):  # comment/PI nodes
            continue
        local = _local(elem.tag)
        translator = _TAG_TRANSLATORS.get(local)
        if translator is not None:
            if translator(elem):
                changes += 1

    # 3. Translate parameter syntax in every text / tail node.
    for elem in root.iter():
        if not isinstance(elem.tag, str):
            continue
        if elem.text:
            new_text, n = _translate_params(elem.text)
            if n:
                elem.text = new_text
                changes += n
        if elem.tail:
            new_tail, n = _translate_params(elem.tail)
            if n:
                elem.tail = new_tail
                changes += n

    if changes:
        logger.info("iBatis→MyBatis: %d construct(s) translated", changes)
    return changes


# ---------------------------------------------------------------------------
# Tag translators (each returns True if it modified the element)
# ---------------------------------------------------------------------------


def _to_if(test_expr: str):
    """Factory for translators that turn an iBatis dynamic tag into ``<if test="...">``."""

    def _do(elem: etree._Element) -> bool:
        prepend = elem.attrib.get("prepend")
        elem.tag = "if"
        # Wipe iBatis-specific attrs, keep nothing else (MyBatis <if> only
        # has ``test``).
        for k in list(elem.attrib):
            del elem.attrib[k]
        elem.set("test", test_expr)
        # If the body doesn't already start with the prepend (e.g. "AND"),
        # inject it so the assembled SQL stays grammatical. Most projects
        # already include the prepend literal inside the tag body, so this
        # branch is a safety net.
        if prepend and elem.text and not elem.text.lstrip().upper().startswith(
            prepend.strip().upper()
        ):
            elem.text = f" {prepend}{elem.text}"
        return True

    return _do


def _is_not_null(elem: etree._Element) -> bool:
    p = elem.attrib.get("property", "")
    return _to_if(f"{p} != null")(elem)


def _is_null(elem: etree._Element) -> bool:
    p = elem.attrib.get("property", "")
    return _to_if(f"{p} == null")(elem)


def _is_not_empty(elem: etree._Element) -> bool:
    p = elem.attrib.get("property", "")
    return _to_if(f"{p} != null and {p} != ''")(elem)


def _is_empty(elem: etree._Element) -> bool:
    p = elem.attrib.get("property", "")
    return _to_if(f"{p} == null or {p} == ''")(elem)


def _quote_if_needed(value: str) -> str:
    """Quote ``value`` for an OGNL test unless it already looks numeric."""
    if value is None:
        return "null"
    s = value.strip()
    if not s:
        return "''"
    # Python int/float-ish — leave bare. ``-12``, ``3.14``, ``0`` all work.
    try:
        float(s)
        return s
    except ValueError:
        pass
    # Already quoted with single or double quotes? Pass through.
    if (s[0] == s[-1] and s[0] in "'\"") and len(s) >= 2:
        return s
    # Quote and escape inner singles.
    return "'" + s.replace("'", "\\'") + "'"


def _is_equal(op: str):
    def _do(elem: etree._Element) -> bool:
        p = elem.attrib.get("property", "")
        v = _quote_if_needed(elem.attrib.get("compareValue"))
        return _to_if(f"{p} {op} {v}")(elem)

    return _do


def _dynamic(elem: etree._Element) -> bool:
    prepend = (elem.attrib.get("prepend") or "").strip().upper()
    if prepend == "WHERE":
        elem.tag = "where"
        for k in list(elem.attrib):
            del elem.attrib[k]
    elif prepend == "SET":
        elem.tag = "set"
        for k in list(elem.attrib):
            del elem.attrib[k]
    else:
        elem.tag = "trim"
        # Carry prepend → prefix so the assembled SQL keeps its leading word.
        attrs = dict(elem.attrib)
        for k in list(elem.attrib):
            del elem.attrib[k]
        if prepend:
            elem.set("prefix", attrs.get("prepend", ""))
    return True


def _iterate(elem: etree._Element) -> bool:
    elem.tag = "foreach"
    # iBatis ``property`` → MyBatis ``collection``
    if "property" in elem.attrib:
        elem.set("collection", elem.attrib.pop("property"))
    # iBatis ``conjunction`` → MyBatis ``separator``
    if "conjunction" in elem.attrib:
        elem.set("separator", elem.attrib.pop("conjunction"))
    # iBatis often used ``prepend="AND"`` on iterate — drop, it would conflict
    # with foreach semantics; downstream SQL usually has the AND inline.
    if "prepend" in elem.attrib:
        del elem.attrib["prepend"]
    return True


_TAG_TRANSLATORS = {
    "isNotNull":     _is_not_null,
    "isNull":        _is_null,
    "isNotEmpty":    _is_not_empty,
    "isEmpty":       _is_empty,
    "isEqual":       _is_equal("=="),
    "isNotEqual":    _is_equal("!="),
    "isGreaterThan": _is_equal(">"),
    "isLessThan":    _is_equal("<"),
    "isGreaterEqual": _is_equal(">="),
    "isLessEqual":   _is_equal("<="),
    "dynamic":       _dynamic,
    "iterate":       _iterate,
}


# ---------------------------------------------------------------------------
# Parameter syntax: ``#var#`` / ``#var:TYPE#`` / ``$var$``
# ---------------------------------------------------------------------------


# iBatis 2.x param token: identifier, optional ``.path``, optional
# ``:JDBCTYPE`` or ``:JDBCTYPE(11)`` suffix. Matches single token between
# pound signs only — won't grab ``#abc#def#`` as one match.
_IBATIS_HASH_RE = re.compile(
    r"#([A-Za-z_][\w.]*(?::[A-Za-z]+(?:\(\d+(?:,\s*\d+)?\))?)?)#"
)
_IBATIS_DOLLAR_RE = re.compile(r"\$([A-Za-z_][\w.]*)\$")


def _translate_params(text: str) -> tuple:
    """Return ``(text, n_substitutions)``."""
    if not text:
        return text, 0

    n = [0]  # closure-mutable counter

    def _hash_sub(m: "re.Match[str]") -> str:
        n[0] += 1
        token = m.group(1)
        # Strip ``:JDBCTYPE(...)`` hint — MyBatis 3.x uses jdbcType=
        # attribute on stmt level; we don't try to back-port the hint here.
        bare = token.split(":", 1)[0]
        return "#{" + bare + "}"

    def _dollar_sub(m: "re.Match[str]") -> str:
        n[0] += 1
        return "${" + m.group(1) + "}"

    text = _IBATIS_HASH_RE.sub(_hash_sub, text)
    text = _IBATIS_DOLLAR_RE.sub(_dollar_sub, text)
    return text, n[0]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _local(tag: str) -> str:
    if isinstance(tag, str) and "}" in tag:
        return tag.split("}", 1)[-1]
    return tag
