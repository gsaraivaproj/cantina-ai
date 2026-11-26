"""Index the demo's rule documents (demo/docs) for search_rules.

uv run cantina-demo-index
"""

import asyncio
import os
from pathlib import Path
from typing import Any

from cantina_ai.engine import docs_index
from cantina_ai.engine.embeddings import OllamaEmbedder

DOCS = Path(__file__).parent / "docs"


def main() -> None:
    embedder = OllamaEmbedder(
        os.environ.get("CANTINA_OLLAMA_URL", "http://localhost:11434"), "bge-m3", keep_alive="1m"
    )

    async def go() -> dict[str, Any]:
        try:
            return await docs_index.index_folder(
                os.environ.get("CANTINA_AI_DB", "dbname=cantina_ai"), DOCS, embedder
            )
        finally:
            await embedder.aclose()

    print(asyncio.run(go()))
