import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_LOG_LEVEL = "INFO"
LOG_LEVEL = os.getenv("LOG_LEVEL", DEFAULT_LOG_LEVEL).upper()

DEFAULT_DECISION_ISSUE_LABEL = "needs:decision"
DECISION_ISSUE_LABEL = os.getenv("DECISION_ISSUE_LABEL", DEFAULT_DECISION_ISSUE_LABEL)
BLOCKING_LABELS: tuple[str, ...] = (DECISION_ISSUE_LABEL, "blocked")

ENABLE_ISSUE_LABEL_PREFIX_VALIDATION: bool = os.getenv("ENABLE_ISSUE_LABEL_PREFIX_VALIDATION", "true").lower() == "true"
REQUIRED_ISSUE_LABEL_PREFIXES: tuple[str, ...] = ("action:",)

# Label transition
PLAN_LABEL = "action:plan"
DEVELOP_LABEL = "action:develop"
REVIEW_LABEL = "action:review"

# Prepare transition
PLANNING_STATUS_OPTIONS: tuple[str, ...] = ("planning",)

# Plan transition (post-plan, ready for development)
READY_STATUS_OPTIONS: tuple[str, ...] = ("ready", "todo")

# Project field transition
REVIEW_STATUS_OPTIONS: tuple[str, ...] = ("in-internal-review", "in-review")

# -- Claude thinking/reasoning controls --
VALID_EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "max")


def _resolve_effort_level() -> str | None:
    """Resolve ASKCC_CLAUDE_EFFORT_LEVEL, warning on invalid values."""
    raw = os.getenv("ASKCC_CLAUDE_EFFORT_LEVEL") or None
    if raw is None:
        return None
    if raw not in VALID_EFFORT_LEVELS:
        logger.warning(
            "Invalid ASKCC_CLAUDE_EFFORT_LEVEL=%r (valid: %s). Ignoring.",
            raw,
            ", ".join(VALID_EFFORT_LEVELS),
        )
        return None
    return raw


ASKCC_CLAUDE_EFFORT_LEVEL: str | None = _resolve_effort_level()

DEFAULT_MAX_THINKING_TOKENS = 21000  # ~5% of Max5 plan daily token budget (~422K tokens/day)

ASKCC_CLAUDE_MAX_THINKING_TOKENS: int = (
    int(os.environ["ASKCC_CLAUDE_MAX_THINKING_TOKENS"])
    if os.getenv("ASKCC_CLAUDE_MAX_THINKING_TOKENS", "").isdigit()
    else DEFAULT_MAX_THINKING_TOKENS
)

ASKCC_CLAUDE_DISABLE_THINKING: bool = os.getenv("ASKCC_CLAUDE_DISABLE_THINKING", "").lower() in ("1", "true")

ASKCC_CLAUDE_DISABLE_ADAPTIVE_THINKING: bool = os.getenv(
    "ASKCC_CLAUDE_DISABLE_ADAPTIVE_THINKING", "true"
).lower() not in ("0", "false")

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
