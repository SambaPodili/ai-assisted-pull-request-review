# CIAA Impact Analyzer — Deployment Guide

Two supported targets:

- **A. Standalone VM** — single Linux host (systemd or Docker)
- **B. OpenShift cluster** — containerized, with Routes + persistent storage

The app is self-contained: it uses an embedded **SQLite** report store and
**in-memory** fallbacks for the vector/graph stores, so the minimal deployment
is just the one container + a volume. Redis / Neo4j / ChromaDB are **optional**
and only needed for multi-replica HA or very large historical datasets.

---

## Prerequisites (both targets)

| Need | Why |
|---|---|
| `ANTHROPIC_API_KEY` (or OpenAI / Azure / Ollama) | LLM agents |
| `GITHUB_TOKEN` (or Bitbucket token) | Fetch PR diffs + auto-clone reference search |
| `git` binary in the runtime | Auto-clone backend (already in the Docker image) |
| One CIAA API key per user/role | See `config/keys.json` |

---

# A. Standalone VM

### Option A0 — No Docker, one command (recommended for a plain Linux box)

A turnkey installer that needs **no container runtime** — just a Linux box with
Python 3.11+. It installs system deps, creates a service user, builds a
virtualenv, and registers a systemd service.

```bash
# Copy the repo to the box, then:
cd impact-analyzer
sudo ./deploy/install-vm.sh            # minimal: SQLite + in-memory (lightest)
#   or
sudo ./deploy/install-vm.sh --full     # adds LangGraph, ChromaDB, Redis, Neo4j clients

# Then set credentials and start:
sudo -u ciaa vi /opt/impact-analyzer/.env     # ANTHROPIC_API_KEY, GITHUB_TOKEN, API_KEYS, SKIP_AUTH=false
sudo systemctl start impact-analyzer
curl -s http://localhost:8080/live
```

The installer is **idempotent** (safe to re-run for upgrades) and supports
RHEL/Fedora/Rocky/Alma (`dnf`) and Debian/Ubuntu (`apt`).

- **Minimal** install uses [`requirements-minimal.txt`](requirements-minimal.txt)
  — core only. The app runs on the built-in SQLite report store, in-memory
  vector fallback, and the in-process NetworkX graph. The threaded pipeline is
  used in place of LangGraph. This is all most single-team deployments need.
- **`--full`** install pulls the optional backends so you can later point at
  Redis / Neo4j / ChromaDB.

Manual equivalent and TLS front-end are in **Option A2** below.

### Option A1 — Docker / Podman (simplest if you already have a runtime)

```bash
# 1. Clone / copy the project to the VM
cd /opt && git clone <your-repo> impact-analyzer && cd impact-analyzer

# 2. Create the env file
cp .env.example .env
vi .env            # set ANTHROPIC_API_KEY, GITHUB_TOKEN, API_KEYS / API_KEYS_FILE, SKIP_AUTH=false

# 3a. Minimal — app only (SQLite + in-memory; survives restarts via the volume)
docker build -t impact-analyzer:latest .
docker run -d --name impact-analyzer \
  --env-file .env \
  -p 8080:8080 \
  -v /opt/impact-analyzer/data:/app/data \
  -v /opt/impact-analyzer/logs:/app/logs \
  --restart unless-stopped \
  impact-analyzer:latest

# 3b. Full stack (app + Redis + ChromaDB + Neo4j) via compose
docker compose up -d
```

Verify:
```bash
curl -s http://localhost:8080/live      # {"status":"alive"}
curl -s http://localhost:8080/health    # component status
# UI: open http://<vm-ip>:8080/  (or serve frontend/index.html and point it at the backend URL)
```

### Option A2 — systemd (no Docker), manual steps

*(This is what Option A0's script automates — use it if you prefer to do each step by hand.)*

```bash
# 1. System packages
sudo dnf install -y python3.13 python3.13-devel git gcc   # RHEL/Fedora
#   (Debian/Ubuntu: sudo apt install python3.13 python3.13-venv git build-essential)

# 2. App user + location
sudo useradd -r -m -d /opt/impact-analyzer ciaa
sudo -u ciaa git clone <your-repo> /opt/impact-analyzer
cd /opt/impact-analyzer

# 3. Virtualenv + deps  (minimal = lightest; use requirements.txt for full backends)
sudo -u ciaa python3.13 -m venv .venv
sudo -u ciaa .venv/bin/pip install --upgrade pip
sudo -u ciaa .venv/bin/pip install -r requirements-minimal.txt   # or requirements.txt

# 4. Config
sudo -u ciaa cp .env.example .env
sudo -u ciaa vi .env            # fill in keys; set SKIP_AUTH=false

# 5. Install the service unit (see deploy/impact-analyzer.service below)
sudo cp deploy/impact-analyzer.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now impact-analyzer
sudo systemctl status impact-analyzer
journalctl -u impact-analyzer -f
```

Put **nginx/Apache** in front for TLS:
```nginx
server {
    listen 443 ssl;
    server_name ciaa.yourcompany.com;
    ssl_certificate     /etc/ssl/certs/ciaa.crt;
    ssl_certificate_key /etc/ssl/private/ciaa.key;
    location / { proxy_pass http://127.0.0.1:8080; proxy_set_header Host $host; }
}
```

Open the firewall:
```bash
sudo firewall-cmd --add-port=8080/tcp --permanent && sudo firewall-cmd --reload
```

---

# B. OpenShift cluster

Manifests live in [`openshift/`](openshift/). The app runs under the default
**restricted-v2 SCC** (arbitrary non-root UID in group 0) — the Dockerfile is
already built for that (`/app` is group-0 writable).

### Step 1 — Log in and create a project
```bash
oc login https://api.your-cluster.example.com:6443
oc new-project impact-analyzer
```

### Step 2 — Build the image in-cluster (no external registry needed)
```bash
# From the project root (where the Dockerfile is):
oc new-build --name impact-analyzer --binary --strategy=docker
oc start-build impact-analyzer --from-dir=. --follow
# → image lands in the internal registry:
#   image-registry.openshift-image-registry.svc:5000/impact-analyzer/impact-analyzer:latest
```
*(Alternatively push to Quay/ECR and set that image in `04-deployment.yaml`.)*

### Step 3 — Create the Secret (don't use the placeholder file for real keys)
```bash
oc create secret generic impact-analyzer-secret \
  --from-literal=ANTHROPIC_API_KEY='sk-ant-...' \
  --from-literal=GITHUB_TOKEN='ghp_...' \
  --from-literal=GITHUB_WEBHOOK_SECRET='' \
  --from-literal=API_KEYS='[{"key":"rev_xxx","roles":["reviewer"],"name":"Alice"},{"key":"admin_xxx","roles":["admin"],"name":"Admin"}]' \
  --from-literal=SMTP_PASSWORD=''
```

### Step 4 — Apply config, storage, deployment, service, route
```bash
# Fix the image namespace placeholder first:
sed -i "s/REPLACE_NAMESPACE/impact-analyzer/" openshift/04-deployment.yaml

oc apply -f openshift/02-configmap.yaml
oc apply -f openshift/03-pvc.yaml
oc apply -f openshift/04-deployment.yaml
oc apply -f openshift/05-service.yaml
oc apply -f openshift/06-route.yaml
```

### Step 5 — Verify
```bash
oc rollout status deploy/impact-analyzer
oc get pods -l app=impact-analyzer
oc logs deploy/impact-analyzer -f

ROUTE=$(oc get route impact-analyzer -o jsonpath='{.spec.host}')
curl -sk https://$ROUTE/live
echo "Open the UI at: https://$ROUTE/"
```

### Step 6 (optional) — wire the image build to auto-redeploy
```bash
oc set triggers deploy/impact-analyzer \
  --from-image=impact-analyzer:latest -c impact-analyzer
```

---

## Optional backends on OpenShift (HA / large history)

The single-pod deploy uses SQLite (single writer → `replicas: 1`, `Recreate`).
To run **multiple replicas**, switch the report store to Redis:

```bash
# Deploy Redis (e.g. from the OperatorHub or a simple Deployment), then:
oc set env deploy/impact-analyzer REDIS_URL=redis://redis:6379/0
oc scale deploy/impact-analyzer --replicas=3
oc patch deploy/impact-analyzer -p '{"spec":{"strategy":{"type":"RollingUpdate"}}}'
```

Neo4j (service dependency graph) and ChromaDB (semantic similarity) are likewise
optional — set `NEO4J_URL` / `CHROMA_HOST` only if you deploy them. Without them
the app uses NetworkX (in-process) and keyword search respectively.

---

## Webhook setup (optional — automatic analysis on every PR)

Point your Git provider's webhook at:
```
https://<route-or-vm-host>/webhooks/github      (or /webhooks/bitbucket)
```
Set the matching `GITHUB_WEBHOOK_SECRET` / `BITBUCKET_WEBHOOK_SECRET`.

---

## Post-deploy checklist

- [ ] `/live` returns 200, `/ready` returns 200
- [ ] UI loads and **Backend config** points at the deployed URL + an API key
- [ ] `SKIP_AUTH=false` in production; at least one `admin` key configured
- [ ] PVC bound (`oc get pvc`) so reports/audit survive restarts
- [ ] TLS in front (Route edge termination on OpenShift; nginx on VM)
- [ ] (If used) digest SMTP test: `POST /admin/digest/send` with an admin key
