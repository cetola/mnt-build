# SPDX-License-Identifier: MIT
import logging
import os
import subprocess
from pathlib import Path
from typing import List, Optional

from config import BuildConfig
from errors import BuildError


class CommandRunner:
    """Runs external commands, logging them and raising BuildError on failure."""

    def __init__(self, config: BuildConfig, logger: logging.Logger):
        self.config = config
        self.logger = logger

    def run(self, cmd: List[str], cwd: Path,
                    check: bool = True, input_data: Optional[str] = None,
                    stream_output: bool = False,
                    log_cmd: bool = True) -> subprocess.CompletedProcess:
        """Run a shell command.

        If stream_output is True, stdout/stderr are streamed live to the logger
        and the log file rather than being captured.
        If log_cmd is False, the command string is logged at DEBUG level instead
        of INFO (useful for high-frequency calls like patch dry-runs).
        """
        cmd_str = ' '.join(cmd)
        (self.logger.info if log_cmd else self.logger.debug)(f"$ {cmd_str}")
        self.logger.debug(f"Running in {cwd}")

        if not stream_output:
            try:
                result = subprocess.run(
                    cmd,
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    check=check,
                    input=input_data
                )
                if result.stdout:
                    self.logger.debug(f"stdout: {result.stdout.strip()}")
                if result.stderr:
                    self.logger.debug(f"stderr: {result.stderr.strip()}")
                return result
            except subprocess.CalledProcessError as e:
                self.logger.error(f"Command failed: {cmd_str}")
                self.logger.error(f"Exit code: {e.returncode}")
                self.logger.error(f"stdout: {e.stdout}")
                self.logger.error(f"stderr: {e.stderr}")
                raise BuildError(f"Command failed: {cmd_str}") from e

        # stream_output is True here. Use Popen, stream lines to logger + file.
        # log_file is None when file logging is off
        logfile_path = Path(self.config.log_file or os.devnull)
        with open(logfile_path, "a", buffering=1) as logfile:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE if input_data is not None else None,
                text=True,
                bufsize=1,
                universal_newlines=True
            )

            if input_data is not None:
                assert proc.stdin is not None
                try:
                    proc.stdin.write(input_data)
                    proc.stdin.close()
                except OSError as e:
                    proc.kill()
                    proc.wait()
                    raise BuildError(f"Failed to write input to command: {cmd_str}") from e

            assert proc.stdout is not None
            for line in iter(proc.stdout.readline, ''):
                self.logger.info(line.rstrip())
                logfile.write(line)

            proc.wait()
            ret = proc.returncode

        if ret != 0 and check:
            raise BuildError(f"Command failed (exit {ret}): {cmd_str}")

        return subprocess.CompletedProcess(cmd, ret, stdout=None, stderr=None)
