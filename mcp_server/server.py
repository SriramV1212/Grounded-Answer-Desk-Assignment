"""MCP server exposing the anthropic_docs Qdrant collection as agent tools.

Runs a streamable-http transport on port 8001 (single endpoint at /mcp) so
OpenClaw can connect to it. Run with: uv run python -m mcp_server.server
"""

import os
from typing import Any

from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from qdrant_client import QdrantClient

from mcp_server.embedder import embed_query

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
QDRANT_COLLECTION = os.environ.get("QDRANT_COLLECTION", "anthropic_docs")

_client = QdrantClient(url=QDRANT_URL)

# DNS-rebinding host checking on the streamable-http transport only allows
# localhost/127.0.0.1/[::1] by default. OpenClaw's Gateway runs in a Docker
# container on normal bridge networking and reaches this host-side server via
# `host.docker.internal`, so that Host header needs to be explicitly trusted
# too -- extending the default allowlist, not disabling the check.
_transport_security = TransportSecuritySettings(
    allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", "host.docker.internal:*"],
    allowed_origins=[
        "http://127.0.0.1:*",
        "http://localhost:*",
        "http://[::1]:*",
        "http://host.docker.internal:*",
    ],
)

mcp = FastMCP("grounded-answer-desk-kb", transport_security=_transport_security)


def _point_to_result(payload: dict[str, Any], score: float | None = None) -> dict[str, Any]:
    result = {
        "chunk_id": payload.get("chunk_id"),
        "text": payload.get("text"),
        "section_heading": payload.get("section_heading"),
        "parent_heading": payload.get("parent_heading"),
        "source_url": payload.get("source_url"),
    }
    if score is not None:
        result["score"] = score
    return result


@mcp.tool()
def search_kb(query: str, top_k: int = 4) -> list[dict[str, Any]]:
    """Search the Anthropic API documentation knowledge base for passages relevant to a question.

    ALWAYS call this before answering any question about Anthropic's API or docs --
    never answer from your own training knowledge. Embeds the query and runs a
    cosine-similarity vector search over the ingested documentation chunks.

    Returns up to top_k results, each a dict with:
      - chunk_id: unique ID of the chunk (use with get_source/get_related)
      - text: the chunk's full passage text
      - score: cosine similarity to the query, 0-1, higher is more relevant
      - section_heading: the specific subsection this passage is from
      - parent_heading: the page/topic this passage belongs to
      - source_url: the documentation URL to cite

    Cite section_heading and source_url for every claim drawn from a result.
    If every returned score is below 0.4, the corpus likely doesn't contain a
    reliable answer -- say so rather than guessing.
    """
    vector = embed_query(query)
    hits = _client.query_points(
        collection_name=QDRANT_COLLECTION,
        query=vector,
        limit=top_k,
        with_payload=True,
    ).points
    return [_point_to_result(hit.payload or {}, score=hit.score) for hit in hits]


@mcp.tool()
def get_source(chunk_id: str) -> dict[str, Any]:
    """Fetch the full text and metadata for one specific chunk by its chunk_id.

    Use this when you need to re-read or double-check the exact stored text of
    a chunk already surfaced by search_kb or get_related. Returns a dict with
    chunk_id, text, section_heading, parent_heading, source_url -- or an empty
    dict if the chunk_id doesn't exist.
    """
    points = _client.retrieve(collection_name=QDRANT_COLLECTION, ids=[chunk_id], with_payload=True)
    if not points:
        return {}
    return _point_to_result(points[0].payload or {})


@mcp.tool()
def list_sections() -> list[str]:
    """List every unique page/topic (parent_heading) covered by the knowledge base.

    Use this to see what subjects the corpus covers overall -- e.g. to gauge
    whether a question is likely answerable before calling search_kb, or to
    suggest related topics to the user.
    """
    headings: set[str] = set()
    next_offset = None
    while True:
        points, next_offset = _client.scroll(
            collection_name=QDRANT_COLLECTION,
            with_payload=["parent_heading"],
            with_vectors=False,
            limit=512,
            offset=next_offset,
        )
        for point in points:
            heading = (point.payload or {}).get("parent_heading")
            if heading:
                headings.add(heading)
        if next_offset is None:
            break
    return sorted(headings)


@mcp.tool()
def get_related(chunk_id: str, top_k: int = 3) -> list[dict[str, Any]]:
    """Find chunks similar to a given chunk, for 'see also' style follow-ups.

    Looks up chunk_id's stored embedding, then searches for the top_k most
    similar OTHER chunks (the chunk itself is excluded from results). Same
    return shape as search_kb: chunk_id, text, score, section_heading,
    parent_heading, source_url.
    """
    points = _client.retrieve(collection_name=QDRANT_COLLECTION, ids=[chunk_id], with_vectors=True)
    if not points:
        return []
    vector = points[0].vector
    hits = _client.query_points(
        collection_name=QDRANT_COLLECTION,
        query=vector,
        limit=top_k + 1,
        with_payload=True,
    ).points
    results = [
        _point_to_result(hit.payload or {}, score=hit.score) for hit in hits if str(hit.id) != str(chunk_id)
    ]
    return results[:top_k]


_mcp_asgi_app = mcp.streamable_http_app()

# Mounting a Starlette sub-app inside FastAPI does not, by itself, forward the
# sub-app's lifespan -- and the streamable-http transport's session manager is
# started from that lifespan. Without wiring it through explicitly, tool calls
# fail at runtime with a "task group is not initialized" error.
app = FastAPI(lifespan=_mcp_asgi_app.router.lifespan_context)
app.mount("/", _mcp_asgi_app)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8001)
