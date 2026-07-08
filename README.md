# Grounded Answer Desk

A RAG system that answers questions grounded in the Anthropic API docs. See `CLAUDE.md` for full architecture and `01-grounded-answer-desk.html` for the assignment brief.

There are three separate, non-overlapping deployment layers, covered in order below:
- **Tier 1 — Redeploying from scratch (required, ~10 min)** — runner-agnostic, entirely over plain HTTP, no domain needed. This is what a grader with a fresh droplet uses, and it's fully self-contained: no nginx, no Certbot, no domain, anywhere in this path.
- **Tier 2 — CI/CD pipeline (optional)** — developer convenience only, not required for grading.
- **Tier 3 — HTTPS setup for live frontend demo (optional)** — only needed if you want to pair a Vercel-hosted (HTTPS) frontend with this backend; browsers block HTTPS pages from calling plain-HTTP APIs (mixed-content blocking), so this tier exists purely to satisfy that. Not required to satisfy any grading criterion on its own.

Plus a separate **Frontend (Vercel)** section, since the frontend deploys independently of the droplet entirely.

---

## Tier 1 — Redeploying from scratch (required, ~10 min)

Assumes nothing except a fresh Ubuntu 22.04 droplet and SSH access as a sudo-capable user. No self-hosted runner, no pre-installed Docker/uv, nothing pre-onboarded.

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

Fill in `ANTHROPIC_API_KEY` now (you'll need it in the OpenClaw onboarding step below). Leave `OPENCLAW_GATEWAY_TOKEN` blank for now — it doesn't exist yet, and step 5 below tells you exactly when to come back and fill it in.

**This step is easy to skip and will silently break `/ask` if you do.** `.env` is gitignored on purpose (never commit secrets), which means `git clone` never creates one on a fresh droplet — nothing else in this path creates it either. If you skip this, `fastapi.service` starts fine and `/health` still works, but any `POST /ask` will fail with an `Illegal header value b'Bearer '` error, because `main.py`'s `load_dotenv()` finds no `.env` file and `OPENCLAW_GATEWAY_TOKEN` silently defaults to an empty string. There's no startup-time check that catches this — the failure only shows up the first time you actually call `/ask`. If you hit that exact error, this is why: check `ls -la ~/Grounded-Answer-Desk-Assignment/.env` first.

### 4. Start Qdrant

```bash
docker compose -f infra/docker-compose.yml up -d
curl http://localhost:6333/healthz   # expect {"title":"qdrant - healthy"}
```

### 5. Onboard OpenClaw (the one manual, interactive step)

Every other step in this path is a script or a single idempotent command. This one isn't, because OpenClaw's onboarding CLI is interactive by design — scripting a non-interactive workaround was considered and deliberately not done, since this step only ever runs once per droplet.

```bash
docker pull ghcr.io/openclaw/openclaw:latest
git clone https://github.com/openclaw/openclaw.git /opt/openclaw
cd /opt/openclaw
export OPENCLAW_IMAGE="ghcr.io/openclaw/openclaw:latest"
./scripts/docker/setup.sh
```

The wizard will prompt you, in order — answer exactly like this:

| Prompt | Answer |
|---|---|
| Select channel (QuickStart) | **Skip for now** |
| Search provider | **Skip for now** |
| LLM provider | **Anthropic** |
| API key | paste your `ANTHROPIC_API_KEY` (`sk-ant-...`) |
| Model | **claude-haiku-4-5** |

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

Both scripts template their `infra/systemd/*.service` file with the actual repo path and `uv` binary location, install it to `/etc/systemd/system/`, `daemon-reload`, `enable`, and start it. Both are idempotent. These are run **directly by hand here** — not through any CI/CD workflow (see "CI/CD pipeline" below for why that's a deliberate separation).

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

Full ingestion, including first-time embedding model download (~550MB from HuggingFace) and processing ~3,100 chunks, completes in under 1 minute on a 2 vCPU/4GB droplet (benchmarked: 48 seconds, cold model cache).

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

The `/ask` response's `retrieved_chunks` field comes from an **independent call** that FastAPI makes directly to the MCP server (using the same question text and `top_k=4` the agent uses), not from literally capturing the agent's internal tool call. This is because OpenClaw's `/tools/invoke` endpoint doesn't yet support MCP-bundled tools in the deployed version (confirmed via source + empirical testing — see `CLAUDE.md`'s "Retrieval inspector data fidelity" section for the full investigation). `agent/SOUL.md` instructs the agent to search using the user's question verbatim, and since retrieval is fully deterministic given identical input, this independent lookup is expected to match what the agent actually retrieved — with the one disclosed residual risk being the agent failing to search verbatim.

### Time budget

| Step | Time |
|---|---|
| Docker + uv install | ~1-2 min |
| Create `.env` | ~10 sec |
| Qdrant up | ~30 sec |
| OpenClaw image pull + interactive onboarding | **not precisely benchmarked** — depends on network speed and how quickly the prompts are answered by hand |
| systemd service setup (both) | ~10 sec |
| MCP server registration + probe | ~10 sec |
| Ingestion + verify | **48 sec (benchmarked)** |
| Final verification | ~10 sec |

Everything except OpenClaw's image pull + interactive wizard is fast and measured. That one step is the single remaining unknown in the ~10 minute budget — it's inherently variable (network-dependent image pull, human-paced prompts) rather than something a benchmark number would meaningfully capture.

---

## Tier 2 — CI/CD pipeline (optional — developer convenience)

**Not required for grading.** This is a separate, optional layer for faster iteration during ongoing development — it assumes Tier 1 above has already been run once on this droplet (systemd services installed, OpenClaw onboarded).

A GitHub Actions self-hosted runner, registered on the droplet, executes `.github/workflows/deploy.yml` on every push to `main`:
1. Pull latest code
2. `uv sync --frozen`
3. `docker compose -f infra/docker-compose.yml up -d` (Qdrant, idempotent)
4. `docker compose -f /opt/openclaw/docker-compose.yml up -d` (OpenClaw, idempotent, does not redo onboarding)
5. `sudo systemctl restart mcp-server`
6. `sudo systemctl restart fastapi`

Deliberately, this workflow only *restarts* already-installed services — it never installs/enables systemd units or re-onboards OpenClaw, so a routine push can never silently change your system configuration. It also does **not** re-run ingestion; that stays a deliberate manual step.

Requires `ANTHROPIC_API_KEY` and `OPENCLAW_GATEWAY_TOKEN` set as GitHub Secrets (Settings → Secrets → Actions), and a self-hosted runner registered against this repo.

---

## Tier 3 — HTTPS setup for live frontend demo (optional)

**Not required for grading, and not part of Tier 1.** The RAG backend itself works fully over plain HTTP — `curl http://<droplet-ip>:8000/ask` is sufficient to exercise and grade the whole system. This tier exists solely because browsers block a page served over HTTPS (like a Vercel deployment) from calling a plain-HTTP API — "mixed content" blocking. If you only want to grade or test the backend directly, skip this section entirely.

If you *do* want to point a live Vercel frontend at this backend, run:

```bash
./infra/scripts/setup-nginx-https.sh [domain]
```

**With a domain you own** (e.g. `api.example.com`), pass it directly — but point its DNS A record at the droplet's public IP *before* running the script, or the certbot step will fail:
```bash
./infra/scripts/setup-nginx-https.sh api.example.com
```

**Without a domain**, run it with no argument:
```bash
./infra/scripts/setup-nginx-https.sh
```
The script auto-detects the droplet's public IP and builds a free [sslip.io](https://sslip.io) hostname from it (e.g. `143.198.51.2.sslip.io`), which resolves to that IP automatically — no DNS setup, no purchase, no registration. Anyone can complete this step at zero cost, regardless of whether they own a domain.

Either way, the script:
1. Installs `nginx` and `certbot` (+ `python3-certbot-nginx`)
2. Writes an nginx reverse-proxy config forwarding the chosen hostname to `localhost:8000`
3. Runs `certbot --nginx -d <hostname>` — **this step is interactive**: certbot asks for an email address (renewal notices) and whether to redirect HTTP to HTTPS (answer yes)
4. Prints a `curl https://<hostname>/health` command to verify

The live personal demo for this project uses `sriramv.tech` (a domain the author owns); a grader running this tier with no arguments gets an equivalent sslip.io hostname with identical behavior.

---

## Frontend (Vercel)

The frontend (`frontend/`, Next.js) deploys independently of the droplet, to Vercel's free tier.

1. **Connect the repo to Vercel** — either import the GitHub repo at [vercel.com/new](https://vercel.com/new) (set the project root to `frontend/`), or deploy directly from the CLI:
   ```bash
   cd frontend
   npx vercel
   ```
2. **Set the environment variable** pointing the frontend at the droplet's public API. In the Vercel dashboard (Project → Settings → Environment Variables) or via CLI:
   ```bash
   vercel env add NEXT_PUBLIC_API_URL
   # value: http://<droplet-public-ip>:8000   (plain HTTP is fine for direct/API-level testing)
   # or, after completing Tier 3 above:
   # value: https://<hostname-from-tier-3>    (needed if the Vercel frontend will call this API from the browser)
   ```
3. **Deploy to production:**
   ```bash
   vercel --prod
   ```
   Vercel prints the resulting public URL (`https://<project>.vercel.app`) — that's the graded frontend URL.

The exact env var name will be confirmed when the frontend is built (Step 5); this section will be updated then.
