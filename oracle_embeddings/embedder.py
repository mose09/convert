import os
import time
import logging

from openai import OpenAI

logger = logging.getLogger(__name__)

MAX_RETRIES = 3


def generate_embeddings(texts: list[str], embedding_config: dict) -> list[list[float]]:
    """Generate embeddings for a list of texts using an OpenAI-compatible API."""
    client = OpenAI(
        api_key=os.environ["OPENAI_API_KEY"],
        base_url=embedding_config.get("api_base", "https://api.openai.com/v1"),
    )
    model = embedding_config["model"]
    batch_size = embedding_config.get("batch_size", 100)
    dimensions = embedding_config.get("dimensions")

    all_embeddings = []
    total = len(texts)

    for i in range(0, total, batch_size):
        batch = texts[i : i + batch_size]
        batch_num = i // batch_size + 1
        total_batches = (total + batch_size - 1) // batch_size
        logger.info("Embedding batch %d/%d (%d texts)", batch_num, total_batches, len(batch))

        result = _call_api(client, batch, model, dimensions)
        all_embeddings.extend(result)

    logger.info("Generated %d embeddings", len(all_embeddings))
    return all_embeddings


def _call_api(client: OpenAI, batch: list[str], model: str, dimensions: int = None) -> list[list[float]]:
    """Call the embeddings API with retry logic."""
    kwargs = {"input": batch, "model": model}
    if dimensions:
        kwargs["dimensions"] = dimensions

    for attempt in range(MAX_RETRIES):
        try:
            response = client.embeddings.create(**kwargs)
            return [item.embedding for item in response.data]
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                wait = 2 ** (attempt + 1)
                logger.warning("API call failed (attempt %d/%d): %s. Retrying in %ds...",
                               attempt + 1, MAX_RETRIES, e, wait)
                time.sleep(wait)
            else:
                raise
