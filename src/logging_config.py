"""
Logging configuration for ApplyPotato.
Sets up file handlers with rotation for scrape and gmail logs.
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from .config import get_config, Config


# Log format with timestamp, level, module, and message
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Max log file size: 5 MB
MAX_BYTES = 5 * 1024 * 1024

# Keep 3 backup files
BACKUP_COUNT = 3


def setup_logging(
    log_name: str = "scrape",
    config: Optional[Config] = None,
    console: bool = True,
) -> logging.Logger:
    """
    Set up logging with file and optional console handlers.

    Args:
        log_name: Name for the log file (without extension).
                  Use "scrape" for scrape_jobs.py, "gmail" for check_gmail.py.
        config: Optional config object. Uses global config if not provided.
        console: Whether to also log to console (stderr).

    Returns:
        Root logger configured with handlers.
    """
    config = config or get_config()

    # Get log level from config
    log_level = getattr(logging, config.log_level, logging.INFO)

    # Create formatter
    formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)

    # Get root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Clear existing handlers to avoid duplicates
    root_logger.handlers.clear()

    # File handler with rotation
    log_file = config.logs_dir / f"{log_name}.log"
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    # Console handler (optional)
    if console:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(log_level)
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)

    # Log startup message
    root_logger.info(f"Logging initialized: level={config.log_level}, file={log_file}")

    return root_logger


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger with the given name.

    Args:
        name: Logger name (typically __name__ from the calling module).

    Returns:
        Logger instance.
    """
    return logging.getLogger(name)
