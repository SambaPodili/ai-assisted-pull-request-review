"""
gunicorn_conf.py — production server config for the CIAA backend.

Run:  gunicorn 'api.app:create_app' --factory -c gunicorn_conf.py

NOTE on workers: the app keeps per-process in-memory state (admission queue,
live per-agent progress, in-flight status). Those are NOT shared across
processes, so progress/queue polling must hit the same worker that ran the
analysis. Until the Redis-backed shared state is added, keep workers = 1 (the
heavy analysis already runs in a thread pool inside the event loop, and the
admission cap controls concurrency). Scale out only after Redis is wired in.
"""
import os

bind = os.environ.get("BIND", "127.0.0.1:8080")   # nginx is the only public listener
workers = int(os.environ.get("WEB_WORKERS", "1"))
worker_class = "uvicorn.workers.UvicornWorker"

# Long enough for a full 20-agent analysis (> analysis_timeout_s, default 600s).
timeout = int(os.environ.get("WEB_TIMEOUT", "660"))
graceful_timeout = 30
keepalive = 5

accesslog = os.environ.get("ACCESS_LOG", "-")      # stdout → journald
errorlog = os.environ.get("ERROR_LOG", "-")
loglevel = os.environ.get("LOG_LEVEL", "info")
proc_name = "ciaa-api"
