import asyncio
import os
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI()

OPENCLAW_GATEWAY_URL = os.environ.get("OPENCLAW_GATEWAY_URL", "http://localhost:18789")
OPENCLAW_GATEWAY_TOKEN = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "")
OPENCLAW_AGENT_ID = os.environ.get("OPENCLAW_AGENT_ID", "main")
# Name OpenClaw exposes our MCP server's search_kb tool as, once registered via
# `openclaw mcp add <server-name> ...` -- OpenClaw prefixes MCP tool names with
# the server name (e.g. "anthropic-docs__search_kb"). Configurable since the
# exact registered server name is a droplet-side choice, not a code constant.
MCP_SEARCH_KB_TOOL = os.environ.get("MCP_SEARCH_KB_TOOL_NAME", "anthropic-docs__search_kb")

# SOUL.md's exact abstention response (see agent/SOUL.md rule 4).
ABSTENTION_SENTENCE = "I could not find reliable information about this in the Anthropic documentation."

REQUEST_TIMEOUT = httpx.Timeout(60.0)


class AskRequest(BaseModel):
    question: str


@app.get("/health")
def health_check():
    return {"status": "ok", "message": "deployment test successful"}


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {OPENCLAW_GATEWAY_TOKEN}"}


async def _invoke_search_kb(client: httpx.AsyncClient, question: str, top_k: int = 4) -> list[dict[str, Any]]:
    """Call search_kb directly via the Gateway's /tools/invoke, so the
    retrieval inspector gets the exact chunk_id/text/score/section_heading/
    parent_heading/source_url fields the MCP server returns -- unmodified."""
    response = await client.post(
        f"{OPENCLAW_GATEWAY_URL}/tools/invoke",
        headers=_auth_headers(),
        json={"tool": MCP_SEARCH_KB_TOOL, "args": {"query": question, "top_k": top_k}},
    )
    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"MCP tool invoke failed ({response.status_code}): {response.text[:300]}",
        )
    body = response.json()
    if not body.get("ok"):
        raise HTTPException(status_code=502, detail=f"MCP tool invoke error: {body.get('error')}")
    return body.get("result") or []


async def _ask_agent(client: httpx.AsyncClient, question: str) -> str:
    """Ask the "main" agent via the Gateway's OpenAI-compatible chat
    completions endpoint. The agent calls search_kb itself internally to
    ground its answer; this call is independent of _invoke_search_kb above,
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
                _invoke_search_kb(client, question),
                _ask_agent(client, question),
            )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail=f"OpenClaw gateway unreachable: {exc}") from exc

    abstained = ABSTENTION_SENTENCE in answer
    citations = [] if abstained else _build_citations(answer, retrieved_chunks)

    return {
        "answer": answer,
        "citations": citations,
        "retrieved_chunks": retrieved_chunks,
        "abstained": abstained,
    }
