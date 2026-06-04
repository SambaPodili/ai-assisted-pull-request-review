# CIAA Deployment (Linux VM, no Docker)

Backend and frontend deploy **independently**. The backend is a pure API; the
frontend is static files that call it over HTTPS.

```
[ browser ] → app.yourdomain.com (nginx, static dist)        ← frontend
            → api.yourdomain.com (nginx → gunicorn :8080)     ← backend API
```

---

## A. Backend — build a sourceless artifact

On your **build machine** (same Python *minor* version as the VM, e.g. 3.11):

```bash
./deploy/build_backend.sh           # → ciaa-backend-YYYYMMDD.tar.gz  (.pyc only, no .py)
```

This bundles every package, compiles to bytecode, **deletes all `.py`**, and
strips secrets/DBs/caches. `config/keys.json` and `.env` are **never** bundled —
you place those on the VM.

> Stronger protection: `.pyc` is decompilable. For unrecoverable source, compile
> with **Nuitka** instead (`pip install nuitka && python -m nuitka --standalone
> --follow-imports main.py` → ship `main.dist/`, run `./main.bin`). The systemd
> `ExecStart` then points at the binary instead of gunicorn. The `.pyc` route
> below is simpler and fine for most internal deployments.

---

## B. Backend — provision the VM (one time)

```bash
sudo useradd --system --home /opt/ciaa --shell /usr/sbin/nologin ciaa
sudo mkdir -p /opt/ciaa/{app,data,logs}
sudo apt install -y python3 python3-venv nginx           # Debian/Ubuntu

# Python env (must match the build's Python minor version)
sudo -u ciaa python3 -m venv /opt/ciaa/venv
sudo -u ciaa /opt/ciaa/venv/bin/pip install -r /path/to/requirements.txt gunicorn uvicorn
```

## C. Backend — deploy the artifact

```bash
# 1. unpack code (sourceless) into /opt/ciaa/app
sudo tar -xzf ciaa-backend-*.tar.gz -C /tmp
sudo rsync -a --delete /tmp/ciaa/ /opt/ciaa/app/

# 2. secrets + config (kept OUT of the code tree, locked down)
sudo cp deploy/.env.example /opt/ciaa/.env        # then edit real values
sudo cp config/keys.example.json /opt/ciaa/keys.json   # then edit real keys
sudo chown -R ciaa:ciaa /opt/ciaa
sudo chmod 600 /opt/ciaa/.env /opt/ciaa/keys.json

# 3. service
sudo cp deploy/ciaa.service /etc/systemd/system/ciaa.service
sudo systemctl daemon-reload
sudo systemctl enable --now ciaa
sudo systemctl status ciaa            # should be active (running)
curl -s localhost:8080/live           # {"status":"ok","version":"..."}
```

## D. Backend — public HTTPS (nginx + certbot)

```bash
sudo cp deploy/nginx-api.conf /etc/nginx/sites-available/ciaa-api.conf
sudo ln -s /etc/nginx/sites-available/ciaa-api.conf /etc/nginx/sites-enabled/
# edit server_name → api.yourdomain.com
sudo certbot --nginx -d api.yourdomain.com
sudo nginx -t && sudo systemctl reload nginx
```

---

## E. Frontend — build & serve (separate host)

```bash
cd frontend
npm ci && npm run build               # → frontend/dist/
```

Serve `dist/` from any static host/CDN, or with nginx on its own box:

```bash
sudo mkdir -p /var/www/ciaa-frontend
sudo rsync -a frontend/dist/ /var/www/ciaa-frontend/
sudo cp deploy/nginx-frontend.conf /etc/nginx/sites-available/ciaa-frontend.conf
sudo ln -s /etc/nginx/sites-available/ciaa-frontend.conf /etc/nginx/sites-enabled/
sudo certbot --nginx -d app.yourdomain.com
sudo nginx -t && sudo systemctl reload nginx
```

Then in the app's **Settings**, point "Backend URL" at `https://api.yourdomain.com`
(and ensure the backend `.env` has `CORS_ORIGINS=https://app.yourdomain.com`).

---

## Updating
```bash
./deploy/build_backend.sh
sudo rsync -a --delete /tmp/ciaa/ /opt/ciaa/app/   # after unpacking new tarball
sudo systemctl restart ciaa
```

## Operational notes
- **Workers = 1** for now (`WEB_WORKERS` in `.env`). The admission queue + live
  progress are per-process; scale to multiple workers only after the Redis-backed
  shared state is added. Throughput is handled by the in-process async + agent
  thread pool, capped by `MAX_CONCURRENT_ANALYSES`.
- **Logs**: `journalctl -u ciaa -f`.
- **Backups**: back up `/opt/ciaa/data/*.db` (reports, feedback) and `keys.json`.
- **Health**: `GET /live` (liveness), `GET /ready` (readiness), `GET /health` (full).
- **Never** set `SKIP_AUTH=true` in production.
