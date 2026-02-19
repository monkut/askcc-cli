import logging
import os
import sys
from pathlib import Path

DEFAULT_LOG_LEVEL = "INFO"
LOG_LEVEL = os.getenv("LOG_LEVEL", DEFAULT_LOG_LEVEL).upper()

ASKCC_HOME: Path = Path(os.getenv("ASKCC_HOME", str(Path.home() / ".askcc")))
TEMPLATES_DIR: Path = ASKCC_HOME / "templates"


def configure_logging() -> None:
    """Configure logging for the application. Call explicitly from entry points."""
    logging.basicConfig(
        stream=sys.stdout,
        level=getattr(logging, LOG_LEVEL, logging.DEBUG),
        format="%(asctime)s [%(levelname)s] (%(name)s) %(funcName)s: %(message)s",
    )
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
