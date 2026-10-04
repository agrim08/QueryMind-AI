"""Scheduled daily eval: python -m evals.daily  (registered once with evals/schedule.ps1)

Makes sure Docker and the eval database are running, then spends today's eval budget on the
questions that still need a fresh Gemini answer (core questions first). Answers from earlier
days are reused, so a full run builds up over several days without any manual steps.
Everything is logged to evals/reports/daily.log; the readable result is evals/reports/latest.md.
"""
# `evals.run` must be imported before anything else from the app: it points the app at the
# local eval database.
from evals import run  # isort: skip

import asyncio
import logging
import subprocess
import sys
import time
from pathlib import Path

from evals.config import DAILY_LOG_FILE, SOURCES
from evals.setup import start_container

logger = logging.getLogger("evals.daily")

DOCKER_DESKTOP = Path(r"C:\Program Files\Docker\Docker\Docker Desktop.exe")
ENGINE_START_TIMEOUT_S = 300


def _engine_running() -> bool:
    result = subprocess.run(
        ["docker", "info"], capture_output=True, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )
    return result.returncode == 0


def ensure_docker() -> None:
    """Start Docker Desktop if its engine isn't running, and wait for it."""
    if _engine_running():
        return
    if not DOCKER_DESKTOP.exists():
        raise RuntimeError("Docker isn't running and Docker Desktop wasn't found. Start Docker and re-run.")
    logger.info("Starting Docker Desktop")
    subprocess.Popen([str(DOCKER_DESKTOP)])
    deadline = time.monotonic() + ENGINE_START_TIMEOUT_S
    while time.monotonic() < deadline:
        time.sleep(5)
        if _engine_running():
            return
    raise RuntimeError("Docker didn't start within 5 minutes. Restart Docker Desktop; the next run will resume.")


def main() -> None:
    DAILY_LOG_FILE.parent.mkdir(exist_ok=True)
    logging.basicConfig(
        filename=DAILY_LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        encoding="utf-8",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("google_genai").setLevel(logging.WARNING)
    # Refused or failed generations are expected here and already summarised by the runner.
    logging.getLogger("app.services.query_pipeline").setLevel(logging.CRITICAL)

    logger.info("Daily eval starting")
    try:
        ensure_docker()
        start_container()
        asyncio.run(run.run_evals(sorted(SOURCES)))
    except Exception:
        logger.exception("Daily eval failed; the next run will pick up where this one stopped")
        sys.exit(1)
    logger.info("Daily eval finished")


if __name__ == "__main__":
    main()
