"""Manual verification client for the running MCP server.

Exercises all 4 tools once (to confirm shape/wiring), then runs a fixed set of
in-corpus and off-corpus questions through search_kb as a retrieval-quality
check. Requires the MCP server to already be running separately
(uv run python -m mcp_server.server) and reachable at MCP_SERVER_URL.

Run with: uv run python mcp_server/test_client.py
"""

import asyncio
import json
import os

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://localhost:8001")

# (question, is_expected_in_corpus)
RETRIEVAL_QUESTIONS = [
    ("How does prompt caching work?", True),
    ("What's the difference between the Messages API and Claude Managed Agents?", True),
    ("What is the context window size for Claude models?", True),
    ("How do I authenticate with the CLI?", True),
    ("What is extended thinking?", True),
    ("How does tool use work with the Messages API?", True),
    ("What models support adaptive thinking?", True),
    ("How do I generate embeddings for my documents?", True),
    ("How do I use GPT-4 for text generation?", False),
    ("What's the best pizza dough recipe?", False),
]


def _extract(result):
    """Unwrap a CallToolResult into plain Python data. list-returning tools
    get wrapped by the SDK as {"result": [...]}; dict-returning tools are
    already a plain object, so .get() falls through to the payload itself."""
    if result.structuredContent is not None:
        payload = result.structuredContent
        return payload.get("result", payload) if isinstance(payload, dict) else payload
    if result.content:
        text = result.content[0].text
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text
    return None


def _print_hits(hits: list[dict]) -> None:
    for i, h in enumerate(hits, 1):
        print(f"  {i}. score={h['score']:.4f} | {h['parent_heading']} > {h['section_heading']} | {h['source_url']}")


async def main() -> None:
    async with streamablehttp_client(f"{MCP_SERVER_URL}/mcp") as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            print("=== Tool smoke tests (all 4 tools) ===\n")

            print("[search_kb] query='how does prompt caching work?' top_k=3")
            r = await session.call_tool("search_kb", {"query": "how does prompt caching work?", "top_k": 3})
            hits = _extract(r)
            _print_hits(hits)
            print()

            first_chunk_id = hits[0]["chunk_id"]

            print(f"[get_source] chunk_id={first_chunk_id}")
            r = await session.call_tool("get_source", {"chunk_id": first_chunk_id})
            src = _extract(r)
            print(f"  section_heading={src.get('section_heading')!r}")
            print(f"  source_url={src.get('source_url')}")
            print(f"  text preview: {src.get('text', '')[:150]!r}")
            print()

            print("[list_sections]")
            r = await session.call_tool("list_sections", {})
            sections = _extract(r)
            print(f"  total unique sections: {len(sections)}")
            print(f"  first 10: {sections[:10]}")
            print()

            print(f"[get_related] chunk_id={first_chunk_id} top_k=3")
            r = await session.call_tool("get_related", {"chunk_id": first_chunk_id, "top_k": 3})
            related = _extract(r)
            _print_hits(related)
            print()

            print("=== Retrieval quality test ===\n")
            for question, in_corpus in RETRIEVAL_QUESTIONS:
                r = await session.call_tool("search_kb", {"query": question, "top_k": 3})
                hits = _extract(r)
                tag = "IN-CORPUS" if in_corpus else "OFF-CORPUS"
                print(f"Q ({tag}): {question}")
                _print_hits(hits)
                print()


if __name__ == "__main__":
    asyncio.run(main())
