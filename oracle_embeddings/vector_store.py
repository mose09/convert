import logging
import os
import re

logger = logging.getLogger(__name__)


def get_embedding_client(config: dict):
    """Create an OpenAI-compatible client for embedding model."""
    from openai import OpenAI

    embedding_cfg = config.get("embedding", {})
    api_key = os.environ.get("EMBEDDING_API_KEY") or os.environ.get("LLM_API_KEY") or embedding_cfg.get("api_key", "ollama")
    api_base = os.environ.get("EMBEDDING_API_BASE") or embedding_cfg.get("api_base", "http://localhost:11434/v1")
    return OpenAI(api_key=api_key, base_url=api_base)


def init_vectordb(db_path: str = "./vectordb"):
    """Initialize ChromaDB persistent client."""
    import chromadb

    os.makedirs(db_path, exist_ok=True)
    client = chromadb.PersistentClient(path=db_path)
    logger.info("ChromaDB initialized at %s", db_path)
    return client


def embed_schema_md(md_path: str, config: dict, db_path: str = "./vectordb"):
    """Read schema .md file, chunk it, embed, and store in ChromaDB."""
    global COLUMNS_PER_CHUNK
    COLUMNS_PER_CHUNK = config.get("vectordb", {}).get("columns_per_chunk", 30)

    client = init_vectordb(db_path)
    embedding_client = get_embedding_client(config)
    model = config.get("embedding", {}).get("model", "nomic-embed-text")

    with open(md_path, "r", encoding="utf-8") as f:
        content = f.read()

    chunks = _chunk_schema_md(content)
    logger.info("Schema: %d chunks from %s", len(chunks), md_path)

    collection = client.get_or_create_collection(
        name="schema",
        metadata={"description": "Oracle schema metadata"},
    )

    _embed_and_store(collection, chunks, embedding_client, model, source=os.path.basename(md_path))
    logger.info("Schema embedded: %d chunks stored", len(chunks))
    return len(chunks)


def embed_query_md(md_path: str, config: dict, db_path: str = "./vectordb"):
    """Read query analysis .md file, chunk it, embed, and store in ChromaDB."""
    client = init_vectordb(db_path)
    embedding_client = get_embedding_client(config)
    model = config.get("embedding", {}).get("model", "nomic-embed-text")

    with open(md_path, "r", encoding="utf-8") as f:
        content = f.read()

    chunks = _chunk_query_md(content)
    logger.info("Query: %d chunks from %s", len(chunks), md_path)

    collection = client.get_or_create_collection(
        name="queries",
        metadata={"description": "MyBatis query analysis and JOIN relationships"},
    )

    _embed_and_store(collection, chunks, embedding_client, model, source=os.path.basename(md_path))
    logger.info("Query analysis embedded: %d chunks stored", len(chunks))
    return len(chunks)


def search(query: str, config: dict, db_path: str = "./vectordb",
           n_results: int = 10, collections: list[str] = None) -> list[dict]:
    """Search vector DB for relevant chunks."""
    client = init_vectordb(db_path)
    embedding_client = get_embedding_client(config)
    model = config.get("embedding", {}).get("model", "nomic-embed-text")

    query_embedding = _get_embedding(embedding_client, model, query)

    if collections is None:
        collections = ["schema", "queries"]

    results = []
    for col_name in collections:
        try:
            collection = client.get_collection(col_name)
        except Exception:
            logger.warning("Collection '%s' not found, skipping", col_name)
            continue

        res = collection.query(
            query_embeddings=[query_embedding],
            n_results=n_results,
        )

        for i, doc in enumerate(res["documents"][0]):
            results.append({
                "text": doc,
                "collection": col_name,
                "metadata": res["metadatas"][0][i] if res["metadatas"] else {},
                "distance": res["distances"][0][i] if res["distances"] else 0,
            })

    # Sort by distance (lower = more relevant)
    results.sort(key=lambda x: x["distance"])
    return results


COLUMNS_PER_CHUNK = 30  # 컬럼 그룹 분할 단위 (config에서 변경 가능)


def _chunk_schema_md(content: str) -> list[dict]:
    """Split schema markdown into multi-level chunks per table.

    Strategy:
    - 컬럼이 적은 테이블: 테이블 전체를 1청크
    - 컬럼이 많은 테이블: 테이블 헤더 + 컬럼 그룹별 N청크 + PK/FK/인덱스 별도 청크
    - 이렇게 해야 임베딩 검색 시 컬럼 정보가 누락되지 않음
    """
    chunks = []

    sections = re.split(r'\n(?=## )', content)

    for section in sections:
        section = section.strip()
        if not section:
            continue

        header_match = re.match(r'^##\s+(\S+)', section)
        table_name = header_match.group(1) if header_match else "HEADER"

        if table_name == "Relationship":
            chunks.append({
                "text": section,
                "type": "relationship_summary",
                "table": "ALL",
            })
        elif section.startswith("# "):
            chunks.append({
                "text": section,
                "type": "schema_overview",
                "table": "ALL",
            })
        else:
            # Table section - multi-level chunking
            chunks.extend(_chunk_table_section(section, table_name))

    return chunks


def _chunk_table_section(section: str, table_name: str) -> list[dict]:
    """Split a single table section into multiple chunks if columns are many."""
    chunks = []
    lines = section.split("\n")

    # Separate header, column rows, and footer (PK/FK/Index)
    header_lines = []      # ## TABLE_NAME, >, table header row, separator
    column_rows = []       # | COL | TYPE | ... |
    footer_lines = []      # **Primary Key**, **Foreign Keys**, **Indexes**, ---

    in_column_table = False
    column_table_done = False

    for line in lines:
        if column_table_done:
            footer_lines.append(line)
        elif line.startswith("| ") and not line.startswith("|--") and not line.startswith("| Column"):
            # This is a data row in the column table
            in_column_table = True
            column_rows.append(line)
        elif in_column_table and not line.startswith("|"):
            # Column table ended
            column_table_done = True
            footer_lines.append(line)
        else:
            header_lines.append(line)

    table_header = "\n".join(header_lines).strip()
    footer_text = "\n".join(footer_lines).strip()

    # If few columns, keep as single chunk
    if len(column_rows) <= COLUMNS_PER_CHUNK:
        chunks.append({
            "text": section,
            "type": "table_schema",
            "table": table_name,
        })
    else:
        # Chunk 1: Table overview (header + column count + PK/FK summary)
        overview = f"{table_header}\n\nTotal columns: {len(column_rows)}\n"
        if footer_text:
            overview += f"\n{footer_text}"
        chunks.append({
            "text": overview,
            "type": "table_overview",
            "table": table_name,
        })

        # Chunk 2~N: Column groups (with table header repeated for context)
        for i in range(0, len(column_rows), COLUMNS_PER_CHUNK):
            group = column_rows[i:i + COLUMNS_PER_CHUNK]
            group_num = i // COLUMNS_PER_CHUNK + 1
            total_groups = (len(column_rows) + COLUMNS_PER_CHUNK - 1) // COLUMNS_PER_CHUNK

            col_text = f"## {table_name} - Columns ({group_num}/{total_groups})\n\n"
            col_text += "| Column | Type | Nullable | Default | Description |\n"
            col_text += "|--------|------|----------|---------|-------------|\n"
            col_text += "\n".join(group)

            chunks.append({
                "text": col_text,
                "type": "table_columns",
                "table": table_name,
            })

    # Separate PK/FK/Index chunk for relationship search accuracy
    if footer_text and ("Primary Key" in footer_text or "Foreign Key" in footer_text
                        or "Index" in footer_text):
        constraint_text = f"## {table_name} - Constraints\n\n{footer_text}"
        chunks.append({
            "text": constraint_text,
            "type": "table_constraints",
            "table": table_name,
        })

    return chunks


QUERIES_PER_CHUNK = 5  # mapper당 쿼리 분할 단위


def _chunk_query_md(content: str) -> list[dict]:
    """Split query analysis markdown into meaningful chunks.

    - Relationship 테이블: 행이 많으면 분할
    - Query Details: mapper 내 쿼리가 많으면 분할
    """
    chunks = []

    sections = re.split(r'\n(?=## )', content)

    for section in sections:
        section = section.strip()
        if not section:
            continue

        header_match = re.match(r'^##\s+(.+)', section)
        section_name = header_match.group(1) if header_match else "HEADER"

        if "Inferred Relationships" in section_name:
            # Extract individual relationships for per-pair chunking
            rel_rows = re.findall(r'^\|(?!\s*-).+\|$', section, re.MULTILINE)
            # Remove header rows
            data_rows = [r for r in rel_rows if not r.startswith("| Table") and not r.startswith("|--")]

            if len(data_rows) <= 15:
                chunks.append({
                    "text": section,
                    "type": "join_relationships",
                    "table": "ALL",
                    "tables_mentioned": _extract_table_names_from_rows(data_rows),
                })
            else:
                # Split relationship rows
                for i in range(0, len(data_rows), 15):
                    group = data_rows[i:i + 15]
                    group_text = "## Inferred Relationships (from JOIN)\n\n"
                    group_text += "| Table A | Column | JOIN | Table B | Column | Type | Source |\n"
                    group_text += "|---------|--------|------|---------|--------|------|--------|\n"
                    group_text += "\n".join(group)
                    tables = _extract_table_names_from_rows(group)
                    chunks.append({
                        "text": group_text,
                        "type": "join_relationships",
                        "table": "ALL",
                        "tables_mentioned": tables,
                    })

        elif "Table Usage" in section_name:
            chunks.append({
                "text": section,
                "type": "table_usage",
                "table": "ALL",
            })

        elif "Query Details" in section_name:
            # Split by mapper, then split large mappers
            mapper_sections = re.split(r'\n(?=### )', section)
            for mapper_sec in mapper_sections:
                mapper_sec = mapper_sec.strip()
                if not mapper_sec or not mapper_sec.startswith("###"):
                    continue
                mapper_match = re.match(r'^###\s+(\S+)', mapper_sec)
                mapper_name = mapper_match.group(1) if mapper_match else "UNKNOWN"

                # Split by individual queries within mapper
                query_blocks = re.split(r'\n(?=\*\*(?:SELECT|INSERT|UPDATE|DELETE)\*\*)', mapper_sec)
                header = query_blocks[0] if query_blocks else ""
                queries = query_blocks[1:] if len(query_blocks) > 1 else []

                if len(queries) <= QUERIES_PER_CHUNK:
                    chunks.append({
                        "text": mapper_sec,
                        "type": "query_detail",
                        "table": mapper_name,
                        "tables_mentioned": _extract_table_names_from_sql(mapper_sec),
                    })
                else:
                    for i in range(0, len(queries), QUERIES_PER_CHUNK):
                        group = queries[i:i + QUERIES_PER_CHUNK]
                        group_text = header + "\n" + "\n".join(group)
                        chunks.append({
                            "text": group_text,
                            "type": "query_detail",
                            "table": mapper_name,
                            "tables_mentioned": _extract_table_names_from_sql(group_text),
                        })

        elif section.startswith("# "):
            chunks.append({
                "text": section,
                "type": "query_overview",
                "table": "ALL",
            })

    return chunks


def _extract_table_names_from_rows(rows: list[str]) -> list[str]:
    """Extract table names from relationship table rows."""
    tables = set()
    for row in rows:
        cells = [c.strip() for c in row.split("|") if c.strip()]
        if len(cells) >= 4:
            tables.add(cells[0])
            tables.add(cells[3])
    return sorted(tables)


def _extract_table_names_from_sql(text: str) -> list[str]:
    """Extract table names from SQL text."""
    tables = set()
    for match in re.finditer(r'(?:FROM|JOIN|INTO|UPDATE)\s+(\w+)', text.upper()):
        table = match.group(1)
        if table not in ("SELECT", "WHERE", "AND", "OR", "ON", "SET", "VALUES", "DUAL"):
            tables.add(table)
    return sorted(tables)


def _embed_and_store(collection, chunks: list[dict], embedding_client,
                     model: str, source: str):
    """Embed chunks and store in ChromaDB collection with rich metadata."""
    batch_size = 50
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i:i + batch_size]
        texts = [c["text"] for c in batch]

        embeddings = _get_embeddings_batch(embedding_client, model, texts)

        ids = [f"{source}_{i + j}" for j in range(len(batch))]
        metadatas = []
        for c in batch:
            meta = {
                "source": source,
                "type": c.get("type", ""),
                "table": c.get("table", ""),
                "chunk_length": len(c["text"]),
            }
            # 연관 테이블 목록 (검색 필터링에 활용)
            tables_mentioned = c.get("tables_mentioned", [])
            if tables_mentioned:
                meta["tables_mentioned"] = ",".join(tables_mentioned)
            # 컬럼명 목록 (스키마 청크에서 추출)
            columns = _extract_column_names(c["text"])
            if columns:
                meta["columns"] = ",".join(columns[:30])  # ChromaDB 메타 크기 제한
            metadatas.append(meta)

        collection.upsert(
            ids=ids,
            documents=texts,
            embeddings=embeddings,
            metadatas=metadatas,
        )


def _extract_column_names(text: str) -> list[str]:
    """Extract column names from markdown table rows."""
    columns = []
    for match in re.finditer(r'^\|\s*(\w+)(?:\s*\(PK\))?\s*\|', text, re.MULTILINE):
        col = match.group(1)
        if col not in ("Column", "Table", "Source", "row_index"):
            columns.append(col)
    return columns


def _get_embeddings_batch(client, model: str, texts: list[str]) -> list[list[float]]:
    """Get embeddings for a batch of texts."""
    response = client.embeddings.create(input=texts, model=model)
    return [item.embedding for item in response.data]


def _get_embedding(client, model: str, text: str) -> list[float]:
    """Get embedding for a single text."""
    response = client.embeddings.create(input=[text], model=model)
    return response.data[0].embedding
