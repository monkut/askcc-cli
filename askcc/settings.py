import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

DEFAULT_LOG_LEVEL = "INFO"
LOG_LEVEL = os.getenv("LOG_LEVEL", DEFAULT_LOG_LEVEL).upper()

DEFAULT_DECISION_ISSUE_LABEL = "needs:decision"
DECISION_ISSUE_LABEL = os.getenv("DECISION_ISSUE_LABEL", DEFAULT_DECISION_ISSUE_LABEL)

ENABLE_ISSUE_LABEL_PREFIX_VALIDATION: bool = os.getenv("ENABLE_ISSUE_LABEL_PREFIX_VALIDATION", "true").lower() == "true"
REQUIRED_ISSUE_LABEL_PREFIXES: tuple[str, ...] = ("action:",)

ASKCC_HOME: Path = Path(os.getenv("ASKCC_HOME") or str(Path.home() / ".askcc")).expanduser().resolve()
TEMPLATES_DIR: Path = ASKCC_HOME / "templates"
LOG_DIR: Path = ASKCC_HOME / "logs"

LOG_FORMAT = "%(asctime)s [%(levelname)s] (%(name)s) %(funcName)s: %(message)s"
LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
LOG_BACKUP_COUNT = 3


def configure_logging() -> None:
    """Configure logging for the application. Call explicitly from entry points."""
    log_level = getattr(logging, LOG_LEVEL, logging.DEBUG)
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(log_level)
    stdout_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root_logger.addHandler(stdout_handler)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        LOG_DIR / "askcc.log",
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root_logger.addHandler(file_handler)

    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
