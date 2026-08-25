"""
Logging configuration for ApplyPotato.
Sets up per-component file handlers with date-stamped rotation.
"""

import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from .config import get_config, Config


# Log format with timestamp, level, module, and message
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Keep 30 days of logs
BACKUP_COUNT = 30

# Date suffix on every log file, and the format used to read it back when pruning.
FILE_DATE_FORMAT = "%Y-%m-%d"


class DateStampedFileHandler(logging.FileHandler):
    """
    Write to `<prefix>-YYYY-MM-DD.log`, opening a new file when the date changes.

    Rotation here happens by *naming*, never by renaming, and that is the whole point.
    The stdlib's TimedRotatingFileHandler rolls by renaming the live file, which Windows
    refuses while another process holds it open — and two daemons plus any hand-run
    script hold this one open at once. Worse, it advances `rolloverAt` only after a
    successful rename, so a single failed midnight leaves the handler retrying the
    rollover on every record and discarding each one. That is how a checker that was
    running fine came to look dead for six days: no log lines, because none survived.

    Nothing is renamed here, so there is nothing another process can block. Concurrent
    appends to one file are fine and always were — that is the one part that never
    broke, even while rotation was failing every minute.
    """

    def __init__(
        self,
        logs_dir: Path,
        prefix: str,
        backup_days: int = BACKUP_COUNT,
        encoding: str = "utf-8",
    ):
        self.logs_dir = Path(logs_dir)
        self.prefix = prefix
        self.backup_days = backup_days
        self._current_date = self._today()
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        super().__init__(self._path_for(self._current_date), mode="a", encoding=encoding)
        self._prune()

    def _today(self) -> date:
        """Overridable so tests can move the clock without touching the system one."""
        return date.today()

    def _path_for(self, day: date) -> Path:
        return self.logs_dir / f"{self.prefix}-{day.strftime(FILE_DATE_FORMAT)}.log"

    def emit(self, record: logging.LogRecord) -> None:
        """Switch files if the day changed, then write as usual."""
        try:
            today = self._today()
            if today != self._current_date:
                self._switch_to(today)
        except OSError:
            # A failed switch must cost this record at most, never the stream. Leaving
            # the handler pointed at yesterday's file is far better than going silent.
            self.handleError(record)
        super().emit(record)

    def _switch_to(self, day: date) -> None:
        self._current_date = day
        self.baseFilename = os.path.abspath(str(self._path_for(day)))
        if self.stream:
            self.stream.close()
            self.stream = None
        self.stream = self._open()
        self._prune()

    def _prune(self) -> None:
        """
        Delete this component's log files older than the retention window.

        Only files strictly older than today are considered, so nothing being written
        to is ever a candidate. A file that cannot be deleted — held open by another
        process, say — is left for the next attempt rather than raising.
        """
        if self.backup_days <= 0:
            return

        cutoff = self._current_date - timedelta(days=self.backup_days)
        for path in self.logs_dir.glob(f"{self.prefix}-*.log"):
            stamp = path.name[len(self.prefix) + 1:-len(".log")]
            try:
                stamped = datetime.strptime(stamp, FILE_DATE_FORMAT).date()
            except ValueError:
                continue  # not one of ours, or hand-renamed
            if stamped < cutoff:
                try:
                    path.unlink()
                except OSError:
                    pass


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

    # One file per component per day. Sharing a single file across pipelines made the
    # combined log unreadable and, once rotation started failing, indistinguishable
    # from a pipeline that had stopped running.
    file_handler = DateStampedFileHandler(config.logs_dir, log_name, BACKUP_COUNT)
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    # Console handler (optional)
    if console:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(log_level)
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)

    root_logger.info(f"Logging initialized: level={config.log_level}, component={log_name}")

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
