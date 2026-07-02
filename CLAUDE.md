# Grounded Answer Desk — Claude Code Master Context

## What This Project Is
A RAG system that answers questions grounded in the Anthropic API docs.
A user asks a question → FastAPI orchestrates → OpenClaw agent calls MCP tools → MCP server searches Qdrant → answer with citations returned → Next.js frontend displays answer + retrieval inspector.

This is an AI Engineer take-home assessment. Quality, architecture clarity, and completeness all matter. Target grade: A+ (95–100).

**Full assignment brief:** `01-grounded-answer-desk.html` in the repo root. This is the original, complete assignment document including the grading rubric. Read it in full before starting any work — it is the primary source of truth for requirements. If anything in this file conflicts with CLAUDE.md, flag it rather than silently picking one.

---

## Development Workflow: Local vs Remote

Claude Code runs locally on the developer's laptop, editing files in this local repo clone. The droplet is a separate remote machine, only reachable through the deploy pipeline — Claude Code does NOT SSH into the droplet to test-run or verify anything.

**Per step, the workflow is:**
1. Write/edit files locally in this repo
2. Explain what was changed and why, and what the expected verification output will be once deployed
3. Do not commit or push — the developer does this manually
4. The developer pushes to main, which triggers the existing GitHub Actions self-hosted runner to deploy to the droplet
5. The developer independently verifies the deployment on the droplet (checking the Actions run, curling endpoints, etc.)
6. The developer reports back the real verification results, and only then authorizes moving to the next step

A step is never "done" until the developer confirms it after a real deploy — Claude Code should not claim something works on the droplet without that confirmation.

---

## Golden Rules for Claude Code

1. **Work in steps.** Complete exactly one step at a time. When a step is done, stop and report back clearly. Do not start the next step until explicitly told to.
2. **Verify before reporting done.** Before saying a step is complete, run the verification command yourself and confirm it passes.
3. **Never hardcode secrets.** API keys and tokens always come from environment variables or `.env` files. `.env` is always in `.gitignore`.
4. **Explain before executing.** For any infrastructure command (installing software, modifying systemd, editing nginx), briefly explain what it does and why before running it.
5. **Preserve what works.** The existing FastAPI `/health` endpoint and CI/CD pipeline are working. Do not break them.
6. **One service at a time.** When adding a new service, get it running and verified before wiring it to anything else.
7. **Verification happens through deployment, not SSH.** Claude Code does not SSH into the droplet to test-run changes. The developer pushes to trigger the GitHub Actions deploy and reports back real results.
8. **Never commit or push without explicit permission.** Report changes and expected verification steps; the developer commits and pushes manually.

---

## Repository
- **GitHub:** https://github.com/SriramV1212/Grounded-Answer-Desk-Assignment
- **Branch:** main
- **CI/CD:** GitHub Actions with a self-hosted runner already running on the droplet
- **Deploy trigger:** push to main → runner pulls code → restarts services via systemd

---

## Infrastructure

### Droplet
- **Provider:** DigitalOcean
- **OS:** Ubuntu 22.04
- **Size:** 2 vCPU / 4GB RAM
- **User:** sriram
- **Package manager for Python:** `uv` (already installed)

### What's Already Working
- FastAPI app running as systemd service named `fastapi`
- Self-hosted GitHub Actions runner on the droplet
- `uv sync --frozen` installs Python dependencies on deploy
- `sudo systemctl restart fastapi` restarts the app on deploy
- **Docker is installed and working**
- **OpenClaw is already set up and running manually** via `/opt/openclaw/docker-compose.yml`, gateway healthy at `http://localhost:18789`, agent "main" registered. DO NOT redo OpenClaw onboarding. Only wire it into the CI/CD workflow so `docker compose up -d` in `/opt/openclaw` keeps it running/updated on future deploys.
- `ANTHROPIC_API_KEY` and `OPENCLAW_GATEWAY_TOKEN` are already set in GitHub Secrets
- **Qdrant is NOT yet installed — this is the first thing to set up**

### Ports in Use
| Port | Service |
|------|---------|
| 8000 | FastAPI main app (existing) |
| 8001 | MCP server |
| 6333 | Qdrant HTTP API + dashboard |
| 6334 | Qdrant gRPC |
| 18789 | OpenClaw gateway |

### Services to Run on Droplet
| Service | How |
|---------|-----|
| Qdrant | Docker container (new, via `infra/docker-compose.yml`) |
| OpenClaw | Docker container (already running via `/opt/openclaw/docker-compose.yml`) |
| MCP server | Python process via systemd (`mcp-server` service) |
| FastAPI orchestration | Python process via systemd (`fastapi` service, already exists) |
| Embedding model | Loaded in-process by MCP server (no separate service) |

---

## Tech Stack

### Corpus
- **Source:** `https://docs.anthropic.com/llms-full.txt`
- **Pages to ingest:** First 100 pages (split on `**URL:**` boundary)
- **Preprocessing:** Strip JSX/MDX component tags (`<Tab>`, `<Steps>`, `<PlatformAvailability />` etc.) before chunking

### Chunking Strategy
- **Pass 1:** Split on markdown headers (`##`, `###`) — each section becomes a candidate chunk
- **Pass 2 (fallback):** If a section exceeds 400 tokens, apply `RecursiveCharacterTextSplitter` within it
- **Target chunk size:** 300–400 tokens
- **Overlap:** 50 tokens
- **Metadata per chunk:** `source_url`, `section_heading`, `parent_heading`, `chunk_index`

### Embeddings
- **Model:** `nomic-ai/nomic-embed-text-v1`
- **Library:** `sentence-transformers` (direct Python, NOT Ollama)
- **Flag required:** `trust_remote_code=True`
- **Dimensions:** 768
- **Loaded in-process** inside the MCP server at startup — no separate embedding service

### Vector Store
- **Qdrant** running in Docker
- **Collection name:** `anthropic_docs`
- **Distance metric:** Cosine
- **Access from Python:** `qdrant-client` library, connecting to `localhost:6333`

### MCP Server
- **Language:** Python
- **Libraries:** `mcp` SDK + `fastapi` + `uvicorn`
- **Port:** 8001
- **Transport:** HTTP (SSE) so OpenClaw can connect to it
- **Tools exposed:**
  - `search_kb(query: str, top_k: int = 4)` — embed query, search Qdrant, return top-k chunks with scores and metadata
  - `get_source(chunk_id: str)` — return full chunk text by ID
  - `list_sections()` — return all unique top-level sections in the corpus
  - `get_related(chunk_id: str, top_k: int = 3)` — find chunks similar to a given chunk

### Agent
- **OpenClaw** already running in Docker at `/opt/openclaw` (`ghcr.io/openclaw/openclaw:latest`), gateway at `http://localhost:18789`, agent named "main"
- **LLM:** `claude-haiku-4-5` via Anthropic API
- **MCP connection:** OpenClaw connects to MCP server at `http://localhost:8001`
- **SOUL.md:** Instructs the agent to ONLY answer from retrieved MCP context, cite sources, and abstain when retrieval scores are low

### FastAPI Orchestration Layer (main.py)
- **Existing endpoint:** `GET /health` — keep this
- **New endpoint:** `POST /ask` — receives question, calls OpenClaw gateway API, returns structured response including answer, citations, and raw retrieved chunks with scores for the inspector panel

### Frontend
- **Framework:** Next.js (TypeScript)
- **Deploy:** Vercel (free tier)
- **Calls:** `POST /ask` on the droplet's public IP
- **UI components:**
  - Question input box
  - Answer display with inline citations
  - Retrieval inspector panel (table: chunk text | score | section | source URL)

---

## A+ Features (implement after core flow works)
- **Hybrid retrieval:** Combine vector search + BM25 keyword search, merge results before returning top-k
- **Reranking:** Cross-encoder reranker on top of hybrid retrieval results
- **Confidence-calibrated abstention:** Use retrieval scores to express confidence level, not just binary abstain/answer
- **UMAP visualization:** 2D projection of all chunk embeddings showing corpus coverage
- **Observability:** Log every query with question, chunks, scores, latency, token count to a `logs` table or JSONL file. Expose `GET /logs` endpoint.
- **Cost story:** Track token usage per query, expose `GET /stats` with total cost estimate

---

## Environment Variables

### On the Droplet (set in `/etc/environment` or injected via systemd service file)
```
ANTHROPIC_API_KEY=sk-ant-...
QDRANT_URL=http://localhost:6333
QDRANT_COLLECTION=anthropic_docs
MCP_SERVER_URL=http://localhost:8001
OPENCLAW_GATEWAY_URL=http://localhost:18789
OPENCLAW_GATEWAY_TOKEN=<generated during openclaw setup>
```

### In GitHub Secrets (Settings → Secrets → Actions)
```
ANTHROPIC_API_KEY
OPENCLAW_GATEWAY_TOKEN
```

---

## Project Structure
```
Grounded-Answer-Desk-Assignment/
├── CLAUDE.md                          ← this file
├── README.md                          ← redeployment instructions (graded)
├── main.py                            ← FastAPI orchestration layer
├── pyproject.toml                     ← Python dependencies (managed with uv)
├── uv.lock
├── .python-version                    ← 3.12
├── .env.example                       ← template (never commit .env)
│
├── ingestion/
│   ├── ingest.py                      ← download, clean, chunk, embed, upsert
│   └── verify.py                      ← confirm collection has expected vectors
│
├── mcp_server/
│   ├── server.py                      ← MCP server with 4 tools
│   └── embedder.py                    ← nomic-embed-text wrapper
│
├── agent/
│   └── SOUL.md                        ← OpenClaw agent personality + grounding instructions
│
├── frontend/
│   └── ...                            ← Next.js app (separate deploy to Vercel)
│
└── infra/
    ├── docker-compose.yml             ← Qdrant container only (OpenClaw already runs separately at /opt/openclaw)
    ├── nginx.conf                     ← reverse proxy config (future)
    └── systemd/
        └── mcp-server.service         ← systemd unit for MCP server
```

---

## CI/CD Pipeline Behaviour (after Step 1)
On every push to main, the GitHub Actions workflow will:
1. Pull latest code on the droplet (self-hosted runner)
2. Run `uv sync --frozen` to install Python dependencies
3. Run `docker compose -f infra/docker-compose.yml up -d` to ensure Qdrant is running (idempotent)
4. Run `docker compose -f /opt/openclaw/docker-compose.yml up -d` to keep OpenClaw current (idempotent, does not redo onboarding)
5. Restart `mcp-server` systemd service
6. Restart `fastapi` systemd service

---

## Step Completion Criteria

### Step 1 — Infrastructure
Run by the developer after pushing and deploying, not by Claude Code:
- `docker compose -f infra/docker-compose.yml ps` shows Qdrant running
- `curl http://localhost:6333/healthz` returns `{"title":"qdrant - healthy"}`
- `curl http://localhost:18789` still returns the OpenClaw Control UI (confirms OpenClaw untouched)
- Push to main triggers workflow successfully

### Step 2 — Ingestion
- `python ingestion/verify.py` reports 400–600 vectors in `anthropic_docs` collection
- Qdrant dashboard at `:6333/dashboard` shows collection with vectors

### Step 3 — MCP Server
- `curl -X POST http://localhost:8001/search_kb -d '{"query":"what is prompt caching"}'` returns JSON with chunks and scores
- All 4 tools return valid responses when called manually

### Step 4 — FastAPI /ask endpoint
- `curl -X POST http://localhost:8000/ask -H 'Content-Type: application/json' -d '{"question":"how does prompt caching work?"}'` returns JSON with `answer`, `citations`, and `chunks` array
- Off-topic question returns abstention response

### Step 5 — Frontend
- Vercel URL loads, question can be submitted, answer + inspector panel display correctly

### Step 6 — A+ features
- Each feature has its own verification (listed in task description when we get there)
