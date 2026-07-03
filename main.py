"""
main.py
--------
Application entry point.
Can be run directly with `python main.py` or via uvicorn.
"""
import logging
import os
import uvicorn

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
)
log = logging.getLogger(__name__)


def main() -> None:
    from config.settings import get_settings
    cfg = get_settings()
    log.info("Starting AI Impact Analysis Framework — Phase %d", cfg.analysis_phase)
    # Auto-reload must be OPT-IN (UVICORN_RELOAD=true), never implied by DEBUG
    # logging: the reloader watches the project dir, and runtime writes
    # (reports.db / audit.jsonl / __pycache__) trigger a mid-analysis RESTART that
    # kills the in-flight run — agents stop and every poll 404s.
    import os
    reload_on = os.getenv("UVICORN_RELOAD", "").strip().lower() in ("true", "1", "yes")
    if reload_on:
        log.warning("UVICORN_RELOAD is ON — code changes restart the server and "
                    "will kill any in-flight analysis. Development only.")
    uvicorn.run(
        "api.app:app",
        host="0.0.0.0",
        port=8080,
        reload=reload_on,
        reload_includes=["*.py"] if reload_on else None,   # never restart on data/db writes
        log_level=cfg.log_level.lower(),
    )


if __name__ == "__main__":
    main()
