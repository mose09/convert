import json
import logging
import os

logger = logging.getLogger(__name__)


def generate_html_erd(schema: dict, joins: list[dict], output_path: str) -> str:
    """Generate an interactive HTML ERD file with D3.js force layout."""

    schema_table_map = {t["name"]: t for t in schema.get("tables", [])}

    # Build nodes (tables)
    nodes = []
    for table in schema.get("tables", []):
        pk_cols = set(table.get("primary_keys", []))
        real_fk_cols = {fk["column"] for fk in table.get("foreign_keys", [])}
        join_ref_cols = set()
        for j in joins:
            if j["table1"] == table["name"]:
                join_ref_cols.add(j["column1"])
            elif j["table2"] == table["name"]:
                join_ref_cols.add(j["column2"])

        columns = []
        for col in table["columns"]:
            role = ""
            if col["column_name"] in pk_cols:
                role = "PK"
            elif col["column_name"] in real_fk_cols:
                role = "FK"
            elif col["column_name"] in join_ref_cols:
                role = "REF"
            columns.append({
                "name": col["column_name"],
                "type": col["data_type"],
                "role": role,
                "comment": col.get("comment") or "",
            })

        nodes.append({
            "id": table["name"],
            "comment": table.get("comment") or "",
            "columns": columns,
            "column_count": len(columns),
        })

    # Build links (relationships) — only between tables in schema. We
    # group by (table1, table2) so that composite JOINs (multiple
    # column pairs between the same two tables) appear as one link
    # whose sourceCol / targetCol list every column joined, instead
    # of silently dropping every column after the first.
    links = []
    pair_bucket: dict[tuple, dict] = {}
    for j in joins:
        if j["table1"] not in schema_table_map or j["table2"] not in schema_table_map:
            continue
        key = tuple(sorted([j["table1"], j["table2"]]))
        # Normalise direction so ``source`` is always key[0].
        if j["table1"] == key[0]:
            c_src, c_tgt = j["column1"], j["column2"]
        else:
            c_src, c_tgt = j["column2"], j["column1"]
        entry = pair_bucket.get(key)
        if entry is None:
            entry = {
                "source": key[0],
                "target": key[1],
                "source_cols": [],
                "target_cols": [],
                "seen": set(),
                "joinType": j.get("join_type", ""),
            }
            pair_bucket[key] = entry
        col_pair = (c_src, c_tgt)
        if col_pair in entry["seen"]:
            continue
        entry["seen"].add(col_pair)
        entry["source_cols"].append(c_src)
        entry["target_cols"].append(c_tgt)

    for entry in pair_bucket.values():
        links.append({
            "source": entry["source"],
            "target": entry["target"],
            "sourceCol": ", ".join(entry["source_cols"]),
            "targetCol": ", ".join(entry["target_cols"]),
            "joinType": entry["joinType"],
        })

    erd_data = json.dumps({"nodes": nodes, "links": links}, ensure_ascii=False)

    html = _build_html(erd_data)

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    logger.info("HTML ERD exported: %s (%d tables, %d relationships)",
                output_path, len(nodes), len(links))
    return output_path


def _build_html(erd_data_json: str) -> str:
    """Build the full HTML page with embedded D3.js and ERD data."""
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>Interactive ERD</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: 'Segoe UI', Tahoma, sans-serif; background: #1a1a2e; color: #eee; overflow: hidden; }}

#toolbar {{
    position: fixed; top: 0; left: 0; right: 0; z-index: 100;
    background: #16213e; padding: 8px 16px; display: flex; align-items: center; gap: 12px;
    border-bottom: 1px solid #0f3460; box-shadow: 0 2px 8px rgba(0,0,0,0.3);
}}
#toolbar input {{
    padding: 6px 12px; border-radius: 4px; border: 1px solid #0f3460;
    background: #1a1a2e; color: #eee; font-size: 14px; width: 250px;
}}
#toolbar .stats {{ color: #888; font-size: 13px; margin-left: auto; }}
#toolbar button {{
    padding: 6px 12px; border-radius: 4px; border: 1px solid #0f3460;
    background: #0f3460; color: #eee; cursor: pointer; font-size: 13px;
}}
#toolbar button:hover {{ background: #533483; }}

#detail-panel {{
    position: fixed; right: -400px; top: 45px; bottom: 0; width: 380px;
    background: #16213e; border-left: 1px solid #0f3460; padding: 16px;
    overflow-y: auto; transition: right 0.3s; z-index: 99;
    box-shadow: -4px 0 12px rgba(0,0,0,0.3);
}}
#detail-panel.open {{ right: 0; }}
#detail-panel h2 {{ color: #e94560; margin-bottom: 8px; font-size: 18px; }}
#detail-panel .table-comment {{ color: #888; margin-bottom: 12px; font-style: italic; }}
#detail-panel table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
#detail-panel th {{ text-align: left; padding: 4px 6px; background: #0f3460; color: #ccc; }}
#detail-panel td {{ padding: 4px 6px; border-bottom: 1px solid #1a1a2e; }}
#detail-panel .pk {{ color: #f0c040; font-weight: bold; }}
#detail-panel .fk {{ color: #4ecdc4; font-weight: bold; }}
#detail-panel .ref {{ color: #a78bfa; font-weight: bold; }}
#detail-panel .close-btn {{
    position: absolute; top: 12px; right: 12px; cursor: pointer;
    color: #888; font-size: 20px;
}}
#detail-panel .close-btn:hover {{ color: #e94560; }}
#detail-panel .relations {{ margin-top: 16px; }}
#detail-panel .relations h3 {{ color: #4ecdc4; margin-bottom: 8px; font-size: 14px; }}
#detail-panel .rel-item {{ padding: 4px 0; font-size: 12px; color: #aaa; }}

svg {{ display: block; }}
.link {{ stroke: #4ecdc4; stroke-opacity: 0.4; stroke-width: 1.5; fill: none; }}
.link:hover {{ stroke-opacity: 1; stroke-width: 2.5; }}
.link.highlighted {{ stroke: #e94560; stroke-opacity: 0.8; stroke-width: 2.5; }}

.node rect {{
    fill: #16213e; stroke: #0f3460; stroke-width: 1.5; rx: 6; cursor: grab;
}}
.node rect:hover {{ stroke: #e94560; stroke-width: 2; }}
.node.selected rect {{ stroke: #e94560; stroke-width: 2.5; }}
.node.related rect {{ stroke: #4ecdc4; stroke-width: 2; }}
.node.dimmed rect {{ opacity: 0.3; }}
.node.dimmed text {{ opacity: 0.3; }}

.node-title {{
    font-size: 12px; font-weight: bold; fill: #e94560; pointer-events: none;
}}
.node-count {{
    font-size: 10px; fill: #888; pointer-events: none;
}}
.link-label {{
    font-size: 9px; fill: #888; pointer-events: none; display: none;
}}
.link.highlighted + .link-label, .link-label.visible {{ display: block; }}

.tooltip {{
    position: fixed; padding: 8px 12px; background: #0f3460; color: #eee;
    border-radius: 4px; font-size: 12px; pointer-events: none;
    box-shadow: 0 2px 8px rgba(0,0,0,0.4); z-index: 200; display: none;
}}
</style>
</head>
<body>

<div id="toolbar">
    <input type="text" id="search" placeholder="Search table... (Enter)" autocomplete="off">
    <button id="btn-reset">Reset View</button>
    <button id="btn-fit">Fit All</button>
    <span class="stats" id="stats"></span>
</div>

<div id="detail-panel">
    <span class="close-btn" id="close-detail">&times;</span>
    <h2 id="detail-title"></h2>
    <div class="table-comment" id="detail-comment"></div>
    <table id="detail-columns">
        <thead><tr><th>Column</th><th>Type</th><th>Key</th><th>Description</th></tr></thead>
        <tbody></tbody>
    </table>
    <div class="relations">
        <h3>Relationships</h3>
        <div id="detail-relations"></div>
    </div>
</div>

<div class="tooltip" id="tooltip"></div>

<script>
// Embedded D3.js v7 minimal (force + zoom + drag)
// Using inline minimal D3 - no CDN needed for air-gapped environments
</script>
<script src="https://d3js.org/d3.v7.min.js"></script>
<script>
// Fallback: if D3 not loaded (offline), load from embedded
if (typeof d3 === 'undefined') {{
    document.write('<div style="padding:40px;color:#e94560;font-size:18px;">'
        + 'D3.js failed to load. For air-gapped environments, download d3.v7.min.js '
        + 'and place it next to this HTML file, then update the script src.</div>');
}}
</script>
<script>
const data = {erd_data_json};

const width = window.innerWidth;
const height = window.innerHeight - 45;
const nodeWidth = 180;
const nodeHeight = 36;

// Stats
document.getElementById('stats').textContent =
    `Tables: ${{data.nodes.length}} | Relationships: ${{data.links.length}}`;

// Build lookup
const nodeMap = new Map(data.nodes.map(n => [n.id, n]));
const linksByNode = new Map();
data.nodes.forEach(n => linksByNode.set(n.id, []));
data.links.forEach(l => {{
    if (linksByNode.has(l.source)) linksByNode.get(l.source).push(l);
    if (linksByNode.has(l.target)) linksByNode.get(l.target).push(l);
}});

// SVG
const svg = d3.select('body').append('svg')
    .attr('width', width).attr('height', height)
    .style('margin-top', '45px');

const g = svg.append('g');

// Zoom
const zoom = d3.zoom()
    .scaleExtent([0.1, 4])
    .on('zoom', (e) => g.attr('transform', e.transform));
svg.call(zoom);

// Force simulation
const simulation = d3.forceSimulation(data.nodes)
    .force('link', d3.forceLink(data.links).id(d => d.id).distance(200))
    .force('charge', d3.forceManyBody().strength(-800))
    .force('center', d3.forceCenter(width / 2, height / 2))
    .force('collision', d3.forceCollide().radius(100));

// Links
const link = g.append('g').selectAll('line')
    .data(data.links).enter().append('line')
    .attr('class', 'link');

// Link labels
const linkLabel = g.append('g').selectAll('text')
    .data(data.links).enter().append('text')
    .attr('class', 'link-label')
    .text(d => `${{d.sourceCol}} = ${{d.targetCol}}`);

// Nodes
const node = g.append('g').selectAll('g')
    .data(data.nodes).enter().append('g')
    .attr('class', 'node')
    .call(d3.drag()
        .on('start', dragStarted)
        .on('drag', dragged)
        .on('end', dragEnded));

node.append('rect')
    .attr('width', nodeWidth)
    .attr('height', nodeHeight)
    .attr('x', -nodeWidth / 2)
    .attr('y', -nodeHeight / 2);

node.append('text')
    .attr('class', 'node-title')
    .attr('text-anchor', 'middle')
    .attr('dy', -2)
    .text(d => d.id.length > 22 ? d.id.slice(0, 20) + '..' : d.id);

node.append('text')
    .attr('class', 'node-count')
    .attr('text-anchor', 'middle')
    .attr('dy', 14)
    .text(d => `${{d.column_count}} columns`);

// Tooltip
const tooltip = document.getElementById('tooltip');
link.on('mouseover', (e, d) => {{
    tooltip.style.display = 'block';
    tooltip.innerHTML = `<b>${{d.source.id || d.source}}</b>.${{d.sourceCol}} → <b>${{d.target.id || d.target}}</b>.${{d.targetCol}}<br>${{d.joinType}}`;
}}).on('mousemove', (e) => {{
    tooltip.style.left = e.clientX + 12 + 'px';
    tooltip.style.top = e.clientY - 10 + 'px';
}}).on('mouseout', () => {{ tooltip.style.display = 'none'; }});

// Click node → show detail
node.on('click', (e, d) => {{
    e.stopPropagation();
    showDetail(d);
    highlightRelated(d);
}});

svg.on('click', () => {{
    clearHighlight();
    closeDetail();
}});

// Simulation tick
simulation.on('tick', () => {{
    link.attr('x1', d => d.source.x).attr('y1', d => d.source.y)
        .attr('x2', d => d.target.x).attr('y2', d => d.target.y);
    linkLabel.attr('x', d => (d.source.x + d.target.x) / 2)
        .attr('y', d => (d.source.y + d.target.y) / 2);
    node.attr('transform', d => `translate(${{d.x}},${{d.y}})`);
}});

// Drag
function dragStarted(e, d) {{
    if (!e.active) simulation.alphaTarget(0.3).restart();
    d.fx = d.x; d.fy = d.y;
}}
function dragged(e, d) {{ d.fx = e.x; d.fy = e.y; }}
function dragEnded(e, d) {{
    if (!e.active) simulation.alphaTarget(0);
    d.fx = null; d.fy = null;
}}

// Detail panel
function showDetail(d) {{
    const panel = document.getElementById('detail-panel');
    document.getElementById('detail-title').textContent = d.id;
    document.getElementById('detail-comment').textContent = d.comment || '';

    const tbody = document.querySelector('#detail-columns tbody');
    tbody.innerHTML = d.columns.map(c => {{
        const roleClass = c.role === 'PK' ? 'pk' : c.role === 'FK' ? 'fk' : c.role === 'REF' ? 'ref' : '';
        const roleText = c.role ? `<span class="${{roleClass}}">${{c.role}}</span>` : '';
        return `<tr><td>${{c.name}}</td><td>${{c.type}}</td><td>${{roleText}}</td><td>${{c.comment}}</td></tr>`;
    }}).join('');

    // Relations
    const rels = linksByNode.get(d.id) || [];
    const relDiv = document.getElementById('detail-relations');
    if (rels.length === 0) {{
        relDiv.innerHTML = '<div class="rel-item">No relationships</div>';
    }} else {{
        relDiv.innerHTML = rels.map(r => {{
            const src = (typeof r.source === 'object') ? r.source.id : r.source;
            const tgt = (typeof r.target === 'object') ? r.target.id : r.target;
            const other = src === d.id ? tgt : src;
            const srcCol = src === d.id ? r.sourceCol : r.targetCol;
            const tgtCol = src === d.id ? r.targetCol : r.sourceCol;
            return `<div class="rel-item">→ <b>${{other}}</b> (${{srcCol}} = ${{tgtCol}})</div>`;
        }}).join('');
    }}

    panel.classList.add('open');
}}

function closeDetail() {{
    document.getElementById('detail-panel').classList.remove('open');
}}
document.getElementById('close-detail').addEventListener('click', (e) => {{
    e.stopPropagation();
    closeDetail();
    clearHighlight();
}});

// Highlight related
function highlightRelated(d) {{
    const relatedIds = new Set();
    relatedIds.add(d.id);
    (linksByNode.get(d.id) || []).forEach(l => {{
        const src = (typeof l.source === 'object') ? l.source.id : l.source;
        const tgt = (typeof l.target === 'object') ? l.target.id : l.target;
        relatedIds.add(src);
        relatedIds.add(tgt);
    }});

    node.classed('selected', n => n.id === d.id)
        .classed('related', n => n.id !== d.id && relatedIds.has(n.id))
        .classed('dimmed', n => !relatedIds.has(n.id));

    link.classed('highlighted', l => {{
        const src = (typeof l.source === 'object') ? l.source.id : l.source;
        const tgt = (typeof l.target === 'object') ? l.target.id : l.target;
        return src === d.id || tgt === d.id;
    }});

    linkLabel.classed('visible', l => {{
        const src = (typeof l.source === 'object') ? l.source.id : l.source;
        const tgt = (typeof l.target === 'object') ? l.target.id : l.target;
        return src === d.id || tgt === d.id;
    }});
}}

function clearHighlight() {{
    node.classed('selected', false).classed('related', false).classed('dimmed', false);
    link.classed('highlighted', false);
    linkLabel.classed('visible', false);
}}

// Search
const searchInput = document.getElementById('search');
searchInput.addEventListener('keydown', (e) => {{
    if (e.key === 'Enter') {{
        const q = searchInput.value.trim().toUpperCase();
        if (!q) {{ clearHighlight(); return; }}
        const found = data.nodes.find(n => n.id.includes(q));
        if (found) {{
            showDetail(found);
            highlightRelated(found);
            // Center on found node
            const t = d3.zoomIdentity.translate(width/2 - found.x, height/2 - found.y);
            svg.transition().duration(500).call(zoom.transform, t);
        }}
    }}
}});

// Reset
document.getElementById('btn-reset').addEventListener('click', () => {{
    svg.transition().duration(500).call(zoom.transform, d3.zoomIdentity);
    clearHighlight();
    closeDetail();
}});

// Fit all
document.getElementById('btn-fit').addEventListener('click', () => {{
    const bounds = g.node().getBBox();
    const scale = Math.min(width / bounds.width, height / bounds.height) * 0.85;
    const t = d3.zoomIdentity
        .translate(width/2, height/2)
        .scale(scale)
        .translate(-bounds.x - bounds.width/2, -bounds.y - bounds.height/2);
    svg.transition().duration(500).call(zoom.transform, t);
}});

// Initial fit after layout settles
setTimeout(() => document.getElementById('btn-fit').click(), 2000);
</script>
</body>
</html>"""
