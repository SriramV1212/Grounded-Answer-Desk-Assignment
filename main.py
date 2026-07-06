import asyncio
import json
import os
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from pydantic import BaseModel

load_dotenv()

app = FastAPI()

OPENCLAW_GATEWAY_URL = os.environ.get("OPENCLAW_GATEWAY_URL", "http://localhost:18789")
OPENCLAW_GATEWAY_TOKEN = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "")
OPENCLAW_AGENT_ID = os.environ.get("OPENCLAW_AGENT_ID", "main")
MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://localhost:8001")

# Must stay identical to whatever top_k the agent effectively retrieves with,
# so the independent lookup below and the agent's internal retrieval search
# under matching parameters (see CLAUDE.md's fallback-fidelity caveat).
SEARCH_KB_TOP_K = 4

# SOUL.md's exact abstention response (see agent/SOUL.md rule 4).
ABSTENTION_SENTENCE = "I could not find reliable information about this in the Anthropic documentation."

# Same interim 0.6 cutoff SOUL.md rule 4 uses (see agent/SOUL.md and CLAUDE.md's
# A+ Features TODO -- this is not a new calibration, just the existing constant
# checked in a second place).
ABSTENTION_SCORE_THRESHOLD = 0.6

REQUEST_TIMEOUT = httpx.Timeout(60.0)


class AskRequest(BaseModel):
    question: str


@app.get("/health")
def health_check():
    return {"status": "ok", "message": "deployment test successful"}


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {OPENCLAW_GATEWAY_TOKEN}"}


def _extract_tool_result(result) -> Any:
    """Unwrap a CallToolResult the same way mcp_server/test_client.py does:
    list-returning tools get wrapped by the SDK as {"result": [...]}, so
    .get("result", payload) falls through to the raw payload for dict-shaped
    tool results that aren't wrapped this way."""
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


async def _search_kb_direct(question: str, top_k: int = SEARCH_KB_TOP_K) -> list[dict[str, Any]]:
    """Fetch retrieved_chunks by connecting directly to our own MCP server and
    calling search_kb ourselves, bypassing OpenClaw's Gateway entirely for this
    call. This exists because OpenClaw's /tools/invoke endpoint does not
    support MCP-bundled tools in the currently deployed version (confirmed via
    source inspection and empirical 404s against a tool the gateway itself
    successfully calls internally during a normal agent turn -- see
    CLAUDE.md's "Retrieval inspector data fidelity" note for the full
    investigation and the resulting fidelity tradeoff this introduces).
    """
    try:
        async with streamablehttp_client(f"{MCP_SERVER_URL}/mcp") as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("search_kb", {"query": question, "top_k": top_k})
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"MCP server unreachable: {exc}") from exc

    if result.isError:
        text = result.content[0].text if result.content else "unknown error"
        raise HTTPException(status_code=502, detail=f"search_kb tool error: {text}")

    hits = _extract_tool_result(result)
    return hits or []


async def _ask_agent(client: httpx.AsyncClient, question: str) -> str:
    """Ask the "main" agent via the Gateway's OpenAI-compatible chat
    completions endpoint. The agent calls search_kb itself internally to
    ground its answer; this call is independent of _search_kb_direct above,
    which is only for surfacing retrieval data to the inspector panel."""
    response = await client.post(
        f"{OPENCLAW_GATEWAY_URL}/v1/chat/completions",
        headers=_auth_headers(),
        json={
            "model": f"openclaw/{OPENCLAW_AGENT_ID}",
            "messages": [{"role": "user", "content": question}],
        },
    )
    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"OpenClaw gateway error ({response.status_code}): {response.text[:300]}",
        )
    body = response.json()
    choices = body.get("choices") or []
    if not choices:
        raise HTTPException(status_code=502, detail="OpenClaw gateway returned no choices")
    return choices[0]["message"]["content"] or ""


def _build_citations(answer: str, chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A chunk is cited if the agent's answer actually references its section
    heading. Matching on source_url alone over-counts: multiple chunks from
    the same page share a URL, so mentioning it once would falsely credit
    every chunk from that page rather than just the one actually discussed."""
    citations = []
    for chunk in chunks:
        heading = chunk.get("section_heading") or ""
        if heading and heading in answer:
            citations.append(
                {
                    "section_heading": chunk.get("section_heading"),
                    "source_url": chunk.get("source_url"),
                    "chunk_id": chunk.get("chunk_id"),
                }
            )
    return citations


@app.post("/ask")
async def ask(request: AskRequest):
    question = (request.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="`question` must be a non-empty string")

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            retrieved_chunks, answer = await asyncio.gather(
                _search_kb_direct(question),
                _ask_agent(client, question),
            )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail=f"OpenClaw gateway unreachable: {exc}") from exc

    # Two independent abstention signals, OR'd together: the agent's own exact
    # refusal sentence (per SOUL.md rule 4), and our own check of the real
    # retrieved_chunks scores. The agent doesn't always reproduce the mandated
    # sentence verbatim even when scores are clearly low (observed directly in
    # testing -- e.g. an off-corpus question where the agent improvised a
    # differently-worded refusal instead of quoting SOUL.md exactly), so
    # trusting the agent's text alone is not reliable enough on its own.
    low_confidence = not retrieved_chunks or max(c["score"] for c in retrieved_chunks) < ABSTENTION_SCORE_THRESHOLD
    abstained = (ABSTENTION_SENTENCE in answer) or low_confidence
    citations = [] if abstained else _build_citations(answer, retrieved_chunks)

    return {
        "answer": answer,
        "citations": citations,
        "retrieved_chunks": retrieved_chunks,
        "abstained": abstained,
    }
