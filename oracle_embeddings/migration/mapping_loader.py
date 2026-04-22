"""Load + validate ``column_mapping.yaml`` (docs/migration/spec.md §4).

Entry point: :func:`load_mapping`. Collects every structural error it can
before raising a :class:`~mapping_model.LoaderErrorGroup`, so the user fixes
the whole file in one pass instead of whack-a-mole.

sqlglot is imported lazily inside expression validators so that unrelated
commands (schema, query, analyze-legacy…) keep working in environments where
sqlglot is not yet installed.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import yaml

from .mapping_model import (
    ColumnAction,
    ColumnMapping,
    ColumnRef,
    CommentScope,
    CommentSource,
    DefaultSchema,
    LoaderError,
    LoaderErrorGroup,
    Mapping,
    MappingOptions,
    SplitTarget,
    TableMapping,
    TableType,
    TransformSpec,
    UnknownTableAction,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

_VALID_COMMENT_SCOPES: Set[str] = {"select", "update", "insert", "where", "join"}
_VALID_COMMENT_SOURCES: Set[str] = {
    "to_be_schema", "terms_dictionary", "both", "mapping", "mapping_first",
}
_VALID_UNKNOWN_TABLE_ACTIONS: Set[str] = {"warn", "error", "drop"}
_VALID_TABLE_TYPES: Set[str] = {"rename", "split", "merge", "drop"}
_VALID_COLUMN_ACTIONS: Set[str] = {"convert", "drop_with_warning"}


# ---------------------------------------------------------------------------
# Error accumulator
# ---------------------------------------------------------------------------


class _Errors:
    """Internal collector. Keeps errors in declaration order."""

    def __init__(self) -> None:
        self._errors: List[LoaderError] = []

    def add(self, message: str, location: Optional[str] = None) -> None:
        self._errors.append(LoaderError(message, location))

    def extend(self, errors: List[LoaderError]) -> None:
        self._errors.extend(errors)

    def __len__(self) -> int:
        return len(self._errors)

    def snapshot(self) -> List[LoaderError]:
        return list(self._errors)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_mapping(
    path: Union[str, Path],
    *,
    as_is_schema: Optional[Dict[str, Set[str]]] = None,
    to_be_schema: Optional[Dict[str, Set[str]]] = None,
) -> Mapping:
    """Load and validate a ``column_mapping.yaml`` file.

    Parameters
    ----------
    path
        Path to the YAML.
    as_is_schema / to_be_schema
        Optional {TABLE_UPPER: {COLUMN_UPPER, ...}} maps. If provided, the
        loader additionally checks that referenced tables/columns exist.
        Raising behaviour follows the ``options.unknown_table_action`` field.

    Raises
    ------
    LoaderErrorGroup
        When at least one structural error is found.
    """

    p = Path(path)
    if not p.exists():
        raise LoaderError(f"mapping file not found: {p}")

    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise LoaderError(f"mapping file must be UTF-8: {exc}") from exc

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise LoaderError(f"YAML syntax error: {exc}") from exc

    if not isinstance(raw, dict):
        raise LoaderError("top-level YAML must be a mapping/dict")

    errors = _Errors()

    version = _require_str(raw, "version", errors, default="1.0")
    default_schema = _parse_default_schema(raw.get("default_schema"), errors)
    options = _parse_options(raw.get("options"), errors)
    tables = _parse_tables(raw.get("tables"), errors)
    columns = _parse_columns(raw.get("columns"), errors)

    mapping = Mapping(
        version=version or "1.0",
        default_schema=default_schema,
        options=options,
        tables=tables,
        columns=columns,
    )
    _build_indexes(mapping, errors)
    _cross_validate(mapping, errors, as_is_schema, to_be_schema)

    if len(errors):
        raise LoaderErrorGroup(errors.snapshot())
    return mapping


def load_mapping_collect(
    path: Union[str, Path],
    *,
    as_is_schema: Optional[Dict[str, Set[str]]] = None,
    to_be_schema: Optional[Dict[str, Set[str]]] = None,
) -> Tuple[Optional[Mapping], List[LoaderError]]:
    """Same as :func:`load_mapping` but returns ``(mapping_or_None, errors)``
    instead of raising — useful for `migration-impact` which wants to surface
    every problem in one report."""

    try:
        mapping = load_mapping(
            path, as_is_schema=as_is_schema, to_be_schema=to_be_schema
        )
        return mapping, []
    except LoaderErrorGroup as grp:
        return None, grp.errors
    except LoaderError as e:
        return None, [e]


# ---------------------------------------------------------------------------
# Section parsers
# ---------------------------------------------------------------------------


def _require_str(
    parent: Dict[str, Any],
    key: str,
    errors: _Errors,
    *,
    default: Optional[str] = None,
    location_prefix: str = "",
) -> Optional[str]:
    if key not in parent:
        if default is None:
            errors.add(f"missing required field '{key}'", location_prefix or None)
        return default
    value = parent[key]
    if not isinstance(value, str):
        errors.add(
            f"'{key}' must be a string (got {type(value).__name__})",
            _loc(location_prefix, key),
        )
        return default
    return value


def _parse_default_schema(node: Any, errors: _Errors) -> DefaultSchema:
    if node is None:
        return DefaultSchema()
    if not isinstance(node, dict):
        errors.add("default_schema must be a mapping", "default_schema")
        return DefaultSchema()
    as_is = node.get("as_is", "LEGACY")
    to_be = node.get("to_be", "NEW")
    if not isinstance(as_is, str):
        errors.add("default_schema.as_is must be a string", "default_schema.as_is")
        as_is = "LEGACY"
    if not isinstance(to_be, str):
        errors.add("default_schema.to_be must be a string", "default_schema.to_be")
        to_be = "NEW"
    return DefaultSchema(as_is=as_is, to_be=to_be)


def _parse_options(node: Any, errors: _Errors) -> MappingOptions:
    opts = MappingOptions()
    if node is None:
        return opts
    if not isinstance(node, dict):
        errors.add("options must be a mapping", "options")
        return opts

    if "emit_column_comments" in node:
        v = node["emit_column_comments"]
        if not isinstance(v, bool):
            errors.add(
                "options.emit_column_comments must be boolean",
                "options.emit_column_comments",
            )
        else:
            opts.emit_column_comments = v

    if "comment_scope" in node:
        v = node["comment_scope"]
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            errors.add(
                "options.comment_scope must be a list of strings",
                "options.comment_scope",
            )
        else:
            bad = [x for x in v if x not in _VALID_COMMENT_SCOPES]
            if bad:
                errors.add(
                    f"options.comment_scope has invalid value(s) {bad}; "
                    f"allowed: {sorted(_VALID_COMMENT_SCOPES)}",
                    "options.comment_scope",
                )
            else:
                opts.comment_scope = list(v)  # type: ignore[assignment]

    if "comment_source" in node:
        v = node["comment_source"]
        if v not in _VALID_COMMENT_SOURCES:
            errors.add(
                f"options.comment_source must be one of "
                f"{sorted(_VALID_COMMENT_SOURCES)} (got {v!r})",
                "options.comment_source",
            )
        else:
            opts.comment_source = v  # type: ignore[assignment]

    if "comment_format" in node:
        v = node["comment_format"]
        if not isinstance(v, str):
            errors.add(
                "options.comment_format must be a string",
                "options.comment_format",
            )
        else:
            opts.comment_format = v

    if "unknown_table_action" in node:
        v = node["unknown_table_action"]
        if v not in _VALID_UNKNOWN_TABLE_ACTIONS:
            errors.add(
                f"options.unknown_table_action must be one of "
                f"{sorted(_VALID_UNKNOWN_TABLE_ACTIONS)} (got {v!r})",
                "options.unknown_table_action",
            )
        else:
            opts.unknown_table_action = v  # type: ignore[assignment]

    if "output_format" in node:
        v = node["output_format"]
        if not isinstance(v, dict):
            errors.add(
                "options.output_format must be a mapping",
                "options.output_format",
            )
        else:
            _parse_output_format(v, opts.output_format, errors)

    return opts


_VALID_FORMAT_STYLES: Set[str] = {"none", "korean_legacy", "ansi"}


def _parse_output_format(node: Dict[str, Any], of, errors: _Errors) -> None:
    loc = "options.output_format"
    if "style" in node:
        v = node["style"]
        if v not in _VALID_FORMAT_STYLES:
            errors.add(
                f"{loc}.style must be one of {sorted(_VALID_FORMAT_STYLES)} "
                f"(got {v!r})", f"{loc}.style",
            )
        else:
            of.style = v
    for key, attr, kind in [
        ("indent", "indent", int),
        ("keyword_case", "keyword_case", str),
        ("leading_comma", "leading_comma", bool),
        ("table_comment_prefix", "table_comment_prefix", str),
        ("normalize_comment_width", "normalize_comment_width", bool),
    ]:
        if key in node:
            v = node[key]
            if not isinstance(v, kind):
                errors.add(f"{loc}.{key} must be {kind.__name__}", f"{loc}.{key}")
            else:
                setattr(of, attr, v)


def _parse_tables(node: Any, errors: _Errors) -> List[TableMapping]:
    if node is None:
        return []
    if not isinstance(node, list):
        errors.add("tables must be a list", "tables")
        return []

    out: List[TableMapping] = []
    for idx, entry in enumerate(node):
        loc = f"tables[{idx}]"
        if not isinstance(entry, dict):
            errors.add("table entry must be a mapping", loc)
            continue

        ttype = entry.get("type")
        if ttype not in _VALID_TABLE_TYPES:
            errors.add(
                f"tables[{idx}].type must be one of {sorted(_VALID_TABLE_TYPES)} "
                f"(got {ttype!r})",
                f"{loc}.type",
            )
            continue

        as_is = entry.get("as_is")
        to_be = entry.get("to_be", None) if "to_be" in entry else None
        as_is_ok = _check_table_shape(ttype, as_is, to_be, errors, loc)
        if not as_is_ok:
            continue

        cm = entry.get("comment")
        if cm is not None and not isinstance(cm, str):
            errors.add("'comment' must be a string", f"{loc}.comment")
            cm = None
        tm = TableMapping(
            type=ttype,  # type: ignore[arg-type]
            as_is=as_is,
            to_be=to_be,
            discriminator_column=entry.get("discriminator_column"),
            discriminator_map=entry.get("discriminator_map"),
            join_condition=entry.get("join_condition"),
            comment=cm,
        )

        # Extra shape checks per type ------------------------------------
        if ttype == "split":
            if tm.discriminator_column is None:
                errors.add(
                    "split table requires 'discriminator_column'",
                    f"{loc}.discriminator_column",
                )
            if tm.discriminator_map is None:
                errors.add(
                    "split table requires 'discriminator_map'",
                    f"{loc}.discriminator_map",
                )
            elif not isinstance(tm.discriminator_map, dict):
                errors.add(
                    "discriminator_map must be a mapping",
                    f"{loc}.discriminator_map",
                )
        if ttype == "merge":
            if tm.join_condition is None:
                errors.add(
                    "merge table requires 'join_condition'",
                    f"{loc}.join_condition",
                )
            elif not isinstance(tm.join_condition, str):
                errors.add(
                    "join_condition must be a string",
                    f"{loc}.join_condition",
                )

        out.append(tm)
    return out


def _check_table_shape(
    ttype: str,
    as_is: Any,
    to_be: Any,
    errors: _Errors,
    loc: str,
) -> bool:
    """Enforce the ``as_is`` / ``to_be`` shape described in spec §4."""

    if ttype == "rename":
        ok = isinstance(as_is, str) and isinstance(to_be, str)
        if not ok:
            errors.add(
                "rename requires as_is=str, to_be=str",
                loc,
            )
        return ok
    if ttype == "split":
        ok = (
            isinstance(as_is, str)
            and isinstance(to_be, list)
            and all(isinstance(x, str) for x in to_be)
            and len(to_be) >= 2
        )
        if not ok:
            errors.add(
                "split requires as_is=str, to_be=list[str] with >= 2 items",
                loc,
            )
        return ok
    if ttype == "merge":
        ok = (
            isinstance(as_is, list)
            and all(isinstance(x, str) for x in as_is)
            and len(as_is) >= 2
            and isinstance(to_be, str)
        )
        if not ok:
            errors.add(
                "merge requires as_is=list[str] with >= 2 items, to_be=str",
                loc,
            )
        return ok
    if ttype == "drop":
        ok = isinstance(as_is, str) and to_be is None
        if not ok:
            errors.add("drop requires as_is=str, to_be=null", loc)
        return ok
    return False


def _parse_columns(node: Any, errors: _Errors) -> List[ColumnMapping]:
    if node is None:
        return []
    if not isinstance(node, list):
        errors.add("columns must be a list", "columns")
        return []

    out: List[ColumnMapping] = []
    for idx, entry in enumerate(node):
        loc = f"columns[{idx}]"
        if not isinstance(entry, dict):
            errors.add("column entry must be a mapping", loc)
            continue

        as_is = _parse_column_as_is(entry.get("as_is"), errors, f"{loc}.as_is")
        to_be = _parse_column_to_be(entry.get("to_be", ...), errors, f"{loc}.to_be")
        if as_is is None or to_be is _SENTINEL:
            # Bail on this entry but keep going for other errors
            continue

        transform = _parse_transform(entry.get("transform"), errors, f"{loc}.transform")

        reverse = entry.get("reverse")
        if reverse is not None and not isinstance(reverse, str):
            errors.add("reverse must be a string", f"{loc}.reverse")
            reverse = None

        value_map = entry.get("value_map")
        if value_map is not None and not isinstance(value_map, dict):
            errors.add("value_map must be a mapping", f"{loc}.value_map")
            value_map = None

        action_raw = entry.get("action", "convert")
        if action_raw not in _VALID_COLUMN_ACTIONS:
            errors.add(
                f"action must be one of {sorted(_VALID_COLUMN_ACTIONS)} "
                f"(got {action_raw!r})",
                f"{loc}.action",
            )
            action_raw = "convert"

        cm = ColumnMapping(
            as_is=as_is,
            to_be=to_be,
            transform=transform,
            reverse=reverse,
            value_map=value_map,
            default_value=entry.get("default_value"),
            action=action_raw,  # type: ignore[arg-type]
        )

        _check_column_shape(cm, errors, loc)
        out.append(cm)
    return out


_SENTINEL = object()


def _parse_column_as_is(
    node: Any, errors: _Errors, loc: str
) -> Union[ColumnRef, List[ColumnRef], None]:
    if node is None:
        errors.add("missing required field 'as_is'", loc)
        return None
    if isinstance(node, dict):
        return _parse_column_ref(node, errors, loc)
    if isinstance(node, list):
        refs: List[ColumnRef] = []
        for i, item in enumerate(node):
            if not isinstance(item, dict):
                errors.add("as_is list item must be a mapping", f"{loc}[{i}]")
                continue
            ref = _parse_column_ref(item, errors, f"{loc}[{i}]")
            if ref is not None:
                refs.append(ref)
        if len(refs) < 2:
            errors.add("as_is list requires >= 2 columns (merge case)", loc)
            return None
        return refs
    errors.add(
        "as_is must be a mapping (1:1) or list of mappings (merge)",
        loc,
    )
    return None


def _parse_column_to_be(
    node: Any, errors: _Errors, loc: str
) -> Union[ColumnRef, List[SplitTarget], None, object]:
    if node is _SENTINEL:
        # missing key → distinct from explicit null (drop). Flag as error.
        errors.add(
            "missing required field 'to_be' (use null for drops)",
            loc,
        )
        return _SENTINEL
    if node is None:
        return None
    if isinstance(node, dict):
        return _parse_column_ref(node, errors, loc)
    if isinstance(node, list):
        targets: List[SplitTarget] = []
        for i, item in enumerate(node):
            if not isinstance(item, dict):
                errors.add("to_be list item must be a mapping", f"{loc}[{i}]")
                continue
            t = item.get("table")
            c = item.get("column")
            xsel = item.get("transform_select")
            if not isinstance(t, str) or not isinstance(c, str):
                errors.add(
                    "split target requires string 'table' and 'column'",
                    f"{loc}[{i}]",
                )
                continue
            if xsel is not None and not isinstance(xsel, str):
                errors.add(
                    "transform_select must be a string",
                    f"{loc}[{i}].transform_select",
                )
                xsel = None
            targets.append(SplitTarget(table=t, column=c, transform_select=xsel))
        if len(targets) < 2:
            errors.add("to_be list requires >= 2 targets (split case)", loc)
            return _SENTINEL
        return targets
    errors.add(
        "to_be must be a mapping (1:1), list (split), or null (drop)",
        loc,
    )
    return _SENTINEL


def _parse_column_ref(
    node: Dict[str, Any], errors: _Errors, loc: str
) -> Optional[ColumnRef]:
    t = node.get("table")
    c = node.get("column")
    if not isinstance(t, str) or not isinstance(c, str):
        errors.add("column reference requires string 'table' and 'column'", loc)
        return None
    ty = node.get("type")
    if ty is not None and not isinstance(ty, str):
        errors.add("'type' must be a string", f"{loc}.type")
        ty = None
    cm = node.get("comment")
    if cm is not None and not isinstance(cm, str):
        errors.add("'comment' must be a string", f"{loc}.comment")
        cm = None
    return ColumnRef(table=t, column=c, type=ty, comment=cm)


def _parse_transform(
    node: Any, errors: _Errors, loc: str
) -> Optional[TransformSpec]:
    if node is None:
        return None
    if not isinstance(node, dict):
        errors.add("transform must be a mapping", loc)
        return None
    spec = TransformSpec()
    for key in ("read", "write", "where", "combine"):
        if key in node:
            v = node[key]
            if not isinstance(v, str):
                errors.add(f"transform.{key} must be a string", f"{loc}.{key}")
                continue
            setattr(spec, key, v)
    return spec


def _check_column_shape(cm: ColumnMapping, errors: _Errors, loc: str) -> None:
    # Merge requires transform.combine
    if isinstance(cm.as_is, list):
        if cm.transform is None or not cm.transform.combine:
            errors.add(
                "merge column requires transform.combine",
                f"{loc}.transform.combine",
            )
    # Split requires each target to have transform_select (otherwise ambiguous)
    if isinstance(cm.to_be, list):
        for i, tgt in enumerate(cm.to_be):
            if tgt.transform_select is None:
                errors.add(
                    "split target requires 'transform_select' expression",
                    f"{loc}.to_be[{i}].transform_select",
                )
    # value_map requires scalar to_be
    if cm.value_map is not None and not isinstance(cm.to_be, ColumnRef):
        errors.add(
            "value_map only valid on 1:1 column mapping",
            f"{loc}.value_map",
        )
    # drop cannot have transform
    if cm.to_be is None and cm.transform is not None and not cm.transform.is_empty():
        errors.add(
            "drop column cannot have transform expressions",
            f"{loc}.transform",
        )


# ---------------------------------------------------------------------------
# Indexing + cross-validation
# ---------------------------------------------------------------------------


def _build_indexes(mapping: Mapping, errors: _Errors) -> None:
    # Tables
    for i, tm in enumerate(mapping.tables):
        for name in tm.as_is_tables():
            key = name.upper()
            if key in mapping.table_as_is_index:
                errors.add(
                    f"duplicate AS-IS table definition: {name}",
                    f"tables[{i}]",
                )
            else:
                mapping.table_as_is_index[key] = tm
        for name in tm.to_be_tables():
            key = name.upper()
            mapping.table_to_be_index.setdefault(key, tm)

    # Columns
    for i, cm in enumerate(mapping.columns):
        for ref in cm.as_is_refs():
            key = ref.key
            if key in mapping.column_by_as_is:
                errors.add(
                    f"duplicate AS-IS column definition: {ref.qualified}",
                    f"columns[{i}]",
                )
            else:
                mapping.column_by_as_is[key] = cm
            mapping.columns_by_as_is_table.setdefault(key[0], []).append(cm)


def _cross_validate(
    mapping: Mapping,
    errors: _Errors,
    as_is_schema: Optional[Dict[str, Set[str]]],
    to_be_schema: Optional[Dict[str, Set[str]]],
) -> None:
    # 1. columns[].as_is.table must appear in tables[]
    for i, cm in enumerate(mapping.columns):
        for ref in cm.as_is_refs():
            if ref.table.upper() not in mapping.table_as_is_index:
                errors.add(
                    f"columns[{i}].as_is.table '{ref.table}' is not declared "
                    "in tables[]",
                    f"columns[{i}].as_is",
                )

    # 2. Schema existence checks (when schemas provided)
    action = mapping.options.unknown_table_action
    if as_is_schema is not None:
        for i, tm in enumerate(mapping.tables):
            for name in tm.as_is_tables():
                if name.upper() not in as_is_schema:
                    msg = f"tables[{i}].as_is '{name}' not found in AS-IS schema"
                    _schema_miss(errors, msg, f"tables[{i}].as_is", action)
        for i, cm in enumerate(mapping.columns):
            for ref in cm.as_is_refs():
                cols = as_is_schema.get(ref.table.upper())
                if cols is not None and ref.column.upper() not in cols:
                    _schema_miss(
                        errors,
                        f"columns[{i}].as_is '{ref.qualified}' not found "
                        "in AS-IS schema",
                        f"columns[{i}].as_is",
                        action,
                    )

    if to_be_schema is not None:
        for i, tm in enumerate(mapping.tables):
            for name in tm.to_be_tables():
                if name.upper() not in to_be_schema:
                    _schema_miss(
                        errors,
                        f"tables[{i}].to_be '{name}' not found in TO-BE schema",
                        f"tables[{i}].to_be",
                        action,
                    )

    # 3. Transform placeholder references
    _validate_placeholders(mapping, errors)

    # 4. Transform expressions parse with sqlglot (lazy import)
    _validate_sqlglot_expressions(mapping, errors)


def _schema_miss(
    errors: _Errors, message: str, location: str, action: UnknownTableAction
) -> None:
    if action == "error":
        errors.add(message, location)
    # warn / drop policies will be applied later by consumers; record as a
    # soft warning via a notes-style error only if action == "warn" and we
    # want users to see it. For loader strictness we only escalate on "error".


def _validate_placeholders(mapping: Mapping, errors: _Errors) -> None:
    for i, cm in enumerate(mapping.columns):
        if cm.transform is None:
            continue
        as_is_names = {r.column.upper() for r in cm.as_is_refs()}
        for key, expr in cm.transform.expressions():
            for ph in _PLACEHOLDER_RE.findall(expr):
                if ph == "src":
                    if isinstance(cm.as_is, list):
                        errors.add(
                            f"'{{src}}' is ambiguous in merge — reference "
                            "columns by name",
                            f"columns[{i}].transform.{key}",
                        )
                    continue
                if ph.upper() not in as_is_names:
                    errors.add(
                        f"'{{{ph}}}' references unknown AS-IS column "
                        f"(known: {sorted(as_is_names)})",
                        f"columns[{i}].transform.{key}",
                    )
        # split transform_select → placeholder must be 'src' only
        if isinstance(cm.to_be, list):
            for j, tgt in enumerate(cm.to_be):
                if not tgt.transform_select:
                    continue
                for ph in _PLACEHOLDER_RE.findall(tgt.transform_select):
                    if ph != "src":
                        errors.add(
                            f"split target only supports '{{src}}' "
                            f"(got '{{{ph}}}')",
                            f"columns[{i}].to_be[{j}].transform_select",
                        )


def _validate_sqlglot_expressions(mapping: Mapping, errors: _Errors) -> None:
    try:
        import sqlglot  # noqa: F401
    except ImportError:
        errors.add(
            "sqlglot is required to validate transform expressions — "
            "run `pip install sqlglot`",
        )
        return

    for i, cm in enumerate(mapping.columns):
        if cm.transform is not None:
            for key, expr in cm.transform.expressions():
                _try_parse_expr(expr, errors, f"columns[{i}].transform.{key}")
        if cm.reverse:
            _try_parse_expr(cm.reverse, errors, f"columns[{i}].reverse")
        if isinstance(cm.to_be, list):
            for j, tgt in enumerate(cm.to_be):
                if tgt.transform_select:
                    _try_parse_expr(
                        tgt.transform_select,
                        errors,
                        f"columns[{i}].to_be[{j}].transform_select",
                    )


def _try_parse_expr(expr: str, errors: _Errors, location: str) -> None:
    import sqlglot

    dummy = _PLACEHOLDER_RE.sub("X", expr)
    try:
        sqlglot.parse_one(dummy, dialect="oracle")
    except Exception as exc:  # sqlglot raises ParseError but keep broad
        errors.add(f"expression failed to parse: {exc}", location)


def _loc(prefix: str, key: str) -> str:
    return f"{prefix}.{key}" if prefix else key
