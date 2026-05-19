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
    uvicorn.run(
        "api.app:app",
        host="0.0.0.0",
        port=8080,
        reload=cfg.log_level == "DEBUG",
        log_level=cfg.log_level.lower(),
    )


if __name__ == "__main__":
    main()
