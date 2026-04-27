import enum
import logging
import os
import sys
import tomllib
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
# NOTE: VALID_EFFORT_LEVELS and SupportedLanguage live here (not definitions.py) to avoid
# a circular import; definitions.py already imports from settings.py.


class VALID_EFFORT_LEVELS(enum.StrEnum):  # noqa: N801
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


class SupportedLanguage(enum.StrEnum):
    ENGLISH = "english"
    JAPANESE = "japanese"


DEFAULT_EFFORT_LEVEL = VALID_EFFORT_LEVELS.XHIGH


def _resolve_effort_level() -> VALID_EFFORT_LEVELS:
    """Resolve ASKCC_CLAUDE_EFFORT_LEVEL, warning on invalid values."""
    raw = os.getenv("ASKCC_CLAUDE_EFFORT_LEVEL") or None
    if raw is None:
        return DEFAULT_EFFORT_LEVEL
    try:
        return VALID_EFFORT_LEVELS(raw)
    except ValueError:
        logger.warning(
            "Invalid ASKCC_CLAUDE_EFFORT_LEVEL=%r (valid: %s). Ignoring.",
            raw,
            ", ".join(VALID_EFFORT_LEVELS),
        )
        return DEFAULT_EFFORT_LEVEL


ASKCC_CLAUDE_EFFORT_LEVEL: VALID_EFFORT_LEVELS = _resolve_effort_level()

DEFAULT_MAX_THINKING_TOKENS = 21000  # ~5% of Max5 plan daily token budget (~422K tokens/day)

_raw_max_thinking = os.getenv("ASKCC_CLAUDE_MAX_THINKING_TOKENS", "")
ASKCC_CLAUDE_MAX_THINKING_TOKENS: int = (
    int(_raw_max_thinking) if _raw_max_thinking.isdigit() else DEFAULT_MAX_THINKING_TOKENS
)

ASKCC_CLAUDE_DISABLE_THINKING: bool = os.getenv("ASKCC_CLAUDE_DISABLE_THINKING", "").lower() in ("1", "true")

ASKCC_CLAUDE_DISABLE_ADAPTIVE_THINKING: bool = os.getenv(
    "ASKCC_CLAUDE_DISABLE_ADAPTIVE_THINKING", "true"
).lower() not in ("0", "false")

# Claude Code subprocess env var names
CLAUDE_ENV_MAX_THINKING_TOKENS = "MAX_THINKING_TOKENS"
CLAUDE_ENV_DISABLE_THINKING = "CLAUDE_CODE_DISABLE_THINKING"
CLAUDE_ENV_DISABLE_ADAPTIVE_THINKING = "CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING"

ASKCC_HOME: Path = Path(os.getenv("ASKCC_HOME") or str(Path.home() / ".askcc")).expanduser().resolve()
TEMPLATES_DIR: Path = ASKCC_HOME / "templates"
LOG_DIR: Path = ASKCC_HOME / "logs"
USER_CONFIG_PATH: Path = ASKCC_HOME / "config.toml"


def _load_user_config(path: Path | None = None) -> dict:
    """Load the user's TOML config file. Missing file → silent {}; malformed → warned {}.

    The optional `path` argument is for tests — it lets the loader target a tmp_path
    without re-importing the module. Production callers should rely on ``_USER_CONFIG``.
    """
    target = path if path is not None else USER_CONFIG_PATH
    if not target.is_file():
        return {}
    try:
        with target.open("rb") as f:
            return tomllib.load(f)
    except (tomllib.TOMLDecodeError, OSError):
        # Path is logged; exception detail is intentionally omitted to avoid echoing
        # config contents into logs.
        logger.warning("Failed to read user config %s. Ignoring.", target)
        return {}


_USER_CONFIG: dict = _load_user_config()


def _resolve_default_language(user_config: dict | None = None) -> SupportedLanguage:
    """Resolve the default language: env var > user config > built-in default.

    The CLI flag layer is handled by argparse (its `default=` is set to the value
    returned here); this function only covers the env-var and config-file layers.
    """
    raw_env = os.getenv("ASKCC_LANGUAGE") or None
    if raw_env is not None:
        try:
            return SupportedLanguage(raw_env)
        except ValueError:
            logger.warning(
                "Invalid ASKCC_LANGUAGE=%r (valid: %s). Ignoring.",
                raw_env,
                ", ".join(SupportedLanguage),
            )

    config = user_config if user_config is not None else _USER_CONFIG
    config_value = config.get("defaults", {}).get("language")
    if config_value is not None:
        try:
            return SupportedLanguage(config_value)
        except ValueError:
            logger.warning(
                "Invalid [defaults].language=%r in %s (valid: %s). Ignoring.",
                config_value,
                USER_CONFIG_PATH,
                ", ".join(SupportedLanguage),
            )

    return SupportedLanguage.ENGLISH


DEFAULT_LANGUAGE: SupportedLanguage = _resolve_default_language()

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
