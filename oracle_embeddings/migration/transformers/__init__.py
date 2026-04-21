"""sqlglot AST transformers (docs/migration/spec.md §6).

Pipeline order (fixed):

    1. TableRenameTransformer
    2. ColumnRenameTransformer
    3. ColumnSplitTransformer
    4. ColumnMergeTransformer
    5. TypeConversionTransformer
    6. ValueMappingTransformer
    7. JoinPathRewriter
    8. DroppedColumnChecker

Each transformer implements ``Transformer.apply(tree, context)`` and returns
a :class:`TransformerResult`. Upstream rewrites become the input to
downstream ones so table-level renames land first.
"""
from .base import RewriteContext, Transformer, TransformerResult
from .column_merge import ColumnMergeTransformer
from .column_rename import ColumnRenameTransformer
from .column_split import ColumnSplitTransformer
from .dropped_column_checker import DroppedColumnChecker
from .join_path_rewriter import JoinPathRewriter
from .table_rename import TableRenameTransformer
from .type_conversion import TypeConversionTransformer
from .value_mapping import ValueMappingTransformer

__all__ = [
    "ColumnMergeTransformer",
    "ColumnRenameTransformer",
    "ColumnSplitTransformer",
    "DroppedColumnChecker",
    "JoinPathRewriter",
    "RewriteContext",
    "TableRenameTransformer",
    "Transformer",
    "TransformerResult",
    "TypeConversionTransformer",
    "ValueMappingTransformer",
]
