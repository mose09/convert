"""Markdown + Excel output for the AS-IS legacy analyzer.

Consumes the result dict produced by ``legacy_analyzer.analyze_legacy`` and
emits:

* ``as_is_analysis_<ts>.md`` — sections: header, summary, menu hierarchy,
  program detail table, unmatched controllers, orphan menus.
* ``as_is_analysis_<ts>.xlsx`` — 7 sheets: Summary, Programs, Menu
  Hierarchy, Unmatched Controllers, Orphan Menu Entries, RFC Calls,
  Tables Cross-Reference.
"""

import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)


def _md_escape(text) -> str:
    """Escape pipes/newlines so Markdown tables don't break."""
    if text is None:
        return ""
    return str(text).replace("|", "\\|").replace("\n", "<br>").replace("\r", "")


def _group_by_menu(rows: list[dict]) -> dict:
    """Group rows by (main, sub, tab) for the menu hierarchy section."""
    groups = {}
    for r in rows:
        if not r.get("matched"):
            continue
        key = (r.get("main_menu", ""), r.get("sub_menu", ""), r.get("tab", ""))
        groups.setdefault(key, []).append(r)
    return groups


def save_legacy_markdown(result: dict, output_dir: str) -> str:
    """Render the analysis result as a Markdown document."""
    os.makedirs(output_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = os.path.join(output_dir, f"as_is_analysis_{ts}.md")

    rows = result.get("rows", [])
    unmatched = result.get("unmatched_controllers", [])
    orphans = result.get("orphan_menus", [])
    stats = result.get("stats", {})

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("# AS-IS Legacy Source Analysis\n\n")
        f.write(f"- Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"- Java dir: `{result.get('java_dir', '')}`\n")
        f.write(f"- MyBatis dir: `{result.get('mybatis_dir', '')}`\n")
        if result.get("react_dir"):
            f.write(f"- React dir: `{result.get('react_dir', '')}`\n")
        f.write("\n")

        f.write("## Summary\n\n")
        f.write("| Category | Count |\n|---|---|\n")
        for label, key in [
            ("Controllers scanned", "controllers"),
            ("Services scanned", "services"),
            ("Mappers scanned", "mappers"),
            ("Endpoints total", "endpoints"),
            ("Matched to menu", "matched"),
            ("Unmatched controllers", "unmatched"),
            ("Orphan menu entries", "orphan_menus"),
            ("Endpoints with React file", "with_react"),
            ("Endpoints with RFC", "with_rfc"),
        ]:
            f.write(f"| {label} | {stats.get(key, 0)} |\n")
        f.write("\n")

        # Menu hierarchy
        groups = _group_by_menu(rows)
        if groups:
            f.write("## Programs by Menu Hierarchy\n\n")
            last_main = None
            last_sub = None
            for (main, sub, tab), entries in sorted(groups.items()):
                if main != last_main:
                    f.write(f"### {main or '(no main)'}\n\n")
                    last_main = main
                    last_sub = None
                if sub != last_sub:
                    f.write(f"#### {sub or '(no sub)'}\n\n")
                    last_sub = sub
                if tab:
                    f.write(f"- **{tab}**\n")
                for e in entries:
                    f.write(f"  - `{e['http_method']}` `{e['url']}` → {e['program_name']} "
                            f"({e['controller_class']})\n")
            f.write("\n")

        # Program detail table
        f.write("## Program Detail\n\n")
        f.write("| Main | Sub | Tab | Program | HTTP | URL | File | React | Controller | Service | Query XML | Tables | RFC |\n")
        f.write("|---|---|---|---|---|---|---|---|---|---|---|---|---|\n")
        for r in rows:
            f.write(
                "| " + " | ".join(_md_escape(r.get(k, "")) for k in [
                    "main_menu", "sub_menu", "tab", "program_name", "http_method",
                    "url", "file_name", "presentation_layer", "controller_class",
                    "service_class", "query_xml", "related_tables", "rfc",
                ]) + " |\n"
            )
        f.write("\n")

        if unmatched:
            f.write(f"## Unmatched Controllers ({len(unmatched)})\n\n")
            f.write("메뉴 테이블에 매칭되지 않은 컨트롤러 엔드포인트. 내부 API 이거나 메뉴 누락일 수 있습니다.\n\n")
            f.write("| HTTP | URL | Controller | Method | File |\n|---|---|---|---|---|\n")
            for u in unmatched:
                f.write(
                    f"| {u['http_method']} | {_md_escape(u['url'])} "
                    f"| {_md_escape(u['controller_class'])} "
                    f"| {_md_escape(u.get('program_name', ''))} "
                    f"| {_md_escape(u.get('file_name', ''))} |\n"
                )
            f.write("\n")

        if orphans:
            f.write(f"## Orphan Menu Entries ({len(orphans)})\n\n")
            f.write("메뉴 테이블에는 있는데 대응하는 컨트롤러를 찾지 못한 URL. 미구현 or 삭제된 기능.\n\n")
            f.write("| Program ID | Main | Sub | Tab | Program | URL |\n|---|---|---|---|---|---|\n")
            for o in orphans:
                f.write(
                    f"| {_md_escape(o.get('program_id', ''))} "
                    f"| {_md_escape(o.get('main_menu', ''))} "
                    f"| {_md_escape(o.get('sub_menu', ''))} "
                    f"| {_md_escape(o.get('tab', ''))} "
                    f"| {_md_escape(o.get('program_name', ''))} "
                    f"| {_md_escape(o.get('url', ''))} |\n"
                )
            f.write("\n")

    logger.info("Legacy markdown saved: %s", filepath)
    return filepath


def save_legacy_excel(result: dict, output_dir: str) -> str:
    """Render the analysis result as a multi-sheet Excel workbook."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    os.makedirs(output_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = os.path.join(output_dir, f"as_is_analysis_{ts}.xlsx")

    rows = result.get("rows", [])
    unmatched = result.get("unmatched_controllers", [])
    orphans = result.get("orphan_menus", [])
    stats = result.get("stats", {})

    wb = Workbook()

    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill(start_color="0F3460", end_color="0F3460", fill_type="solid")
    header_align = Alignment(horizontal="center", vertical="center")
    thin_border = Border(
        left=Side(style="thin"), right=Side(style="thin"),
        top=Side(style="thin"), bottom=Side(style="thin"),
    )
    yellow_fill = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")
    gray_fill = PatternFill(start_color="EEEEEE", end_color="EEEEEE", fill_type="solid")

    def _write_header(ws, headers):
        for col, h in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col, value=h)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_align
            cell.border = thin_border

    def _write_row(ws, row_num, values, fill=None):
        for col, v in enumerate(values, 1):
            cell = ws.cell(row=row_num, column=col, value=v)
            cell.border = thin_border
            if fill is not None:
                cell.fill = fill

    def _auto_width(ws):
        for col in ws.columns:
            max_len = 0
            col_letter = col[0].column_letter
            for cell in col:
                if cell.value is not None:
                    max_len = max(max_len, len(str(cell.value)))
            ws.column_dimensions[col_letter].width = min(max_len + 4, 50)

    # Sheet 1: Summary
    ws = wb.active
    ws.title = "Summary"
    _write_header(ws, ["Category", "Count"])
    summary_rows = [
        ("Controllers scanned", stats.get("controllers", 0)),
        ("Services scanned", stats.get("services", 0)),
        ("Mappers scanned", stats.get("mappers", 0)),
        ("Endpoints total", stats.get("endpoints", 0)),
        ("Matched to menu", stats.get("matched", 0)),
        ("Unmatched controllers", stats.get("unmatched", 0)),
        ("Orphan menu entries", stats.get("orphan_menus", 0)),
        ("Endpoints with React file", stats.get("with_react", 0)),
        ("Endpoints with RFC", stats.get("with_rfc", 0)),
    ]
    for i, (k, v) in enumerate(summary_rows, 2):
        _write_row(ws, i, [k, v])
    _auto_width(ws)

    # Sheet 2: Programs (main deliverable)
    ws = wb.create_sheet("Programs")
    headers = ["No", "Main", "Sub", "Tab", "Program", "HTTP", "URL",
               "File", "React", "Controller", "Service",
               "Query XML", "Tables", "RFC"]
    _write_header(ws, headers)
    for i, r in enumerate(rows, 2):
        fill = None
        if not r.get("matched"):
            fill = yellow_fill
        elif not r.get("query_xml") and not r.get("related_tables"):
            fill = gray_fill
        _write_row(ws, i, [
            i - 1,
            r.get("main_menu", ""), r.get("sub_menu", ""), r.get("tab", ""),
            r.get("program_name", ""), r.get("http_method", ""), r.get("url", ""),
            r.get("file_name", ""), r.get("presentation_layer", ""),
            r.get("controller_class", ""), r.get("service_class", ""),
            r.get("query_xml", ""), r.get("related_tables", ""),
            r.get("rfc", ""),
        ], fill=fill)
    ws.freeze_panes = "A2"
    _auto_width(ws)

    # Sheet 3: Menu Hierarchy
    ws = wb.create_sheet("Menu Hierarchy")
    _write_header(ws, ["Program ID", "Main", "Sub", "Tab", "Program", "URL",
                        "Matched", "# Endpoints"])
    hier_seen = set()
    row_num = 2
    for r in rows:
        if not r.get("matched"):
            continue
        key = (r.get("program_id", ""), r.get("url", ""))
        if key in hier_seen:
            continue
        hier_seen.add(key)
        count = sum(1 for x in rows if x.get("program_id") == r.get("program_id"))
        _write_row(ws, row_num, [
            r.get("program_id", ""), r.get("main_menu", ""), r.get("sub_menu", ""),
            r.get("tab", ""), r.get("program_name", ""), r.get("url", ""),
            "Y", count,
        ])
        row_num += 1
    for o in orphans:
        _write_row(ws, row_num, [
            o.get("program_id", ""), o.get("main_menu", ""), o.get("sub_menu", ""),
            o.get("tab", ""), o.get("program_name", ""), o.get("url", ""),
            "N", 0,
        ], fill=yellow_fill)
        row_num += 1
    _auto_width(ws)

    # Sheet 4: Unmatched Controllers
    ws = wb.create_sheet("Unmatched Controllers")
    _write_header(ws, ["HTTP", "URL", "Controller", "Method", "File"])
    for i, u in enumerate(unmatched, 2):
        _write_row(ws, i, [
            u.get("http_method", ""), u.get("url", ""),
            u.get("controller_class", ""), u.get("program_name", ""),
            u.get("file_name", ""),
        ])
    _auto_width(ws)

    # Sheet 5: Orphan Menu Entries
    ws = wb.create_sheet("Orphan Menu Entries")
    _write_header(ws, ["Program ID", "Main", "Sub", "Tab", "Program", "URL"])
    for i, o in enumerate(orphans, 2):
        _write_row(ws, i, [
            o.get("program_id", ""), o.get("main_menu", ""),
            o.get("sub_menu", ""), o.get("tab", ""),
            o.get("program_name", ""), o.get("url", ""),
        ])
    _auto_width(ws)

    # Sheet 6: RFC Calls (cross-reference)
    ws = wb.create_sheet("RFC Calls")
    _write_header(ws, ["RFC", "Program", "Controller", "URL", "File"])
    rfc_row = 2
    for r in rows:
        if not r.get("rfc"):
            continue
        for name in [s.strip() for s in r["rfc"].split(",") if s.strip()]:
            _write_row(ws, rfc_row, [
                name, r.get("program_name", ""),
                r.get("controller_class", ""), r.get("url", ""),
                r.get("file_name", ""),
            ])
            rfc_row += 1
    _auto_width(ws)

    # Sheet 7: Tables Cross-Reference
    ws = wb.create_sheet("Tables Cross-Reference")
    _write_header(ws, ["Table", "# Programs", "Programs"])
    table_to_programs = {}
    for r in rows:
        if not r.get("related_tables"):
            continue
        for t in [s.strip() for s in r["related_tables"].split(",") if s.strip()]:
            table_to_programs.setdefault(t, []).append(r.get("program_name", ""))
    for i, (table, progs) in enumerate(sorted(table_to_programs.items()), 2):
        progs_sorted = sorted(set(progs))
        preview = ", ".join(progs_sorted[:10])
        if len(progs_sorted) > 10:
            preview += f", … (+{len(progs_sorted) - 10})"
        _write_row(ws, i, [table, len(progs_sorted), preview])
    _auto_width(ws)

    wb.save(filepath)
    logger.info("Legacy excel saved: %s", filepath)
    return filepath
