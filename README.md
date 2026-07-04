# Grounded Answer Desk

A RAG system that answers questions grounded in the Anthropic API docs. See `CLAUDE.md` for full architecture and `01-grounded-answer-desk.html` for the assignment brief.

There are two separate deployment paths, covered in order below:
- **Redeploying from scratch** — the required path, runner-agnostic, must complete in ~10 minutes. This is what a grader with a fresh droplet uses.
- **CI/CD pipeline** — optional, developer-convenience only, not required for grading.

Plus a separate **Frontend (Vercel)** section, since the frontend deploys independently of the droplet entirely.

---

## Redeploying from scratch (required, ~10 min)

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

### 3. Start Qdrant

```bash
docker compose -f infra/docker-compose.yml up -d
curl http://localhost:6333/healthz   # expect {"title":"qdrant - healthy"}
```

### 4. Onboard OpenClaw (the one manual, interactive step)

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

Save that token as `OPENCLAW_GATEWAY_TOKEN` in your `.env` (see `.env.example`).

### 5. Install the fastapi and mcp-server systemd services

```bash
cd ~/Grounded-Answer-Desk-Assignment
./infra/scripts/setup-fastapi-service.sh
./infra/scripts/setup-mcp-service.sh
```

Both scripts template their `infra/systemd/*.service` file with the actual repo path and `uv` binary location, install it to `/etc/systemd/system/`, `daemon-reload`, `enable`, and start it. Both are idempotent. These are run **directly by hand here** — not through any CI/CD workflow (see "CI/CD pipeline" below for why that's a deliberate separation).

### 6. Populate the knowledge base

```bash
uv run python ingestion/ingest.py
uv run python ingestion/verify.py
```

`ingest.py` downloads the corpus, chunks it, embeds ~3,100 chunks, and populates the `anthropic_docs` Qdrant collection (it drops and recreates the collection each run, so it's always safe to re-run). `verify.py` should report a vector count in the 2,500-3,500 range.

Full ingestion, including first-time embedding model download (~550MB from HuggingFace) and processing ~3,100 chunks, completes in under 1 minute on a 2 vCPU/4GB droplet (benchmarked: 48 seconds, cold model cache).

### 7. Verify everything

```bash
curl http://localhost:8000/health
curl http://localhost:6333/healthz
sudo systemctl status mcp-server fastapi
uv run python mcp_server/spot_check.py
uv run python mcp_server/test_client.py
```

### Time budget

| Step | Time |
|---|---|
| Docker + uv install | ~1-2 min |
| Qdrant up | ~30 sec |
| OpenClaw image pull + interactive onboarding | **not precisely benchmarked** — depends on network speed and how quickly the prompts are answered by hand |
| systemd service setup (both) | ~10 sec |
| Ingestion + verify | **48 sec (benchmarked)** |
| Final verification | ~10 sec |

Everything except OpenClaw's image pull + interactive wizard is fast and measured. That one step is the single remaining unknown in the ~10 minute budget — it's inherently variable (network-dependent image pull, human-paced prompts) rather than something a benchmark number would meaningfully capture.

---

## CI/CD pipeline (optional — developer convenience)

**Not required for grading.** This is a separate, optional layer for faster iteration during ongoing development — it assumes "Redeploying from scratch" above has already been run once on this droplet (systemd services installed, OpenClaw onboarded).

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
   # value: http://<droplet-public-ip>:8000
   ```
3. **Deploy to production:**
   ```bash
   vercel --prod
   ```
   Vercel prints the resulting public URL (`https://<project>.vercel.app`) — that's the graded frontend URL.

The exact env var name will be confirmed when the frontend is built (Step 5); this section will be updated then.
