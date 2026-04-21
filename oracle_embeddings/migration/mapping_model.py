"""Data model for SQL migration mapping (docs/migration/spec.md §4, §5).

Intentionally decoupled from YAML I/O: ``mapping_loader`` owns parsing /
validation; downstream transformers / rewriter consume these dataclasses.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple, Union

# ---- Literal aliases (kept flat for 3.10+ isinstance checks) --------------

TableType = Literal["rename", "split", "merge", "drop"]
CommentScope = Literal["select", "update", "insert", "where", "join"]
CommentSource = Literal["to_be_schema", "terms_dictionary", "both"]
UnknownTableAction = Literal["warn", "error", "drop"]
ColumnAction = Literal["convert", "drop_with_warning"]
Status = Literal["AUTO", "AUTO_WARN", "NEEDS_LLM", "UNRESOLVED", "PARSE_FAIL"]
SqlType = Literal["SELECT", "INSERT", "UPDATE", "DELETE", "MERGE"]
ConversionMethod = Literal["DSL", "sqlglot-AST", "LLM", "manual"]
ChangeKind = Literal["column", "table", "value", "type_wrap", "join_path"]
ColumnMappingKind = Literal[
    "rename", "split", "merge", "drop", "type_convert", "value_map"
]


# ---- Identifiers ----------------------------------------------------------


@dataclass(frozen=True)
class ColumnRef:
    """AS-IS or TO-BE column reference. ``type`` is an optional Oracle
    datatype string used for type conversion / compatibility checks."""

    table: str
    column: str
    type: Optional[str] = None

    @property
    def qualified(self) -> str:
        return f"{self.table}.{self.column}"

    @property
    def key(self) -> Tuple[str, str]:
        return (self.table.upper(), self.column.upper())


@dataclass
class SplitTarget:
    """TO-BE side of a 1:N column split."""

    table: str
    column: str
    transform_select: Optional[str] = None


# ---- Table mapping --------------------------------------------------------


@dataclass
class TableMapping:
    """tables[] entry. ``as_is`` / ``to_be`` shape depends on ``type``.

    - rename  : as_is=str, to_be=str
    - split   : as_is=str, to_be=list[str]
    - merge   : as_is=list[str], to_be=str
    - drop    : as_is=str, to_be=None
    """

    type: TableType
    as_is: Union[str, List[str]]
    to_be: Union[str, List[str], None]
    discriminator_column: Optional[str] = None
    discriminator_map: Optional[Dict[str, str]] = None
    join_condition: Optional[str] = None

    def as_is_tables(self) -> List[str]:
        return list(self.as_is) if isinstance(self.as_is, list) else [self.as_is]

    def to_be_tables(self) -> List[str]:
        if self.to_be is None:
            return []
        return list(self.to_be) if isinstance(self.to_be, list) else [self.to_be]


# ---- Transform ------------------------------------------------------------


@dataclass
class TransformSpec:
    """Oracle SQL expression templates using ``{src}`` / ``{colname}``
    placeholders. Context keys:

    - read    : SELECT projection context
    - write   : INSERT VALUES / UPDATE SET context
    - where   : predicate context (WHERE / JOIN ON)
    - combine : N→1 merge; references all AS-IS column names by name
    """

    read: Optional[str] = None
    write: Optional[str] = None
    where: Optional[str] = None
    combine: Optional[str] = None

    def is_empty(self) -> bool:
        return not any([self.read, self.write, self.where, self.combine])

    def expressions(self) -> List[Tuple[str, str]]:
        return [(k, v) for k, v in (
            ("read", self.read),
            ("write", self.write),
            ("where", self.where),
            ("combine", self.combine),
        ) if v]


# ---- Column mapping -------------------------------------------------------


@dataclass
class ColumnMapping:
    as_is: Union[ColumnRef, List[ColumnRef]]
    to_be: Union[ColumnRef, List[SplitTarget], None]
    transform: Optional[TransformSpec] = None
    reverse: Optional[str] = None
    value_map: Optional[Dict[Any, Any]] = None
    default_value: Optional[Any] = None
    action: ColumnAction = "convert"

    @property
    def kind(self) -> ColumnMappingKind:
        if self.to_be is None:
            return "drop"
        if isinstance(self.as_is, list):
            return "merge"
        if isinstance(self.to_be, list):
            return "split"
        if self.value_map is not None:
            return "value_map"
        if self.transform is not None and not self.transform.is_empty():
            return "type_convert"
        return "rename"

    def as_is_refs(self) -> List[ColumnRef]:
        if isinstance(self.as_is, list):
            return list(self.as_is)
        return [self.as_is]

    def to_be_refs(self) -> List[Union[ColumnRef, SplitTarget]]:
        if self.to_be is None:
            return []
        if isinstance(self.to_be, list):
            return list(self.to_be)
        return [self.to_be]


# ---- Global options + container ------------------------------------------


@dataclass
class MappingOptions:
    emit_column_comments: bool = False
    comment_scope: List[CommentScope] = field(
        default_factory=lambda: ["select", "update", "insert"]
    )
    comment_source: CommentSource = "terms_dictionary"
    comment_format: str = "/* {ko_name} */"
    unknown_table_action: UnknownTableAction = "warn"


@dataclass
class DefaultSchema:
    as_is: str = "LEGACY"
    to_be: str = "NEW"


@dataclass
class Mapping:
    """Root object produced by ``mapping_loader.load_mapping``."""

    version: str
    default_schema: DefaultSchema
    options: MappingOptions
    tables: List[TableMapping]
    columns: List[ColumnMapping]

    # Indexes populated by the loader (upper-cased keys). These are not part
    # of the YAML itself and should be treated as read-only at runtime.
    table_as_is_index: Dict[str, TableMapping] = field(default_factory=dict)
    table_to_be_index: Dict[str, TableMapping] = field(default_factory=dict)
    column_by_as_is: Dict[Tuple[str, str], ColumnMapping] = field(
        default_factory=dict
    )
    # Reverse index: AS-IS table name (upper) → column mappings referencing it
    columns_by_as_is_table: Dict[str, List[ColumnMapping]] = field(
        default_factory=dict
    )

    # Convenience lookups ---------------------------------------------------

    def find_column(self, table: str, column: str) -> Optional[ColumnMapping]:
        return self.column_by_as_is.get((table.upper(), column.upper()))

    def find_table(self, as_is_name: str) -> Optional[TableMapping]:
        return self.table_as_is_index.get(as_is_name.upper())

    def list_as_is_tables(self) -> List[str]:
        return sorted(self.table_as_is_index.keys())


# ---- Rewrite result (spec §5) --------------------------------------------


@dataclass
class ChangeItem:
    kind: ChangeKind
    as_is: str
    to_be: str
    count: int
    transformer: str


@dataclass
class RewriteResult:
    # Identity
    xml_file: Path
    namespace: str
    sql_id: str
    sql_type: SqlType

    # Original / converted
    as_is_sql: str
    to_be_sql: Optional[str] = None

    # Status
    status: Status = "AUTO"

    # Conversion detail
    applied_transformers: List[str] = field(default_factory=list)
    conversion_method: ConversionMethod = "DSL"
    changed_items: List[ChangeItem] = field(default_factory=list)
    dynamic_paths_expanded: int = 1

    # LLM
    llm_confidence: Optional[float] = None
    llm_reasoning: Optional[str] = None

    # Validation
    stage_a_pass: Optional[bool] = None
    stage_b_pass: Optional[bool] = None
    parse_error: Optional[str] = None

    # Meta
    warnings: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    last_modified: datetime = field(default_factory=datetime.now)


# ---- Loader errors --------------------------------------------------------


class LoaderError(Exception):
    """Raised by ``mapping_loader`` when a mapping YAML is invalid.

    ``location`` is a dotted path like ``columns[3].transform.read`` so the
    user can jump straight to the offending field. Multiple errors may be
    collected and re-raised as ``LoaderErrorGroup``.
    """

    def __init__(self, message: str, location: Optional[str] = None):
        self.message = message
        self.location = location
        super().__init__(str(self))

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.location:
            return f"[{self.location}] {self.message}"
        return self.message


class LoaderErrorGroup(LoaderError):
    """One or more ``LoaderError`` reported together."""

    def __init__(self, errors: List[LoaderError]):
        self.errors = list(errors)
        header = f"{len(errors)} mapping error(s):"
        body = "\n".join(f"  - {e}" for e in errors)
        super().__init__(f"{header}\n{body}")
