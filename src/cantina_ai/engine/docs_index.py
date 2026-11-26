"""RAG over a folder of markdown documents: heading-aware chunks, local embeddings
(Ollama), vectors in Postgres (pgvector), cosine search.

    await index_folder(conninfo, Path("docs/rules"))
    hits = await search(conninfo, "why is lunch charged without balance?")
"""

import hashlib
from pathlib import Path
from typing import Any

import psycopg

from cantina_ai.engine.chunking import chunk_note, note_title
from cantina_ai.engine.embeddings import OllamaEmbedder, to_pgvector

DDL = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS doc_chunks (
    id bigserial PRIMARY KEY,
    path text NOT NULL,
    title text NOT NULL,
    heading_path text[] NOT NULL,
    position int NOT NULL,
    content text NOT NULL,
    embedded_text text NOT NULL,
    content_hash text NOT NULL,
    embedding vector(1024) NOT NULL
);
"""


async def index_folder(
    conninfo: str, folder: Path, embedder: OllamaEmbedder, max_tokens: int = 400
) -> dict[str, Any]:
    rows = []
    for file in sorted(folder.rglob("*.md")):
        rel = file.relative_to(folder).as_posix()
        text = file.read_text(encoding="utf-8")
        title = note_title(text, rel)
        for pos, chunk in enumerate(chunk_note(text, title, max_tokens)):
            digest = hashlib.sha256(f"{embedder.model}\n{chunk.embedded_text}".encode())
            rows.append((rel, title, list(chunk.heading_path), pos, chunk, digest.hexdigest()))
    await embedder.pull_model()
    vectors = await embedder.embed([r[4].embedded_text for r in rows])
    with psycopg.connect(conninfo) as conn, conn.transaction():
        conn.execute(DDL)
        conn.execute("DELETE FROM doc_chunks")
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO doc_chunks (path, title, heading_path, position, content, "
                "embedded_text, content_hash, embedding) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector)",
                [
                    (rel, title, hp, pos, c.content, c.embedded_text, h, to_pgvector(v))
                    for (rel, title, hp, pos, c, h), v in zip(rows, vectors, strict=True)
                ],
            )
    return {"files": len({r[0] for r in rows}), "chunks": len(rows)}


async def search(
    conninfo: str, query: str, embedder: OllamaEmbedder, k: int = 5
) -> list[tuple[str, list[str], str, float]]:
    [vector] = await embedder.embed([query])
    v = to_pgvector(vector)
    with psycopg.connect(conninfo) as conn:
        return conn.execute(
            "SELECT path, heading_path, content, 1 - (embedding <=> %s::vector) "
            "FROM doc_chunks ORDER BY embedding <=> %s::vector LIMIT %s",
            (v, v, k),
        ).fetchall()


def format_hits(hits: list[tuple[str, list[str], str, float]], min_score: float = 0.45) -> str:
    good = [h for h in hits if h[3] >= min_score]
    if not good:
        return "no passage of the documentation seems to answer this"
    parts = []
    for i, (path, headings, content, score) in enumerate(good, 1):
        where = " > ".join(headings) if headings else path
        parts.append(f"[{i}] {path} — {where} (similarity {score:.2f})\n{content}")
    return "\n\n".join(parts)
