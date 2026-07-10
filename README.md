# Grounded Answer Desk

**Live demo:** [grounded-answer-desk-frontend.vercel.app](https://grounded-answer-desk-frontend.vercel.app/)

A retrieval-augmented generation (RAG) system that answers questions grounded in the Anthropic API documentation. A question comes in through a FastAPI backend, which orchestrates an OpenClaw agent that calls MCP tools to search a Qdrant vector store and returns a cited, grounded answer. The backend also queries the MCP server directly to surface the retrieved passages and their similarity scores, and a Next.js frontend renders the answer with inline citations alongside a retrieval inspector panel showing exactly what was retrieved.

## Tech Stack & Architecture

**Tech stack:**
- **Backend orchestration:** FastAPI
- **Agent runtime:** OpenClaw
- **Tool protocol:** Python MCP SDK
- **Vector store:** Qdrant
- **Embeddings:** `nomic-embed-text` via `sentence-transformers`
- **Frontend:** Next.js, TypeScript, Tailwind CSS
- **Containerization:** Docker
- **Process management:** systemd
- **HTTPS (optional):** nginx + Certbot
- **CI/CD (optional):** GitHub Actions

**Architecture overview:** a question submitted to `POST /ask` is orchestrated by FastAPI, which sends it to an OpenClaw agent. The agent calls MCP tools (`search_kb`, `get_source`, `list_sections`, `get_related`) exposed by the MCP server, which embeds the query and searches the Qdrant vector store, returning ranked chunks with citations. The agent grounds its answer strictly in those retrieved chunks and returns a cited response.

Separately, FastAPI's `/ask` endpoint also makes an **independent call directly to the MCP server** to populate the `retrieved_chunks` field used by the frontend's retrieval inspector panel. This second call exists because OpenClaw's `/tools/invoke` endpoint doesn't yet support MCP-bundled tools in the deployed version, so the agent's own internal tool-call result can't be fetched directly from the gateway. See "A note on `retrieved_chunks` fidelity" further below for the full investigation and why this is a safe substitute rather than a guess.

There are three sections below:
- **Backend Deployment Instructions (required, ~10 min)** — runner-agnostic, entirely over plain HTTP, no domain needed. This is what anyone deploying to a fresh VPS uses, and it's fully self-contained: no nginx, no Certbot, no domain, anywhere in this path.
- **CI/CD Pipeline Setup (optional)** — developer convenience only.
- **Frontend Deployment Instructions (optional)** — the HTTPS setup this backend needs to work with a Vercel-hosted (HTTPS) frontend, plus a link to the frontend's own repository and deployment docs.

---

## Backend Deployment Instructions

Assumes nothing except a fresh Ubuntu 22.04 VPS and SSH access as a sudo-capable user. No self-hosted runner, no pre-installed Docker/uv, nothing pre-onboarded.

### 1. Install Docker and uv

```bash
curl -fsSL https://get.docker.com | sh
sudo systemctl enable docker
sudo apt-get install -y docker-compose-plugin

curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc   # or open a new shell, so `uv` is on PATH
```

### 2. Clone the repo and install Python dependencies

```bash
git clone <repo-url> ~/Grounded-Answer-Desk-Assignment
cd ~/Grounded-Answer-Desk-Assignment
uv sync --frozen
```

### 3. Create `.env` with real secrets

```bash
cp .env.example .env
```

Paste the API key for your chosen AI provider into `LLM_API_KEY` now (you'll need it in the OpenClaw onboarding step below — see "Choosing a model" below if you're not sure which provider/model to use). Leave `OPENCLAW_GATEWAY_TOKEN` blank for now — it doesn't exist yet, and step 5 below tells you exactly when to come back and fill it in.

**This step is easy to skip and will silently break `/ask` if you do.** `.env` is gitignored on purpose (never commit secrets), which means `git clone` never creates one on a fresh VPS — nothing else in this path creates it either. If you skip this, `fastapi.service` starts fine and `/health` still works, but any `POST /ask` will fail with an `Illegal header value b'Bearer '` error, because `main.py`'s `load_dotenv()` finds no `.env` file and `OPENCLAW_GATEWAY_TOKEN` silently defaults to an empty string. There's no startup-time check that catches this — the failure only shows up the first time you actually call `/ask`. If you hit that exact error, this is why: check `ls -la ~/Grounded-Answer-Desk-Assignment/.env` first.

This one-time repo-root `.env` is all a manual redeploy needs. If you also plan to use the CI/CD pipeline for ongoing deploys, see that section below — it automatically recreates this same file on every push, since a CI checkout would otherwise wipe it.

#### Choosing a model

This project's own deployment uses **Claude Haiku 4.5** (`claude-haiku-4-5`) via the Anthropic API — chosen for its low cost, not because the system depends on it. Any provider/model OpenClaw supports will work, as long as it's reasonably capable at following instructions and calling tools. To help you judge whether a given model is a good fit (cost, context window, capability), here's what this system actually asks of it:

- **Claude Haiku 4.5 specs (the model this deployment uses):** 200K-token context window, up to 64K output tokens, priced at $1.00 / $5.00 per 1M input/output tokens. Rate limits are account-tier-dependent — check your provider's current limits before assuming a number (for Anthropic: [platform.claude.com/docs/en/api/rate-limits](https://platform.claude.com/docs/en/api/rate-limits)).
- **Typical token usage per grounded-answer call:** this is a rough estimate from the pipeline's shape, not a benchmarked number (there's no token-usage logging in this build yet — see the `GET /stats` idea under A+ Features in `CLAUDE.md` if you want to add real measurement). Per `/ask` request, the agent's context includes: the system prompt (`agent/SOUL.md`, ~500-600 tokens), the 4 MCP tool schemas (`search_kb`, `get_source`, `list_sections`, `get_related`, a few hundred tokens combined), the user's question, and the `search_kb` tool result — `top_k=4` retrieved chunks at ~300-400 tokens each (per this project's chunking target), roughly 1,200-1,600 tokens. Altogether that's on the order of **2,000-3,000 input tokens** across the tool-use round trip, with a typical answer well under 500 output tokens.
- **What this means for picking a model:** the retrieved-context size (a handful of ~300-400 token chunks), not context window size, is the real constraint here — almost any current model's context window comfortably fits this workload, so context window isn't usually the deciding factor. What matters more is whether your chosen provider/model reliably follows the "only answer from retrieved context, cite sources, abstain when unsure" instructions in `agent/SOUL.md`, and what it costs at your expected query volume.

### 4. Start Qdrant

```bash
docker compose -f infra/docker-compose.yml up -d
curl http://localhost:6333/healthz   # expect {"title":"qdrant - healthy"}
```

### 5. Onboard OpenClaw (the one manual, interactive step)

Every other step in this path is a script or a single idempotent command. This one isn't, because OpenClaw's onboarding CLI is interactive by design — scripting a non-interactive workaround was considered and deliberately not done, since this step only ever runs once per VPS.

```bash
docker pull ghcr.io/openclaw/openclaw:latest
git clone https://github.com/openclaw/openclaw.git /opt/openclaw
cd /opt/openclaw
export OPENCLAW_IMAGE="ghcr.io/openclaw/openclaw:latest"
./scripts/docker/setup.sh
```

The wizard will prompt you, in order:

| Prompt | What to answer |
|---|---|
| Select channel (QuickStart) | **Skip for now** |
| Search provider | **Skip for now** |
| LLM provider | Whichever provider you have an API key for — OpenClaw supports several; pick yours |
| API key | Paste the API key for the provider you selected (the value you put in `.env`'s `LLM_API_KEY` field in step 3) |
| Model | Pick a model from that provider — see "Choosing a model" in step 3 above for what to consider |

This project's own deployment answers **Anthropic** / **`claude-haiku-4-5`** here — shown as a concrete, working example, not a requirement.

`setup.sh` starts the gateway automatically via Docker Compose once the wizard finishes. Retrieve the gateway token afterward:

```bash
sudo docker compose -f /opt/openclaw/docker-compose.yml exec openclaw-gateway env | grep -i TOKEN
curl http://localhost:18789   # expect the OpenClaw Control UI
```

Save that token as `OPENCLAW_GATEWAY_TOKEN` in the `.env` file you created in step 3.

### 6. Install the fastapi and mcp-server systemd services

```bash
cd ~/Grounded-Answer-Desk-Assignment
./infra/scripts/setup-fastapi-service.sh
./infra/scripts/setup-mcp-service.sh
```

Both scripts template their `infra/systemd/*.service` file with the actual repo path and `uv` binary location, install it to `/etc/systemd/system/`, `daemon-reload`, `enable`, and start it. Both are idempotent. These are run **directly by hand here** — not through any CI/CD workflow (see "CI/CD Pipeline Setup" below for why that's a deliberate separation).

### 7. Register the MCP server with OpenClaw

With `mcp-server` now running (previous step), register it as a tool source for the "main" agent:

```bash
docker compose -f /opt/openclaw/docker-compose.yml run --rm openclaw-cli mcp add anthropic-docs \
  --url http://host.docker.internal:8001/mcp --transport streamable-http
docker compose -f /opt/openclaw/docker-compose.yml run --rm openclaw-cli mcp probe anthropic-docs --json
```

Use `host.docker.internal`, not `localhost` — the OpenClaw gateway runs in a Docker container with normal bridge networking, and `mcp-server` runs natively on the host, so `localhost:8001` from inside the container would not resolve to it. The `probe` command should report 4 tools (`search_kb`, `get_source`, `list_sections`, `get_related`). This step is idempotent — re-running `mcp add` against an already-registered server is safe.

### 8. Populate the knowledge base

```bash
uv run python ingestion/ingest.py
uv run python ingestion/verify.py
```

`ingest.py` downloads the corpus, chunks it, embeds ~3,100 chunks, and populates the `anthropic_docs` Qdrant collection (it drops and recreates the collection each run, so it's always safe to re-run). `verify.py` should report a vector count in the 2,500-3,500 range.

Full ingestion, including first-time embedding model download (~550MB from HuggingFace) and processing ~3,100 chunks, completes in under 1 minute on a 2 vCPU/4GB VPS (benchmarked: 48 seconds, cold model cache).

### 9. Verify everything

```bash
curl http://localhost:8000/health
curl http://localhost:6333/healthz
sudo systemctl status mcp-server fastapi
uv run python mcp_server/spot_check.py
uv run python mcp_server/test_client.py

# In-corpus question — expect a grounded answer with citations and real retrieved_chunks
curl -X POST http://localhost:8000/ask -H 'Content-Type: application/json' \
  -d '{"question":"How does prompt caching work?"}'

# Off-corpus question — expect "abstained": true
curl -X POST http://localhost:8000/ask -H 'Content-Type: application/json' \
  -d '{"question":"What is the best pizza dough recipe?"}'

# Malformed request — expect HTTP 400
curl -i -X POST http://localhost:8000/ask -H 'Content-Type: application/json' -d '{"question":""}'
```

#### A note on `retrieved_chunks` fidelity

The `/ask` response's `retrieved_chunks` field comes from an **independent call** that FastAPI makes directly to the MCP server (using the same question text and `top_k=4` the agent uses), not from literally capturing the agent's internal tool call. This is because OpenClaw's `/tools/invoke` endpoint doesn't yet support MCP-bundled tools in the deployed version (confirmed via source + empirical testing). `agent/SOUL.md` instructs the agent to search using the user's question verbatim, and since retrieval is fully deterministic given identical input, this independent lookup is expected to match what the agent actually retrieved — with the one disclosed residual risk being the agent failing to search verbatim.

---

## CI/CD Pipeline Setup

This section documents the CI/CD pipeline the way I actually set it up during development — a working example of automating redeploys, not just an abstract option. **Optional:** this is a separate layer for faster iteration during ongoing development, and it assumes the Backend Deployment Instructions above have already been run once on this VPS (systemd services installed, OpenClaw onboarded).

I registered a GitHub Actions self-hosted runner directly on the VPS, via the repo's Settings → Actions → Runners → New self-hosted runner flow, which walks through downloading the runner binary, configuring it against this repo, and installing it as a service so it keeps running across reboots. Once registered, it executes `.github/workflows/deploy.yml` on every push to `main`:
1. Pull latest code (clean checkout)
2. `uv sync --frozen`
3. Write `.env` in the repo root from GitHub Secrets
4. `docker compose -f infra/docker-compose.yml up -d` (Qdrant, idempotent)
5. `docker compose -f /opt/openclaw/docker-compose.yml up -d` (OpenClaw, idempotent, does not redo onboarding)
6. `sudo systemctl restart mcp-server`
7. `sudo systemctl restart fastapi`

I deliberately kept step 3 as its own clearly separated step rather than folding it into another one, because it's handling secrets specifically. The actual secret values (`LLM_API_KEY`, `OPENCLAW_GATEWAY_TOKEN`) are stored as **GitHub Secrets** (Settings → Secrets and variables → Actions), **not** maintained as a static `.env` file sitting in the project directory. The workflow writes `.env` fresh from those secrets on every deploy, rather than relying on a file that's expected to just persist between runs.

**Why this step exists at all:** `actions/checkout@v4` does a clean checkout on every run, which silently wipes any gitignored/untracked file inside the repo directory — including a repo-root `.env` — on every single deploy. I hit this for real during development: a manually-created repo-root `.env` (from the Backend Deployment Instructions above) worked fine until the next push, at which point the clean checkout removed it and `/ask` started failing with `Illegal header value b'Bearer '` again. The fix is to just recreate it: step 3 runs right after checkout and rewrites `.env` in the repo root from GitHub Secrets, every push, before `fastapi` gets restarted. This uses the exact same repo-root `.env` + `load_dotenv()` mechanism as the manual setup — no separate file, no systemd changes — it's simply redone automatically on every deploy instead of once by hand.

Deliberately, this workflow only *restarts* already-installed services — it never installs/enables systemd units or re-onboards OpenClaw, so a routine push can never silently change the system configuration. It also does **not** re-run ingestion; that stays a deliberate manual step.

Requires `LLM_API_KEY` and `OPENCLAW_GATEWAY_TOKEN` set as GitHub Secrets (Settings → Secrets and variables → Actions), and a self-hosted runner registered against the repo.

---

## Frontend Deployment Instructions

**Optional, and not part of the Backend Deployment Instructions above.** The RAG backend itself works fully over plain HTTP — `curl http://<VPS-ip>:8000/ask` is sufficient to exercise the whole system. This section exists because of two separate browser-side restrictions a Vercel-hosted frontend runs into: **mixed content blocking** (browsers block a page served over HTTPS from calling a plain-HTTP API — this is nginx/Certbot's problem, below) and **CORS** (the backend only allows browser requests from origins explicitly listed in `ALLOWED_ORIGINS` — see "After deploying the frontend" below). If you only want to test the backend directly with `curl`, neither restriction applies, and you can skip this section entirely.

If you *do* want to point a live Vercel frontend at this backend, run:

```bash
./infra/scripts/setup-nginx-https.sh [domain]
```

**With a domain you own** (e.g. `api.example.com`), pass it directly — but point its DNS A record at the VPS's public IP *before* running the script, or the certbot step will fail:
```bash
./infra/scripts/setup-nginx-https.sh api.example.com
```

**Without a domain**, run it with no argument:
```bash
./infra/scripts/setup-nginx-https.sh
```
The script auto-detects the VPS's public IP and builds a free [sslip.io](https://sslip.io) hostname from it (e.g. `143.198.51.2.sslip.io`), which resolves to that IP automatically — no DNS setup, no purchase, no registration. Anyone can complete this step at zero cost, regardless of whether they own a domain.

Either way, the script:
1. Installs `nginx` and `certbot` (+ `python3-certbot-nginx`)
2. Writes an nginx reverse-proxy config forwarding the chosen hostname to `localhost:8000`
3. Runs `certbot --nginx -d <hostname>` — **this step is interactive**: certbot asks for an email address (renewal notices) and whether to redirect HTTP to HTTPS (answer yes)
4. Prints a `curl https://<hostname>/health` command to verify

For example, running the script with a domain argument like `example.com` uses that domain directly, while running it with no arguments gets an equivalent sslip.io hostname with identical behavior.

### Deploying the frontend itself

The frontend (Next.js, deployed to Vercel) lives in its own repository: **[Grounded-Answer-Desk-Frontend](https://github.com/SriramV1212/Grounded-Answer-Desk-Frontend)**. Build steps, environment variables (including pointing it at this backend's URL), and Vercel deployment instructions are documented in that repo's own README, not duplicated here.

### After deploying the frontend: update CORS

`ALLOWED_ORIGINS` (see `.env.example`) defaults to just `http://localhost:3000` — your local frontend dev origin. It does not automatically pick up a new frontend URL, because that URL doesn't exist until *after* you deploy the frontend in the step above. Once you have your real Vercel URL:

1. Add it to `ALLOWED_ORIGINS` in this backend's `.env` on the VPS (comma-separated, alongside `http://localhost:3000`) — either by hand, or by re-running Tier 2's CI/CD workflow after updating the hardcoded value in `.github/workflows/deploy.yml`'s "Write .env from secrets" step, if you're using that path.
2. Restart the backend so it picks up the change: `sudo systemctl restart fastapi`.

Until you do this, the deployed frontend's browser calls to `/ask` will fail with a CORS error, even though the backend itself is reachable and healthy — `curl` and server-to-server calls are unaffected, since CORS is a browser-enforced restriction, not a server-side check.
