"""Load the DB menu table and flatten it into program-level entries.

Each program (= leaf menu node) is returned with its ancestry resolved into
``main_menu`` / ``sub_menu`` / ``tab`` / ``program_name``, matching the
output columns expected by the legacy analyzer.

The menu table is highly project-specific so column names are configurable
via ``config.yaml``:

```
legacy:
  menu:
    table: "TB_MENU"
    columns:
      program_id: "PROGRAM_ID"
      program_nm: "PROGRAM_NM"
      url:        "URL"
      parent_id:  "PARENT_ID"
      level:      "LEVEL"
```
"""

import logging

from .legacy_util import normalize_url

logger = logging.getLogger(__name__)


DEFAULT_COLUMNS = {
    "program_id": "PROGRAM_ID",
    "program_nm": "PROGRAM_NM",
    "url": "URL",
    "parent_id": "PARENT_ID",
    "level": "LEVEL",
}


def _menu_config(config: dict) -> tuple[str, dict]:
    """Resolve ``table`` and ``columns`` from config with sane defaults."""
    legacy_cfg = (config or {}).get("legacy", {}) or {}
    menu_cfg = legacy_cfg.get("menu", {}) or {}
    table = menu_cfg.get("table", "TB_MENU")
    columns = {**DEFAULT_COLUMNS, **(menu_cfg.get("columns") or {})}
    return table, columns


def load_menu_rows(config: dict, table_override: str | None = None) -> list[dict]:
    """Query the configured menu table and return a list of raw rows.

    Each row is normalized to the internal key set
    ``program_id/program_nm/url/parent_id/level`` regardless of the
    project's column names. Rows with ``url IS NULL`` are kept because
    parent nodes often have no URL — the tree walk needs them.
    """
    from .db import execute_query, get_connection  # lazy: oracledb is optional

    table, columns = _menu_config(config)
    if table_override:
        table = table_override

    sql = (
        f'SELECT {columns["program_id"]} AS program_id, '
        f'{columns["program_nm"]} AS program_nm, '
        f'{columns["url"]} AS url, '
        f'{columns["parent_id"]} AS parent_id, '
        f'{columns["level"]} AS "LEVEL" '
        f'FROM {table}'
    )
    connection = get_connection(config)
    try:
        col_names, raw = execute_query(connection, sql)
    finally:
        connection.close()

    idx = {c.upper(): i for i, c in enumerate(col_names)}
    rows = []
    for r in raw:
        rows.append({
            "program_id": r[idx["PROGRAM_ID"]],
            "program_nm": r[idx["PROGRAM_NM"]],
            "url": r[idx["URL"]],
            "parent_id": r[idx["PARENT_ID"]],
            "level": r[idx["LEVEL"]],
        })
    logger.info("Loaded %d menu rows from %s", len(rows), table)
    return rows


def build_menu_tree(rows: list[dict]) -> list[dict]:
    """Turn the raw menu rows into a list of program entries with ancestry.

    The convention we apply to the resolved hierarchy:

    * level 1 ancestor → ``main_menu``
    * level 2 ancestor → ``sub_menu``
    * level 3 ancestor → ``tab``
    * leaf itself      → ``program_name``

    Works for any menu depth: the deepest three ancestors map onto
    main/sub/tab, the leaf name becomes ``program_name``, and anything
    shallower than level 3 leaves the unused slots empty.

    Only leaves with a non-empty URL produce program entries; parent-only
    rows are skipped but still used for the ancestry walk.
    """
    by_id = {r["program_id"]: r for r in rows}
    children = {}
    for r in rows:
        children.setdefault(r.get("parent_id"), []).append(r)

    def _ancestry(row: dict) -> list[dict]:
        chain = []
        cur = row
        safety = 0
        while cur and safety < 20:
            chain.append(cur)
            cur = by_id.get(cur.get("parent_id"))
            safety += 1
        chain.reverse()  # root-first
        return chain

    programs = []
    for r in rows:
        if not r.get("url"):
            continue
        chain = _ancestry(r)
        # Skip if not a leaf (has children with URLs pointing elsewhere)
        # We still emit nodes that happen to be branches with a URL — those
        # are navigation shortcuts, not pure containers.
        main_menu = chain[0]["program_nm"] if len(chain) >= 1 else ""
        sub_menu = chain[1]["program_nm"] if len(chain) >= 2 else ""
        tab = chain[2]["program_nm"] if len(chain) >= 3 else ""
        # ``program_name`` is always the leaf row name for visibility
        program_name = r["program_nm"]
        # If chain is only 1 deep, main_menu IS the program; blank sub/tab.
        if len(chain) == 1:
            sub_menu = ""
            tab = ""
        # If chain is 2 deep, the leaf is itself the sub_menu level. Keep
        # main_menu as the root, leave tab blank, and set program_name.
        if len(chain) == 2 and tab:
            tab = ""
        programs.append({
            "program_id": r["program_id"],
            "program_name": program_name,
            "main_menu": main_menu,
            "sub_menu": sub_menu,
            "tab": tab,
            "url": r["url"],
        })
    logger.info("Built menu tree: %d programs with URL", len(programs))
    return programs


def build_url_index(programs: list[dict]) -> dict:
    """Return ``{normalized_url: program_entry}``.

    Duplicate URLs (same normalized key) resolve to the first entry — those
    are typically data bugs in the menu table and are surfaced implicitly by
    the analyzer's "orphan menu" section.
    """
    idx = {}
    for p in programs:
        key = normalize_url(p.get("url", ""))
        if not key:
            continue
        idx.setdefault(key, p)
    return idx


def load_menu_hierarchy(config: dict, table_override: str | None = None) -> list[dict]:
    """Convenience: load + flatten into program entries in one call."""
    rows = load_menu_rows(config, table_override=table_override)
    return build_menu_tree(rows)
