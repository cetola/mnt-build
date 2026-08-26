# SPDX-License-Identifier: MIT
import logging
import re
import sys
from pathlib import Path


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


def setup_logging(log_file: Path) -> logging.Logger:
    logger = logging.getLogger('kernel_build')
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(ColoredFormatter())

    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        PlainFormatter('%(levelname)s %(asctime)s - %(message)s',
                       datefmt='%Y-%m-%d %H:%M:%S')
    )

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)

    return logger
