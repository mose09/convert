"""sqlglot AST transformers (docs/migration/spec.md §6).

Each transformer implements ``Transformer.apply(tree, context)`` and returns
a :class:`TransformerResult`. The pipeline in ``sql_rewriter`` chains them in
a fixed order (table → column → split → merge → type → value_map → join →
dropped-col) so upstream rewrites become the input to downstream ones.
"""
from .base import RewriteContext, Transformer, TransformerResult
from .column_rename import ColumnRenameTransformer
from .table_rename import TableRenameTransformer

__all__ = [
    "ColumnRenameTransformer",
    "RewriteContext",
    "TableRenameTransformer",
    "Transformer",
    "TransformerResult",
]
