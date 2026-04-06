import logging
import os
import re

logger = logging.getLogger(__name__)


def get_embedding_client(config: dict):
    """Create an OpenAI-compatible client for embedding model."""
    from openai import OpenAI

    embedding_cfg = config.get("embedding", {})
    api_key = os.environ.get("LLM_API_KEY") or embedding_cfg.get("api_key", "ollama")
    return OpenAI(
        api_key=api_key,
        base_url=embedding_cfg.get("api_base", "http://localhost:11434/v1"),
    )


def init_vectordb(db_path: str = "./vectordb"):
    """Initialize ChromaDB persistent client."""
    import chromadb

    os.makedirs(db_path, exist_ok=True)
    client = chromadb.PersistentClient(path=db_path)
    logger.info("ChromaDB initialized at %s", db_path)
    return client


def embed_schema_md(md_path: str, config: dict, db_path: str = "./vectordb"):
    """Read schema .md file, chunk it, embed, and store in ChromaDB."""
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


def _chunk_schema_md(content: str) -> list[dict]:
    """Split schema markdown into chunks per table section."""
    chunks = []

    # Split by ## headers (table sections)
    sections = re.split(r'\n(?=## )', content)

    for section in sections:
        section = section.strip()
        if not section:
            continue

        # Extract table name from header
        header_match = re.match(r'^##\s+(\S+)', section)
        table_name = header_match.group(1) if header_match else "HEADER"

        if table_name == "Relationship":
            # Relationship Summary section - keep as one chunk
            chunks.append({
                "text": section,
                "type": "relationship_summary",
                "table": "ALL",
            })
        elif section.startswith("# "):
            # Main header with overview
            chunks.append({
                "text": section,
                "type": "schema_overview",
                "table": "ALL",
            })
        else:
            # Table section
            chunks.append({
                "text": section,
                "type": "table_schema",
                "table": table_name,
            })

    return chunks


def _chunk_query_md(content: str) -> list[dict]:
    """Split query analysis markdown into meaningful chunks."""
    chunks = []

    sections = re.split(r'\n(?=## )', content)

    for section in sections:
        section = section.strip()
        if not section:
            continue

        header_match = re.match(r'^##\s+(.+)', section)
        section_name = header_match.group(1) if header_match else "HEADER"

        if "Inferred Relationships" in section_name:
            chunks.append({
                "text": section,
                "type": "join_relationships",
                "table": "ALL",
            })
        elif "Table Usage" in section_name:
            chunks.append({
                "text": section,
                "type": "table_usage",
                "table": "ALL",
            })
        elif "Query Details" in section_name:
            # Split query details by mapper (### headers)
            mapper_sections = re.split(r'\n(?=### )', section)
            for mapper_sec in mapper_sections:
                mapper_sec = mapper_sec.strip()
                if not mapper_sec:
                    continue
                mapper_match = re.match(r'^###\s+(\S+)', mapper_sec)
                mapper_name = mapper_match.group(1) if mapper_match else "HEADER"
                chunks.append({
                    "text": mapper_sec,
                    "type": "query_detail",
                    "table": mapper_name,
                })
        elif section.startswith("# "):
            chunks.append({
                "text": section,
                "type": "query_overview",
                "table": "ALL",
            })

    return chunks


def _embed_and_store(collection, chunks: list[dict], embedding_client,
                     model: str, source: str):
    """Embed chunks and store in ChromaDB collection."""
    batch_size = 50
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i:i + batch_size]
        texts = [c["text"] for c in batch]

        embeddings = _get_embeddings_batch(embedding_client, model, texts)

        ids = [f"{source}_{i + j}" for j in range(len(batch))]
        metadatas = [{
            "source": source,
            "type": c.get("type", ""),
            "table": c.get("table", ""),
        } for c in batch]

        collection.upsert(
            ids=ids,
            documents=texts,
            embeddings=embeddings,
            metadatas=metadatas,
        )


def _get_embeddings_batch(client, model: str, texts: list[str]) -> list[list[float]]:
    """Get embeddings for a batch of texts."""
    response = client.embeddings.create(input=texts, model=model)
    return [item.embedding for item in response.data]


def _get_embedding(client, model: str, text: str) -> list[float]:
    """Get embedding for a single text."""
    response = client.embeddings.create(input=[text], model=model)
    return response.data[0].embedding
