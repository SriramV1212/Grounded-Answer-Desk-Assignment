"""Embedding wrapper shared by the MCP server's tools.

Uses the same model and settings as ingestion/ingest.py (no query/document
prefix), so query embeddings stay in the same space as the stored chunk
embeddings. The model is loaded once, at import time, and reused for every
call -- reloading per request would add real latency to every search_kb call.
"""

from sentence_transformers import SentenceTransformer

EMBED_MODEL = "nomic-ai/nomic-embed-text-v1"

print(f"[mcp_server.embedder] Loading embedding model {EMBED_MODEL} ...")
_model = SentenceTransformer(EMBED_MODEL, trust_remote_code=True)
print("[mcp_server.embedder] Model loaded.")


def embed_query(text: str) -> list[float]:
    embedding = _model.encode(text, convert_to_numpy=True)
    return embedding.tolist()
