# SPDX-License-Identifier: MIT
import logging
import re
import sys
import threading
from pathlib import Path
from typing import Optional


class Colors:
    RED = '\033[0;31m'
    GREEN = '\033[0;32m'
    YELLOW = '\033[1;33m'
    BLUE = '\033[0;34m'
    RESET = '\033[0m'


class ColoredFormatter(logging.Formatter):
    FORMATS = {
        logging.DEBUG:    f"{Colors.BLUE}[DEBUG]{Colors.RESET} %(asctime)s - %(message)s",
        logging.INFO:     f"{Colors.BLUE}[INFO]{Colors.RESET} %(asctime)s - %(message)s",
        logging.WARNING:  f"{Colors.YELLOW}[WARN]{Colors.RESET} %(asctime)s - %(message)s",
        logging.ERROR:    f"{Colors.RED}[ERROR]{Colors.RESET} %(asctime)s - %(message)s",
        logging.CRITICAL: f"{Colors.RED}[CRITICAL]{Colors.RESET} %(asctime)s - %(message)s",
    }

    def format(self, record):
        log_fmt = self.FORMATS.get(record.levelno)
        formatter = logging.Formatter(log_fmt, datefmt='%Y-%m-%d %H:%M:%S')
        return formatter.format(record)


class PlainFormatter(logging.Formatter):
    ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')

    def format(self, record):
        rendered = super().format(record)
        return self.ANSI_RE.sub('', rendered)


def setup_logging(log_file: Optional[Path]) -> logging.Logger:
    """Set up console logging, plus file logging when log_file is given."""
    logger = logging.getLogger('kernel_build')
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(ColoredFormatter())

    logger.addHandler(console_handler)

    if log_file is not None:
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            PlainFormatter('%(levelname)s %(asctime)s - %(message)s',
                           datefmt='%Y-%m-%d %H:%M:%S')
        )
        logger.addHandler(file_handler)

    return logger


class Spinner:
    FRAMES = '|/-\\'

    def __init__(self, message: str, interval: float = 0.1):
        self.message = message
        self.interval = interval
        self.enabled = sys.stdout.isatty()
        self._stop = threading.Event()
        self._thread = None

    def _spin(self):
        i = 0
        while not self._stop.is_set():
            sys.stdout.write(f"\r{self.FRAMES[i % len(self.FRAMES)]} {self.message}")
            sys.stdout.flush()
            i += 1
            self._stop.wait(self.interval)

    def __enter__(self):
        if self.enabled:
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._thread:
            self._stop.set()
            self._thread.join()
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
        return False
